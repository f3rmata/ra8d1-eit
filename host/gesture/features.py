#!/usr/bin/env python3
"""Feature extraction from EIT reconstruction frames for gesture classification.

Extracts spatial region features (quadrants, rings, electrode regions),
global statistical features, summary stats, and temporal (frame-to-frame)
features from ds_node[261] reconstruction vectors.

The node coordinates from g_eit_recon_node_xy[261][2] are parsed from
the C model source at initialization time to build region masks.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
import numpy as np

# ---------------------------------------------------------------------------
# Constants matching eit_recon_model.h
# ---------------------------------------------------------------------------
NUM_ELECTRODES = 8
NUM_NODES = 261
NUM_ROUTES = 40

# Electrode angular positions (radians), S1 at top (pi/2), clockwise.
# These correspond to the first 8 nodes in g_eit_recon_node_xy.
ELECTRODE_ANGLES = np.array([
    math.pi / 2,                # S1: top  (0, 1)
    math.pi / 4,                # S2: top-right
    0.0,                        # S3: right (1, 0)
    -math.pi / 4,               # S4: bottom-right
    -math.pi / 2,               # S5: bottom
    -3 * math.pi / 4,           # S6: bottom-left
    math.pi,                    # S7: left (-1, 0)
    3 * math.pi / 4,            # S8: top-left
], dtype=np.float64)


# ---------------------------------------------------------------------------
# Node coordinate parser
# ---------------------------------------------------------------------------

def _find_model_source() -> Path:
    """Locate eit_recon_model.c relative to this file."""
    this_dir = Path(__file__).resolve().parent
    # host/gesture/features.py -> src/eit_recon_model.c
    candidates = [
        this_dir.parent.parent / "src" / "eit_recon_model.c",
        this_dir / ".." / ".." / "src" / "eit_recon_model.c",
    ]
    for c in candidates:
        if c.exists():
            return c.resolve()
    raise FileNotFoundError(
        "Cannot find src/eit_recon_model.c. "
        "Ensure this module lives under host/gesture/ in the ra8d1_eit repo."
    )


def parse_node_coordinates(model_path: str | Path | None = None) -> np.ndarray:
    """Parse g_eit_recon_node_xy[261][2] from the C model source.

    Returns (261, 2) float64 array of (x, y) coordinates.
    """
    if model_path is None:
        model_path = _find_model_source()

    text = Path(model_path).read_text(encoding="utf-8")

    # Locate the array body: const float g_eit_recon_node_xy[...][2] = { ... };
    match = re.search(
        r"g_eit_recon_node_xy\[[^\]]+\]\[[^\]]+\]\s*=\s*\{(.*?)\};",
        text,
        re.DOTALL,
    )
    if match is None:
        raise ValueError("Could not find g_eit_recon_node_xy array in model source")

    body = match.group(1)

    # Extract { x, y } pairs
    pairs = re.findall(r'\{\s*([^\}]+)\s*\}', body)
    coords: list[tuple[float, float]] = []
    for pair in pairs:
        parts = pair.split(",")
        if len(parts) >= 2:
            coords.append((
                float(parts[0].strip().rstrip("f").rstrip("F")),
                float(parts[1].strip().rstrip("f").rstrip("F")),
            ))

    if len(coords) != NUM_NODES:
        raise ValueError(
            f"Expected {NUM_NODES} node coordinates, found {len(coords)}"
        )

    return np.array(coords, dtype=np.float64)


# ---------------------------------------------------------------------------
# Region masks (computed once)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegionMasks:
    """Precomputed per-node region assignments for fast feature extraction."""

    quadrant: np.ndarray  # [261] int in 0..3
    ring: np.ndarray      # [261] int in 0..2
    electrode_region: np.ndarray  # [261] int in 0..7 (nearest electrode)

    QUADRANT_NAMES: tuple[str, ...] = ("dorsal", "radial", "ventral", "ulnar")
    RING_NAMES: tuple[str, ...] = ("inner", "middle", "outer")


def build_region_masks(node_xy: np.ndarray) -> RegionMasks:
    """Build per-node region assignments from (261, 2) coordinates.

    Quadrants (by angle from origin):
        Q0 (dorsal):    pi/4     .. 3*pi/4   (y > |x|, top)
        Q1 (radial):   -pi/4    .. pi/4      (x > |y|, right/thumb)
        Q2 (ventral):  -3*pi/4 .. -pi/4      (y < -|x|, bottom/palm)
        Q3 (ulnar):    complementary          (x < -|y|, left/pinky)

    Rings (by radial distance from origin):
        Inner:   r < 0.3   (deep tissue)
        Middle:  0.3 <= r < 0.7  (muscle)
        Outer:   r >= 0.7  (near skin/electrodes)
    """
    x, y = node_xy[:, 0], node_xy[:, 1]
    angles = np.arctan2(y, x)
    radii = np.sqrt(x * x + y * y)

    # Quadrant assignment
    quadrant = np.zeros(NUM_NODES, dtype=np.int32)
    # Q0: angles between pi/4 and 3*pi/4
    quadrant[(angles > math.pi / 4) & (angles <= 3 * math.pi / 4)] = 0
    # Q1: angles between -pi/4 and pi/4
    quadrant[(angles > -math.pi / 4) & (angles <= math.pi / 4)] = 1
    # Q2: angles between -3*pi/4 and -pi/4
    quadrant[(angles > -3 * math.pi / 4) & (angles <= -math.pi / 4)] = 2
    # Q3: the rest (left side)
    quadrant[
        (angles > 3 * math.pi / 4)
        | (angles <= -3 * math.pi / 4)
    ] = 3

    # Ring assignment
    ring = np.zeros(NUM_NODES, dtype=np.int32)
    ring[(radii >= 0.3) & (radii < 0.7)] = 1
    ring[radii >= 0.7] = 2

    # Electrode region assignment (nearest of the 8 electrode nodes)
    electrode_xy = node_xy[:NUM_ELECTRODES]
    electrode_region = np.zeros(NUM_NODES, dtype=np.int32)
    for i in range(NUM_NODES):
        dists = np.sum((electrode_xy - node_xy[i]) ** 2, axis=1)
        electrode_region[i] = int(np.argmin(dists))

    return RegionMasks(
        quadrant=quadrant,
        ring=ring,
        electrode_region=electrode_region,
    )


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

# Threshold fraction of |ds| > ds_abs_p98 * ACTIVATION_RATIO_THRESHOLD
ACTIVATION_THRESHOLD_FRAC = 0.3


@dataclass
class GestureFeatures:
    """Container for all extracted features from one frame."""

    values: np.ndarray  # flat feature vector
    names: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, float]:
        return dict(zip(self.names, self.values))


def extract_features(
    ds_node: np.ndarray,           # [261] float
    summary: dict[str, float],     # from ReconFastBinFrame
    prev_ds_node: np.ndarray | None = None,
    regions: RegionMasks | None = None,
    node_xy: np.ndarray | None = None,
) -> GestureFeatures:
    """Extract all engineered features from one reconstruction frame.

    Args:
        ds_node: [261] delta-sigma node values.
        summary: Dict with keys valid_count, invalid_count, retry_count,
                 ds_min, ds_max, ds_abs_p98, rel_l2.
        prev_ds_node: Previous frame's ds_node for temporal features (optional).
        regions: Precomputed RegionMasks. If None, built from node_xy.
        node_xy: [261, 2] node coordinates. Required if regions is None.

    Returns:
        GestureFeatures with flat vector and feature names.
    """
    ds = np.asarray(ds_node, dtype=np.float64)
    if ds.shape != (NUM_NODES,):
        raise ValueError(f"Expected ds_node shape ({NUM_NODES},), got {ds.shape}")

    if regions is None:
        if node_xy is None:
            raise ValueError("Either regions or node_xy must be provided")
        regions = build_region_masks(node_xy)

    features: dict[str, float] = {}

    # -- Quadrant features (12) --
    for q in range(4):
        mask = regions.quadrant == q
        q_ds = ds[mask]
        if len(q_ds) == 0:
            features[f"q{q}_{regions.QUADRANT_NAMES[q]}_mean"] = 0.0
            features[f"q{q}_{regions.QUADRANT_NAMES[q]}_absmax"] = 0.0
            features[f"q{q}_{regions.QUADRANT_NAMES[q]}_active"] = 0.0
            continue
        features[f"q{q}_{regions.QUADRANT_NAMES[q]}_mean"] = float(np.mean(q_ds))
        features[f"q{q}_{regions.QUADRANT_NAMES[q]}_absmax"] = float(np.max(np.abs(q_ds)))

        p98 = summary.get("ds_abs_p98", 0.0)
        threshold = p98 * ACTIVATION_THRESHOLD_FRAC
        if threshold > 0:
            features[f"q{q}_{regions.QUADRANT_NAMES[q]}_active"] = float(
                np.mean(np.abs(q_ds) > threshold)
            )
        else:
            features[f"q{q}_{regions.QUADRANT_NAMES[q]}_active"] = 0.0

    # -- Ring features (6) --
    for r in range(3):
        mask = regions.ring == r
        r_ds = ds[mask]
        if len(r_ds) == 0:
            features[f"ring_{regions.RING_NAMES[r]}_mean"] = 0.0
            features[f"ring_{regions.RING_NAMES[r]}_std"] = 0.0
            continue
        features[f"ring_{regions.RING_NAMES[r]}_mean"] = float(np.mean(r_ds))
        features[f"ring_{regions.RING_NAMES[r]}_std"] = float(np.std(r_ds))

    # -- Electrode region features (8) --
    for e in range(NUM_ELECTRODES):
        mask = regions.electrode_region == e
        e_ds = ds[mask]
        features[f"elec_{e}_mean"] = float(np.mean(e_ds)) if len(e_ds) > 0 else 0.0

    # -- Global statistical features (7) --
    features["global_mean"] = float(np.mean(ds))
    features["global_std"] = float(np.std(ds))
    features["global_skew"] = float(_skewness(ds))
    features["global_kurtosis"] = float(_kurtosis(ds))

    abs_ds = np.abs(ds)
    p98 = float(np.percentile(abs_ds, 98))
    threshold = p98 * ACTIVATION_THRESHOLD_FRAC if p98 > 0 else 0.01
    features["activation_ratio"] = float(np.mean(abs_ds > threshold))

    # Spatial centroid (|ds|-weighted)
    if node_xy is not None and np.sum(abs_ds) > 0:
        features["centroid_x"] = float(np.average(node_xy[:, 0], weights=abs_ds))
        features["centroid_y"] = float(np.average(node_xy[:, 1], weights=abs_ds))
    else:
        features["centroid_x"] = 0.0
        features["centroid_y"] = 0.0

    # -- Summary features from MCU (7) --
    for key in [
        "valid_count", "invalid_count", "retry_count",
        "ds_min", "ds_max", "ds_abs_p98", "rel_l2",
    ]:
        features[f"summary_{key}"] = float(summary.get(key, 0.0))

    # -- Temporal features (12) --
    if prev_ds_node is not None:
        prev_ds = np.asarray(prev_ds_node, dtype=np.float64)
        delta = ds - prev_ds
        features["delta_l2"] = float(np.sqrt(np.sum(delta * delta)))

        for q in range(4):
            mask = regions.quadrant == q
            q_delta = delta[mask]
            features[f"delta_q{q}_mean"] = float(np.mean(q_delta)) if len(q_delta) > 0 else 0.0

        for key in ["ds_min", "ds_max", "ds_abs_p98", "rel_l2"]:
            prev_val = summary.get(f"prev_{key}", summary.get(key, 0.0))
            features[f"delta_{key}"] = features[f"summary_{key}"] - prev_val
    else:
        features["delta_l2"] = 0.0
        for q in range(4):
            features[f"delta_q{q}_mean"] = 0.0
        for key in ["ds_min", "ds_max", "ds_abs_p98", "rel_l2"]:
            features[f"delta_{key}"] = 0.0

    # Build ordered vector
    names = sorted(features.keys())
    values = np.array([features[n] for n in names], dtype=np.float64)

    return GestureFeatures(values=values, names=names)


def _skewness(x: np.ndarray) -> float:
    """Pearson's moment coefficient of skewness."""
    n = len(x)
    if n < 3:
        return 0.0
    mean = np.mean(x)
    std = np.std(x)
    if std < 1e-15:
        return 0.0
    return float((np.sum((x - mean) ** 3) / n) / (std ** 3))


def _kurtosis(x: np.ndarray) -> float:
    """Excess kurtosis (Fisher definition, normal=0)."""
    n = len(x)
    if n < 4:
        return 0.0
    mean = np.mean(x)
    std = np.std(x)
    if std < 1e-15:
        return 0.0
    return float((np.sum((x - mean) ** 4) / n) / (std ** 4) - 3.0)


# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_node_xy_cache: np.ndarray | None = None
_region_masks_cache: RegionMasks | None = None


def get_node_xy(model_path: str | Path | None = None) -> np.ndarray:
    """Return cached (261, 2) node coordinate array."""
    global _node_xy_cache
    if _node_xy_cache is None:
        _node_xy_cache = parse_node_coordinates(model_path)
    return _node_xy_cache


def get_region_masks(
    model_path: str | Path | None = None,
) -> RegionMasks:
    """Return cached RegionMasks, building them if necessary."""
    global _region_masks_cache
    if _region_masks_cache is None:
        _region_masks_cache = build_region_masks(get_node_xy(model_path))
    return _region_masks_cache
