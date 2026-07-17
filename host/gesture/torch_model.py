#!/usr/bin/env python3
"""Runtime loader for the exported PyTorch EIT gesture MLP."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import torch


class TorchGestureClassifier:
    """Load a TorchScript MLP and its explicit preprocessing metadata.

    The confidence threshold is intentionally above the three-class chance
    level. Offline calibration metadata is retained separately from runtime
    scoring so the Web confidence remains comparable to RF predict_proba.
    """

    def __init__(self, model_dir: str | Path, confidence_threshold: float = 0.38, smoothing: float = 0.75):
        path = Path(model_dir).expanduser().resolve()
        if path.is_file():
            path = path.parent
        self.model_dir = path
        self.model = torch.jit.load(str(path / "gesture_mlp.ts"), map_location="cpu").eval()
        self.preprocess = json.loads((path / "preprocess.json").read_text(encoding="utf-8"))
        self.label_encoder = _LabelEncoder(json.loads((path / "labels.json").read_text(encoding="utf-8")))
        evaluation = json.loads((path / "evaluation.json").read_text(encoding="utf-8"))
        model_meta = evaluation.get("model", {})
        self.calibrated_temperature = float(model_meta.get("temperature", 1.0))
        # Runtime scores stay comparable to the original RF predict_proba
        # output. The calibrated value is retained for offline evaluation.
        self.temperature = float(model_meta.get("runtime_temperature", 1.0))
        self.confidence_threshold = float(confidence_threshold)
        self.smoothing = min(max(float(smoothing), 0.0), 1.0)
        self._ema_logits: np.ndarray | None = None

        self._keep_indices = np.asarray(self.preprocess["keep_indices"], dtype=np.int64)
        self._center = np.asarray(self.preprocess["center"], dtype=np.float32)
        self._scale = np.asarray(self.preprocess["scale"], dtype=np.float32)
        self._clip = float(self.preprocess.get("clip", 6.0))
        expected = len(self._keep_indices)
        if tuple(self.model(torch.zeros(1, expected)).shape) != (1, len(self.label_encoder.classes_)):
            raise ValueError("Torch gesture model shape does not match metadata")

    def reset(self) -> None:
        self._ema_logits = None

    def _transform(self, features: np.ndarray) -> np.ndarray:
        x = np.asarray(features, dtype=np.float32).reshape(1, -1)
        if x.shape[1] != len(self.preprocess["input_feature_names"]):
            raise ValueError(
                f"gesture feature count mismatch: got {x.shape[1]}, "
                f"expected {len(self.preprocess['input_feature_names'])}"
            )
        if not np.isfinite(x).all():
            raise ValueError("gesture features contain NaN or infinity")
        x = x[:, self._keep_indices]
        x = (x - self._center) / self._scale
        return np.clip(x, -self._clip, self._clip).astype(np.float32)

    def predict(self, features: np.ndarray) -> tuple[str, float, dict[str, float]]:
        x = self._transform(features)
        with torch.no_grad():
            logits = self.model(torch.from_numpy(x)).cpu().numpy()[0]

        if self._ema_logits is None or self.smoothing >= 1.0:
            self._ema_logits = logits
        else:
            self._ema_logits = self.smoothing * logits + (1.0 - self.smoothing) * self._ema_logits

        scaled = self._ema_logits / max(self.temperature, 1e-6)
        scaled -= np.max(scaled)
        exp = np.exp(scaled)
        probas = exp / np.sum(exp)
        best = int(np.argmax(probas))
        confidence = float(probas[best])
        label = self.label_encoder.classes_[best]
        if confidence < self.confidence_threshold:
            label = "unknown"
        return label, confidence, {
            name: float(probas[index])
            for index, name in enumerate(self.label_encoder.classes_)
        }


class _LabelEncoder:
    def __init__(self, classes: list[str]):
        self.classes_ = [str(label) for label in classes]
