import datetime
import json
import os
import torch
import numpy as np
import wandb
import argparse

from torch.utils.data import DataLoader, Subset
from tqdm import tqdm
from dotenv import load_dotenv

from datasets import get_dataloaders
from utils import get_model, compute_topk, load_checkpoint


def train_loop(model, train_loader, criterion, optimizer, device):

    model.train()

    losses = []

    for _, data in enumerate(tqdm(train_loader)):

        image = data["image"]
        label = data["label"]

        image = image.to(device)
        label = label.to(device)

        optimizer.zero_grad()

        output = model(image)

        loss = criterion(output, label)

        losses.append(loss.item())

        loss.backward()
        optimizer.step()

    return np.mean(losses)


def test_loop(model, loader, criterion, device):

    model.eval()

    losses = []
    top1 = 0
    top5 = 0
    for idx, data in enumerate(tqdm(loader)):

        image = data["image"]
        labels = data["label"]

        image = image.to(device, non_blocking=True)
        labels = labels.to(device, non_blocking=True)

        with torch.no_grad():

            output = model(image)
            loss = criterion(output, labels)

            losses.append(loss.item())

            top1 += compute_topk(labels, output, 1)
            top5 += compute_topk(labels, output, 5)

    top1_acc = top1 / len(loader.dataset)
    top5_acc = top5 / len(loader.dataset)

    return np.mean(losses), top1_acc, top5_acc


def get_args():

    parser = argparse.ArgumentParser(description="Retrain model")

    parser.add_argument("--model", type=str, default="resnet18")
    parser.add_argument("--dataset", type=str, default="cifar10")
    parser.add_argument("--comment", type=str, default="pretrained")
    parser.add_argument("--lr", type=float, default=0.1)

    # Set this to 0 to train on all data
    parser.add_argument("--unlr", type=float, default=None)  # unlearning ratio.
    parser.add_argument("--itf", type=str, default=None)  # idx to forget
    parser.add_argument("--cf", type=int, default=None)  # class to forget
    args = parser.parse_args()

    return args


if __name__ == "__main__":

    args = get_args()

    SAVE_PATH = "checkpoints/"
    LOG = False

    # resnet18 vit_tiny_patch16_224 ... use timm.list_models() to get all models
    MODEL = "resnet18"

    # cifar10 cifar100 svnh ...
    DSET = "cifar10"

    comment = args.comment
    UNLR = None
    ITF = None

    CFS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]

    PAT = 10
    EPOCHS = 200
    LR = 0.1

    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

    nclasses = 100 if DSET == "cifar100" else 10

    for CF in CFS:

        comment = f"cls_{CF}"

        model, config, transform = get_model(MODEL, nclasses, True)

        (
            train_loader,
            val_loader,
            test_loader,
            forget_loader,
            retain_loader,
            _,
        ) = get_dataloaders(DSET, transform, unlr=None, itf=None, cf=CF)

        model_savename = f"{SAVE_PATH}{MODEL}_{DSET}_{comment}"

        ds = train_loader.dataset.dataset
        with open(f"{model_savename}_forget.json", "w") as f:
            json.dump(ds.FORGET, f)

        config = {
            "model": MODEL,
            "dataset": DSET,
            "nclasses": nclasses,
        }

        model = model.to(DEVICE)

        optimizer = torch.optim.SGD(
            model.parameters(), lr=LR, momentum=0.9, weight_decay=5e-4
        )
        criterion = torch.nn.CrossEntropyLoss()
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, mode="max", factor=0.2, patience=5, verbose=True
        )

        print("Training model")

        best_acc = 0
        pat = PAT
        for epoch in range(EPOCHS):

            loss = train_loop(model, retain_loader, criterion, optimizer, DEVICE)

            val_loss, val_top1, val_top5 = test_loop(
                model, val_loader, criterion, DEVICE
            )
            for_loss, for_top1, for_top5 = test_loop(
                model, forget_loader, criterion, DEVICE
            )

            scheduler.step(val_top1)

            print(f"lr: {optimizer.param_groups[0]['lr']}")

            print(
                f"Epoch: {epoch}, Loss: {round(loss,3)}, Val Loss: {round(val_loss,3)}, Val Top1: {round(val_top1,3)} Val Top5: {round(val_top5,3)} PAT: {pat}"
            )
            print(
                f"Epoch: {epoch}, Loss: {round(loss,3)}, For Loss: {round(for_loss,3)}, For Top1: {round(for_top1,3)} For Top5: {round(for_top5,3)} PAT: {pat}"
            )

            if val_top1 > best_acc:
                best_acc = val_top1
                pat = PAT

                model_savefile = {
                    "model": model.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "config": config,
                }

                model_savepath = f"{model_savename}.pt"
                torch.save(model_savefile, model_savepath)

            else:
                pat -= 1
                if pat == 0:
                    break

        test_loss, test_top1, test_top5 = test_loop(
            model, test_loader, criterion, DEVICE
        )

        print(
            f"Test Loss: {round(test_loss,3)}, Test Top1: {round(test_top1,3)} Test Top5: {round(test_top5,3)}"
        )
