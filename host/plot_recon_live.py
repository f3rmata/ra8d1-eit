#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib.pyplot as plt
import matplotlib.tri as mtri
import numpy as np

try:
    import serial
except ImportError as exc:  # pragma: no cover - depends on local env
    raise RuntimeError("pyserial is required. Use the project .venv or install pyserial.") from exc


@dataclass(frozen=True)
class ReconSummary:
    valid: int
    invalid: int
    retry: int
    ds_min: float
    ds_max: float
    ds_abs_p98: float
    rel_l2: float


@dataclass(frozen=True)
class ReconFrame:
    frame_id: int
    electrodes: int
    routes: int
    nodes: np.ndarray
    summary: ReconSummary


@dataclass
class PlotView:
    fig: plt.Figure | None = None
    triangulation: mtri.Triangulation | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live plot RA8D1 MCU-side EIT reconstruction frames")
    parser.add_argument("--port", required=True, help="USB serial port, for example /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--electrodes", type=int, default=8)
    parser.add_argument("--samples", type=int, default=256)
    parser.add_argument("--settle-ms", type=int, default=20)
    parser.add_argument("--rate", type=int, default=200000)
    parser.add_argument("--pp-limit", type=int, default=180)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--baseline-frames", type=int, default=0, help="send reconbase before live plotting; 0 skips")
    parser.add_argument("--gain", nargs=2, type=int, metavar=("DRIVE", "MEAS"), default=(512, 6))
    parser.add_argument("--no-power-init", action="store_true", help="do not send 'p 1 0 0' at startup")
    parser.add_argument("--frames", type=int, default=0, help="stop after this many frames; 0 runs until interrupted")
    parser.add_argument("--interval", type=float, default=0.0, help="sleep between frames")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--vmax", type=float, default=0.0, help="fixed color scale; 0 uses per-frame p98")
    parser.add_argument("--min-vmax", type=float, default=1.0e-4, help="minimum auto color scale")
    parser.add_argument("--deadband", type=float, default=0.0, help="set abs(ds) below this value to zero for display")
    parser.add_argument("--out-dir", type=Path, default=Path("eit_recon_mcu_live"))
    parser.add_argument("--prefix", default="mcu")
    parser.add_argument("--latest-only", action="store_true")
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument("--no-electrode-labels", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def clean_line(line: str) -> str:
    markers = (
        "RECONDUMP,",
        "RECONBASE_BEGIN,",
        "RECONBASE_FRAME,",
        "RECONBASE_DONE,",
        "RECON_BEGIN,",
        "RECON_SUMMARY,",
        "RECON_DONE",
        "ERR:",
        "bad command",
        "node,x,y,ds",
    )
    for marker in markers:
        index = line.find(marker)
        if index >= 0:
            return line[index:]
    stripped = line.strip()
    if stripped and stripped[0].isdigit():
        return stripped
    return stripped


def send_command_and_drain(ser: "serial.Serial", command: str, args: argparse.Namespace, wait_s: float = 0.4) -> None:
    ser.write((command.rstrip() + "\r\n").encode())
    ser.flush()
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        decoded = raw.decode("utf-8", errors="replace").rstrip()
        if args.debug:
            print("serial:", repr(decoded))


def init_board(ser: "serial.Serial", args: argparse.Namespace) -> None:
    ser.reset_input_buffer()
    ser.write(b"\r\n")
    ser.flush()
    time.sleep(0.05)
    ser.reset_input_buffer()

    if not args.no_power_init:
        send_command_and_drain(ser, "p 1 0 0", args)
    if args.gain is not None:
        send_command_and_drain(ser, "g {} {}".format(args.gain[0], args.gain[1]), args)
    send_command_and_drain(ser, "recondump", args)


def recon_command(args: argparse.Namespace) -> str:
    return "recon {} {} {} {} {} {}".format(
        args.electrodes,
        args.samples,
        args.settle_ms,
        args.rate,
        args.pp_limit,
        args.retries,
    )


def reconbase_command(args: argparse.Namespace) -> str:
    return "reconbase {} {} {} {} {} {} {}".format(
        args.electrodes,
        args.baseline_frames,
        args.samples,
        args.settle_ms,
        args.rate,
        args.pp_limit,
        args.retries,
    )


def capture_baseline(ser: "serial.Serial", args: argparse.Namespace) -> None:
    if args.baseline_frames <= 0:
        return

    command = reconbase_command(args)
    print("baseline:", command)
    ser.reset_input_buffer()
    ser.write((command + "\r\n").encode())
    ser.flush()

    recent: list[str] = []
    deadline = time.monotonic() + max(args.timeout, args.baseline_frames * args.timeout)
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        decoded = raw.decode("utf-8", errors="replace").rstrip()
        line = clean_line(decoded)
        recent.append(decoded)
        recent = recent[-20:]
        if args.debug:
            print("serial:", repr(decoded))
        if line.startswith("ERR:"):
            raise RuntimeError(line)
        if line.startswith("RECONBASE_FRAME,"):
            print(line)
        if line.startswith("RECONBASE_DONE,"):
            print(line)
            return

    raise TimeoutError("Timed out waiting for RECONBASE_DONE. Recent serial lines:\n{}".format("\n".join(recent)))


def capture_recon_frame(ser: "serial.Serial", args: argparse.Namespace) -> ReconFrame:
    command = recon_command(args)
    recent: list[str] = []
    frame_id: int | None = None
    electrodes = args.electrodes
    routes = 0
    expected_nodes: int | None = None
    summary: ReconSummary | None = None
    rows: list[tuple[int, float, float, float]] = []
    reading_nodes = False

    ser.reset_input_buffer()
    ser.write((command + "\r\n").encode())
    ser.flush()

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        deadline = time.monotonic() + args.timeout
        decoded = raw.decode("utf-8", errors="replace").rstrip()
        line = clean_line(decoded)
        recent.append(decoded)
        recent = recent[-20:]
        if args.debug:
            print("serial:", repr(decoded))
        if not line:
            continue
        if line.startswith("ERR:") or line.startswith("bad command"):
            raise RuntimeError(line)
        if line.startswith("RECON_BEGIN,"):
            parts = line.split(",")
            frame_id = int(parts[1])
            electrodes = int(parts[2])
            routes = int(parts[3])
            expected_nodes = int(parts[4])
            continue
        if line.startswith("RECON_SUMMARY,"):
            parts = line.split(",")
            summary = ReconSummary(
                valid=int(parts[1]),
                invalid=int(parts[2]),
                retry=int(parts[3]),
                ds_min=float(parts[4]),
                ds_max=float(parts[5]),
                ds_abs_p98=float(parts[6]),
                rel_l2=float(parts[7]),
            )
            continue
        if line == "node,x,y,ds":
            reading_nodes = True
            continue
        if line == "RECON_DONE":
            if frame_id is None or expected_nodes is None or summary is None:
                raise RuntimeError("RECON_DONE before complete frame metadata")
            if len(rows) != expected_nodes:
                raise RuntimeError("Frame {} has {} node rows, expected {}".format(frame_id, len(rows), expected_nodes))
            nodes = np.asarray(rows, dtype=np.float64)
            order = np.argsort(nodes[:, 0].astype(int))
            return ReconFrame(frame_id, electrodes, routes, nodes[order], summary)

        if reading_nodes:
            parts = line.split(",")
            if len(parts) == 4:
                rows.append((int(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])))

    raise TimeoutError("Timed out waiting for RECON_DONE. Recent serial lines:\n{}".format("\n".join(recent)))


def save_nodes(path: Path, frame: ReconFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["frame", "node", "x", "y", "ds"])
        for node, x, y, ds in frame.nodes:
            writer.writerow([frame.frame_id, int(node), "{:.9e}".format(x), "{:.9e}".format(y), "{:.9e}".format(ds)])


def draw_frame(path: Path | None, frame: ReconFrame, args: argparse.Namespace, view: PlotView | None) -> None:
    node_id = frame.nodes[:, 0].astype(int)
    x = frame.nodes[:, 1]
    y = frame.nodes[:, 2]
    ds = frame.nodes[:, 3]
    if args.deadband > 0.0:
        ds = np.where(np.abs(ds) >= args.deadband, ds, 0.0)

    if args.vmax > 0.0:
        vmax = args.vmax
    else:
        vmax = float(np.nanpercentile(np.abs(ds), 98))
        vmax = max(vmax, args.min_vmax)
    if not np.isfinite(vmax) or vmax <= 0.0:
        vmax = args.min_vmax

    show = view is not None
    if show:
        if view.fig is None or not plt.fignum_exists(view.fig.number):
            view.fig = plt.figure(figsize=(7.0, 6.4))
            view.triangulation = None
        fig = view.fig
        fig.clf()
    else:
        fig = plt.figure(figsize=(7.0, 6.4))

    ax = fig.add_subplot(111)
    if show and view.triangulation is not None and len(view.triangulation.x) == len(x):
        triangulation = view.triangulation
    else:
        triangulation = mtri.Triangulation(x, y)
        if show:
            view.triangulation = triangulation

    image = ax.tripcolor(triangulation, ds, shading="gouraud", cmap="coolwarm", vmin=-vmax, vmax=vmax)
    ax.set_aspect("equal")
    ax.set_axis_off()
    ax.set_title(
        "RA8D1 MCU JAC frame {} | valid={} invalid={} retry={} p98={:.3e}".format(
            frame.frame_id,
            frame.summary.valid,
            frame.summary.invalid,
            frame.summary.retry,
            frame.summary.ds_abs_p98,
        )
    )
    if not args.no_electrode_labels:
        for index in range(frame.electrodes):
            angle = (np.pi / 2.0) - (2.0 * np.pi * index / frame.electrodes)
            ex = np.cos(angle)
            ey = np.sin(angle)
            ax.plot([ex], [ey], marker="o", markersize=4, color="black", zorder=4)
            ax.text(ex * 1.09, ey * 1.09, "S{}".format(index + 1), ha="center", va="center", fontsize=9)
    fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04, label="conductivity change")
    fig.tight_layout()

    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=160)
    if show:
        fig.canvas.draw()
        fig.canvas.flush_events()
        plt.pause(0.01)
    else:
        plt.close(fig)

    _ = node_id


def main() -> int:
    args = parse_args()
    view = None
    if not args.no_plot:
        backend = plt.get_backend().lower()
        non_gui_backend = (
            backend == "agg"
            or "inline" in backend
            or backend.startswith(("pdf", "pgf", "ps", "svg", "template", "cairo"))
        )
        if non_gui_backend:
            print("warning: matplotlib backend is '{}'; live window disabled, PNGs will still be saved.".format(
                plt.get_backend()
            ))
        else:
            plt.ion()
            view = PlotView()

    if not args.no_save:
        args.out_dir.mkdir(parents=True, exist_ok=True)

    with serial.Serial(args.port, args.baud, timeout=0.2) as ser:
        time.sleep(0.3)
        init_board(ser, args)
        capture_baseline(ser, args)

        frame_index = 0
        while True:
            frame = capture_recon_frame(ser, args)
            latest_csv = None if args.no_save else args.out_dir / "{}_latest_nodes.csv".format(args.prefix)
            latest_png = None if args.no_save else args.out_dir / "{}_latest.png".format(args.prefix)
            if latest_csv is not None:
                save_nodes(latest_csv, frame)
            if not args.latest_only and not args.no_save:
                save_nodes(args.out_dir / "{}_{:06d}_nodes.csv".format(args.prefix, frame.frame_id), frame)

            draw_frame(latest_png, frame, args, view)
            if not args.latest_only and not args.no_save:
                draw_frame(args.out_dir / "{}_{:06d}.png".format(args.prefix, frame.frame_id), frame, args, None)

            print(
                "frame {}: valid={} invalid={} retry={} rel_l2={:.6e} ds_min={:.6e} ds_max={:.6e} p98={:.6e}".format(
                    frame.frame_id,
                    frame.summary.valid,
                    frame.summary.invalid,
                    frame.summary.retry,
                    frame.summary.rel_l2,
                    frame.summary.ds_min,
                    frame.summary.ds_max,
                    frame.summary.ds_abs_p98,
                )
            )

            frame_index += 1
            if args.frames > 0 and frame_index >= args.frames:
                break
            if args.interval > 0.0:
                time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
