import json
import os
from dotenv import load_dotenv
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import roc_auc_score
import torch
from tqdm import tqdm
import wandb

from utils import get_args, load_checkpoint, gen_run_name, compute_topk, get_model
from datasets import get_dataloaders
from unlearn import compute_mask


def rand_label(model, image, target, idx, criterion, loader):
    # Assign random labels to forget data
    ds = loader.dataset.dataset
    forget_tensor = torch.tensor(ds.FORGET).to(DEVICE)
    which_is_in = (idx.unsqueeze(1) == forget_tensor).any(dim=1)
    rand_targets = torch.randint(1, len(ds.classes), target.shape).to(DEVICE)
    rand_targets = (target + rand_targets) % len(ds.classes)
    target[which_is_in] = rand_targets[which_is_in]

    output = model(image)
    loss = criterion(output, target)
    loss = loss.mean()

    return loss


def grad_ascent(model, image, target, idx, criterion, loader):
    output = model(image)
    loss = criterion(output, target)

    ds = loader.dataset.dataset
    forget_tensor = torch.tensor(ds.FORGET).to(DEVICE)
    which_is_in = (idx.unsqueeze(1) == forget_tensor).any(dim=1)
    loss[which_is_in] *= -1
    loss = loss.mean()

    return loss


def grad_ascent_small(model, image, target, idx, criterion, loader):
    output = model(image)
    loss = -criterion(output, target)
    loss = loss.mean()

    return loss


def retrain(model, image, target, idx, criterion, loader):
    output = model(image)
    loss = criterion(output, target)
    loss = loss.mean()

    return loss


def compute_basic_mia(retain_losses, forget_losses, val_losses, test_losses):
    train_loss = (
        torch.cat((retain_losses, val_losses), dim=0).unsqueeze(1).cpu().numpy()
    )
    train_target = torch.cat(
        (torch.ones(retain_losses.size(0)), torch.zeros(val_losses.size(0))), dim=0
    ).numpy()
    test_loss = (
        torch.cat((forget_losses, test_losses), dim=0).unsqueeze(1).cpu().numpy()
    )
    test_target = (
        torch.cat((torch.ones(forget_losses.size(0)), torch.zeros(test_losses.size(0))))
        .cpu()
        .numpy()
    )

    best_auc = 0
    best_acc = 0
    for n_est in [20, 50, 100]:
        for criterion in ["gini", "entropy"]:
            mia_model = RandomForestClassifier(
                n_estimators=n_est, criterion=criterion, n_jobs=8, random_state=0
            )
            mia_model.fit(train_loss, train_target)

            y_hat = mia_model.predict_proba(test_loss)[:, 1]
            auc = roc_auc_score(test_target, y_hat) * 100
            # breakpoint()
            y_hat = mia_model.predict(forget_losses.unsqueeze(1).cpu().numpy()).mean()
            acc = (1 - y_hat) * 100

            if acc > best_acc:
                best_acc = acc
                best_auc = auc

    return best_auc, best_acc


def eval_unlearning(model, loaders, names, criterion, DEVICE):

    model.eval()
    tot_acc = 0
    accs = {}
    losses = {}
    for loader, name in zip(loaders, names):

        losses[name] = []
        for data in tqdm(loader, desc=f"{name}\t"):

            image = data["image"]
            target = data["label"]
            image = image.to(DEVICE)
            target = target.to(DEVICE)

            output = model(image)
            loss = criterion(output, target)

            losses[name].append(loss.mean().item())

            acc = compute_topk(target, output, 1)

            tot_acc += acc

        tot_acc /= len(loader.dataset)
        accs[name] = tot_acc
    return accs, losses


if __name__ == "__main__":

    args, args_dict = get_args()

    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {DEVICE}")

    LOAD = args.load

    default = {
        "checkpoint": "checkpoints/resnet18_cifar10_best.pt",
        "class_to_forget": None,
        "unlearning_rate": None,
        "load_mask": False,
        "use_mask": True,
        "mask_thr": 0.5,
        "lr": 0.1,
        "epochs": 10,
        "method": "rl",
        "tag": None,
    }

    if LOAD == "":
        print("[LOADER] Loading parameters from command line")
        experiments = [args_dict]

    elif LOAD == "exp":
        print("[LOADER] Loading parameters from experiments set")
        experiments = [{}]
    else:
        print("[LOADER] Loading parameters from json file")
        experiments = json.load(open(LOAD, "r"))

    LOG = not args.no_log
    if LOG:
        load_dotenv()
        WANDB_SECRET = os.getenv("WANDB_SECRET")
        wandb.login(key=WANDB_SECRET)

    ###################################################################

    for nexp, exp in enumerate(experiments):

        settings = {**default, **exp}

        print(f"[EXP {nexp+1} of {len(experiments)}] Running settings: {settings}")

        CF = settings["class_to_forget"]
        CHKP = settings["checkpoint"]
        USE_MASK = settings["use_mask"]
        MASK_THR = settings["mask_thr"]
        LR = settings["lr"]
        UNLR = settings["unlearning_rate"]
        EPOCHS = settings["epochs"]
        METHOD = settings["method"]

        # LOAD_MASK = config["load_mask"]
        # MASK_PATH = f"checkpoints/mask_resnet18_cifar10_{CLASS_TO_FORGET}.pt"

        model, config, transform, opt = load_checkpoint(CHKP)

        DSET = config["dataset"]
        MODEL = config["model"]

        (
            train_loader,
            val_loader,
            test_loader,
            forget_loader,
            retain_loader,
        ) = get_dataloaders(DSET, transform, unlr=UNLR, cf=CF)

        if METHOD == "retrain":
            classes = train_loader.dataset.dataset.classes
            model, _, _ = get_model(MODEL, len(classes), False)

        model = model.to(DEVICE)
        optimizer = torch.optim.SGD(model.parameters(), lr=LR)
        criterion = torch.nn.CrossEntropyLoss(reduction="none")

        if LOG:
            config = {**config, **settings}
            run_name = METHOD + "_" + gen_run_name(config)
            wandb.init(project="TrendsAndApps", name=run_name, config=config)

        if USE_MASK:
            mask = compute_mask(
                model,
                forget_loader,
                unlearn_lr=LR,
                saliency_threshold=MASK_THR,
                device=DEVICE,
            )

        # -------------------------------------------------------------

        print("[MAIN] Evaluating model")
        accs, losses = eval_unlearning(
            model,
            [test_loader, forget_loader, retain_loader, val_loader],
            ["test", "forget", "retain", "val"],
            criterion,
            DEVICE,
        )
        accs["forget"] = 1 - accs["forget"]

        print("[MAIN] Computing MIA")
        mia_auc, mia_acc = compute_basic_mia(
            torch.tensor(losses["retain"]),
            torch.tensor(losses["forget"]),
            torch.tensor(losses["val"]),
            torch.tensor(losses["test"]),
        )

        for key, value in accs.items():
            print(f"{key}: {round(value,2)}")
        print(f"MIA AUC: {round(mia_auc,2)}, MIA ACC: {round(mia_acc,2)}")

        # -------------------------------------------------------------

        if LOG:
            wandb.log(
                {
                    "base_test": accs["test"],
                    "base_forget": accs["forget"],
                    "base_retain": accs["retain"],
                    "base_val": accs["val"],
                    "base_mia_auc": mia_auc,
                    "base_mia_acc": mia_acc,
                }
            )
        print("[MAIN] Unlearning model")

        best_test_acc = 0
        best_test = {}
        best_forget_acc = 100
        best_forget = {}

        if METHOD == "rl":
            method = rand_label
            loader = train_loader
        elif METHOD == "ga":
            method = grad_ascent
            loader = train_loader
        elif METHOD == "ga_small":
            method = grad_ascent_small
            loader = forget_loader
        elif METHOD == "retrain":
            method = retrain
            loader = retain_loader

        for epoch in range(EPOCHS):

            print(f"Epoch {epoch}")

            model.train()

            for data in tqdm(loader):

                image = data["image"]
                target = data["label"]
                idx = data["idx"]

                image = image.to(DEVICE)
                target = target.to(DEVICE)
                idx = idx.to(DEVICE)

                loss = method(model, image, target, idx, criterion, loader)
                loss.backward()

                if USE_MASK:
                    for name, param in model.named_parameters():
                        if name in mask:
                            param.grad *= mask[name]

                optimizer.step()
                optimizer.zero_grad()

            # -------------------------------------------------------------

            print("[MAIN] Evaluating model")
            accs, losses = eval_unlearning(
                model,
                [test_loader, forget_loader, retain_loader, val_loader],
                ["test", "forget", "retain", "val"],
                criterion,
                DEVICE,
            )
            accs["forget"] = 1 - accs["forget"]

            print("[MAIN] Computing MIA")
            mia_auc, mia_acc = compute_basic_mia(
                torch.tensor(losses["retain"]),
                torch.tensor(losses["forget"]),
                torch.tensor(losses["val"]),
                torch.tensor(losses["test"]),
            )
            test_acc = accs["test"]
            forget_acc = accs["forget"]
            retain_acc = accs["retain"]
            val_acc = accs["val"]

            if test_acc > best_test_acc:
                best_test_acc = test_acc
                best_test = accs
            if forget_acc > best_forget_acc:
                best_forget_acc = forget_acc
                best_forget = accs

            for key, value in accs.items():
                print(f"{key}: {round(value,2)}")
            print(f"MIA AUC: {round(mia_auc,2)}, MIA ACC: {round(mia_acc,2)}")

            if LOG:
                wandb.log(
                    {
                        "test": test_acc,
                        "forget": forget_acc,
                        "retain": retain_acc,
                        "val": val_acc,
                        "mia_auc": mia_auc,
                        "mia_acc": mia_acc,
                    }
                )

            # -------------------------------------------------------------

        print(f"Best test: {best_test}")
        print(f"Best forget: {best_forget}")
        wandb.finish()
