#!/usr/bin/env python3
"""Runtime scoring for saved 1D CNN autoencoder checkpoints."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any


class CnnEmbeddingScorer:
    """Load a saved CNN autoencoder and score one waveform at a time.

    Imports torch/numpy lazily so the rest of the MQTT analyzer can still run
    without deep-learning dependencies unless ``--cnn-model`` is supplied.
    """

    def __init__(self, model_path: Path, *, device: str = "auto", top_k: int = 3) -> None:
        import numpy as np  # noqa: PLC0415
        import torch  # noqa: PLC0415

        self.np = np
        self.torch = torch
        self.model_path = Path(model_path)
        self.top_k = max(1, int(top_k))
        self.device = self._choose_device(device)
        checkpoint = self._load_checkpoint(self.model_path)
        self.config = checkpoint["config"]

        self.model = Conv1dAutoencoder(
            int(self.config["input_length"]),
            int(self.config["embedding_dim"]),
            torch=torch,
        ).to(self.device)
        self.model.load_state_dict(checkpoint["state_dict"])
        self.model.eval()

        reference = checkpoint.get("reference") or {}
        ref_embeddings = reference.get("metric_embeddings")
        ref_rows = reference.get("rows")
        if ref_embeddings is not None:
            self.reference_metric_embeddings = ref_embeddings.detach().cpu().numpy().astype("float64")
            if ref_rows is not None:
                self.reference_rows = ref_rows.detach().cpu().numpy().astype("int64")
            else:
                self.reference_rows = np.arange(len(self.reference_metric_embeddings), dtype="int64")
        else:
            self.reference_metric_embeddings = None
            self.reference_rows = None

    def _choose_device(self, raw: str):
        torch = self.torch
        if raw != "auto":
            requested = torch.device(raw)
            if requested.type == "mps" and not torch.backends.mps.is_available():
                raise RuntimeError("Requested --cnn-device mps, but torch MPS is not available.")
            return requested
        if torch.cuda.is_available():
            return torch.device("cuda")
        if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
            return torch.device("mps")
        return torch.device("cpu")

    def _load_checkpoint(self, path: Path) -> dict[str, Any]:
        torch = self.torch
        try:
            return torch.load(path, map_location="cpu", weights_only=False)
        except TypeError:
            return torch.load(path, map_location="cpu")

    def score(self, samples: list[float]) -> dict[str, Any]:
        np = self.np
        torch = self.torch
        values = np.asarray(samples, dtype="float32")
        normalized = self._preprocess(values)
        with torch.no_grad():
            tensor = torch.from_numpy(normalized[None, None, :]).to(self.device)
            reconstruction, embedding = self.model(tensor)
            mse = torch.mean((reconstruction - tensor) ** 2, dim=(1, 2)).cpu().item()
            embedding_np = embedding.cpu().numpy().astype("float64")[0]

        metric_embedding = self._metric_embedding(embedding_np)
        neighbors = self._nearest_neighbors(metric_embedding)
        nearest_distance = neighbors[0]["embedding_distance"] if neighbors else None
        out = {
            "model": str(self.model_path),
            "device": str(self.device),
            "input_mode": self.config.get("input_mode", "full"),
            "embedding_dim": int(self.config["embedding_dim"]),
            "reconstruction_mse": float(mse),
            "reconstruction_thresholds": self.config.get("reconstruction_thresholds", {}),
            "nearest_embedding_distance": nearest_distance,
            "nearest_embedding_distance_thresholds": self.config.get(
                "nearest_embedding_distance_thresholds", {}
            ),
            "nearest_similarity": (
                float(1.0 / (1.0 + nearest_distance))
                if nearest_distance is not None
                else None
            ),
            "nearest_neighbors": neighbors,
            "reference_count": (
                int(len(self.reference_metric_embeddings))
                if self.reference_metric_embeddings is not None
                else 0
            ),
        }
        return out

    def _preprocess(self, values):
        np = self.np
        input_info = self.config.get("input_info", {})
        baseline_n = int(input_info.get("baseline_samples", 160))
        baseline_n = max(1, min(baseline_n, values.shape[0]))
        baseline = float(np.median(values[:baseline_n]))
        centered = values - baseline

        input_mode = self.config.get("input_mode", "full")
        if input_mode == "gate":
            info = self.config.get("input_info", {})
            gate_start = int(info.get("gate_start", self.config.get("profile", {}).get("gate_start", 0)))
            gate_end = int(info.get("gate_end", self.config.get("profile", {}).get("gate_end", len(values))))
            centered = centered[gate_start:gate_end]

        expected = int(self.config["input_length"])
        if centered.shape[0] != expected:
            raise ValueError(
                f"CNN model expects {expected} samples after preprocessing, "
                f"got {centered.shape[0]}."
            )

        normalization = self.config["normalization"]
        scale = max(float(normalization["scale"]), 1e-9)
        clip_sigma = float(self.config.get("clip_sigma", 6.0))
        return np.clip(centered / scale, -clip_sigma, clip_sigma).astype("float32")

    def _metric_embedding(self, embedding):
        np = self.np
        metric = self.config.get("embedding_metric") or {}
        center = np.asarray(metric.get("center", [0.0] * len(embedding)), dtype="float64")
        scale = np.asarray(metric.get("scale", [1.0] * len(embedding)), dtype="float64")
        return (embedding - center) / np.maximum(scale, 1e-12)

    def _nearest_neighbors(self, metric_embedding) -> list[dict[str, Any]]:
        np = self.np
        if self.reference_metric_embeddings is None or self.reference_rows is None:
            # Older checkpoints did not persist a reference bank. We can still
            # expose distance-to-center as a coarse drift metric, but threshold
            # comparability is weaker than true nearest-neighbor distance.
            distance = float(np.linalg.norm(metric_embedding))
            return [{
                "row": None,
                "embedding_distance": distance,
                "distance_similarity": float(1.0 / (1.0 + distance)),
                "kind": "distance_to_embedding_center",
            }]

        diff = self.reference_metric_embeddings - metric_embedding[None, :]
        distances = np.sqrt(np.maximum(np.sum(diff * diff, axis=1), 0.0))
        k = min(self.top_k, len(distances))
        idx = np.argpartition(distances, kth=k - 1)[:k]
        idx = idx[np.argsort(distances[idx])]
        return [
            {
                "row": int(self.reference_rows[i]),
                "embedding_distance": float(distances[i]),
                "distance_similarity": float(1.0 / (1.0 + distances[i])),
                "kind": "nearest_reference_embedding",
            }
            for i in idx
        ]


class Conv1dAutoencoder:
    def __new__(cls, input_length: int, embedding_dim: int, *, torch):
        nn = torch.nn

        class _Conv1dAutoencoder(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                if input_length % 8 != 0:
                    raise ValueError("input_length must be divisible by 8.")
                self.input_length = input_length
                encoded_length = input_length // 8
                encoded_size = 64 * encoded_length
                self.encoder_conv = nn.Sequential(
                    nn.Conv1d(1, 16, kernel_size=9, padding=4),
                    nn.ReLU(),
                    nn.Conv1d(16, 16, kernel_size=9, padding=4),
                    nn.ReLU(),
                    nn.MaxPool1d(2),
                    nn.Conv1d(16, 32, kernel_size=7, padding=3),
                    nn.ReLU(),
                    nn.MaxPool1d(2),
                    nn.Conv1d(32, 64, kernel_size=5, padding=2),
                    nn.ReLU(),
                    nn.MaxPool1d(2),
                )
                self.to_embedding = nn.Linear(encoded_size, embedding_dim)
                self.from_embedding = nn.Sequential(
                    nn.Linear(embedding_dim, encoded_size),
                    nn.ReLU(),
                )
                self.decoder_conv = nn.Sequential(
                    nn.Upsample(scale_factor=2, mode="nearest"),
                    nn.Conv1d(64, 32, kernel_size=5, padding=2),
                    nn.ReLU(),
                    nn.Upsample(scale_factor=2, mode="nearest"),
                    nn.Conv1d(32, 16, kernel_size=7, padding=3),
                    nn.ReLU(),
                    nn.Upsample(scale_factor=2, mode="nearest"),
                    nn.Conv1d(16, 8, kernel_size=9, padding=4),
                    nn.ReLU(),
                    nn.Conv1d(8, 1, kernel_size=9, padding=4),
                )

            def encode(self, x):
                z = self.encoder_conv(x)
                return self.to_embedding(z.flatten(start_dim=1))

            def decode(self, embedding):
                encoded_length = self.input_length // 8
                z = self.from_embedding(embedding).reshape(-1, 64, encoded_length)
                return self.decoder_conv(z)

            def forward(self, x):
                embedding = self.encode(x)
                reconstruction = self.decode(embedding)
                return reconstruction, embedding

        return _Conv1dAutoencoder()
