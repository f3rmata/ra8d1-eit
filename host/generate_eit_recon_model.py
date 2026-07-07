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

from pyeit.eit.interp2d import tri_area
from pyeit.eit.jac import JAC
from pyeit.eit.protocol import PyEITProtocol
import pyeit.mesh as mesh


def route_signature(electrodes: int) -> list[tuple[int, int, int, int]]:
    routes: list[tuple[int, int, int, int]] = []
    for src in range(electrodes):
        sink = (src + 1) % electrodes
        for vp in range(electrodes):
            vn = (vp + 1) % electrodes
            if vp in (src, sink) or vn in (src, sink):
                continue
            routes.append((src, sink, vp, vn))
    return routes


def build_protocol(routes: list[tuple[int, int, int, int]]) -> PyEITProtocol:
    ex_lookup: dict[tuple[int, int], int] = {}
    ex_order: list[tuple[int, int]] = []
    meas_rows: list[list[int]] = []
    for src, sink, vp, vn in routes:
        ex_key = (src, sink)
        if ex_key not in ex_lookup:
            ex_lookup[ex_key] = len(ex_order)
            ex_order.append(ex_key)
        meas_rows.append([vp, vn, ex_lookup[ex_key]])
    return PyEITProtocol(
        ex_mat=np.asarray(ex_order, dtype=int),
        meas_mat=np.asarray(meas_rows, dtype=int),
        keep_ba=np.ones(len(meas_rows), dtype=bool),
    )


def rotate_plot_points_s1_up(points: np.ndarray) -> np.ndarray:
    rotated = np.empty((points.shape[0], 2), dtype=points.dtype)
    rotated[:, 0] = points[:, 1]
    rotated[:, 1] = -points[:, 0]
    return rotated


def element_to_node_matrix(nodes: np.ndarray, elements: np.ndarray) -> np.ndarray:
    weights = tri_area(nodes, elements)
    mapping = np.zeros((nodes.shape[0], elements.shape[0]), dtype=np.float64)
    for element_index, element in enumerate(elements):
        for node_index in element:
            mapping[node_index, element_index] += weights[element_index]
    row_sum = mapping.sum(axis=1)
    if np.any(row_sum == 0.0):
        raise RuntimeError("mesh contains node(s) with no adjacent element")
    return mapping / row_sum[:, None]


def read_baseline(path: Path | None, routes: list[tuple[int, int, int, int]]) -> tuple[np.ndarray, str]:
    if path is None:
        return np.ones(len(routes), dtype=np.float64), "unit"

    values: dict[tuple[int, int, int, int], float] = {}
    with path.open(newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            key = (int(row["src"]), int(row["sink"]), int(row["vp"]), int(row["vn"]))
            values[key] = float(row["baseline_amp_v"])

    missing = [route for route in routes if route not in values]
    if missing:
        raise RuntimeError(f"baseline CSV is missing {len(missing)} route(s): {missing[:3]}")
    return np.asarray([values[route] for route in routes], dtype=np.float64), path.as_posix()


def c_float(value: float) -> str:
    if not np.isfinite(value):
        raise ValueError(f"non-finite model value: {value}")
    return f"{np.float32(value):.9e}f"


def wrapped(items: list[str], indent: str = "    ", per_line: int = 6) -> str:
    lines: list[str] = []
    for offset in range(0, len(items), per_line):
        lines.append(indent + ", ".join(items[offset:offset + per_line]) + ",")
    return "\n".join(lines)


def write_header(path: Path, nodes: int, elements: int, routes: int) -> None:
    text = f"""#ifndef EIT_RECON_MODEL_H
#define EIT_RECON_MODEL_H

#include <stdint.h>

#define EIT_RECON_MODEL_VERSION \"jac8-h0.12-kotre-p0.5-lambda0.01-v1\"
#define EIT_RECON_ELECTRODES (8U)
#define EIT_RECON_ROUTES ({routes}U)
#define EIT_RECON_NODES ({nodes}U)
#define EIT_RECON_ELEMENTS ({elements}U)

extern const uint8_t g_eit_recon_routes[EIT_RECON_ROUTES][4];
extern const float g_eit_recon_baseline_amp_v[EIT_RECON_ROUTES];
extern const float g_eit_recon_node_xy[EIT_RECON_NODES][2];
extern const uint16_t g_eit_recon_elements[EIT_RECON_ELEMENTS][3];
extern const float g_eit_recon_matrix[EIT_RECON_NODES][EIT_RECON_ROUTES];
extern const char g_eit_recon_baseline_source[];

#endif
"""
    path.write_text(text)


def write_source(
    path: Path,
    routes: list[tuple[int, int, int, int]],
    baseline: np.ndarray,
    baseline_source: str,
    node_xy: np.ndarray,
    elements: np.ndarray,
    matrix: np.ndarray,
) -> None:
    lines: list[str] = [
        '#include "eit_recon_model.h"',
        "",
        f'const char g_eit_recon_baseline_source[] = "{baseline_source}";',
        "",
        "const uint8_t g_eit_recon_routes[EIT_RECON_ROUTES][4] =",
        "{",
    ]
    lines.extend(f"    {{ {src}U, {sink}U, {vp}U, {vn}U }}," for src, sink, vp, vn in routes)
    lines.extend(["};", ""])

    lines.extend([
        "const float g_eit_recon_baseline_amp_v[EIT_RECON_ROUTES] =",
        "{",
        wrapped([c_float(value) for value in baseline]),
        "};",
        "",
        "const float g_eit_recon_node_xy[EIT_RECON_NODES][2] =",
        "{",
    ])
    lines.extend(f"    {{ {c_float(x)}, {c_float(y)} }}," for x, y in node_xy)
    lines.extend(["};", ""])

    lines.extend([
        "const uint16_t g_eit_recon_elements[EIT_RECON_ELEMENTS][3] =",
        "{",
    ])
    lines.extend(f"    {{ {int(a)}U, {int(b)}U, {int(c)}U }}," for a, b, c in elements)
    lines.extend(["};", ""])

    lines.extend([
        "const float g_eit_recon_matrix[EIT_RECON_NODES][EIT_RECON_ROUTES] =",
        "{",
    ])
    for row in matrix:
        lines.append("    {")
        lines.append(wrapped([c_float(value) for value in row], "        ", 5))
        lines.append("    },")
    lines.extend(["};", ""])
    path.write_text("\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate fixed RA8D1 EIT JAC reconstruction model constants")
    parser.add_argument("--electrodes", type=int, default=8)
    parser.add_argument("--mesh-h0", type=float, default=0.12)
    parser.add_argument("--baseline-csv", type=Path, default=Path("eit_reconstruct_ra8d1_live/ra8_baseline.csv"))
    parser.add_argument("--out-header", type=Path, default=Path("src/eit_recon_model.h"))
    parser.add_argument("--out-source", type=Path, default=Path("src/eit_recon_model.c"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.electrodes != 8:
        raise RuntimeError("the firmware model currently supports only 8 electrodes")

    routes = route_signature(args.electrodes)
    protocol = build_protocol(routes)
    mesh_obj = mesh.create(protocol.n_el, h0=args.mesh_h0)
    solver = JAC(mesh_obj, protocol)
    solver.setup(p=0.5, lamb=0.01, method="kotre", perm=1, jac_normalized=True)

    e2n = element_to_node_matrix(mesh_obj.node, mesh_obj.element)
    recon_matrix = e2n @ (-solver.H)
    baseline, baseline_source = read_baseline(args.baseline_csv, routes)
    node_xy = rotate_plot_points_s1_up(mesh_obj.node)

    args.out_header.parent.mkdir(parents=True, exist_ok=True)
    args.out_source.parent.mkdir(parents=True, exist_ok=True)
    write_header(args.out_header, mesh_obj.n_nodes, mesh_obj.n_elems, len(routes))
    write_source(args.out_source, routes, baseline, baseline_source, node_xy, mesh_obj.element, recon_matrix)

    probe = baseline * (1.0 + np.linspace(-0.01, 0.01, len(routes)))
    direct = e2n @ solver.solve(probe, baseline, normalize=True)
    generated = np.asarray(recon_matrix, dtype=np.float32) @ np.asarray(
        (probe - baseline) / np.abs(baseline),
        dtype=np.float32,
    )
    max_err = float(np.max(np.abs(direct - generated)))
    print(
        "generated {} and {}: routes={} nodes={} elements={} max_equiv_err={:.3e}".format(
            args.out_header, args.out_source, len(routes), mesh_obj.n_nodes, mesh_obj.n_elems, max_err
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
