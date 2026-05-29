"""Asynchronous CNN waveform scoring.

The MQTT ingest path must stay fast even when CNN inference runs on GPU. This
worker accepts waveforms with ``submit()``, batches them in a background thread,
and calls back with the completed ``cnn_analysis`` so storage can update the
related frame later.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from cnn_embedding_runtime import CnnEmbeddingScorer


@dataclass
class CnnTask:
    serial: str | None
    timestamp: Any
    samples: list[float]
    queued_at: float


class AsyncCnnScorer:
    def __init__(
        self,
        scorer: CnnEmbeddingScorer,
        *,
        on_result: Callable[[CnnTask, dict[str, Any]], None],
        batch_size: int = 8,
        queue_size: int = 512,
        flush_ms: int = 100,
    ) -> None:
        self.scorer = scorer
        self.on_result = on_result
        self.batch_size = max(1, int(batch_size or 8))
        self.flush_s = max(0.001, float(flush_ms or 100) / 1000.0)
        self.queue: queue.Queue[CnnTask] = queue.Queue(maxsize=max(1, int(queue_size or 512)))
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="async-cnn-scorer", daemon=True)
        self.submitted = 0
        self.scored = 0
        self.dropped = 0
        self.failed = 0
        self.thread.start()

    def submit(self, samples: list[float], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        metadata = metadata or {}
        task = CnnTask(
            serial=metadata.get("serial"),
            timestamp=metadata.get("timestamp"),
            samples=list(samples),
            queued_at=time.time(),
        )
        try:
            self.queue.put_nowait(task)
        except queue.Full:
            self.dropped += 1
            return self._status("dropped", reason="cnn_queue_full")
        self.submitted += 1
        return self._status("queued")

    def _status(self, status: str, *, reason: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": status,
            "async": True,
            "model": str(self.scorer.model_path),
            "device": str(self.scorer.device),
            "requested_device": self.scorer.requested_device,
            "device_fallback_reason": self.scorer.device_fallback_reason,
            "queue_size": self.queue.qsize(),
            "submitted": self.submitted,
            "scored": self.scored,
            "dropped": self.dropped,
        }
        if reason:
            out["reason"] = reason
        return out

    def _run(self) -> None:
        while not self.stop_event.is_set() or not self.queue.empty():
            try:
                first = self.queue.get(timeout=self.flush_s)
            except queue.Empty:
                continue
            batch = [first]
            deadline = time.time() + self.flush_s
            while len(batch) < self.batch_size and time.time() < deadline:
                timeout = max(0.0, deadline - time.time())
                try:
                    batch.append(self.queue.get(timeout=timeout))
                except queue.Empty:
                    break
            self._score_batch(batch)

    def _score_batch(self, batch: list[CnnTask]) -> None:
        try:
            results = self.scorer.score_many([task.samples for task in batch])
        except Exception:
            # A single malformed waveform can poison a batch. Retry one by one
            # so healthy tasks still complete.
            for task in batch:
                try:
                    result = self.scorer.score(task.samples)
                except Exception as exc:  # noqa: BLE001
                    self.failed += 1
                    self._emit(task, {"status": "error", "async": True, "error": str(exc)})
                else:
                    self.scored += 1
                    result["status"] = "complete"
                    result["async"] = True
                    result["queue_latency_ms"] = int((time.time() - task.queued_at) * 1000)
                    self._emit(task, result)
            return

        for task, result in zip(batch, results):
            self.scored += 1
            result["status"] = "complete"
            result["async"] = True
            result["queue_latency_ms"] = int((time.time() - task.queued_at) * 1000)
            self._emit(task, result)

    def _emit(self, task: CnnTask, result: dict[str, Any]) -> None:
        try:
            self.on_result(task, result)
        except Exception:  # noqa: BLE001
            self.failed += 1

    def close(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        self.stop_event.set()
        if wait and self.thread.is_alive():
            self.thread.join(timeout=timeout)
