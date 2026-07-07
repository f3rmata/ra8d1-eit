#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
PYEIT_ROOT = REPO_ROOT / "pyEIT"
if str(PYEIT_ROOT) not in sys.path:
    sys.path.insert(0, str(PYEIT_ROOT))

from pyeit.eit.jac import JAC
from pyeit.eit.protocol import PyEITProtocol
import pyeit.mesh as mesh

from generate_eit_recon_model import (
    build_protocol,
    element_to_node_matrix,
    read_baseline,
    route_signature,
)


def read_features(path: Path, routes: list[tuple[int, int, int, int]], baseline: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    values = {route: baseline[index] for index, route in enumerate(routes)}
    valid = {route: False for route in routes}
    with path.open(newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            route = (int(row["src"]), int(row["sink"]), int(row["vp"]), int(row["vn"]))
            if route not in values:
                continue
            flags = int(row["flags"])
            raw_flags = int(row["raw_flags"])
            overrange = int(row["overrange_count"])
            if flags == 0 and raw_flags == 0 and overrange == 0:
                values[route] = float(row["amp_v"])
                valid[route] = True
    return (
        np.asarray([values[route] for route in routes], dtype=np.float64),
        np.asarray([valid[route] for route in routes], dtype=bool),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare pyEIT/JAC with firmware-style float32 matrix reconstruction")
    parser.add_argument("--baseline-csv", type=Path, default=Path("eit_reconstruct_ra8d1_live/ra8_baseline.csv"))
    parser.add_argument("--features-csv", type=Path, default=Path("eit_reconstruct_ra8d1_live/ra8_latest_features.csv"))
    parser.add_argument("--mesh-h0", type=float, default=0.12)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    routes = route_signature(8)
    baseline, _source = read_baseline(args.baseline_csv, routes)
    frame, valid = read_features(args.features_csv, routes, baseline)

    protocol = build_protocol(routes)
    mesh_obj = mesh.create(protocol.n_el, h0=args.mesh_h0)
    solver = JAC(mesh_obj, protocol)
    solver.setup(p=0.5, lamb=0.01, method="kotre", perm=1, jac_normalized=True)
    e2n = element_to_node_matrix(mesh_obj.node, mesh_obj.element)

    py_node = e2n @ solver.solve(frame, baseline, normalize=True)
    matrix_f32 = np.asarray(e2n @ (-solver.H), dtype=np.float32)
    dv_f32 = np.asarray((frame - baseline) / np.abs(baseline), dtype=np.float32)
    mcu_node = matrix_f32 @ dv_f32

    abs_err = np.abs(py_node - mcu_node)
    print("routes={} valid={} nodes={}".format(len(routes), int(valid.sum()), mesh_obj.n_nodes))
    print("max_abs_err={:.9e}".format(float(np.max(abs_err))))
    print("rms_abs_err={:.9e}".format(float(np.sqrt(np.mean(abs_err * abs_err)))))
    print("py_min={:.9e} py_max={:.9e} py_abs_p98={:.9e}".format(
        float(np.min(py_node)),
        float(np.max(py_node)),
        float(np.percentile(np.abs(py_node), 98)),
    ))
    print("mcu_min={:.9e} mcu_max={:.9e} mcu_abs_p98={:.9e}".format(
        float(np.min(mcu_node)),
        float(np.max(mcu_node)),
        float(np.percentile(np.abs(mcu_node), 98)),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
