#!/usr/bin/env python3
"""Train a lightweight MLP for gesture classification and export to TFLite/ONNX.

Loads features.csv from the gesture collection sessions, trains a small Keras MLP
(49 → 32 → 16 → 3), evaluates against the Random Forest baseline (93.3%),
and exports for RUHMI compilation.
"""

import csv
import sys
from pathlib import Path

import numpy as np
import tensorflow as tf

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
def load_all_features(csv_paths: list[Path]) -> tuple[np.ndarray, np.ndarray, list[str], list[str]]:
    """Load features from multiple CSV files and return (X, y_encoded, label_names)."""
    rows = []
    for path in csv_paths:
        with open(path, newline="") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                rows.append(row)

    meta_cols = {"frame_id", "gesture", "rep"}
    feature_names = [c for c in rows[0].keys() if c not in meta_cols]

    X = np.array([[float(r[f]) for f in feature_names] for r in rows], dtype=np.float32)
    y_str = [r["gesture"] for r in rows]

    labels = sorted(set(y_str))
    label_to_idx = {l: i for i, l in enumerate(labels)}
    y = np.array([label_to_idx[l] for l in y_str], dtype=np.int32)

    print(f"Loaded {len(rows)} samples, {len(feature_names)} features, {len(labels)} classes: {labels}")
    return X, y, feature_names, labels


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
def build_mlp(n_features: int, n_classes: int) -> tf.keras.Model:
    """Small MLP: input → 32 → 16 → output."""
    model = tf.keras.Sequential([
        tf.keras.layers.Input(shape=(n_features,), name="features"),
        tf.keras.layers.Dense(32, activation="relu", name="dense1"),
        tf.keras.layers.Dense(16, activation="relu", name="dense2"),
        tf.keras.layers.Dense(n_classes, name="logits"),
    ])
    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=0.001),
        loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
        metrics=["accuracy"],
    )
    return model


# ---------------------------------------------------------------------------
# Evaluate
# ---------------------------------------------------------------------------
def evaluate(model: tf.keras.Model, X_test: np.ndarray, y_test: np.ndarray, labels: list[str]) -> dict:
    """Compute accuracy and per-class F1."""
    from sklearn.metrics import classification_report, f1_score

    logits = model.predict(X_test, verbose=0)
    y_pred = np.argmax(logits, axis=1)

    acc = float(np.mean(y_pred == y_test))
    f1_macro = float(f1_score(y_test, y_pred, average="macro"))

    print(f"\nAccuracy: {acc:.4f}  F1-macro: {f1_macro:.4f}")
    print(classification_report(y_test, y_pred, target_names=labels))

    return {"accuracy": acc, "f1_macro": f1_macro}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export_tflite(model: tf.keras.Model, out_path: Path) -> None:
    """Export FP32 TFLite model."""
    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    tflite_model = converter.convert()
    out_path.write_bytes(tflite_model)
    print(f"TFLite saved: {out_path} ({len(tflite_model)} bytes)")


def export_savedmodel(model: tf.keras.Model, out_dir: Path) -> None:
    """Export SavedModel for ONNX conversion."""
    tf.saved_model.save(model, str(out_dir))
    print(f"SavedModel saved: {out_dir}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    # Find feature CSVs
    session_dirs = sorted(Path("gestures").glob("session_*"))
    if not session_dirs:
        # try host/gestures/
        session_dirs = sorted(Path("host/gestures").glob("session_*"))
    if not session_dirs:
        print("ERROR: No session directories found under gestures/ or host/gestures/", file=sys.stderr)
        return 1

    csv_paths = [d / "features.csv" for d in session_dirs if (d / "features.csv").exists()]
    if not csv_paths:
        print("ERROR: No features.csv found", file=sys.stderr)
        return 1

    print(f"Found {len(csv_paths)} feature CSV(s):")
    for p in csv_paths:
        print(f"  {p}")

    X, y, feature_names, labels = load_all_features(csv_paths)  # type: ignore[assignment]
    n_features = len(feature_names)
    n_classes = len(labels)

    # Train/test split
    from sklearn.model_selection import train_test_split
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=42
    )
    print(f"Train: {len(X_train)}, Test: {len(X_test)}")

    # Standardize
    mean = X_train.mean(axis=0)
    std = X_train.std(axis=0) + 1e-8
    X_train_s = (X_train - mean) / std
    X_test_s = (X_test - mean) / std

    # Build & train
    model = build_mlp(n_features, n_classes)
    model.summary()

    model.fit(
        X_train_s, y_train,
        validation_data=(X_test_s, y_test),
        epochs=100,
        batch_size=16,
        verbose=0,
        callbacks=[
            tf.keras.callbacks.EarlyStopping(patience=15, restore_best_weights=True, verbose=1),
        ],
    )

    # Evaluate
    metrics = evaluate(model, X_test_s, y_test, labels)

    # Export
    out_dir = Path("gestures/ruhmi_model")
    out_dir.mkdir(parents=True, exist_ok=True)

    export_tflite(model, out_dir / "gesture_classifier.tflite")
    export_savedmodel(model, out_dir / "gesture_savedmodel")

    # Save normalization params
    np.savez(
        out_dir / "norm_params.npz",
        mean=mean, std=std,
        feature_names=np.array(feature_names),
        labels=np.array(labels),
    )

    print(f"\nModel ready for RUHMI in: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
