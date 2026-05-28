#!/usr/bin/env python3
"""Train a 1D CNN autoencoder and export waveform embeddings.

This is a deep-learning companion to the existing robust feature and k-means
scripts. It learns a compact embedding from known-good ultrasonic ADC
waveforms, then reports two complementary anomaly signals:

* reconstruction MSE: how well the autoencoder can redraw the waveform
* nearest-neighbor similarity: how close the learned embedding is to the
  historical good embedding cloud

The script is intentionally offline/batch-oriented. It reads the same GUI CSV
format used elsewhere in this repo: metadata columns followed by ``s_0`` ...
``s_N`` sample columns.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from pathlib import Path
from typing import Any, Callable

try:
    import numpy as np
    import torch
    from torch import nn
    from torch.utils.data import DataLoader, TensorDataset
except ImportError as exc:  # pragma: no cover - exercised by missing envs.
    raise SystemExit(
        "train_cnn_embedding_analyzer.py requires numpy and torch. "
        "Install the project requirements, then rerun."
    ) from exc

from train_oneclass_meter_model import (
    BASELINE_SAMPLES,
    count_rows,
    iter_waveforms,
    label_row_filter,
    learn_template,
    quantiles,
    read_header,
    split_selector,
)


DEFAULT_MODEL_OUT = Path("cnn_autoencoder_model.pt")
DEFAULT_REPORT_OUT = Path("cnn_embedding_report.json")
DEFAULT_ANALYSIS_OUT = Path("cnn_embedding_analysis.jsonl")


# ── CSV and metadata helpers ──────────────────────────────────────────────────


def parse_label_set(raw: str) -> set[str]:
    return {tok.strip().lower() for tok in raw.split(",") if tok.strip()}


def read_metadata(path: Path) -> dict[int, dict[str, Any]]:
    """Read non-sample columns keyed by 1-based CSV data row."""
    metadata: dict[int, dict[str, Any]] = {}
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return metadata
        keys = [name for name in reader.fieldnames if not name.startswith("s_")]
        for row_idx, row in enumerate(reader, start=1):
            metadata[row_idx] = {key: row.get(key, "") for key in keys}
    return metadata


def make_label_filter(path: Path, labels: set[str]) -> Callable[[int], bool] | None:
    if not labels:
        return None
    return label_row_filter(path, labels)


def combine_filters(*filters: Callable[[int], bool] | None) -> Callable[[int], bool]:
    active = [f for f in filters if f is not None]
    if not active:
        return lambda row_idx: True
    return lambda row_idx: all(f(row_idx) for f in active)


def load_waveforms(
    path: Path,
    sample_indices: list[int],
    include_row: Callable[[int], bool],
    max_rows: int | None,
) -> tuple[np.ndarray, np.ndarray]:
    rows: list[int] = []
    values: list[np.ndarray] = []
    for row_idx, waveform in iter_waveforms(path, sample_indices):
        if not include_row(row_idx):
            continue
        rows.append(row_idx)
        values.append(np.asarray(waveform, dtype=np.float32))
        if max_rows is not None and len(rows) >= max_rows:
            break
    if not values:
        raise ValueError("No waveforms matched the requested filters.")
    return np.asarray(rows, dtype=np.int64), np.stack(values)


# ── Preprocessing ─────────────────────────────────────────────────────────────


def build_centered_inputs(
    raw_waveforms: np.ndarray,
    input_mode: str,
    profile: dict[str, Any] | None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Baseline-center waveforms and optionally crop to the learned echo gate."""
    if raw_waveforms.ndim != 2:
        raise ValueError("Expected a 2D waveform matrix.")

    baseline_n = min(BASELINE_SAMPLES, raw_waveforms.shape[1])
    baselines = np.median(raw_waveforms[:, :baseline_n], axis=1, keepdims=True)
    centered = raw_waveforms - baselines
    info: dict[str, Any] = {
        "baseline_samples": int(baseline_n),
        "mode": input_mode,
        "source_sample_count": int(raw_waveforms.shape[1]),
    }

    if input_mode == "full":
        return centered.astype(np.float32), info

    if profile is None:
        raise ValueError("Gate mode requires a learned acoustic profile.")
    gate_start = int(profile["gate_start"])
    gate_end = int(profile["gate_end"])
    if gate_start < 0 or gate_end > raw_waveforms.shape[1] or gate_start >= gate_end:
        raise ValueError(f"Invalid gate range: {gate_start}..{gate_end}")
    info.update(
        {
            "gate_start": gate_start,
            "gate_end": gate_end,
            "gate_start_us": profile.get("gate_start_us"),
            "gate_end_us": profile.get("gate_end_us"),
            "template_peak_idx": profile.get("template_peak_idx"),
            "template_peak_time_us": profile.get("template_peak_time_us"),
        }
    )
    return centered[:, gate_start:gate_end].astype(np.float32), info


def fit_normalization(
    centered_inputs: np.ndarray,
    train_mask: np.ndarray,
    scale_percentile: float,
    min_scale: float,
) -> dict[str, float]:
    train_values = np.abs(centered_inputs[train_mask]).reshape(-1)
    if train_values.size == 0:
        raise ValueError("Training split is empty after filters.")
    scale = float(np.percentile(train_values, scale_percentile))
    scale = max(scale, float(min_scale))
    return {
        "center": 0.0,
        "scale": scale,
        "scale_percentile": float(scale_percentile),
        "min_scale": float(min_scale),
    }


def apply_normalization(
    centered_inputs: np.ndarray,
    normalization: dict[str, float],
    clip_sigma: float,
) -> np.ndarray:
    scale = max(float(normalization["scale"]), 1e-9)
    normalized = centered_inputs / scale
    return np.clip(normalized, -clip_sigma, clip_sigma).astype(np.float32)


# ── Model ────────────────────────────────────────────────────────────────────


class Conv1dAutoencoder(nn.Module):
    def __init__(self, input_length: int, embedding_dim: int) -> None:
        super().__init__()
        if input_length % 8 != 0:
            raise ValueError(
                f"input_length={input_length} must be divisible by 8 "
                "for the current CNN pooling layout."
            )
        self.input_length = input_length
        self.embedding_dim = embedding_dim
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

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        z = self.encoder_conv(x)
        return self.to_embedding(z.flatten(start_dim=1))

    def decode(self, embedding: torch.Tensor) -> torch.Tensor:
        encoded_length = self.input_length // 8
        z = self.from_embedding(embedding).reshape(-1, 64, encoded_length)
        return self.decoder_conv(z)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        embedding = self.encode(x)
        reconstruction = self.decode(embedding)
        return reconstruction, embedding


def choose_device(raw: str) -> torch.device:
    if raw != "auto":
        return torch.device(raw)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def train_autoencoder(
    inputs: np.ndarray,
    train_mask: np.ndarray,
    validation_mask: np.ndarray,
    *,
    embedding_dim: int,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    weight_decay: float,
    device: torch.device,
    seed: int,
) -> tuple[Conv1dAutoencoder, list[dict[str, float]]]:
    set_seed(seed)
    model = Conv1dAutoencoder(inputs.shape[1], embedding_dim).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=learning_rate,
        weight_decay=weight_decay,
    )

    train_tensor = torch.from_numpy(inputs[train_mask, None, :])
    train_loader = DataLoader(
        TensorDataset(train_tensor),
        batch_size=batch_size,
        shuffle=True,
        generator=torch.Generator().manual_seed(seed),
    )
    val_tensor = torch.from_numpy(inputs[validation_mask, None, :])

    history: list[dict[str, float]] = []
    best_state: dict[str, torch.Tensor] | None = None
    best_loss = float("inf")

    for epoch in range(1, epochs + 1):
        model.train()
        total_loss = 0.0
        seen = 0
        for (batch,) in train_loader:
            batch = batch.to(device)
            optimizer.zero_grad(set_to_none=True)
            reconstruction, _ = model(batch)
            loss = torch.mean((reconstruction - batch) ** 2)
            loss.backward()
            optimizer.step()
            total_loss += float(loss.detach().cpu()) * batch.shape[0]
            seen += batch.shape[0]

        train_loss = total_loss / max(seen, 1)
        validation_loss = evaluate_loss(model, val_tensor, batch_size, device)
        selection_loss = validation_loss if math.isfinite(validation_loss) else train_loss
        if selection_loss < best_loss:
            best_loss = selection_loss
            best_state = {
                key: value.detach().cpu().clone()
                for key, value in model.state_dict().items()
            }

        history.append(
            {
                "epoch": float(epoch),
                "train_loss": train_loss,
                "validation_loss": validation_loss,
            }
        )
        print(
            f"epoch {epoch:02d}/{epochs}: "
            f"train_mse={train_loss:.6f}, val_mse={validation_loss:.6f}"
        )

    if best_state is not None:
        model.load_state_dict(best_state)
    return model, history


@torch.no_grad()
def evaluate_loss(
    model: Conv1dAutoencoder,
    tensor: torch.Tensor,
    batch_size: int,
    device: torch.device,
) -> float:
    if tensor.shape[0] == 0:
        return float("nan")
    model.eval()
    loader = DataLoader(TensorDataset(tensor), batch_size=batch_size, shuffle=False)
    total = 0.0
    seen = 0
    for (batch,) in loader:
        batch = batch.to(device)
        reconstruction, _ = model(batch)
        loss = torch.mean((reconstruction - batch) ** 2)
        total += float(loss.cpu()) * batch.shape[0]
        seen += batch.shape[0]
    return total / max(seen, 1)


@torch.no_grad()
def encode_and_score(
    model: Conv1dAutoencoder,
    inputs: np.ndarray,
    batch_size: int,
    device: torch.device,
) -> tuple[np.ndarray, np.ndarray]:
    model.eval()
    tensor = torch.from_numpy(inputs[:, None, :])
    loader = DataLoader(TensorDataset(tensor), batch_size=batch_size, shuffle=False)
    embeddings: list[np.ndarray] = []
    reconstruction_mse: list[np.ndarray] = []

    for (batch,) in loader:
        batch = batch.to(device)
        reconstruction, embedding = model(batch)
        mse = torch.mean((reconstruction - batch) ** 2, dim=(1, 2))
        embeddings.append(embedding.cpu().numpy().astype(np.float32))
        reconstruction_mse.append(mse.cpu().numpy().astype(np.float64))

    return np.vstack(embeddings), np.concatenate(reconstruction_mse)


# ── Similarity and reporting ─────────────────────────────────────────────────


def safe_quantiles(values: list[float] | np.ndarray) -> dict[str, float | None]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if not vals:
        return {
            "p01": None,
            "p05": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "p995": None,
            "p999": None,
        }
    return {key: float(value) for key, value in quantiles(vals).items()}


def l2_normalize(values: np.ndarray) -> np.ndarray:
    denom = np.linalg.norm(values, axis=1, keepdims=True)
    denom = np.maximum(denom, 1e-12)
    return values / denom


def fit_embedding_metric(
    embeddings: np.ndarray,
    train_mask: np.ndarray,
    min_scale: float = 1e-6,
) -> dict[str, Any]:
    train_embeddings = embeddings[train_mask].astype(np.float64)
    center = np.median(train_embeddings, axis=0)
    mad = np.median(np.abs(train_embeddings - center), axis=0)
    scale = np.maximum(1.4826 * mad, min_scale)
    return {
        "center": center.tolist(),
        "scale": scale.tolist(),
        "min_scale": float(min_scale),
        "note": "robust per-dimension median/MAD scaling for nearest-neighbor distance",
    }


def apply_embedding_metric(
    embeddings: np.ndarray,
    metric: dict[str, Any],
) -> np.ndarray:
    center = np.asarray(metric["center"], dtype=np.float64)
    scale = np.asarray(metric["scale"], dtype=np.float64)
    return (embeddings.astype(np.float64) - center) / np.maximum(scale, 1e-12)


def compute_nearest_neighbors(
    embeddings: np.ndarray,
    metric_embeddings: np.ndarray,
    row_indices: np.ndarray,
    reference_mask: np.ndarray,
    top_k: int,
    chunk_size: int,
) -> list[list[dict[str, float | int]]]:
    reference_embeddings = metric_embeddings[reference_mask]
    reference_raw_embeddings = embeddings[reference_mask]
    reference_rows = row_indices[reference_mask]
    if reference_embeddings.shape[0] == 0:
        raise ValueError("No reference embeddings are available for similarity search.")

    all_norm = l2_normalize(embeddings.astype(np.float64))
    ref_norm = l2_normalize(reference_raw_embeddings.astype(np.float64))
    ref_by_row = {int(row): i for i, row in enumerate(reference_rows)}
    effective_k = min(top_k, reference_embeddings.shape[0])
    results: list[list[dict[str, float | int]]] = []
    ref_sq_norm = np.sum(reference_embeddings * reference_embeddings, axis=1)

    for start in range(0, embeddings.shape[0], chunk_size):
        end = min(start + chunk_size, embeddings.shape[0])
        query = metric_embeddings[start:end]
        query_sq_norm = np.sum(query * query, axis=1, keepdims=True)
        distances_sq = query_sq_norm + ref_sq_norm[None, :] - 2.0 * (query @ reference_embeddings.T)
        distances_sq = np.maximum(distances_sq, 0.0)
        cosine_sims = all_norm[start:end] @ ref_norm.T
        for local_i, row_idx in enumerate(row_indices[start:end]):
            own_ref = ref_by_row.get(int(row_idx))
            if own_ref is not None:
                distances_sq[local_i, own_ref] = np.inf

        if effective_k == 0:
            results.extend([] for _ in range(start, end))
            continue

        top_unsorted = np.argpartition(distances_sq, kth=effective_k - 1, axis=1)[:, :effective_k]
        for local_i in range(end - start):
            candidate_idx = top_unsorted[local_i]
            candidate_idx = candidate_idx[np.argsort(distances_sq[local_i, candidate_idx])]
            neighbors: list[dict[str, float | int]] = []
            for ref_i in candidate_idx:
                distance = math.sqrt(float(distances_sq[local_i, ref_i]))
                if not math.isfinite(distance):
                    continue
                cosine_similarity = float(cosine_sims[local_i, ref_i])
                neighbors.append(
                    {
                        "row": int(reference_rows[ref_i]),
                        "embedding_distance": distance,
                        "distance_similarity": float(1.0 / (1.0 + distance)),
                        "cosine_similarity": cosine_similarity,
                        "cosine_distance": float(1.0 - cosine_similarity),
                    }
                )
            results.append(neighbors)

    return results


def label_from_thresholds(
    reconstruction_mse: float,
    nearest_distance: float | None,
    reconstruction_thresholds: dict[str, float],
    distance_thresholds: dict[str, float | None],
) -> str:
    if reconstruction_mse >= reconstruction_thresholds["anomaly"]:
        return "anomaly"
    if nearest_distance is not None:
        anomaly_distance = distance_thresholds.get("anomaly")
        if anomaly_distance is not None and nearest_distance >= anomaly_distance:
            return "anomaly"
    if reconstruction_mse >= reconstruction_thresholds["suspect"]:
        return "suspect"
    if nearest_distance is not None:
        suspect_distance = distance_thresholds.get("suspect")
        if suspect_distance is not None and nearest_distance >= suspect_distance:
            return "suspect"
    return "normal"


def row_record(
    i: int,
    row_indices: np.ndarray,
    metadata: dict[int, dict[str, Any]],
    reconstruction_mse: np.ndarray,
    embeddings: np.ndarray,
    neighbors: list[list[dict[str, float | int]]],
    reconstruction_thresholds: dict[str, float],
    distance_thresholds: dict[str, float | None],
    embedding_digits: int,
) -> dict[str, Any]:
    row_idx = int(row_indices[i])
    row_neighbors = neighbors[i]
    nearest_similarity = (
        float(row_neighbors[0]["distance_similarity"]) if row_neighbors else None
    )
    nearest_distance = (
        float(row_neighbors[0]["embedding_distance"]) if row_neighbors else None
    )
    state = label_from_thresholds(
        float(reconstruction_mse[i]),
        nearest_distance,
        reconstruction_thresholds,
        distance_thresholds,
    )
    return {
        "row": row_idx,
        "metadata": metadata.get(row_idx, {}),
        "cnn_state": state,
        "reconstruction_mse": float(reconstruction_mse[i]),
        "nearest_embedding_distance": nearest_distance,
        "nearest_similarity": nearest_similarity,
        "nearest_neighbors": row_neighbors,
        "embedding": [round(float(v), embedding_digits) for v in embeddings[i]],
    }


def compact_row(
    i: int,
    row_indices: np.ndarray,
    metadata: dict[int, dict[str, Any]],
    reconstruction_mse: np.ndarray,
    neighbors: list[list[dict[str, float | int]]],
) -> dict[str, Any]:
    row_idx = int(row_indices[i])
    nearest = neighbors[i][0] if neighbors[i] else None
    return {
        "row": row_idx,
        "metadata": metadata.get(row_idx, {}),
        "reconstruction_mse": float(reconstruction_mse[i]),
        "nearest": nearest,
    }


def build_report(
    args: argparse.Namespace,
    *,
    row_indices: np.ndarray,
    train_mask: np.ndarray,
    validation_mask: np.ndarray,
    metadata: dict[int, dict[str, Any]],
    input_info: dict[str, Any],
    normalization: dict[str, float],
    embedding_metric: dict[str, Any],
    profile: dict[str, Any] | None,
    history: list[dict[str, float]],
    reconstruction_mse: np.ndarray,
    neighbors: list[list[dict[str, float | int]]],
    device: torch.device,
) -> tuple[dict[str, Any], dict[str, float], dict[str, float | None], dict[str, float | None]]:
    train_mse = reconstruction_mse[train_mask]
    mse_q = safe_quantiles(train_mse)
    reconstruction_thresholds = {
        "suspect": float(mse_q["p995"] or 0.0),
        "anomaly": float(mse_q["p999"] or 0.0),
    }

    nearest_similarity = np.asarray(
        [
            float(row_neighbors[0]["distance_similarity"])
            if row_neighbors
            else float("nan")
            for row_neighbors in neighbors
        ],
        dtype=np.float64,
    )
    nearest_distance = np.asarray(
        [
            float(row_neighbors[0]["embedding_distance"])
            if row_neighbors
            else float("nan")
            for row_neighbors in neighbors
        ],
        dtype=np.float64,
    )
    train_similarity = nearest_similarity[train_mask]
    train_distance = nearest_distance[train_mask]
    sim_q = safe_quantiles(train_similarity)
    distance_q = safe_quantiles(train_distance)
    distance_thresholds: dict[str, float | None] = {
        "suspect": distance_q["p95"],
        "anomaly": distance_q["p99"],
    }
    similarity_thresholds: dict[str, float | None] = {
        "suspect_below": sim_q["p05"],
        "anomaly_below": sim_q["p01"],
    }

    recon_order = np.argsort(-reconstruction_mse)
    distance_order = np.argsort(-nearest_distance)
    top_n = args.top_outliers
    top_reconstruction = [
        compact_row(i, row_indices, metadata, reconstruction_mse, neighbors)
        for i in recon_order[:top_n]
    ]
    top_similarity = [
        compact_row(i, row_indices, metadata, reconstruction_mse, neighbors)
        for i in distance_order
        if math.isfinite(float(nearest_distance[i]))
    ][:top_n]

    labels: dict[str, int] = {}
    for i in range(len(row_indices)):
        state = label_from_thresholds(
            float(reconstruction_mse[i]),
            None if not math.isfinite(float(nearest_distance[i])) else float(nearest_distance[i]),
            reconstruction_thresholds,
            distance_thresholds,
        )
        labels[state] = labels.get(state, 0) + 1

    report = {
        "model_type": "cnn_1d_autoencoder_embedding_analyzer",
        "dataset": str(args.csv),
        "rows_loaded": int(len(row_indices)),
        "csv_rows": int(count_rows(args.csv)),
        "train_rows": int(np.sum(train_mask)),
        "validation_rows": int(np.sum(validation_mask)),
        "train_fraction": float(args.train_fraction),
        "split_mode": args.split_mode,
        "train_labels": sorted(parse_label_set(args.train_labels)),
        "analyze_labels": sorted(parse_label_set(args.analyze_labels)),
        "input": input_info,
        "normalization": normalization,
        "embedding_metric": embedding_metric,
        "embedding_dim": int(args.embedding_dim),
        "epochs": int(args.epochs),
        "batch_size": int(args.batch_size),
        "learning_rate": float(args.learning_rate),
        "weight_decay": float(args.weight_decay),
        "device": str(device),
        "training_history": history,
        "reconstruction_thresholds": reconstruction_thresholds,
        "reconstruction_threshold_note": (
            "suspect=p99.5 and anomaly=p99.9 of train reconstruction MSE"
        ),
        "similarity_thresholds": similarity_thresholds,
        "similarity_threshold_note": (
            "distance_similarity=1/(1+embedding_distance); lower is more unusual"
        ),
        "nearest_embedding_distance_thresholds": distance_thresholds,
        "nearest_embedding_distance_threshold_note": (
            "suspect=training p95 and anomaly=training p99 of nearest-neighbor "
            "distance in robust-standardized embedding space"
        ),
        "summary_labels": labels,
        "reconstruction_mse_quantiles": {
            "train": safe_quantiles(reconstruction_mse[train_mask]),
            "validation": safe_quantiles(reconstruction_mse[validation_mask]),
            "all": safe_quantiles(reconstruction_mse),
        },
        "nearest_similarity_quantiles": {
            "train": sim_q,
            "validation": safe_quantiles(nearest_similarity[validation_mask]),
            "all": safe_quantiles(nearest_similarity),
        },
        "nearest_embedding_distance_quantiles": {
            "train": distance_q,
            "validation": safe_quantiles(nearest_distance[validation_mask]),
            "all": safe_quantiles(nearest_distance),
        },
        "top_reconstruction_outliers": top_reconstruction,
        "top_similarity_outliers": top_similarity,
    }
    if profile is not None:
        report["profile"] = {
            key: value
            for key, value in profile.items()
            if key != "template_gate"
        }
    return report, reconstruction_thresholds, distance_thresholds, similarity_thresholds


def write_analysis_jsonl(
    path: Path,
    *,
    row_indices: np.ndarray,
    metadata: dict[int, dict[str, Any]],
    reconstruction_mse: np.ndarray,
    embeddings: np.ndarray,
    neighbors: list[list[dict[str, float | int]]],
    reconstruction_thresholds: dict[str, float],
    distance_thresholds: dict[str, float | None],
    embedding_digits: int,
) -> None:
    with path.open("w") as f:
        for i in range(len(row_indices)):
            record = row_record(
                i,
                row_indices,
                metadata,
                reconstruction_mse,
                embeddings,
                neighbors,
                reconstruction_thresholds,
                distance_thresholds,
                embedding_digits,
            )
            f.write(json.dumps(record, separators=(",", ":")) + "\n")


def print_report(report: dict[str, Any], model_out: Path, report_out: Path, analysis_out: Path) -> None:
    print("\n1D CNN autoencoder embedding analyzer")
    print(f"  dataset: {report['dataset']}")
    print(
        f"  rows: train={report['train_rows']}, "
        f"validation={report['validation_rows']}, loaded={report['rows_loaded']}"
    )
    print(
        f"  input: {report['input']['mode']} "
        f"({report['input']['source_sample_count']} samples -> "
        f"{report['input'].get('gate_end', report['input']['source_sample_count']) - report['input'].get('gate_start', 0)} cnn samples)"
    )
    print(f"  embedding dim: {report['embedding_dim']}")
    print(
        "  reconstruction thresholds: "
        f"suspect>={report['reconstruction_thresholds']['suspect']:.6f}, "
        f"anomaly>={report['reconstruction_thresholds']['anomaly']:.6f}"
    )
    sim = report["similarity_thresholds"]
    if sim["suspect_below"] is not None and sim["anomaly_below"] is not None:
        print(
            "  similarity thresholds: "
            f"suspect<={sim['suspect_below']:.6f}, "
            f"anomaly<={sim['anomaly_below']:.6f}"
        )
    dist = report["nearest_embedding_distance_thresholds"]
    if dist["suspect"] is not None and dist["anomaly"] is not None:
        print(
            "  embedding distance thresholds: "
            f"suspect>={dist['suspect']:.6f}, "
            f"anomaly>={dist['anomaly']:.6f}"
        )
    print(f"  summary labels: {report['summary_labels']}")

    print("\nTop reconstruction outliers")
    for item in report["top_reconstruction_outliers"][:5]:
        meta = item["metadata"]
        nearest = item["nearest"] or {}
        print(
            f"  row {item['row']}: mse={item['reconstruction_mse']:.6f}, "
            f"label={meta.get('label', '')}, sq={meta.get('sq', '')}, "
            f"nearest={nearest.get('row', '')} "
            f"dist={nearest.get('embedding_distance', '')} "
            f"sim={nearest.get('distance_similarity', '')}"
        )

    print("\nWrote:")
    print(f"  model: {model_out}")
    print(f"  report: {report_out}")
    print(f"  per-row analysis: {analysis_out}")


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument(
        "--input-mode",
        choices=["full", "gate"],
        default="full",
        help="full uses all samples; gate crops to the learned echo gate.",
    )
    parser.add_argument("--embedding-dim", type=int, default=64)
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument(
        "--split-mode",
        choices=["interleaved", "temporal"],
        default="interleaved",
        help="interleaved checks same-distribution behavior; temporal checks time drift.",
    )
    parser.add_argument(
        "--train-labels",
        default="good",
        help="Comma-separated labels used for training. Default: good. "
             "If the CSV has no label column, all rows are eligible.",
    )
    parser.add_argument(
        "--analyze-labels",
        default="",
        help="Comma-separated labels to analyze. Empty = analyze all rows.",
    )
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--top-outliers", type=int, default=20)
    parser.add_argument("--neighbor-chunk-size", type=int, default=512)
    parser.add_argument("--scale-percentile", type=float, default=99.0)
    parser.add_argument("--min-scale", type=float, default=0.05)
    parser.add_argument("--clip-sigma", type=float, default=6.0)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--embedding-digits", type=int, default=6)
    parser.add_argument("--model-out", type=Path, default=DEFAULT_MODEL_OUT)
    parser.add_argument("--report-out", type=Path, default=DEFAULT_REPORT_OUT)
    parser.add_argument("--analysis-out", type=Path, default=DEFAULT_ANALYSIS_OUT)
    args = parser.parse_args()

    if not 0.01 <= args.train_fraction <= 0.99:
        raise SystemExit("--train-fraction must be between 0.01 and 0.99")
    if args.top_k < 1:
        raise SystemExit("--top-k must be >= 1")

    header, sample_indices = read_header(args.csv)
    total_rows = count_rows(args.csv)
    base_train_filter = split_selector(total_rows, args.train_fraction, args.split_mode)
    train_label_filter = make_label_filter(args.csv, parse_label_set(args.train_labels))
    analyze_label_filter = make_label_filter(args.csv, parse_label_set(args.analyze_labels))
    train_filter = combine_filters(base_train_filter, train_label_filter)
    analyze_filter = combine_filters(analyze_label_filter)

    profile = None
    if args.input_mode == "gate":
        profile = learn_template(args.csv, sample_indices, train_filter)

    print(f"Loading waveforms from {args.csv}...")
    row_indices, raw_waveforms = load_waveforms(
        args.csv,
        sample_indices,
        analyze_filter,
        args.max_rows,
    )
    metadata = read_metadata(args.csv)
    centered_inputs, input_info = build_centered_inputs(raw_waveforms, args.input_mode, profile)
    train_mask = np.asarray([train_filter(int(row)) for row in row_indices], dtype=bool)
    validation_mask = np.logical_not(train_mask)
    if int(np.sum(train_mask)) < 2:
        raise SystemExit(
            "Training split has fewer than 2 rows after filters. "
            "Relax --train-labels, increase --max-rows, or change --split-mode."
        )

    normalization = fit_normalization(
        centered_inputs,
        train_mask,
        args.scale_percentile,
        args.min_scale,
    )
    normalized_inputs = apply_normalization(centered_inputs, normalization, args.clip_sigma)
    device = choose_device(args.device)
    print(
        f"Training CNN autoencoder on {int(np.sum(train_mask))} rows "
        f"({normalized_inputs.shape[1]} samples each) using {device}..."
    )
    model, history = train_autoencoder(
        normalized_inputs,
        train_mask,
        validation_mask,
        embedding_dim=args.embedding_dim,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.learning_rate,
        weight_decay=args.weight_decay,
        device=device,
        seed=args.seed,
    )

    print("Encoding waveforms and computing reconstruction errors...")
    embeddings, reconstruction_mse = encode_and_score(
        model,
        normalized_inputs,
        args.batch_size,
        device,
    )
    embedding_metric = fit_embedding_metric(embeddings, train_mask)
    metric_embeddings = apply_embedding_metric(embeddings, embedding_metric)
    print("Computing nearest neighbors in embedding space...")
    neighbors = compute_nearest_neighbors(
        embeddings,
        metric_embeddings,
        row_indices,
        train_mask,
        args.top_k,
        args.neighbor_chunk_size,
    )

    (
        report,
        reconstruction_thresholds,
        distance_thresholds,
        similarity_thresholds,
    ) = build_report(
        args,
        row_indices=row_indices,
        train_mask=train_mask,
        validation_mask=validation_mask,
        metadata=metadata,
        input_info=input_info,
        normalization=normalization,
        embedding_metric=embedding_metric,
        profile=profile,
        history=history,
        reconstruction_mse=reconstruction_mse,
        neighbors=neighbors,
        device=device,
    )

    checkpoint = {
        "model_type": report["model_type"],
        "state_dict": model.cpu().state_dict(),
        "reference": {
            "rows": torch.from_numpy(row_indices[train_mask].astype(np.int64)),
            "metric_embeddings": torch.from_numpy(metric_embeddings[train_mask].astype(np.float32)),
        },
        "config": {
            "input_length": int(normalized_inputs.shape[1]),
            "input_mode": args.input_mode,
            "embedding_dim": int(args.embedding_dim),
            "normalization": normalization,
            "embedding_metric": embedding_metric,
            "clip_sigma": float(args.clip_sigma),
            "input_info": input_info,
            "sample_columns": [header[i] for i in sample_indices],
            "reconstruction_thresholds": reconstruction_thresholds,
            "nearest_embedding_distance_thresholds": distance_thresholds,
            "similarity_thresholds": similarity_thresholds,
        },
        "report": {
            key: value
            for key, value in report.items()
            if key not in {"top_reconstruction_outliers", "top_similarity_outliers"}
        },
    }
    if profile is not None:
        checkpoint["config"]["profile"] = profile

    torch.save(checkpoint, args.model_out)
    args.report_out.write_text(json.dumps(report, indent=2))
    write_analysis_jsonl(
        args.analysis_out,
        row_indices=row_indices,
        metadata=metadata,
        reconstruction_mse=reconstruction_mse,
        embeddings=embeddings,
        neighbors=neighbors,
        reconstruction_thresholds=reconstruction_thresholds,
        distance_thresholds=distance_thresholds,
        embedding_digits=args.embedding_digits,
    )
    print_report(report, args.model_out, args.report_out, args.analysis_out)


if __name__ == "__main__":
    main()
