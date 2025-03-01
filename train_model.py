import argparse
import os
from pathlib import Path
from typing import List, Optional

from torch import Tensor

from model.diffusion import Trainer

# from model.unet import UNet
# from model.fdnunet import FDNUNet
from model.fdnunetwithaux import create_models

parser = argparse.ArgumentParser(description="Train model.")

parser.add_argument("--data_dir", type=str, default="data", help="Data directory.")
parser.add_argument(
    "--sample_data_dir", type=str, default="sample_data", help="Sample data directory."
)
parser.add_argument(
    "--num_steps_per_sample_condition",
    type=int,
    default=6,
    help="Number of steps per sample condition.",
)
parser.add_argument(
    "--num_steps_per_condition",
    type=int,
    default=6,
    help="Number of steps per condition.",
)
parser.add_argument(
    "--num_sample_conditions_per_plate",
    type=int,
    default=1,
    help="Number of sample conditions per plate.",
)
parser.add_argument(
    "--results_dir", type=str, default="results", help="Results directory."
)
parser.add_argument("--image_size", type=int, default=256, help="Image size.")
parser.add_argument("--batch_size", type=int, default=16, help="Batch size.")
parser.add_argument(
    "--num_gradient_accumulation_steps",
    type=int,
    default=1,
    help="Number of gradient accumulation steps.",
)
parser.add_argument("--num_steps", type=int, default=10000, help="Number of steps.")
parser.add_argument(
    "--num_steps_per_milestone",
    type=int,
    default=500,
    help="Number of steps per milestone.",
)
# parser.add_argument(
#     "--ema_steps_per_milestone", type=int, default=10, help="EMA steps per milestone."
# )
parser.add_argument("--learning_rate", type=float, default=3e-4, help="Learning rate.")
parser.add_argument("--loss_type", type=str, default="l1", help="Loss type.")
parser.add_argument(
    "--checkpoint",
    type=str,
    default=None,
    help="Checkpoint to load from (should be in results folder).",
)
parser.add_argument("--use_wandb", action="store_true", help="Use wandb.")
parser.add_argument("--wandb_project", type=str, help="Wandb project name.")
parser.add_argument(
    "--wandb_restrict_cache", type=int, default=10, help="Restrict wandb cache."
)

args = parser.parse_args()

if args.use_wandb:
    import wandb

    assert args.wandb_project is not None, "Must specify wandb project name."
    run = wandb.init(
        # set the wandb project where this run will be logged
        project=args.wandb_project,
    )
    wandb.define_metric("step")
    wandb.define_metric("train_loss", step_metric="step")
    wandb.define_metric("sample_loss", step_metric="step")


def inject_function(
    step: int,
    loss: float,
    sample_loss: Optional[float],
    image_filenames: Optional[List[str]],
    ranges: Optional[Tensor],
    milestone: Optional[int],
):
    log_dict = {
        "step": step,
        "train_loss": loss,
    }
    if sample_loss is not None:
        log_dict["sample_loss"] = sample_loss
    if image_filenames is not None:
        log_dict["samples"] = [wandb.Image(image) for image in image_filenames]
    if ranges is not None:
        log_dict["ranges"] = ranges
    wandb.log(log_dict)

    if milestone is not None:
        if args.wandb_restrict_cache is not None:
            os.system(f"wandb artifact cache cleanup {args.wandb_restrict_cache}")
        artifact = wandb.Artifact(name=f"checkpoint-{wandb.run.id}", type="model")
        if milestone == "latest":
            artifact.add_file((Path(args.results_dir) / f"model-{milestone}-prev.zip"))
        artifact.add_file((Path(args.results_dir) / f"model-{milestone}.zip"))


# model = UNet(
#     input_dim=64,
#     num_channels=2, # displacement (2)
#     num_condition_channels=4, # constraints (1) + force (2) + geometry (1)
# )

# model = FDNUNet(
#     input_dim=64,
#     num_channels=2,  # geometry (2)
#     # num_condition_channels=1, # geometry (1)
#     num_auxiliary_condition_channels=3,  # constraints (1) + force (2)
#     num_stages=4,
# )

encoder, decoder, auxiliary = create_models(
    input_dim=64,
    image_height=args.image_size,
    image_width=args.image_size,
    num_channels=2,  # materials (2)
    # num_condition_channels=1, # geometry (1)
    num_auxiliary_condition_channels=3,  # constraints (1) + force (2)
    num_stages=4,
)

# model = FDNUNetWithAux(
#     input_dim=64,
#     image_height=args.image_size,
#     image_width=args.image_size,
#     num_channels=2,  # materials (2)
#     # num_condition_channels=1, # geometry (1)
#     num_auxiliary_condition_channels=3,  # constraints (1) + force (2)
#     num_stages=4,
# )

trainer = Trainer(
    encoder=encoder,
    decoder=decoder,
    auxiliary=auxiliary,
    disable_auxiliary=True,  # I have disabled range prediction for now as discussed
    only_auxiliary=False,  # This disables the image output and only trains the range predictor (as well as the encoder currently, we can discuss this later)
    dataset_folder=args.data_dir,
    sample_dataset_folder=args.sample_data_dir,
    num_steps_per_condition=args.num_steps_per_condition,
    num_steps_per_sample_condition=args.num_steps_per_sample_condition,
    num_sample_conditions_per_plate=args.num_sample_conditions_per_plate,
    num_gradient_accumulation_steps=args.num_gradient_accumulation_steps,
    dataset_image_size=args.image_size,
    train_batch_size=args.batch_size,
    train_learning_rate=args.learning_rate,
    num_train_steps=args.num_steps,
    num_steps_per_milestone=args.num_steps_per_milestone,
    # ema_steps_per_milestone=args.ema_steps_per_milestone,
    loss_type=args.loss_type,
    results_folder=args.results_dir,
)

if args.checkpoint is not None:
    trainer.load_checkpoint(args.checkpoint)

if args.use_wandb:
    trainer.train(wandb_inject_function=inject_function)
    wandb.finish()
else:
    trainer.train()
