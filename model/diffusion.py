from pathlib import Path

from tqdm import tqdm

import torch
from torch import nn, Tensor
from torch.optim import Adam
from torch.utils.data import Dataset, DataLoader
import torch.nn.functional as F
import torchvision.transforms as transforms
import torchvision.transforms.functional as TF

import numpy as np

from accelerate import Accelerator

from ema_pytorch import EMA

from PIL import Image

import logging
from datetime import datetime

from typing import Optional, Dict, Tuple, List

# In regular diffusion implementations, a groundtruth image is provided from the dataset, a random timestep is generated for each forward pass,
# the corresponding amount of noise is added to the groundtruth image to degrade it, and the predicted image is sampled from the model.
# 
# In our case however, the dataset contains both the groundtruth (final) image to be generated as well as the intermediate images, so we can
# use the intermediate images as the groundtruth images for each timestep. This means that each intermediate image and corresponding timestep is
# provided as a separate datapoint sampled from the dataset.
# 
# In this case then, the diffusion class is not necessary and we can just create a Trainer class directly.

def exists(value):
    return value is not None

class FEADataset(Dataset):
    def __init__(
            self, 
            folder: str, 
            extension: str = 'png', 
            image_size = 256, 
            augmentation: bool = True, 
            conditions_per_plate: int = 4, 
            num_steps: int = 11, 
            # displacement: bool = True, 
            # strain: bool = False, 
            # stress: bool = False, 
            min_max_magnitude: Tuple[int, int] = (500, 5000)
        ):
        super().__init__()
        self.path = Path(f'{folder}')
        assert self.path.exists(), f'Error: Dataset directory {self.path} does not exist.'
        self.extension = extension
        self.image_size = image_size
        self.augmentation = augmentation

        self.number_of_plate_geometries = len(list(self.path.glob('*')))
        
        self.conditions_per_plate_geometry = conditions_per_plate
        
        self.num_steps = num_steps - 1 # 0th index step is not used

        self.samples_per_plate = self.conditions_per_plate_geometry * self.num_steps

        self.total_samples = self.number_of_plate_geometries * self.samples_per_plate

        # self.displacement = displacement
        # self.strain = strain
        # self.stress = stress
        self.min_max_magnitude = min_max_magnitude

    def normalize_by_division(self, tensor: Tensor, value: float) -> Tensor:
        return tensor / value
    
    def normalize_to_negative_one_to_one(self, tensor: Tensor) -> Tensor:
        return tensor * 2.0 - 1.0
    
    def __len__(self):
        return self.total_samples
    
    def __getitem__(self, index: int) -> Dict[str, Tensor]:
        plate_index = (index // (self.samples_per_plate)) + 1
        condition_index = (index % (self.samples_per_plate)) // self.num_steps + 1
        step_index = (index % (self.samples_per_plate)) % self.num_steps + 1

        transform = transforms.Compose([
            transforms.Resize((self.image_size, self.image_size)),
            transforms.Grayscale(),
            transforms.RandomHorizontalFlip() if self.augmentation else transforms.Lambda(lambda x: x),
            transforms.RandomVerticalFlip() if self.augmentation else transforms.Lambda(lambda x: x),
            transforms.Lambda(lambda x: TF.invert(x)),
            transforms.PILToTensor(),
            transforms.Lambda(lambda x: self.normalize_by_division(x, 255.0)),
        ])
        
        sample = {}

        sample['geometry'] = self.normalize_to_negative_one_to_one(transform(Image.open(self.path / f'{plate_index}' / f'input.{self.extension}')))
        
        sample['iteration_index'] = torch.tensor(step_index)

        if step_index == 1:
            sample['previous_iteration'] = (
                sample['geometry'],
            )*2
        else:
            sample['previous_iteration'] = (
                self.normalize_to_negative_one_to_one(transform(Image.open(self.path / f'{plate_index}' / f'{condition_index}' / f'outputs_displacement_x_{step_index - 1}.{self.extension}'))), 
                self.normalize_to_negative_one_to_one(transform(Image.open(self.path / f'{plate_index}' / f'{condition_index}' / f'outputs_displacement_y_{step_index - 1}.{self.extension}')))
            )
        sample['previous_iteration'] = torch.cat(sample['previous_iteration'], dim = 0)

        # if self.displacement:
        sample['displacement'] = (
            self.normalize_to_negative_one_to_one(transform(Image.open(self.path / f'{plate_index}' / f'{condition_index}' / f'outputs_displacement_x_{step_index}.{self.extension}'))), 
            self.normalize_to_negative_one_to_one(transform(Image.open(self.path / f'{plate_index}' / f'{condition_index}' / f'outputs_displacement_y_{step_index}.{self.extension}')))
        )
        sample['displacement'] = torch.cat(sample['displacement'], dim = 0)

        # if self.strain:
        #     sample['strain'] = (transform(Image.open(self.path / f'{plate_index}' / f'{condition_index}' / f'outputs_strain_x_{step_index}.{self.extension}')), transform(Image.open(self.path / f'{plate_index}' / f'{condition_index}' / f'outputs_strain_y_{step_index}.{self.extension}')))
        # if self.stress:
        #     sample['stress'] = (transform(Image.open(self.path / f'{plate_index}' / f'{condition_index}' / f'outputs_stress_x_{step_index}.{self.extension}')), transform(Image.open(self.path / f'{plate_index}' / f'{condition_index}' / f'outputs_stress_y_{step_index}.{self.extension}')))
        
        sample['constraints'] = self.normalize_to_negative_one_to_one(transform(Image.open(self.path / f'{plate_index}' / f'{condition_index}' / f'regions_constraints.{self.extension}')))

        with open(self.path / f'{plate_index}' / f'{condition_index}' / f'magnitudes.txt', 'r') as f:
            magnitudes = list(map(lambda x: tuple(x.strip().split(':')), f.readlines()))
        
        forces = []

        for name, values in magnitudes:
            force_tensor = transform(Image.open(self.path / f'{plate_index}' / f'{condition_index}' / f'regions_{name}.{self.extension}'))
            normalized_magnitude = tuple(map(lambda value: np.sign(value) * ((float(abs(value)) - self.min_max_magnitude[0]) / (self.min_max_magnitude[1] - self.min_max_magnitude[0])), values))
            forces.append((force_tensor * normalized_magnitude[0], force_tensor * normalized_magnitude[1]))

        # Combine all forces into one tensor
        sample['forces'] = torch.sum(torch.stack(forces, dim = 0), dim = 0) / len(forces)
        sample['forces'] = torch.cat(sample['forces'], dim = 0)
        
        return sample

class Trainer():
    def __init__(
            self,
            model: nn.Module,
            dataset_folder: str,
            sample_dataset_folder: str,
            dataset_image_size: int = 256,
            use_dataset_augmentation: bool = True,
            train_batch_size: int = 16,
            sample_batch_size: Optional[int] = None,
            num_gradient_accumulation_steps: int = 1,
            train_learning_rate: float = 1e-4,
            num_train_epochs: int = 1000,
            num_epochs_per_milestone: int = 100,
            adam_betas = (0.9, 0.99),
            max_gradient_norm: float = 1.0,
            ema_decay: float = 0.995,
            ema_epochs_per_milestone: int = 10,
            results_folder: str = 'results',
            use_batch_split_over_devices: bool = True,
    ):
        super().__init__()
        self.accelerator = Accelerator(
            split_batches=use_batch_split_over_devices,
        )

        self.model = model
        
        # Parameters
        self.num_epochs_per_milestone = num_epochs_per_milestone
        self.sample_batch_size = sample_batch_size if exists(sample_batch_size) else train_batch_size
        
        self.train_batch_size = train_batch_size
        self.num_gradient_accumulation_steps = num_gradient_accumulation_steps

        self.max_gradient_norm = max_gradient_norm
        
        assert (train_batch_size * num_gradient_accumulation_steps) >= 16, f'your effective batch size (train_batch_size x gradient_accumulate_every) should be at least 16 or above'

        self.num_train_epochs = num_train_epochs

        # Dataset
        self.image_size = dataset_image_size
        self.dataset = FEADataset(dataset_folder, image_size=dataset_image_size, augmentation=use_dataset_augmentation)
        self.sample_dataset = FEADataset(sample_dataset_folder, image_size=dataset_image_size, augmentation=False)
        self.num_samples = len(self.sample_dataset)

        assert len(self.dataset) >= 100, 'you should have at least 100 samples in your folder. at least 10k images recommended'

        self.train_dataloader = DataLoader(self.dataset, batch_size=self.train_batch_size, shuffle=True, num_workers=0, pin_memory=True)
        self.sample_dataloader = DataLoader(self.sample_dataset, batch_size=self.sample_batch_size, shuffle=False, num_workers=0, pin_memory=True)

        # Optimizer
        self.optimizer = Adam(self.model.parameters(), lr=train_learning_rate, betas=adam_betas)

        # Results
        if self.accelerator.is_main_process:
            self.ema = EMA(self.model, decay=ema_decay, update_every=ema_epochs_per_milestone)
            self.ema.to(self.device)


        self.results_folder = Path(results_folder)
        self.results_folder.mkdir(exist_ok=True)

        log_name = 'train-e{}-b{}-lr{}-{}.log'.format(num_train_epochs, train_batch_size, str(train_learning_rate)[2:], datetime.now().strftime("%Y-%m-%d-%H-%M-%S"))
        logging.basicConfig(filename=(self.results_folder / log_name), level=logging.INFO, format='%(asctime)s %(message)s', force=True)

        self.epoch = 0

        # Prepare mode, optimizer, dataloader with accelerator
        self.model, self.optimizer, self.train_dataloader, self.sample_dataloader = self.accelerator.prepare(self.model, self.optimizer, self.train_dataloader, self.sample_dataloader)
        self.train_yielder = self.yield_data(self.train_dataloader)

    @property
    def device(self):
        return self.accelerator.device
    
    def save_checkpoint(self, milestone: int):
        if not self.accelerator.is_local_main_process:
            return
        
        checkpoint = {
            'epoch': self.epoch,
            'model': self.accelerator.get_state_dict(self.model),
            'optimizer': self.optimizer.state_dict(),
            'ema': self.ema.state_dict(),
        }

        torch.save(checkpoint, str(self.results_folder / f'model-{milestone}.pt'))

    def load_checkpoint(self, milestone: int):
        accelerator = self.accelerator
        device = accelerator.device

        checkpoint = torch.load(str(self.results_folder / f'model-{milestone}.pt'), map_location=device)

        model: nn.Module = self.accelerator.unwrap_model(self.model)
        model.load_state_dict(checkpoint['model'])

        self.epoch = checkpoint['epoch']
        self.optimizer.load_state_dict(checkpoint['optimizer'])

        if self.accelerator.is_main_process:
            self.ema.load_state_dict(checkpoint['ema'])

    def calculate_losses(self, sampled_iteration: Tensor, groundtruth_iteration: Tensor) -> Tensor:
        return F.mse_loss(sampled_iteration, groundtruth_iteration)

    @staticmethod
    def yield_data(dataloader) -> Dict[str, Tensor]:
        while True:
            for data in dataloader:
                yield data
    
    def unnormalize_from_negative_one_to_one(self, tensor: Tensor) -> Tensor:
        return (tensor + 1.0) / 2.0

    def create_view_friendly_image(self, image: Tensor) -> Image.Image:
        image = self.unnormalize_from_negative_one_to_one(image)
        image = image * 255.0
        image = TF.invert(image)
        image = image.repeat(3, 1, 1)
        image = TF.to_pil_image(image)

    def sample_model(self, sample: Dict[str, Tensor], use_ema_model: bool = False) -> Tensor:
        conditions = torch.cat((sample['forces'], sample['constraints']), dim = 1).to(self.device)
        previous_iteration = sample['previous_iteration'].to(self.device)
        iteration_index = sample['iteration_index'].to(self.device)
        prediction = self.model(previous_iteration, iteration_index, conditions) if not use_ema_model else self.ema.ema_model(previous_iteration, iteration_index, conditions)

        masked_prediction = prediction * (1.0 - self.unnormalize_from_negative_one_to_one(sample['constraints'])) # Mask out the regions that are constrained
        masked_prediction = masked_prediction * self.unnormalize_from_negative_one_to_one(sample['geometry']) # Mask out the regions that are not part of the geometry
        return masked_prediction

    def sample(self, batch, use_ema_model: bool = False) -> Tuple[List[Image.Image], Tensor]:
        with torch.inference_mode():
            output = self.sample_model(batch, use_ema_model)
            loss = self.calculate_losses(output, batch['displacement'])
            images = [self.create_view_friendly_image(output[i]) for i in range(output.shape[0])]
            return images, loss
            
    def train(self):
        with tqdm(initial = self.epoch, total = self.num_train_epochs, disable = not self.accelerator.is_main_process) as progress_bar:
            while self.epoch < self.num_train_epochs:
                total_loss = 0.0

                for _ in range(self.num_gradient_accumulation_steps):
                    sample: dict = next(self.train_yielder)
                    output = self.sample_model(sample)

                    loss = self.calculate_losses(output, sample['displacement'])
                    loss = loss / self.num_gradient_accumulation_steps
                    total_loss += loss.item()

                    self.accelerator.backward(loss)

                progress_bar.set_description(f'loss: {total_loss:.4f}')
                
                logging.info(f'epoch: {self.epoch}, loss: {total_loss:.4f}')

                self.accelerator.wait_for_everyone()
                self.accelerator.clip_grad_norm_(self.model.parameters(), self.max_gradient_norm) # Gradient clipping

                self.optimizer.step()
                self.optimizer.zero_grad()

                self.accelerator.wait_for_everyone()

                self.epoch += 1

                if self.accelerator.is_main_process:
                    self.ema.update()
                    if self.epoch != 0 and self.epoch % self.num_epochs_per_milestone == 0:
                        self.ema.ema_model.eval()
                        
                        with torch.inference_mode():
                            milestone = self.epoch // self.num_epochs_per_milestone
                            loss = 0.0
                            num_batches = 0
                            sampled_images: List[Image.Image] = []
                            for batch in self.sample_dataloader:
                                sampled_batch, batch_loss = self.sample(batch, use_ema_model=True)
                                sampled_images += sampled_batch
                                loss += batch_loss
                                num_batches += 1
                            loss /= num_batches
                            # self.accelerator.print(f'sample loss: {loss:.4f}')
                            logging.info(f'sample loss: {loss:.4f}')
                            for i, image in enumerate(sampled_images):
                                image.save(str(self.results_folder / f'milestone-{milestone}-sample-{i}.png'))
                            
                        self.save_checkpoint(milestone)

                progress_bar.update(1)
        self.accelerator.print('Training done!')