#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib.pyplot as plt
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
PYEIT_ROOT = REPO_ROOT / "pyEIT"
if str(PYEIT_ROOT) not in sys.path:
    sys.path.insert(0, str(PYEIT_ROOT))

try:
    from pyeit.eit.interp2d import sim2pts
    from pyeit.eit.jac import JAC
    from pyeit.eit.protocol import PyEITProtocol
    import pyeit.mesh as mesh
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local env
    raise RuntimeError(
        "pyEIT dependencies are incomplete. Run this with an environment that has "
        "numpy, scipy, matplotlib, pyserial, and shapely installed. The existing "
        "pico2_eit_validation/.venv currently satisfies these dependencies."
    ) from exc

try:
    import serial
except ImportError as exc:  # pragma: no cover - optional until live mode is used
    raise RuntimeError("pyserial is required for live reconstruction") from exc


@dataclass(frozen=True)
class StatRow:
    frame: int
    route_index: int
    src: int
    sink: int
    vp: int
    vn: int
    mean_code: float
    pp_code: float
    rms_code: float
    amp_v: float
    overrange_count: int
    valid_count: int
    flags: int
    retry_count: int
    raw_flags: int


@dataclass
class StatFrame:
    frame_id: int
    electrodes: int
    rows: list[StatRow]


@dataclass
class ReconstructionView:
    fig: plt.Figure | None = None


@dataclass
class ReconstructionResult:
    frame: StatFrame
    raw_vector: np.ndarray
    guarded_vector: np.ndarray
    solve_vector: np.ndarray
    ds_node: np.ndarray
    valid_count: int
    invalid_count: int
    retry_count: int
    route_guard_replaced: int
    route_guard_max_step: float
    route_guard_indices: list[int]


def rotate_plot_points_s1_up(points: np.ndarray) -> np.ndarray:
    """Rotate pyEIT's default electrode layout so S1 is at the top."""
    rotated = np.empty_like(points)
    rotated[:, 0] = points[:, 1]
    rotated[:, 1] = -points[:, 0]
    return rotated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live pyEIT/JAC reconstruction from Pico PIO scanstat frames")
    parser.add_argument("--port", required=True, help="USB serial port, for example /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--electrodes", type=int, default=8)
    parser.add_argument("--samples", type=int, default=256)
    parser.add_argument("--settle-ms", type=int, default=20)
    parser.add_argument("--rate", type=int, default=200000)
    parser.add_argument("--vref", type=float, default=2.5)
    parser.add_argument("--gain", nargs=2, type=int, metavar=("DRIVE", "MEAS"), default=(512, 6))
    parser.add_argument("--stat-pp-limit", type=int, default=180)
    parser.add_argument("--stat-retries", type=int, default=1)
    parser.add_argument("--baseline-warmup", type=int, default=3)
    parser.add_argument("--baseline-frames", type=int, default=5)
    parser.add_argument("--frames", type=int, default=0, help="live frames after baseline; 0 runs until interrupted")
    parser.add_argument("--mesh-h0", type=float, default=0.12)
    parser.add_argument("--min-valid-routes", type=int, default=36)
    parser.add_argument(
        "--temporal-median",
        type=int,
        default=3,
        help="median-filter this many measurement vectors before reconstruction; 1 disables it",
    )
    parser.add_argument(
        "--route-step-limit",
        type=float,
        default=0.08,
        help="replace isolated routes whose amplitude jumps this fraction from recent history; 0 disables it",
    )
    parser.add_argument("--route-guard-history", type=int, default=5)
    parser.add_argument("--route-guard-max-routes", type=int, default=3)
    parser.add_argument(
        "--display-deadband",
        type=float,
        default=0.0,
        help="set reconstructed node values below this absolute magnitude to zero in the displayed/saved image",
    )
    parser.add_argument(
        "--blank-calibration-frames",
        type=int,
        default=0,
        help="capture this many empty-tank frames after baseline to estimate display deadband",
    )
    parser.add_argument(
        "--blank-threshold-scale",
        type=float,
        default=1.5,
        help="display deadband multiplier for the empty-tank calibration noise floor",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("eit_reconstruct"))
    parser.add_argument("--prefix", default="eit")
    parser.add_argument("--latest-only", action="store_true")
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument("--hold-on-exit", action="store_true", help="keep the plot window open after a finite run")
    parser.add_argument("--no-electrode-labels", action="store_true")
    parser.add_argument("--frame-retries", type=int, default=2, help="retry malformed or incomplete scanstat frames")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def route_key(row: StatRow) -> tuple[int, int, int, int]:
    return row.src, row.sink, row.vp, row.vn


def row_is_valid(row: StatRow) -> bool:
    return row.flags == 0 and row.raw_flags == 0 and row.overrange_count == 0


def send_command_and_drain(ser: "serial.Serial", command: str, debug: bool, wait_s: float = 0.4) -> None:
    ser.write((command.rstrip() + "\r\n").encode())
    ser.flush()
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        decoded = raw.decode("utf-8", errors="replace").rstrip()
        if debug:
            print("serial:", repr(decoded))


def init_board(ser: "serial.Serial", args: argparse.Namespace) -> None:
    ser.reset_input_buffer()
    ser.write(b"\r\n")
    ser.flush()
    time.sleep(0.05)
    ser.reset_input_buffer()
    send_command_and_drain(ser, "p 1 0 0", args.debug)
    send_command_and_drain(ser, "g {} {}".format(args.gain[0], args.gain[1]), args.debug)


def expected_route_count(electrodes: int) -> int:
    return electrodes * max(0, electrodes - 3)


def capture_stat_frame_once(ser: "serial.Serial", args: argparse.Namespace) -> StatFrame:
    command = "scanstat {} {} {} {} {} {}\r\n".format(
        args.electrodes,
        args.samples,
        args.settle_ms,
        args.rate,
        args.stat_pp_limit,
        args.stat_retries,
    ).encode()
    rows: list[StatRow] = []
    frame_id: int | None = None
    electrodes = args.electrodes
    scale = args.vref / 1023.0
    recent: list[str] = []
    malformed: list[str] = []

    ser.reset_input_buffer()
    ser.write(command)
    ser.flush()

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        deadline = time.monotonic() + args.timeout
        decoded = raw.decode("utf-8", errors="replace").rstrip()
        recent.append(decoded)
        recent = recent[-20:]
        line = decoded.strip()
        if args.debug:
            print("serial:", repr(line))
        if not line:
            continue
        if line.startswith("ERR:") or line.startswith("bad command"):
            raise RuntimeError(line)
        if line.startswith("STAT_BEGIN,"):
            parts = line.split(",")
            frame_id = int(parts[1])
            if len(parts) >= 3:
                electrodes = int(parts[2])
            continue
        if line == "STAT_DONE":
            if frame_id is None:
                raise RuntimeError("STAT_DONE before STAT_BEGIN")
            expected_routes = expected_route_count(electrodes)
            if expected_routes > 0 and len(rows) != expected_routes:
                raise RuntimeError(
                    "Incomplete STAT frame {}: got {} route(s), expected {}. Malformed lines: {}".format(
                        frame_id,
                        len(rows),
                        expected_routes,
                        malformed[-3:],
                    )
                )
            return StatFrame(frame_id=frame_id, electrodes=electrodes, rows=rows)
        if line.startswith("route,") or line.startswith("STAT_ROUTE_BEGIN,"):
            continue

        parts = line.split(",")
        if len(parts) >= 15 and parts[0].isdigit() and frame_id is not None:
            try:
                mean_code = float(parts[5])
                pp_code = float(parts[8])
                rms_code = float(parts[9])
                route_index = int(parts[0])
                src = int(parts[1])
                sink = int(parts[2])
                vp = int(parts[3])
                vn = int(parts[4])
                overrange_count = int(parts[10])
                valid_count = int(parts[11])
                flags = int(parts[12])
                retry_count = int(parts[13])
                raw_flags = int(parts[14])
            except ValueError:
                malformed.append(line)
                malformed = malformed[-10:]
                continue
            rows.append(
                StatRow(
                    frame=frame_id,
                    route_index=route_index,
                    src=src,
                    sink=sink,
                    vp=vp,
                    vn=vn,
                    mean_code=mean_code,
                    pp_code=pp_code,
                    rms_code=rms_code,
                    amp_v=rms_code * scale * math.sqrt(2.0),
                    overrange_count=overrange_count,
                    valid_count=valid_count,
                    flags=flags,
                    retry_count=retry_count,
                    raw_flags=raw_flags,
                )
            )

    raise TimeoutError("Timed out waiting for STAT_DONE. Recent serial lines:\n{}".format("\n".join(recent)))


def capture_stat_frame(ser: "serial.Serial", args: argparse.Namespace) -> StatFrame:
    attempts = max(1, args.frame_retries + 1)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return capture_stat_frame_once(ser, args)
        except (RuntimeError, TimeoutError) as exc:
            if str(exc).startswith("ERR:") or str(exc).startswith("bad command"):
                raise
            last_error = exc
            if attempt >= attempts:
                break
            print("warning: discarded bad STAT frame attempt {}/{}: {}".format(attempt, attempts, exc))
            time.sleep(0.05)
    assert last_error is not None
    raise last_error


def build_protocol_from_rows(rows: list[StatRow]) -> PyEITProtocol:
    ex_lookup: dict[tuple[int, int], int] = {}
    ex_order: list[tuple[int, int]] = []
    meas_rows: list[list[int]] = []
    for row in rows:
        ex_key = (row.src, row.sink)
        if ex_key not in ex_lookup:
            ex_lookup[ex_key] = len(ex_order)
            ex_order.append(ex_key)
        meas_rows.append([row.vp, row.vn, ex_lookup[ex_key]])

    return PyEITProtocol(
        ex_mat=np.asarray(ex_order, dtype=int),
        meas_mat=np.asarray(meas_rows, dtype=int),
        keep_ba=np.ones(len(meas_rows), dtype=bool),
    )


def build_solver(protocol: PyEITProtocol, mesh_h0: float):
    mesh_obj = mesh.create(protocol.n_el, h0=mesh_h0)
    solver = JAC(mesh_obj, protocol)
    solver.setup(p=0.5, lamb=0.01, method="kotre", perm=1, jac_normalized=True)
    return mesh_obj, solver


def rows_to_vector(
    rows: list[StatRow],
    signature: list[tuple[int, int, int, int]],
    baseline: dict[tuple[int, int, int, int], float],
) -> tuple[np.ndarray, int, int, int]:
    by_key = {route_key(row): row for row in rows}
    values: list[float] = []
    valid_count = 0
    retry_count = 0
    invalid_count = 0
    for key in signature:
        row = by_key.get(key)
        if row is None:
            values.append(baseline[key])
            invalid_count += 1
            continue
        retry_count += row.retry_count
        if row_is_valid(row):
            values.append(row.amp_v)
            valid_count += 1
        else:
            values.append(baseline[key])
            invalid_count += 1
    return np.asarray(values, dtype=np.float64), valid_count, invalid_count, retry_count


def collect_baseline(args: argparse.Namespace, ser: "serial.Serial"):
    for index in range(args.baseline_warmup):
        frame = capture_stat_frame(ser, args)
        invalid = sum(1 for row in frame.rows if not row_is_valid(row))
        print("warmup {}/{}: frame={} routes={} invalid={}".format(
            index + 1, args.baseline_warmup, frame.frame_id, len(frame.rows), invalid
        ))

    signature: list[tuple[int, int, int, int]] | None = None
    samples: dict[tuple[int, int, int, int], list[float]] = {}
    protocol: PyEITProtocol | None = None
    for index in range(args.baseline_frames):
        frame = capture_stat_frame(ser, args)
        frame_signature = [route_key(row) for row in frame.rows]
        if signature is None:
            signature = frame_signature
            protocol = build_protocol_from_rows(frame.rows)
        elif frame_signature != signature:
            raise RuntimeError("Route order changed during baseline capture")

        added = 0
        invalid = 0
        for row in frame.rows:
            if row_is_valid(row):
                samples.setdefault(route_key(row), []).append(row.amp_v)
                added += 1
            else:
                invalid += 1
        print("baseline {}/{}: frame={} valid_added={} invalid={}".format(
            index + 1, args.baseline_frames, frame.frame_id, added, invalid
        ))

    assert signature is not None and protocol is not None
    missing = [key for key in signature if key not in samples]
    if missing:
        raise RuntimeError(
            "Baseline did not get valid values for {} route(s). Increase --baseline-frames. Missing: {}".format(
                len(missing), missing[:5]
            )
        )

    baseline = {key: float(np.median(samples[key])) for key in signature}
    return signature, baseline, np.asarray([baseline[key] for key in signature], dtype=np.float64), protocol


def save_features(path: Path, frame: StatFrame, rows: list[StatRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow([
            "frame", "route_index", "src", "sink", "vp", "vn", "amp_v", "pp_code", "rms_code",
            "overrange_count", "valid_count", "flags", "retry_count", "raw_flags",
        ])
        for row in rows:
            writer.writerow([
                frame.frame_id, row.route_index, row.src, row.sink, row.vp, row.vn, row.amp_v, row.pp_code,
                row.rms_code, row.overrange_count, row.valid_count, row.flags, row.retry_count, row.raw_flags,
            ])


def save_baseline(path: Path, signature: list[tuple[int, int, int, int]], baseline: dict[tuple[int, int, int, int], float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["route_index", "src", "sink", "vp", "vn", "baseline_amp_v"])
        for index, key in enumerate(signature):
            writer.writerow([index, key[0], key[1], key[2], key[3], baseline[key]])


def write_summary_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow([
            "timestamp_s",
            "frame",
            "valid_count",
            "invalid_count",
            "retry_count",
            "ds_min",
            "ds_max",
            "ds_mean",
            "ds_abs_p98",
            "raw_vector_rel_l2",
            "guarded_vector_rel_l2",
            "solve_vector_rel_l2",
            "temporal_median",
            "route_guard_replaced",
            "route_guard_max_step",
            "route_guard_indices",
            "display_deadband",
        ])


def relative_l2(vector: np.ndarray, baseline_vector: np.ndarray) -> float:
    denom = float(np.linalg.norm(baseline_vector))
    if denom <= 0.0 or not np.isfinite(denom):
        return float("nan")
    return float(np.linalg.norm(vector - baseline_vector) / denom)


def append_summary(
    path: Path,
    frame: StatFrame,
    valid_count: int,
    invalid_count: int,
    retry_count: int,
    ds_node: np.ndarray,
    raw_vector: np.ndarray,
    guarded_vector: np.ndarray,
    solve_vector: np.ndarray,
    baseline_vector: np.ndarray,
    temporal_median: int,
    route_guard_replaced: int,
    route_guard_max_step: float,
    route_guard_indices: list[int],
    display_deadband: float,
) -> None:
    with path.open("a", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow([
            "{:.3f}".format(time.time()),
            frame.frame_id,
            valid_count,
            invalid_count,
            retry_count,
            "{:.9e}".format(float(np.nanmin(ds_node))),
            "{:.9e}".format(float(np.nanmax(ds_node))),
            "{:.9e}".format(float(np.nanmean(ds_node))),
            "{:.9e}".format(float(np.nanpercentile(np.abs(ds_node), 98))),
            "{:.9e}".format(relative_l2(raw_vector, baseline_vector)),
            "{:.9e}".format(relative_l2(guarded_vector, baseline_vector)),
            "{:.9e}".format(relative_l2(solve_vector, baseline_vector)),
            temporal_median,
            route_guard_replaced,
            "{:.9e}".format(route_guard_max_step),
            ";".join(str(index) for index in route_guard_indices),
            "{:.9e}".format(display_deadband),
        ])


def filter_vector(
    history: list[np.ndarray],
    vector: np.ndarray,
    temporal_median: int,
    route_step_limit: float,
    route_guard_history: int,
    route_guard_max_routes: int,
) -> tuple[np.ndarray, np.ndarray, int, float, list[int]]:
    guarded = vector.copy()
    replaced_indices: list[int] = []
    max_step = 0.0

    if route_step_limit > 0.0 and route_guard_history > 0 and len(history) >= 3:
        recent = np.vstack(history[-route_guard_history:])
        reference = np.median(recent, axis=0)
        denom = np.maximum(np.abs(reference), 1.0e-12)
        rel_step = np.abs(vector - reference) / denom
        finite = np.isfinite(rel_step)
        if np.any(finite):
            max_step = float(np.nanmax(rel_step[finite]))
        candidate_indices = np.flatnonzero(finite & (rel_step > route_step_limit)).tolist()
        if 0 < len(candidate_indices) <= route_guard_max_routes:
            guarded[candidate_indices] = reference[candidate_indices]
            replaced_indices = candidate_indices

    history.append(guarded.copy())
    keep = max(1, temporal_median, route_guard_history)
    del history[:-keep]

    if temporal_median <= 1 or len(history) < temporal_median:
        return guarded, guarded, len(replaced_indices), max_step, replaced_indices
    return guarded, np.median(np.vstack(history[-temporal_median:]), axis=0), len(replaced_indices), max_step, replaced_indices


def draw_reconstruction(
    path: Path,
    mesh_obj,
    ds_node: np.ndarray,
    frame: StatFrame,
    valid_count: int,
    invalid_count: int,
    retry_count: int,
    electrode_labels: bool,
    display_deadband: float,
    show: bool,
    view: ReconstructionView | None = None,
) -> None:
    pts = rotate_plot_points_s1_up(mesh_obj.node)
    tri = mesh_obj.element
    if display_deadband > 0.0:
        display_node = np.where(np.abs(ds_node) >= display_deadband, ds_node, 0.0)
    else:
        display_node = ds_node
    vmax = float(np.nanpercentile(np.abs(display_node), 98))
    if display_deadband > 0.0:
        vmax = max(vmax, display_deadband)
    if not np.isfinite(vmax) or vmax <= 0.0:
        vmax = 1.0

    if show:
        if view is None:
            raise ValueError("show=True requires a ReconstructionView")
        if view.fig is None or not plt.fignum_exists(view.fig.number):
            view.fig = plt.figure(figsize=(7.2, 6.4))
        fig = view.fig
        fig.clf()
        ax = fig.add_subplot(111)
    else:
        fig, ax = plt.subplots(figsize=(7.2, 6.4))

    im = ax.tripcolor(
        pts[:, 0],
        pts[:, 1],
        tri,
        display_node,
        shading="gouraud",
        cmap="coolwarm",
        vmin=-vmax,
        vmax=vmax,
    )
    ax.set_aspect("equal")
    ax.set_title(
        "pyEIT JAC frame {} | valid={} invalid={} retries={}".format(
            frame.frame_id, valid_count, invalid_count, retry_count
        )
    )
    if display_deadband > 0.0:
        ax.text(
            0.02,
            0.02,
            "deadband={:.3e}".format(display_deadband),
            transform=ax.transAxes,
            ha="left",
            va="bottom",
            fontsize=8,
            color="black",
        )
    if electrode_labels:
        for label_index, node_index in enumerate(mesh_obj.el_pos):
            x, y = pts[node_index, 0], pts[node_index, 1]
            ax.plot([x], [y], marker="o", markersize=4, color="black", zorder=4)
            ax.text(
                x * 1.09,
                y * 1.09,
                "S{}".format(label_index + 1),
                ha="center",
                va="center",
                fontsize=9,
                color="black",
                zorder=5,
            )
    ax.set_axis_off()
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="conductivity change")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    if show:
        fig.canvas.draw()
        fig.canvas.flush_events()
        plt.pause(0.05)
    else:
        plt.close(fig)


def reconstruct_stat_frame(
    ser: "serial.Serial",
    args: argparse.Namespace,
    signature: list[tuple[int, int, int, int]],
    baseline: dict[tuple[int, int, int, int], float],
    vector_history: list[np.ndarray],
    solver: JAC,
    mesh_obj,
    baseline_vector: np.ndarray,
) -> ReconstructionResult:
    frame = capture_stat_frame(ser, args)
    frame_signature = [route_key(row) for row in frame.rows]
    if frame_signature != signature:
        raise RuntimeError("Route order changed in live frame {}".format(frame.frame_id))

    raw_vector, valid_count, invalid_count, retry_count = rows_to_vector(frame.rows, signature, baseline)
    guarded_vector, solve_vector, route_guard_replaced, route_guard_max_step, route_guard_indices = filter_vector(
        vector_history,
        raw_vector,
        max(1, args.temporal_median),
        max(0.0, args.route_step_limit),
        max(0, args.route_guard_history),
        max(0, args.route_guard_max_routes),
    )
    ds = solver.solve(solve_vector, baseline_vector, normalize=True)
    ds_node = sim2pts(mesh_obj.node, mesh_obj.element, np.real(ds))
    return ReconstructionResult(
        frame=frame,
        raw_vector=raw_vector,
        guarded_vector=guarded_vector,
        solve_vector=solve_vector,
        ds_node=ds_node,
        valid_count=valid_count,
        invalid_count=invalid_count,
        retry_count=retry_count,
        route_guard_replaced=route_guard_replaced,
        route_guard_max_step=route_guard_max_step,
        route_guard_indices=route_guard_indices,
    )


def estimate_blank_deadband(
    ser: "serial.Serial",
    args: argparse.Namespace,
    signature: list[tuple[int, int, int, int]],
    baseline: dict[tuple[int, int, int, int], float],
    vector_history: list[np.ndarray],
    solver: JAC,
    mesh_obj,
    baseline_vector: np.ndarray,
) -> float:
    if args.blank_calibration_frames <= 0:
        return max(0.0, args.display_deadband)

    noise_levels: list[float] = []
    target_count = args.blank_calibration_frames
    while len(noise_levels) < target_count:
        result = reconstruct_stat_frame(ser, args, signature, baseline, vector_history, solver, mesh_obj, baseline_vector)
        if result.valid_count < args.min_valid_routes:
            print(
                "blank calibration skipped frame {}: valid={} invalid={} retries={}".format(
                    result.frame.frame_id,
                    result.valid_count,
                    result.invalid_count,
                    result.retry_count,
                )
            )
            continue
        noise = float(np.nanpercentile(np.abs(result.ds_node), 98))
        noise_levels.append(noise)
        print(
            "blank calibration {}/{}: frame={} ds_abs_p98={:.6e}".format(
                len(noise_levels),
                target_count,
                result.frame.frame_id,
                noise,
            )
        )

    calibrated = float(np.median(noise_levels) * max(0.0, args.blank_threshold_scale))
    deadband = max(0.0, args.display_deadband, calibrated)
    print(
        "display deadband: manual={:.6e} calibrated={:.6e} active={:.6e}".format(
            max(0.0, args.display_deadband),
            calibrated,
            deadband,
        )
    )
    return deadband


def main() -> int:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    view = None
    if not args.no_plot:
        backend = plt.get_backend().lower()
        non_gui_backend = (
            backend == "agg"
            or "inline" in backend
            or backend.startswith(("pdf", "pgf", "ps", "svg", "template", "cairo"))
        )
        if non_gui_backend:
            print("warning: matplotlib backend is '{}'; no GUI window will be shown. Unset MPLBACKEND for live display.".format(
                plt.get_backend()
            ))
        else:
            plt.ion()
            view = ReconstructionView()

    with serial.Serial(args.port, args.baud, timeout=0.2) as ser:
        time.sleep(0.3)
        init_board(ser, args)
        signature, baseline, v0, protocol = collect_baseline(args, ser)
        mesh_obj, solver = build_solver(protocol, args.mesh_h0)
        save_baseline(args.out_dir / "{}_baseline.csv".format(args.prefix), signature, baseline)
        summary_path = args.out_dir / "{}_summary.csv".format(args.prefix)
        write_summary_header(summary_path)
        print("baseline captured: routes={} mesh_nodes={} mesh_elements={}".format(
            len(signature), mesh_obj.n_nodes, mesh_obj.n_elems
        ))

        frame_index = 0
        vector_history: list[np.ndarray] = []
        active_display_deadband = estimate_blank_deadband(
            ser,
            args,
            signature,
            baseline,
            vector_history,
            solver,
            mesh_obj,
            v0,
        )
        while True:
            result = reconstruct_stat_frame(ser, args, signature, baseline, vector_history, solver, mesh_obj, v0)
            frame = result.frame
            latest_features = args.out_dir / "{}_latest_features.csv".format(args.prefix)
            save_features(latest_features, frame, frame.rows)
            if not args.latest_only:
                save_features(args.out_dir / "{}_{:06d}_features.csv".format(args.prefix, frame.frame_id), frame, frame.rows)

            if result.valid_count < args.min_valid_routes:
                print(
                    "frame {} skipped: valid={} invalid={} retries={}".format(
                        frame.frame_id,
                        result.valid_count,
                        result.invalid_count,
                        result.retry_count,
                    )
                )
                continue

            append_summary(
                summary_path,
                frame,
                result.valid_count,
                result.invalid_count,
                result.retry_count,
                result.ds_node,
                result.raw_vector,
                result.guarded_vector,
                result.solve_vector,
                v0,
                max(1, args.temporal_median),
                result.route_guard_replaced,
                result.route_guard_max_step,
                result.route_guard_indices,
                active_display_deadband,
            )
            latest_png = args.out_dir / "{}_latest_recon.png".format(args.prefix)
            draw_reconstruction(
                latest_png,
                mesh_obj,
                result.ds_node,
                frame,
                result.valid_count,
                result.invalid_count,
                result.retry_count,
                not args.no_electrode_labels,
                active_display_deadband,
                view is not None,
                view,
            )
            if not args.latest_only:
                draw_reconstruction(
                    args.out_dir / "{}_{:06d}_recon.png".format(args.prefix, frame.frame_id),
                    mesh_obj,
                    result.ds_node,
                    frame,
                    result.valid_count,
                    result.invalid_count,
                    result.retry_count,
                    not args.no_electrode_labels,
                    active_display_deadband,
                    False,
                )

            print(
                "frame {}: valid={} invalid={} retries={} guard={}/{} rel_l2={:.6e}->{:.6e}->{:.6e} ds_min={:.6e} ds_max={:.6e} deadband={:.6e} -> {}".format(
                    frame.frame_id,
                    result.valid_count,
                    result.invalid_count,
                    result.retry_count,
                    result.route_guard_replaced,
                    "{:.3f}".format(result.route_guard_max_step),
                    relative_l2(result.raw_vector, v0),
                    relative_l2(result.guarded_vector, v0),
                    relative_l2(result.solve_vector, v0),
                    float(np.nanmin(result.ds_node)),
                    float(np.nanmax(result.ds_node)),
                    active_display_deadband,
                    latest_png,
                )
            )

            frame_index += 1
            if args.frames > 0 and frame_index >= args.frames:
                break

    if args.hold_on_exit and view is not None and view.fig is not None and plt.fignum_exists(view.fig.number):
        plt.ioff()
        plt.show()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
