#!/usr/bin/env python3
"""Train a small PyTorch MLP for EIT gesture classification.

The training split is grouped by collection session and repetition so adjacent
frames from one held gesture cannot appear in both training and validation.
The exported network contains only Linear and ReLU operators; preprocessing is
saved separately to keep the eventual MCU implementation explicit.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import torch
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score
from sklearn.model_selection import StratifiedGroupKFold
from torch import nn
from torch.utils.data import DataLoader, TensorDataset


META_COLUMNS = {"frame_id", "gesture", "rep"}
DEFAULT_GESTURE_ROOT = Path("host/gestures")


@dataclass
class Dataset:
    x: np.ndarray
    y: np.ndarray
    groups: np.ndarray
    feature_names: list[str]
    label_names: list[str]
    sources: list[str]


@dataclass
class RobustPreprocessor:
    feature_names: list[str]
    keep_indices: np.ndarray
    center: np.ndarray
    scale: np.ndarray
    clip: float

    @property
    def kept_feature_names(self) -> list[str]:
        return [self.feature_names[int(i)] for i in self.keep_indices]

    @classmethod
    def fit(
        cls,
        x: np.ndarray,
        feature_names: list[str],
        *,
        clip: float = 6.0,
        variance_floor: float = 1e-10,
    ) -> "RobustPreprocessor":
        std = np.std(x, axis=0)
        keep = np.flatnonzero(std > variance_floor)
        if keep.size == 0:
            raise ValueError("all input features are constant")

        selected = x[:, keep]
        center = np.median(selected, axis=0)
        q25, q75 = np.percentile(selected, [25.0, 75.0], axis=0)
        scale = q75 - q25

        # IQR can be zero for count-like features even when the feature varies.
        # Fall back to standard deviation, then to one to avoid huge MCU weights.
        fallback = np.std(selected, axis=0)
        scale = np.where(scale > 1e-6, scale, fallback)
        scale = np.where(scale > 1e-6, scale, 1.0)
        return cls(
            feature_names=list(feature_names),
            keep_indices=keep.astype(np.int64),
            center=center.astype(np.float32),
            scale=scale.astype(np.float32),
            clip=float(clip),
        )

    def transform(self, x: np.ndarray) -> np.ndarray:
        selected = np.asarray(x, dtype=np.float32)[:, self.keep_indices]
        result = (selected - self.center) / self.scale
        return np.clip(result, -self.clip, self.clip).astype(np.float32)

    def to_json(self) -> dict[str, object]:
        return {
            "input_feature_names": self.feature_names,
            "kept_feature_names": self.kept_feature_names,
            "keep_indices": self.keep_indices.tolist(),
            "center": self.center.tolist(),
            "scale": self.scale.tolist(),
            "clip": self.clip,
        }


class GestureMLP(nn.Module):
    def __init__(self, input_size: int, hidden: tuple[int, ...], output_size: int, dropout: float = 0.0):
        super().__init__()
        layers: list[nn.Module] = []
        width = input_size
        for units in hidden:
            layers.append(nn.Linear(width, units))
            layers.append(nn.ReLU())
            if dropout > 0.0:
                layers.append(nn.Dropout(dropout))
            width = units
        layers.append(nn.Linear(width, output_size))
        self.layers = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.layers(x)


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def acquisition_metadata(path: Path) -> dict[str, object]:
    metadata_path = path.parent / "metadata.json"
    if not metadata_path.exists():
        return {}
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def discover_training_data(root: Path, expected_samples: int, expected_settle_ms: int) -> list[Path]:
    result: list[Path] = []
    for path in sorted(root.glob("session_*/features.csv")):
        if not path.is_file() or path.stat().st_size == 0:
            continue
        metadata = acquisition_metadata(path)
        if (
            int(metadata.get("samples", -1)) == expected_samples
            and int(metadata.get("settle_ms", -1)) == expected_settle_ms
        ):
            result.append(path)
    return result


def validate_acquisition(
    paths: Iterable[Path],
    expected_samples: int,
    expected_settle_ms: int,
    allow_mismatch: bool,
) -> dict[str, dict[str, object]]:
    configurations: dict[str, dict[str, object]] = {}
    mismatches: list[str] = []
    for path in paths:
        resolved = path.resolve()
        metadata = acquisition_metadata(resolved)
        samples = metadata.get("samples")
        settle_ms = metadata.get("settle_ms")
        configurations[str(resolved)] = {
            "samples": samples,
            "settle_ms": settle_ms,
            "rate_hz": metadata.get("rate_hz"),
        }
        if samples != expected_samples or settle_ms != expected_settle_ms:
            mismatches.append(f"{resolved}: samples={samples}, settle_ms={settle_ms}")

    if mismatches and not allow_mismatch:
        details = "\n  ".join(mismatches)
        raise ValueError(
            f"training data must use samples={expected_samples}, settle_ms={expected_settle_ms}:\n"
            f"  {details}\n"
            "Use --allow-acquisition-mismatch only for explicit cross-condition experiments."
        )
    return configurations


def load_dataset(paths: Iterable[Path]) -> Dataset:
    records: list[tuple[np.ndarray, str, str]] = []
    feature_names: list[str] | None = None
    sources: list[str] = []

    for path in paths:
        path = path.resolve()
        with path.open(newline="") as fp:
            rows = list(csv.DictReader(fp))
        if not rows:
            continue

        current_names = [name for name in rows[0] if name not in META_COLUMNS]
        if feature_names is None:
            feature_names = current_names
        elif current_names != feature_names:
            raise ValueError(f"feature columns differ in {path}")

        source = path.parent.name
        sources.append(str(path))
        for row in rows:
            values = np.asarray([float(row[name]) for name in current_names], dtype=np.float32)
            records.append((values, row["gesture"], f"{source}:rep{row['rep']}"))

    if not records or feature_names is None:
        raise ValueError("no non-empty feature CSV files were loaded")

    label_names = sorted({label for _, label, _ in records})
    label_to_index = {label: index for index, label in enumerate(label_names)}
    x = np.stack([values for values, _, _ in records])
    y = np.asarray([label_to_index[label] for _, label, _ in records], dtype=np.int64)
    groups = np.asarray([group for _, _, group in records])
    if not np.isfinite(x).all():
        raise ValueError("input data contains NaN or infinity")
    return Dataset(x, y, groups, feature_names, label_names, sources)


def make_loader(x: np.ndarray, y: np.ndarray, batch_size: int, shuffle: bool) -> DataLoader:
    dataset = TensorDataset(torch.from_numpy(x), torch.from_numpy(y))
    generator = torch.Generator().manual_seed(42)
    return DataLoader(dataset, batch_size=batch_size, shuffle=shuffle, generator=generator)


def predict_logits(model: nn.Module, x: np.ndarray) -> np.ndarray:
    model.eval()
    with torch.no_grad():
        return model(torch.from_numpy(x)).cpu().numpy()


def train_fold(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_val: np.ndarray,
    y_val: np.ndarray,
    *,
    hidden: tuple[int, ...],
    dropout: float,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    weight_decay: float,
    noise_std: float,
    label_smoothing: float,
    patience: int,
    seed: int,
) -> tuple[GestureMLP, int, float]:
    set_seed(seed)
    model = GestureMLP(x_train.shape[1], hidden, int(max(y_train.max(), y_val.max())) + 1, dropout)
    counts = np.bincount(y_train, minlength=int(max(y_train.max(), y_val.max())) + 1)
    class_weights = counts.sum() / np.maximum(counts, 1) / len(counts)
    loss_fn = nn.CrossEntropyLoss(
        weight=torch.tensor(class_weights, dtype=torch.float32),
        label_smoothing=label_smoothing,
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, epochs), eta_min=learning_rate * 0.03)
    loader = make_loader(x_train, y_train, batch_size, shuffle=True)

    best_state: dict[str, torch.Tensor] | None = None
    best_f1 = -math.inf
    best_epoch = 0
    stale = 0

    for epoch in range(1, epochs + 1):
        model.train()
        for xb, yb in loader:
            if noise_std > 0.0:
                xb = xb + torch.randn_like(xb) * noise_std
            optimizer.zero_grad(set_to_none=True)
            loss = loss_fn(model(xb), yb)
            loss.backward()
            optimizer.step()
        scheduler.step()

        val_pred = np.argmax(predict_logits(model, x_val), axis=1)
        val_f1 = float(f1_score(y_val, val_pred, average="macro", zero_division=0))
        if val_f1 > best_f1 + 1e-5:
            best_f1 = val_f1
            best_epoch = epoch
            best_state = {key: value.detach().clone() for key, value in model.state_dict().items()}
            stale = 0
        else:
            stale += 1
            if stale >= patience:
                break

    if best_state is None:
        raise RuntimeError("training did not produce a model")
    model.load_state_dict(best_state)
    return model, best_epoch, best_f1


def fit_temperature(logits: np.ndarray, labels: np.ndarray) -> float:
    logits_t = torch.tensor(logits, dtype=torch.float32)
    labels_t = torch.tensor(labels, dtype=torch.int64)
    candidates = torch.logspace(math.log10(0.25), math.log10(8.0), 240)
    losses = torch.stack([nn.functional.cross_entropy(logits_t / value, labels_t) for value in candidates])
    return float(candidates[int(torch.argmin(losses))])


def probabilities(logits: np.ndarray, temperature: float) -> np.ndarray:
    scaled = logits / max(temperature, 1e-6)
    scaled -= np.max(scaled, axis=1, keepdims=True)
    exp = np.exp(scaled)
    return exp / np.sum(exp, axis=1, keepdims=True)


def evaluate(logits: np.ndarray, labels: np.ndarray, label_names: list[str], temperature: float) -> dict[str, object]:
    prob = probabilities(logits, temperature)
    pred = np.argmax(prob, axis=1)
    confidence = np.max(prob, axis=1)
    return {
        "accuracy": float(accuracy_score(labels, pred)),
        "f1_macro": float(f1_score(labels, pred, average="macro", zero_division=0)),
        "mean_confidence": float(np.mean(confidence)),
        "median_confidence": float(np.median(confidence)),
        "fraction_confidence_ge_0_6": float(np.mean(confidence >= 0.6)),
        "confusion_matrix": confusion_matrix(labels, pred).tolist(),
        "classification_report": classification_report(
            labels,
            pred,
            labels=list(range(len(label_names))),
            target_names=label_names,
            output_dict=True,
            zero_division=0,
        ),
    }


def cross_validate(
    dataset: Dataset,
    hidden: tuple[int, ...],
    args: argparse.Namespace,
) -> tuple[dict[str, object], np.ndarray, np.ndarray, list[int]]:
    splitter = StratifiedGroupKFold(n_splits=args.folds, shuffle=True, random_state=args.seed)
    oof_logits = np.zeros((len(dataset.y), len(dataset.label_names)), dtype=np.float32)
    fold_rows: list[dict[str, object]] = []
    best_epochs: list[int] = []

    for fold, (train_idx, val_idx) in enumerate(splitter.split(dataset.x, dataset.y, dataset.groups), 1):
        prep = RobustPreprocessor.fit(dataset.x[train_idx], dataset.feature_names, clip=args.clip)
        x_train = prep.transform(dataset.x[train_idx])
        x_val = prep.transform(dataset.x[val_idx])
        model, best_epoch, best_f1 = train_fold(
            x_train,
            dataset.y[train_idx],
            x_val,
            dataset.y[val_idx],
            hidden=hidden,
            dropout=args.dropout,
            epochs=args.epochs,
            batch_size=args.batch_size,
            learning_rate=args.learning_rate,
            weight_decay=args.weight_decay,
            noise_std=args.noise_std,
            label_smoothing=args.label_smoothing,
            patience=args.patience,
            seed=args.seed + fold,
        )
        logits = predict_logits(model, x_val)
        oof_logits[val_idx] = logits
        best_epochs.append(best_epoch)
        fold_rows.append({
            "fold": fold,
            "samples": int(len(val_idx)),
            "groups": sorted(set(dataset.groups[val_idx].tolist())),
            "best_epoch": best_epoch,
            "f1_macro": best_f1,
        })
        print(f"  fold {fold}: F1={best_f1:.4f}, epoch={best_epoch}, samples={len(val_idx)}")

    temperature = fit_temperature(oof_logits, dataset.y)
    metrics = evaluate(oof_logits, dataset.y, dataset.label_names, temperature)
    metrics.update({"folds": fold_rows, "temperature": temperature})
    return metrics, oof_logits, dataset.y, best_epochs


def train_final(dataset: Dataset, hidden: tuple[int, ...], epochs: int, args: argparse.Namespace) -> tuple[GestureMLP, RobustPreprocessor]:
    prep = RobustPreprocessor.fit(dataset.x, dataset.feature_names, clip=args.clip)
    x = prep.transform(dataset.x)
    set_seed(args.seed)
    model = GestureMLP(x.shape[1], hidden, len(dataset.label_names), 0.0)
    counts = np.bincount(dataset.y, minlength=len(dataset.label_names))
    weights = counts.sum() / np.maximum(counts, 1) / len(counts)
    loss_fn = nn.CrossEntropyLoss(
        weight=torch.tensor(weights, dtype=torch.float32),
        label_smoothing=args.label_smoothing,
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, epochs), eta_min=args.learning_rate * 0.03)
    loader = make_loader(x, dataset.y, args.batch_size, shuffle=True)
    for _ in range(epochs):
        model.train()
        for xb, yb in loader:
            if args.noise_std > 0.0:
                xb = xb + torch.randn_like(xb) * args.noise_std
            optimizer.zero_grad(set_to_none=True)
            loss = loss_fn(model(xb), yb)
            loss.backward()
            optimizer.step()
        scheduler.step()
    model.eval()
    return model, prep


def parameter_count(input_size: int, hidden: tuple[int, ...], output_size: int) -> int:
    widths = (input_size, *hidden, output_size)
    return sum(widths[i] * widths[i + 1] + widths[i + 1] for i in range(len(widths) - 1))


def export_model(
    model: GestureMLP,
    prep: RobustPreprocessor,
    dataset: Dataset,
    hidden: tuple[int, ...],
    temperature: float,
    report: dict[str, object],
    out_dir: Path,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    torch.save({
        "state_dict": model.state_dict(),
        "input_size": len(prep.keep_indices),
        "hidden": hidden,
        "label_names": dataset.label_names,
        "temperature": temperature,
    }, out_dir / "gesture_mlp_state.pt")

    example = torch.zeros(1, len(prep.keep_indices), dtype=torch.float32)
    traced = torch.jit.trace(model, example)
    traced.save(str(out_dir / "gesture_mlp.ts"))

    torch.onnx.export(
        model,
        (example,),
        str(out_dir / "gesture_mlp.onnx"),
        input_names=["features"],
        output_names=["logits"],
        opset_version=12,
        dynamo=False,
    )

    with torch.no_grad():
        expected = model(example)
        actual = torch.jit.load(str(out_dir / "gesture_mlp.ts"))(example)
    if not torch.allclose(expected, actual, rtol=1e-6, atol=1e-7):
        raise RuntimeError("TorchScript output differs from the PyTorch model")

    (out_dir / "preprocess.json").write_text(
        json.dumps(prep.to_json(), indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "labels.json").write_text(
        json.dumps(dataset.label_names, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "evaluation.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def parse_hidden(value: str) -> tuple[int, ...]:
    result = tuple(int(part) for part in value.split(",") if part.strip())
    if not result or any(width <= 0 for width in result):
        raise argparse.ArgumentTypeError("hidden widths must be positive comma-separated integers")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", action="append", type=Path, help="Training features.csv; may be repeated")
    parser.add_argument("--test-data", type=Path, help="Optional independent evaluation features.csv")
    parser.add_argument("--out-dir", type=Path, default=Path("host/gestures/torch_mlp_model"))
    parser.add_argument("--expected-samples", type=int, default=256)
    parser.add_argument("--expected-settle-ms", type=int, default=20)
    parser.add_argument("--allow-acquisition-mismatch", action="store_true")
    parser.add_argument("--hidden", action="append", type=parse_hidden, help="Candidate widths, e.g. 48,24")
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--epochs", type=int, default=350)
    parser.add_argument("--patience", type=int, default=55)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--learning-rate", type=float, default=1.5e-3)
    parser.add_argument("--weight-decay", type=float, default=1e-3)
    parser.add_argument("--noise-std", type=float, default=0.04)
    parser.add_argument("--dropout", type=float, default=0.10)
    parser.add_argument("--label-smoothing", type=float, default=0.04)
    parser.add_argument("--clip", type=float, default=6.0)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    data_paths = args.data or discover_training_data(
        DEFAULT_GESTURE_ROOT,
        args.expected_samples,
        args.expected_settle_ms,
    )
    if not data_paths:
        raise ValueError(
            f"no non-empty session data matched samples={args.expected_samples}, "
            f"settle_ms={args.expected_settle_ms}; collect a matching dataset or pass --data"
        )
    acquisition = validate_acquisition(
        data_paths,
        args.expected_samples,
        args.expected_settle_ms,
        args.allow_acquisition_mismatch,
    )
    dataset = load_dataset(data_paths)
    candidates = args.hidden or [(32, 16), (48, 24), (64, 32)]
    if len(set(dataset.groups.tolist())) < args.folds:
        raise ValueError("number of repetition groups must be at least --folds")

    print(f"samples={len(dataset.y)}, features={len(dataset.feature_names)}, groups={len(set(dataset.groups))}")
    print(f"labels={dataset.label_names}")
    print(f"sources={dataset.sources}")

    results: list[tuple[float, tuple[int, ...], dict[str, object], list[int]]] = []
    selected_input_size = int(np.count_nonzero(np.std(dataset.x, axis=0) > 1e-10))
    for hidden in candidates:
        print(f"\narchitecture={hidden}")
        metrics, _, _, best_epochs = cross_validate(dataset, hidden, args)
        params = parameter_count(selected_input_size, hidden, len(dataset.label_names))
        print(
            f"  grouped OOF: accuracy={metrics['accuracy']:.4f}, "
            f"F1={metrics['f1_macro']:.4f}, temperature={metrics['temperature']:.3f}"
        )
        results.append((float(metrics["f1_macro"]), hidden, metrics, best_epochs))

    results.sort(key=lambda item: (item[0], -sum(item[1])), reverse=True)
    _, best_hidden, cv_metrics, best_epochs = results[0]
    final_epochs = max(10, int(round(float(np.median(best_epochs)))))
    print(f"\nselected={best_hidden}, final_epochs={final_epochs}")
    model, prep = train_final(dataset, best_hidden, final_epochs, args)

    report: dict[str, object] = {
        "model": {
            "framework": "pytorch",
            "architecture": [len(prep.keep_indices), *best_hidden, len(dataset.label_names)],
            "operators": ["Linear", "ReLU"],
            "parameter_count": parameter_count(len(prep.keep_indices), best_hidden, len(dataset.label_names)),
            "temperature": cv_metrics["temperature"],
        },
        "training": {
            "sources": dataset.sources,
            "expected_acquisition": {
                "samples": args.expected_samples,
                "settle_ms": args.expected_settle_ms,
            },
            "source_acquisition": acquisition,
            "samples": int(len(dataset.y)),
            "groups": int(len(set(dataset.groups.tolist()))),
            "final_epochs": final_epochs,
            "dropped_features": [
                name for index, name in enumerate(dataset.feature_names) if index not in set(prep.keep_indices.tolist())
            ],
        },
        "grouped_cross_validation": cv_metrics,
        "candidate_models": [
            {"hidden": list(hidden), "f1_macro": score, "metrics": metrics}
            for score, hidden, metrics, _ in results
        ],
    }

    if args.test_data:
        test = load_dataset([args.test_data])
        if test.feature_names != dataset.feature_names or test.label_names != dataset.label_names:
            raise ValueError("test data feature or label order differs from training data")
        logits = predict_logits(model, prep.transform(test.x))
        report["independent_test"] = evaluate(
            logits,
            test.y,
            dataset.label_names,
            float(cv_metrics["temperature"]),
        )
        print(
            f"independent test: accuracy={report['independent_test']['accuracy']:.4f}, "
            f"F1={report['independent_test']['f1_macro']:.4f}"
        )

    export_model(
        model,
        prep,
        dataset,
        best_hidden,
        float(cv_metrics["temperature"]),
        report,
        args.out_dir,
    )
    print(f"exported={args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
