#!/usr/bin/env python3
"""Smoke checks for optional async CNN scoring.

This is intentionally lightweight: it proves the default no-GPU path stays
usable, async scoring callbacks work, and CUDA requests fallback cleanly when
CUDA is unavailable. A real GPU environment should still run this with
``CNN_DEVICE=cuda`` to confirm the active device is actually CUDA.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from async_cnn_worker import AsyncCnnScorer
from cnn_embedding_runtime import CnnEmbeddingScorer
from meter_data_store import NullMeterDataStore
from mqtt_stream_analyzer import build_cnn_scorer, build_parser


class FakeScorer:
    model_path = "fake.pt"
    device = "cpu"
    requested_device = "cpu"
    device_fallback_reason = None

    def score_many(self, samples_batch: list[list[float]]) -> list[dict[str, Any]]:
        return [
            {
                "reconstruction_mse": float(len(samples)),
                "nearest_embedding_distance": 0.1,
            }
            for samples in samples_batch
        ]

    def score(self, samples: list[float]) -> dict[str, Any]:
        return self.score_many([samples])[0]


def model_sample_count(scorer: CnnEmbeddingScorer) -> int:
    config = scorer.config
    input_mode = config.get("input_mode", "full")
    input_info = config.get("input_info", {})
    if input_mode == "gate":
        return int(input_info.get("gate_end", config["input_length"]))
    return int(config["input_length"])


def smoke_async_worker() -> None:
    results: list[tuple[Any, dict[str, Any]]] = []
    worker = AsyncCnnScorer(
        FakeScorer(),
        on_result=lambda task, result: results.append((task.timestamp, result)),
        batch_size=4,
        flush_ms=20,
    )
    try:
        for index in range(3):
            status = worker.submit([1.0, 2.0, 3.0], {"serial": "SMOKE", "timestamp": f"T{index}"})
            assert status["status"] == "queued", status
        deadline = time.time() + 2.0
        while len(results) < 3 and time.time() < deadline:
            time.sleep(0.02)
        assert len(results) == 3, results
        assert all(result["status"] == "complete" and result["async"] for _, result in results)
    finally:
        worker.close()


def smoke_missing_model_skip() -> None:
    args = build_parser().parse_args([
        "--cnn-model",
        "does-not-exist.pt",
        "--cnn-fallback",
        "skip",
    ])
    assert build_cnn_scorer(args, NullMeterDataStore()) is None


def smoke_real_model_cpu(model_path: Path) -> dict[str, Any] | None:
    if not model_path.exists():
        return None
    scorer = CnnEmbeddingScorer(model_path, device="cpu", top_k=1)
    samples = [0.0] * model_sample_count(scorer)
    result = scorer.score(samples)
    assert result["device"] == "cpu", result
    assert isinstance(result["reconstruction_mse"], float), result
    assert result["nearest_neighbors"], result
    return {
        "device": result["device"],
        "input_mode": result["input_mode"],
        "embedding_dim": result["embedding_dim"],
    }


def smoke_cuda_fallback(model_path: Path) -> dict[str, Any] | None:
    if not model_path.exists():
        return None
    scorer = CnnEmbeddingScorer(model_path, device="cuda", fallback="cpu", top_k=1)
    active = str(scorer.device)
    assert active in {"cpu", "cuda"}, active
    if active == "cpu":
        assert scorer.device_fallback_reason, "Expected CUDA fallback reason on CPU."
    return {
        "requested": scorer.requested_device,
        "active": active,
        "fallback": scorer.device_fallback_reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=Path("cnn_autoencoder_model.pt"))
    args = parser.parse_args()

    smoke_async_worker()
    smoke_missing_model_skip()
    cpu_result = smoke_real_model_cpu(args.model)
    cuda_result = smoke_cuda_fallback(args.model)
    print({
        "async_worker": "ok",
        "missing_model_skip": "ok",
        "cpu_model": cpu_result or "skipped_no_model",
        "cuda_request": cuda_result or "skipped_no_model",
    })


if __name__ == "__main__":
    main()
