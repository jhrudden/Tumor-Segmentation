import torch
from torch.utils.data import DataLoader, Dataset, Subset
from torchmetrics import MetricCollection
import pathlib
from tqdm import tqdm
from sklearn.model_selection import KFold
from typing import Optional
from argparse import Namespace

from src.models.model_utils import build_model_from_config
from src.utils.config import TrainingConfig
from src.utils.logging import LoggerMixin

def run_training_and_evaluation_cycle(
    model: torch.nn.Module,
    train_dataloader: DataLoader,
    val_dataloader: DataLoader,
    optimizer: torch.optim.Optimizer,
    lr_scheduler: Optional[torch.optim.lr_scheduler.LRScheduler],
    loss_fn: torch.nn.Module,
    config: TrainingConfig,
    metrics: MetricCollection,
    device: torch.device,
    logger=LoggerMixin,
) -> pathlib.Path:
    """
    Main training loop for the model

    Args:
    - model (torch.nn.Module): the model to train
    - train_dataloader (DataLoader): the training set dataloader
    - val_dataloader (DataLoader): the validation set dataloader
    - optimizer (torch.optim.Optimizer): the optimizer algorithm (e.g. SGD, Adam, etc.)
    - lr_scheduler (torch.optim.lr_scheduler.LRScheduler): the learning rate scheduler
    - loss_fn (torch.nn.Module): the loss function being optimized
    - metrics (MetricCollection): the metrics to track
    - logger (Logger): the logger to use for tracking metrics

    Returns:
    - pathlib.Path: the path to the best model
    """
    num_epochs = config.hyperparameters.n_epochs
    total_steps = len(train_dataloader) + len(val_dataloader)

    model.to(device)

    metrics_bundled = {}

    for epoch in range(num_epochs):
        with tqdm(
            total=total_steps, desc=f"Epoch {epoch+1}/{num_epochs}", unit="batch"
        ) as pbar:
            train_loss = train(
                model, train_dataloader, optimizer, loss_fn, config, device, pbar
            )

            val_metrics = evaluate(
                model, val_dataloader, loss_fn, metrics, device, pbar
            )

            metrics_bundled = {
                f"val_{metric_name}": metric_value
                for metric_name, metric_value in val_metrics.items()
            }
            metrics_bundled["train_loss"] = (
                train_loss  # add the training loss to the metrics
            )
            print(metrics_bundled)
            logger.log_metrics(metrics_bundled)

            # NOTE: checkpoints not implemented
        
        if (epoch + 1) in config.checkpoints:
            # save the model
            logger.save_model(model, f'model_checkpoint_{epoch+1}')
            print(f"Model checkpoint saved at epoch {epoch+1}")
    
        if lr_scheduler is not None:
            lr_scheduler.step()

    return metrics_bundled # last computed metrics

def train(
    model: torch.nn.Module,
    train_dataloader: DataLoader,
    optimizer: torch.optim.Optimizer,
    loss_fn: torch.nn.Module,
    config: Namespace,
    device: torch.device,
    pbar: tqdm,
):
    """
    Train the model on the training set

    Args:
    - model (torch.nn.Module): the model to train
    - train_dataloader (DataLoader): the training set dataloader
    - optimizer (torch.optim.Optimizer): the optimizer algorithm (e.g. SGD, Adam, etc.)
    - loss_fn (torch.nn.Module): the loss function being optimized
    - config (dict): the wandb configuration for the training
    - device (torch.device): the device to use for training
    - pbar (tqdm): the progress bar to update
    """
    model.train()
    cumulative_loss = 0
    optimizer.zero_grad()
    for i, (imgs, labels) in enumerate(train_dataloader):
        imgs, labels = imgs.to(device), labels.to(device)
        output = model(imgs)
        loss = loss_fn(output, labels)
        loss.backward()
        cumulative_loss += (loss.item() - cumulative_loss) / (i + 1)

        if (i + 1) % config.hyperparameters.accumulation_steps == 0: # Gradient accumulation
            optimizer.step()
            optimizer.zero_grad()

        pbar.set_postfix({"Avg Train Loss": f"{cumulative_loss}", "Phase": "Train"})
        pbar.update()

    return cumulative_loss


def evaluate(
    model: torch.nn.Module,
    val_dataloader: DataLoader,
    loss_fn: torch.nn.Module,
    metrics: MetricCollection,
    device: torch.device,
    pbar: tqdm,
):
    """
    Evaluate the model on the validation set

    Args:
    - model (torch.nn.Module): the model to evaluate
    - dataloader (DataLoader): the validation set dataloader
    - loss_fn (torch.nn.Module): the loss function to use
    - config (TrainingConfig): the training configuration
    - metrics (MetricCollection): the metrics to track
    - device (torch.device): the device to use for evaluation
    - pbar (tqdm): the progress bar to update
    """
    model.eval()
    # Reset the metrics
    metrics.reset()
    with torch.inference_mode():
        cumulative_loss = 0
        for i, (imgs, masks) in enumerate(val_dataloader):
            imgs, masks = imgs.to(device), masks.to(device)
            output = model(imgs)
            loss = loss_fn(output, masks)

            preds = output.detach()

            cumulative_loss += (loss.item() - cumulative_loss) / (i + 1)

            metrics(preds.cpu(), masks.type(torch.int32).cpu())
            pbar.set_postfix({"Avg Val Loss": f"{cumulative_loss}", "Phase": "Validation"})
            pbar.update()

    total_metrics = (
        metrics.compute()
    )  # Compute the metrics over the entire validation set

    # average metrics over samples
    for metric_name, metric_value in total_metrics.items():
        total_metrics[metric_name] = torch.mean(metric_value).item()

    return {"loss": cumulative_loss, **total_metrics}

def k_fold_cross_validation(config: TrainingConfig, train_dataset: Dataset, metrics: MetricCollection, logger: LoggerMixin):
    """
    Perform k-fold cross validation on the given training dataset

    Args:
    - config (TrainingConfig): the training configuration
    - train_dataset (Dataset): the training dataset
    - metrics (MetricCollection): the metrics to track
    - logger (LoggerMixin): logging object to track metrics, models, and visuals
    """
    # Create a KFold instance
    kfold = KFold(
        n_splits=config.n_folds,
        shuffle=True,
        random_state=config.random_state,
    )

    avg_metrics = {}

    # Train the model using k-fold cross validation
    for fold, (train_ids, val_ids) in enumerate(kfold.split(train_dataset)):
        print(f"Fold {fold}")
        print("---------------------------")

        sub_train_dataset = Subset(train_dataset, train_ids)
        val_dataset = Subset(train_dataset, val_ids)

        train_metrics = train_model(config, sub_train_dataset, val_dataset, metrics, logger)

        for metric_name, metric_value in train_metrics.items():
            if metric_name in avg_metrics:
                avg_metrics[metric_name] += (metric_value - avg_metrics[metric_name]) / (fold + 1) # update the average
            else:
                avg_metrics[metric_name] = metric_value
    
    return avg_metrics # average metrics over all folds

def train_model(config: TrainingConfig, train_dataset: Dataset, test_dataset: Dataset, metrics: MetricCollection, logger: LoggerMixin):
    """
    Train a segmentation model using the given configuration, datasets, metrics, and logger

    Args:
    - config (TrainingConfig): the training configuration
    - train_dataset (Dataset): the training dataset
    - test_dataset (Dataset): the testing dataset (or validation dataset if using k-fold cross validation)
    - metrics (MetricCollection): the metrics to track
    - logger (LoggerMixin): logging object to track metrics, models, and visuals
    """
    try:
        run_name = f"{config.architecture}_{config.dataset}"
        logger.init(name=run_name)
        device = config.device

        train_dataloader = DataLoader(
            train_dataset, batch_size=config.hyperparameters.batch_size
        )
        test_dataloader = DataLoader(
            test_dataset, batch_size=config.hyperparameters.batch_size
        )

        model, optimizer, lr_scheduler, loss_fn = build_model_from_config(config)
        
        # Train the model
        computed_metrics = run_training_and_evaluation_cycle(
            model,
            train_dataloader,
            test_dataloader,
            optimizer,
            lr_scheduler,
            loss_fn,
            config,
            metrics,
            device,
            logger=logger,
        )
    finally:
        logger.plot_metrics()
        logger.finish()
    
    return computed_metrics
