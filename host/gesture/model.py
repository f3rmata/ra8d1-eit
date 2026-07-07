#!/usr/bin/env python3
"""Gesture classification model: training, inference, persistence.

Uses Random Forest as the primary classifier with scikit-learn.
Trained model is serialized via joblib.

Usage:
    # Train from collected data
    python -m host.gesture.model --data gestures/session_001/features.csv \
        --out gestures/model.joblib

    # Evaluate a trained model
    python -m host.gesture.model --eval gestures/model.joblib \
        --data gestures/session_002/features.csv
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
)
from sklearn.model_selection import (
    GridSearchCV,
    StratifiedKFold,
    train_test_split,
)
from sklearn.preprocessing import LabelEncoder, StandardScaler

# ---------------------------------------------------------------------------
# Default model path
# ---------------------------------------------------------------------------
DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[2] / "gestures" / "model.joblib"


# ---------------------------------------------------------------------------
# GestureClassifier
# ---------------------------------------------------------------------------

class GestureClassifier:
    """EIT gesture classifier with Random Forest + StandardScaler + LabelEncoder.

    Persists the full pipeline to disk via joblib.
    """

    def __init__(
        self,
        n_estimators: int = 200,
        max_depth: int = 12,
        random_state: int = 42,
        confidence_threshold: float = 0.6,
    ) -> None:
        self.scaler = StandardScaler()
        self.label_encoder = LabelEncoder()
        self.model = RandomForestClassifier(
            n_estimators=n_estimators,
            max_depth=max_depth,
            class_weight="balanced",
            random_state=random_state,
            n_jobs=-1,
        )
        self.confidence_threshold = confidence_threshold
        self.feature_names: list[str] = []
        self._fitted = False

    # -- Training ----------------------------------------------------------

    def fit(
        self,
        X: np.ndarray,
        y: list[str] | np.ndarray,
        feature_names: list[str] | None = None,
    ) -> GestureClassifier:
        """Fit the classifier on feature matrix X and string labels y.

        Args:
            X: (N, D) feature matrix.
            y: (N,) string gesture labels.
            feature_names: Ordered feature names for introspection.
        """
        if len(X) < 10:
            raise ValueError(f"Need at least 10 training samples, got {len(X)}")

        y_encoded = self.label_encoder.fit_transform(y)
        X_scaled = self.scaler.fit_transform(X)
        self.model.fit(X_scaled, y_encoded)
        self.feature_names = list(feature_names) if feature_names is not None else []
        self._fitted = True
        return self

    def grid_search(
        self,
        X: np.ndarray,
        y: list[str] | np.ndarray,
        cv: int = 5,
        verbose: int = 1,
    ) -> dict[str, Any]:
        """Run GridSearchCV to tune hyperparameters, then refit with best params.

        Returns the cv_results_ dict.
        """
        y_encoded = self.label_encoder.fit_transform(y)
        X_scaled = self.scaler.fit_transform(X)

        param_grid = {
            "n_estimators": [100, 200, 300],
            "max_depth": [8, 12, 16, None],
            "min_samples_split": [2, 5],
        }

        search = GridSearchCV(
            self.model,
            param_grid,
            cv=StratifiedKFold(n_splits=cv, shuffle=True, random_state=42),
            scoring="f1_macro",
            n_jobs=-1,
            verbose=verbose,
        )
        search.fit(X_scaled, y_encoded)

        # Replace with best estimator
        self.model = search.best_estimator_
        self._fitted = True

        print(f"\nBest params: {search.best_params_}")
        print(f"Best CV F1-macro: {search.best_score_:.4f}")
        return search.cv_results_

    # -- Inference ---------------------------------------------------------

    def predict(self, features: np.ndarray) -> tuple[str, float, dict[str, float]]:
        """Classify a single feature vector.

        Args:
            features: (D,) or (1, D) feature array.

        Returns:
            (label, confidence, {label: probability}) tuple.
            If confidence < threshold, label is "unknown".
        """
        if not self._fitted:
            raise RuntimeError("Classifier not fitted. Call fit() or load() first.")

        X = np.atleast_2d(features)
        X_scaled = self.scaler.transform(X)
        probas = self.model.predict_proba(X_scaled)[0]
        idx = int(np.argmax(probas))
        confidence = float(probas[idx])

        label = self.label_encoder.inverse_transform([idx])[0]
        if confidence < self.confidence_threshold:
            label = "unknown"

        all_probas = {
            str(self.label_encoder.inverse_transform([i])[0]): float(p)
            for i, p in enumerate(probas)
        }
        return label, confidence, all_probas

    def predict_batch(
        self, X: np.ndarray
    ) -> list[tuple[str, float, dict[str, float]]]:
        """Batch classification."""
        return [self.predict(row) for row in X]

    # -- Evaluation --------------------------------------------------------

    def evaluate(
        self, X: np.ndarray, y_true: list[str] | np.ndarray
    ) -> dict[str, Any]:
        """Compute classification metrics on test data.

        Returns dict with keys: accuracy, f1_macro, report, confusion_matrix,
        per_class_f1, feature_importances.
        """
        if not self._fitted:
            raise RuntimeError("Classifier not fitted.")

        y_enc = self.label_encoder.transform(y_true)
        X_scaled = self.scaler.transform(X)
        y_pred_enc = self.model.predict(X_scaled)

        accuracy = float(accuracy_score(y_enc, y_pred_enc))
        f1_macro = float(f1_score(y_enc, y_pred_enc, average="macro"))
        report = classification_report(
            y_enc, y_pred_enc,
            target_names=[str(l) for l in self.label_encoder.classes_],
        )
        cm = confusion_matrix(y_enc, y_pred_enc)

        per_class_f1 = dict(zip(
            [str(l) for l in self.label_encoder.classes_],
            f1_score(y_enc, y_pred_enc, average=None),
        ))

        importances: dict[str, float] = {}
        if self.feature_names and hasattr(self.model, "feature_importances_"):
            importances = dict(
                sorted(
                    zip(self.feature_names, self.model.feature_importances_),
                    key=lambda kv: kv[1],
                    reverse=True,
                )[:20]
            )

        return {
            "accuracy": accuracy,
            "f1_macro": f1_macro,
            "report": report,
            "confusion_matrix": cm.tolist(),
            "class_labels": [str(l) for l in self.label_encoder.classes_],
            "per_class_f1": per_class_f1,
            "feature_importances_top20": importances,
        }

    # -- Persistence -------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """Save the full pipeline to disk."""
        data = {
            "scaler": self.scaler,
            "label_encoder": self.label_encoder,
            "model": self.model,
            "feature_names": self.feature_names,
            "confidence_threshold": self.confidence_threshold,
            "_fitted": self._fitted,
        }
        joblib.dump(data, str(path))
        print(f"Model saved to {path}")

    @classmethod
    def load(cls, path: str | Path) -> "GestureClassifier":
        """Load a saved pipeline from disk."""
        data = joblib.load(str(path))
        obj = cls(confidence_threshold=data.get("confidence_threshold", 0.6))
        obj.scaler = data["scaler"]
        obj.label_encoder = data["label_encoder"]
        obj.model = data["model"]
        obj.feature_names = data.get("feature_names", [])
        obj._fitted = data.get("_fitted", True)
        print(f"Model loaded from {path}")
        print(f"  Gestures: {list(obj.label_encoder.classes_)}")
        print(f"  Features: {len(obj.feature_names)}")
        return obj


# ---------------------------------------------------------------------------
# Utility: load feature CSV
# ---------------------------------------------------------------------------

def load_features_csv(path: str | Path) -> tuple[np.ndarray, list[str], list[str]]:
    """Load a features.csv produced by collect.py.

    Returns (X, y_labels, feature_names).
    """
    import csv

    rows = []
    with open(path, newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            rows.append(row)

    if not rows:
        raise ValueError(f"No data in {path}")

    # Identify feature columns (exclude frame_id, gesture, rep)
    meta_cols = {"frame_id", "gesture", "rep"}
    feature_names = [c for c in rows[0].keys() if c not in meta_cols]

    X = np.array([[float(row[f]) for f in feature_names] for row in rows])
    y = [row["gesture"] for row in rows]

    return X, y, feature_names


# ---------------------------------------------------------------------------
# Training pipeline
# ---------------------------------------------------------------------------

def train_from_csv(
    csv_path: str | Path,
    output_path: str | Path | None = None,
    grid_search: bool = True,
    cv_folds: int = 5,
    test_split: float = 0.2,
) -> tuple[GestureClassifier, dict[str, Any]]:
    """End-to-end training from a features CSV.

    Args:
        csv_path: Path to features.csv from collect.py.
        output_path: Where to save the model. If None, uses default.
        grid_search: Whether to run GridSearchCV.
        cv_folds: Cross-validation folds.
        test_split: Fraction of data to hold out for evaluation.

    Returns:
        (fitted_classifier, evaluation_metrics).
    """
    print(f"Loading features from {csv_path}...")
    X, y, feature_names = load_features_csv(csv_path)
    print(f"  Samples: {len(X)}")
    print(f"  Features: {len(feature_names)}")
    print(f"  Classes: {len(set(y))} — {sorted(set(y))}")

    # Train/test split
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_split, stratify=y, random_state=42,
    )
    print(f"  Train: {len(X_train)}, Test: {len(X_test)}")

    # Train classifier
    clf = GestureClassifier()
    clf.fit(X_train, y_train, feature_names=feature_names)

    if grid_search and len(X_train) >= 50:
        print("\nRunning GridSearchCV...")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            clf.grid_search(X_train, y_train, cv=min(cv_folds, len(X_train) // 10))

    # Evaluate
    print("\n=== Evaluation ===")
    metrics = clf.evaluate(X_test, y_test)
    print(f"Accuracy:  {metrics['accuracy']:.4f}")
    print(f"F1-macro:  {metrics['f1_macro']:.4f}")
    print(f"\n{metrics['report']}")

    if metrics["feature_importances_top20"]:
        print("\nTop 20 Feature Importances:")
        for feat, imp in list(metrics["feature_importances_top20"].items())[:10]:
            print(f"  {feat}: {imp:.4f}")

    if metrics["per_class_f1"]:
        print("\nPer-class F1:")
        for cls_name, f1 in metrics["per_class_f1"].items():
            print(f"  {cls_name}: {f1:.4f}")

    # Save
    save_path = Path(output_path) if output_path else DEFAULT_MODEL_PATH
    save_path.parent.mkdir(parents=True, exist_ok=True)
    clf.save(save_path)

    # Also save evaluation report
    report_path = save_path.with_suffix(".eval.json")
    report_path.write_text(json.dumps(metrics, indent=2, default=str) + "\n")
    print(f"Evaluation report saved to {report_path}")

    return clf, metrics


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Train and evaluate EIT gesture classifier"
    )
    p.add_argument("--data", help="Path to features.csv from collect.py")
    p.add_argument("--out", default=None, help="Output path for model.joblib")
    p.add_argument("--eval", help="Evaluate a saved model on --data")
    p.add_argument("--no-grid-search", action="store_true",
                   help="Skip GridSearchCV")
    p.add_argument("--cv", type=int, default=5, help="Cross-validation folds")
    p.add_argument("--test-split", type=float, default=0.2,
                   help="Fraction for test split")
    p.add_argument("--threshold", type=float, default=0.6,
                   help="Confidence threshold for 'unknown'")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Evaluate existing model
    if args.eval:
        if not args.data:
            print("Error: --data required with --eval", file=sys.stderr)
            sys.exit(1)
        clf = GestureClassifier.load(args.eval)
        if args.threshold != 0.6:
            clf.confidence_threshold = args.threshold
        X, y, _ = load_features_csv(args.data)
        metrics = clf.evaluate(X, y)
        print(f"\nAccuracy:  {metrics['accuracy']:.4f}")
        print(f"F1-macro:  {metrics['f1_macro']:.4f}")
        print(f"\n{metrics['report']}")
        return

    # Train new model
    if not args.data:
        print("Error: --data required for training", file=sys.stderr)
        sys.exit(1)

    train_from_csv(
        csv_path=args.data,
        output_path=args.out,
        grid_search=not args.no_grid_search,
        cv_folds=args.cv,
        test_split=args.test_split,
    )


if __name__ == "__main__":
    main()
