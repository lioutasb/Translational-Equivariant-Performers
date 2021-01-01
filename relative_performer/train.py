#!/usr/bin/env python3
"""Train a model."""
import argparse
from pathlib import Path
import torch
import torch.nn as nn
import pytorch_lightning as pl
import pl_bolts.datamodules as datasets
from einops import rearrange

from relative_performer.constrained_relative_encoding import (
    RelativePerformer, LearnableSinusoidEncoding)

GPU_AVAILABLE = torch.cuda.is_available() and torch.cuda.device_count() > 0
DATA_PATH = Path(__file__).parent.parent.joinpath('data')


class RelativePerformerModel(pl.LightningModule):
    def __init__(self, dim, depth, heads, pos_scales, in_features=1,
                 pos_dims=1, max_pos=32, num_classes=10, **kwargs):
        super().__init__()
        self.save_hyperparameters()
        self.positional_embedding = LearnableSinusoidEncoding(
            pos_scales*2, max_timescale_init=max_pos*2)
        self.content_embedding = nn.Linear(in_features, dim)
        self.class_query = nn.Parameter(torch.Tensor(dim))
        self.performer = RelativePerformer(
            dim,
            depth,
            heads,
            pos_dims=pos_dims,
            pos_scales=pos_scales
        )
        self.output_layer = nn.Linear(dim, num_classes)
        self.loss = nn.CrossEntropyLoss()
        self.reset_parameters()

        self.train_acc = pl.metrics.Accuracy()
        self.val_acc = pl.metrics.Accuracy()

    def reset_parameters(self) -> None:
        nn.init.normal_(self.class_query)

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), self.hparams.learning_rate)

    def _flatten_to_sequence(self, input: torch.Tensor):
        """Flatten the 2D input into a 1D sequence.

        Preserve positional information in separate tensor.

        Args:
            input (torch.Tensor): Embeddings [bs, nx, ny, d]

        Returns:
            embeddings [bs, nx*ny, d], positions [bs, nx*ny, 2]
        """
        device, dtype = input.device, input.dtype
        nx, ny = input.shape[1:3]
        x_pos = torch.arange(0, nx, device=device, dtype=dtype)
        y_pos = torch.arange(0, ny, device=device, dtype=dtype)
        positions = torch.stack(torch.meshgrid(x_pos, y_pos), axis=-1)
        del x_pos, y_pos
        return (
            rearrange(input, 'b x y d -> b (x y) d'),
            rearrange(positions, 'x y d -> 1 (x y) d')
        )

    def _compute_positional_embeddings(self, positions):
        """Compute positional embeddings."""
        return rearrange(
            self.positional_embedding(positions), 'b n p d -> b n (p d)')

    def _add_class_query(self, embedding, pos_embedding):
        """Add class query element to beginning of sequences.

        Args:
            embedding: The element embeddings
            pos_embedding: The positional embedding

        Returns:
            embeddings, pos_embeddings both with additional class query element
            at the beginning of the sequence.
        """
        bs, *_, pos_embedding_dim = pos_embedding.shape
        # Add learnt class query to input, with zero positional encoding
        embedding = torch.cat(
            [
                self.class_query[None, None, :].expand(bs, 1, 1),
                embedding
            ],
            axis=1
        )
        pos_embedding = torch.cat(
            [
                torch.zeros(1, 1, pos_embedding_dim),
                pos_embedding
            ],
            axis=1
        )
        return embedding, pos_embedding

    def forward(self, x):
        embedding = self.content_embedding(x)
        embedding, positions = self._flatten_to_sequence(embedding)
        positions = self._compute_positional_embeddings(positions)
        # First element contains class prediction
        out = self.performer(embedding, positions)[:, 0]
        return self.output_layer(out)

    def training_step(self, batch, batch_idx):
        x, y = batch
        # The datasets always input in the format (C, W, H) instead of (W, H,
        # C).
        x = x.permute(0, 2, 3, 1)
        logits = self(x)
        loss = self.loss(logits, y)
        self.train_acc(logits, y)
        self.log('train_acc', self.train_acc, on_step=True, on_epoch=True,
                 prog_bar=True)
        return loss

    def validation_step(self, batch, batch_idx):
        x, y = batch
        # The datasets always input in the format (C, W, H) instead of (W, H,
        # C).
        x = x.permute(0, 2, 3, 1)
        logits = self(x)
        loss = self.loss(logits, y)
        self.val_acc(logits, y)
        self.log('val_acc', self.val_acc, on_step=True, on_epoch=True)
        return loss

    @ staticmethod
    def add_model_specific_args(parent_parser):
        parser = argparse.ArgumentParser(
            parents=[parent_parser], add_help=False)
        parser.add_argument('--learning_rate', default=0.001, type=float)
        parser.add_argument('--dim', type=int, default=128)
        parser.add_argument('--depth', type=int, default=4)
        parser.add_argument('--heads', type=int, default=4)
        parser.add_argument('--pos_scales', type=int, default=4)
        return parser


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('dataset', choices=[
        'FashionMNIST', 'MNIST', 'CIFAR10', 'TinyCIFAR10'])
    parser.add_argument('--batch_size', default=16, type=int)

    parser = RelativePerformerModel.add_model_specific_args(parser)
    args = parser.parse_args()

    data_cls = getattr(datasets, args.dataset + 'DataModule')
    try:
        dataset = data_cls(
            DATA_PATH.joinpath(args.dataset),
            normalize=True,
            num_workers=4,
            # shuffle=True  # TODO: Newer versions might require this to be set
        )
    except TypeError:
        # Some of the dataset modules don't support the normalize keyword
        dataset = data_cls(
            DATA_PATH.joinpath(args.dataset),
            num_workers=4
            # shuffle=True  # TODO: Newer versions might require this to be set
        )
    in_features, nx, ny = dataset.dims
    max_pos = max(nx, ny)
    model = RelativePerformerModel(
        **vars(args),
        in_features=in_features,
        pos_dims=2,
        max_pos=max_pos,
        num_classes=dataset.num_classes
    )

    trainer = pl.Trainer(gpus=-1 if GPU_AVAILABLE else None)
    trainer.fit(
        model,
        train_dataloader=dataset.train_dataloader(batch_size=args.batch_size),
        val_dataloaders=dataset.val_dataloader(batch_size=args.batch_size)
    )
