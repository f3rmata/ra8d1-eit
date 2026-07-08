#!/usr/bin/env python3
"""Improved MLP training for gesture classification.

Improvements over v1:
  - Architecture search (multiple widths/depths)
  - L2 weight regularization
  - Gaussian noise augmentation during training
  - Cosine learning rate decay with warmup
  - Leave-one-session-out cross-validation
  - Model selection by validation F1-macro
"""

import csv
import sys
from pathlib import Path

import numpy as np
import tensorflow as tf

# Reproducibility
tf.random.set_seed(42)
np.random.seed(42)

# ---------------------------------------------------------------------------
# Load data — returns separate sessions for cross-validation
# ---------------------------------------------------------------------------
def load_sessions(csv_paths: list[Path]) -> list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[str], list[str]]]:
    """Load each session as (X_train, X_test, y_train, y_test, feature_names, label_names)."""
    results = []
    all_labels = set()

    for path in csv_paths:
        with open(path, newline="") as fp:
            rows = list(csv.DictReader(fp))

        meta_cols = {"frame_id", "gesture", "rep"}
        feature_names = [c for c in rows[0].keys() if c not in meta_cols]
        X = np.array([[float(r[f]) for f in feature_names] for r in rows], dtype=np.float32)
        y_str = [r["gesture"] for r in rows]
        all_labels.update(y_str)
        results.append((X, y_str, feature_names))

    # Ensure consistent label ordering across sessions
    label_names = sorted(all_labels)
    label_to_idx = {l: i for i, l in enumerate(label_names)}

    final = []
    for X, y_str, fnames in results:
        y = np.array([label_to_idx[l] for l in y_str], dtype=np.int32)
        final.append((X, y, fnames, label_names))

    return final


# ---------------------------------------------------------------------------
# Data augmentation
# ---------------------------------------------------------------------------
class GaussianNoiseLayer(tf.keras.layers.Layer):
    """Add Gaussian noise during training only."""
    def __init__(self, stddev: float = 0.05, **kwargs):
        super().__init__(**kwargs)
        self.stddev = stddev

    def call(self, x, training=None):
        if training:
            noise = tf.random.normal(tf.shape(x), mean=0.0, stddev=self.stddev)
            return x + noise
        return x

    def get_config(self):
        config = super().get_config()
        config["stddev"] = self.stddev
        return config


# ---------------------------------------------------------------------------
# Model builder
# ---------------------------------------------------------------------------
def build_mlp(
    n_features: int,
    n_classes: int,
    hidden: list[int],
    l2: float = 1e-4,
    noise_std: float = 0.03,
    dropout: float = 0.0,
) -> tf.keras.Model:
    """Build MLP with noise augmentation, L2 regularization, optional dropout."""
    layers = [tf.keras.layers.Input(shape=(n_features,), name="features")]

    # Input noise layer (only during training)
    if noise_std > 0:
        layers.append(GaussianNoiseLayer(stddev=noise_std, name="input_noise"))

    for i, units in enumerate(hidden):
        layers.append(tf.keras.layers.Dense(
            units,
            activation="relu",
            kernel_regularizer=tf.keras.regularizers.l2(l2) if l2 > 0 else None,
            name=f"dense{i+1}",
        ))
        if dropout > 0:
            layers.append(tf.keras.layers.Dropout(dropout, name=f"dropout{i+1}"))

    layers.append(tf.keras.layers.Dense(n_classes, name="logits"))

    model = tf.keras.Sequential(layers)
    return model


# ---------------------------------------------------------------------------
# Cosine decay schedule with warmup
# ---------------------------------------------------------------------------
class WarmupCosineDecay(tf.keras.optimizers.schedules.LearningRateSchedule):
    def __init__(self, peak_lr: float, warmup_steps: int, total_steps: int, min_lr: float = 1e-6):
        super().__init__()
        self.peak_lr = peak_lr
        self.warmup_steps = warmup_steps
        self.total_steps = total_steps
        self.min_lr = min_lr

    def __call__(self, step):
        step = tf.cast(step, tf.float32)
        warmup = tf.cast(self.warmup_steps, tf.float32)
        total = tf.cast(self.total_steps, tf.float32)

        # Linear warmup
        warmup_lr = self.peak_lr * (step / warmup)

        # Cosine decay
        progress = (step - warmup) / (total - warmup)
        progress = tf.clip_by_value(progress, 0.0, 1.0)
        cosine_lr = self.min_lr + 0.5 * (self.peak_lr - self.min_lr) * (1.0 + tf.cos(np.pi * progress))

        return tf.where(step < warmup, warmup_lr, cosine_lr)

    def get_config(self):
        return {
            "peak_lr": self.peak_lr, "warmup_steps": self.warmup_steps,
            "total_steps": self.total_steps, "min_lr": self.min_lr,
        }


# ---------------------------------------------------------------------------
# Train & evaluate one model config
# ---------------------------------------------------------------------------
def train_and_eval(
    X_train: np.ndarray, y_train: np.ndarray,
    X_val: np.ndarray, y_val: np.ndarray,
    n_features: int, n_classes: int,
    hidden: list[int],
    l2: float, noise_std: float, dropout: float,
    batch_size: int, epochs: int, peak_lr: float,
) -> tuple[tf.keras.Model, float, dict]:
    """Train one model and return (model, val_f1, history)."""

    # Standardize per fold
    mean = X_train.mean(axis=0)
    std = X_train.std(axis=0) + 1e-8
    X_train_s = (X_train - mean) / std
    X_val_s = (X_val - mean) / std

    model = build_mlp(n_features, n_classes, hidden, l2, noise_std, dropout)

    steps_per_epoch = max(1, len(X_train) // batch_size)
    total_steps = steps_per_epoch * epochs
    warmup_steps = steps_per_epoch * 3

    lr_schedule = WarmupCosineDecay(peak_lr, warmup_steps, total_steps)

    model.compile(
        optimizer=tf.keras.optimizers.AdamW(
            learning_rate=lr_schedule,
            weight_decay=1e-5,
        ),
        loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
        metrics=["accuracy"],
    )

    history = model.fit(
        X_train_s, y_train,
        validation_data=(X_val_s, y_val),
        epochs=epochs,
        batch_size=batch_size,
        verbose=0,
        callbacks=[
            tf.keras.callbacks.EarlyStopping(
                monitor="val_accuracy", patience=30,
                restore_best_weights=True, verbose=0,
            ),
        ],
    )

    # Compute validation F1
    logits = model.predict(X_val_s, verbose=0)
    y_pred = np.argmax(logits, axis=1)

    from sklearn.metrics import f1_score
    f1 = float(f1_score(y_val, y_pred, average="macro"))

    return model, f1, {"mean": mean, "std": std, "best_epoch": len(history.history["loss"])}


# ---------------------------------------------------------------------------
# Leave-one-session-out CV
# ---------------------------------------------------------------------------
def loso_cv(
    sessions: list,
    hidden: list[int],
    l2: float, noise_std: float, dropout: float,
    batch_size: int, epochs: int, peak_lr: float,
) -> float:
    """Leave-one-session-out cross-validation, returns mean F1."""
    f1s = []
    for i in range(len(sessions)):
        X_test, y_test, _, _ = sessions[i]

        # Train on all other sessions
        X_parts, y_parts = [], []
        for j in range(len(sessions)):
            if j != i:
                X_parts.append(sessions[j][0])
                y_parts.append(sessions[j][1])
        X_train = np.concatenate(X_parts)
        y_train = np.concatenate(y_parts)

        _, f1, _ = train_and_eval(
            X_train, y_train, X_test, y_test,
            n_features=X_train.shape[1],
            n_classes=len(sessions[0][3]),
            hidden=hidden, l2=l2, noise_std=noise_std, dropout=dropout,
            batch_size=batch_size, epochs=epochs, peak_lr=peak_lr,
        )
        f1s.append(f1)
        print(f"    Fold {i+1}/{len(sessions)}: val F1={f1:.4f}")

    return float(np.mean(f1s))


# ---------------------------------------------------------------------------
# Export with standardization layer baked in
# ---------------------------------------------------------------------------
def export_final_model(
    model: tf.keras.Model,
    mean: np.ndarray, std: np.ndarray,
    label_names: list[str],
    out_dir: Path,
) -> None:
    """Export TFLite model with standardization as part of the graph."""
    # Build a wrapper model that includes standardization
    feat_input = tf.keras.layers.Input(shape=(len(mean),), name="raw_features")
    normalized = (feat_input - tf.constant(mean, dtype=tf.float32)) / tf.constant(std + 1e-8, dtype=tf.float32)
    logits = model(normalized, training=False)
    wrapper = tf.keras.Model(inputs=feat_input, outputs=logits, name="gesture_classifier_wrapper")

    out_dir.mkdir(parents=True, exist_ok=True)

    # TFLite
    converter = tf.lite.TFLiteConverter.from_keras_model(wrapper)
    tflite_model = converter.convert()
    tflite_path = out_dir / "gesture_classifier.tflite"
    tflite_path.write_bytes(tflite_model)
    print(f"TFLite: {tflite_path} ({len(tflite_model)} bytes)")

    # Norm params (backup)
    np.savez(out_dir / "norm_params.npz", mean=mean, std=std,
             labels=np.array(label_names))

    print(f"Labels: {label_names}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def stratified_kfold_cv(
    X: np.ndarray, y: np.ndarray,
    hidden: list[int], l2: float, noise_std: float, dropout: float,
    batch_size: int, epochs: int, peak_lr: float,
    n_splits: int = 5,
) -> float:
    """Stratified k-fold CV, returns mean F1."""
    from sklearn.model_selection import StratifiedKFold
    skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
    f1s = []
    for fold, (train_idx, val_idx) in enumerate(skf.split(X, y)):
        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]
        _, f1, _ = train_and_eval(
            X_train, y_train, X_val, y_val,
            n_features=X.shape[1], n_classes=len(np.unique(y)),
            hidden=hidden, l2=l2, noise_std=noise_std, dropout=dropout,
            batch_size=batch_size, epochs=epochs, peak_lr=peak_lr,
        )
        f1s.append(f1)
        print(f"    Fold {fold+1}/{n_splits}: val F1={f1:.4f}")
    return float(np.mean(f1s))


def main() -> int:
    # Only use session 220027
    csv_path = Path("gestures/session_20260707_220027/features.csv")
    if not csv_path.exists():
        csv_path = Path("host/gestures/session_20260707_220027/features.csv")
    if not csv_path.exists():
        print("ERROR: session_20260707_220027/features.csv not found", file=sys.stderr)
        return 1

    sessions = load_sessions([csv_path])
    X_all, y_all, feature_names_list, label_names = sessions[0]
    n_features = X_all.shape[1]
    n_classes = len(label_names)

    print(f"Data: {csv_path}")
    print(f"Samples: {len(X_all)}, Features: {n_features}, Classes: {label_names}")
    from collections import Counter
    print(f"Distribution: {dict(Counter(y_all))}")
    print()

    # ---- Architecture search ----
    configs = [
        # (hidden, l2, noise, dropout, label)
        ([64, 32], 1e-4, 0.05, 0.0, "64→32 (wider)"),
        ([64, 32], 1e-4, 0.05, 0.15, "64→32 +drop15%"),
        ([64, 32, 16], 1e-5, 0.03, 0.0, "64→32→16 (deeper)"),
        ([48, 24], 1e-4, 0.05, 0.0, "48→24"),
        ([80, 40], 1e-4, 0.05, 0.1, "80→40 +drop10%"),
        ([32, 16], 1e-3, 0.08, 0.0, "32→16 heavy l2+noise"),
    ]

    print("=" * 70)
    print("Architecture Search (Stratified 5-Fold CV)")
    print("=" * 70)

    results = []
    for hidden, l2, noise, dropout, label in configs:
        params = sum(h * n_features if i == 0 else h * hidden[i-1] for i, h in enumerate(hidden))
        params += sum(hidden) + n_classes  # biases
        params += hidden[-1] * n_classes  # output weights

        print(f"\n  {label}  (params={params})  l2={l2} noise={noise:.2f}")
        f1 = stratified_kfold_cv(
            X_all, y_all, hidden=hidden,
            l2=l2, noise_std=noise, dropout=dropout,
            batch_size=16, epochs=200, peak_lr=0.002,
        )
        results.append((f1, hidden, l2, noise, dropout, label, params))
        print(f"  → Mean CV F1: {f1:.4f}")

    # Sort by F1
    results.sort(key=lambda r: r[0], reverse=True)
    print(f"\n{'='*70}")
    print("Results (best first):")
    print(f"{'Rank':<5} {'F1':>7} {'Params':>7}  Config")
    print("-" * 70)
    for rank, (f1, hidden, l2, noise, dropout, label, params) in enumerate(results, 1):
        marker = " ← BEST" if rank == 1 else ""
        print(f"{rank:<5} {f1:.4f}  {params:>5}   {label}{marker}")

    # ---- Train final model with best config ----
    best_f1, best_hidden, best_l2, best_noise, best_dropout, best_label, _ = results[0]

    print(f"\n{'='*70}")
    print(f"Training final model: {best_label}")
    print(f"{'='*70}")

    # Train on all data with held-out test
    from sklearn.model_selection import train_test_split
    X_train, X_test, y_train, y_test = train_test_split(
        X_all, y_all, test_size=0.15, stratify=y_all, random_state=42,
    )

    model, f1, norm = train_and_eval(
        X_train, y_train, X_test, y_test,
        n_features=n_features, n_classes=n_classes,
        hidden=best_hidden,
        l2=best_l2, noise_std=best_noise, dropout=best_dropout,
        batch_size=8, epochs=300, peak_lr=0.002,
    )

    # Full evaluation
    from sklearn.metrics import classification_report, confusion_matrix
    mean_s, std_s = norm["mean"], norm["std"]
    X_test_s = (X_test - mean_s) / (std_s + 1e-8)
    logits = model.predict(X_test_s, verbose=0)
    y_pred = np.argmax(logits, axis=1)

    acc = float(np.mean(y_pred == y_test))
    f1_macro = float(f1_score_import(y_test, y_pred))
    print(f"\nFinal Test Results:")
    print(f"  Accuracy: {acc:.4f}")
    print(f"  F1-macro: {f1_macro:.4f}")
    print()
    print(classification_report(y_test, y_pred, target_names=label_names))
    print("Confusion Matrix:")
    print(confusion_matrix(y_test, y_pred))

    # Export
    out_dir = Path("gestures/ruhmi_model")
    export_final_model(model, mean_s, std_s, label_names, out_dir)

    return 0


def f1_score_import(y_true, y_pred):
    from sklearn.metrics import f1_score
    return float(f1_score(y_true, y_pred, average="macro"))


if __name__ == "__main__":
    raise SystemExit(main())
