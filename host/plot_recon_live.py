#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib.pyplot as plt
import matplotlib.tri as mtri
import numpy as np

from eit_binary import read_reconfast_frame
from serial_lines import SerialLineReader, clean_protocol_line

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
    ax: plt.Axes | None = None
    triangulation: mtri.Triangulation | None = None
    image: object | None = None
    colorbar: object | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live plot RA8D1 MCU-side EIT reconstruction frames")
    parser.add_argument("--port", required=True, help="USB serial port, for example /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--electrodes", type=int, default=8)
    parser.add_argument("--samples", type=int, default=256)
    parser.add_argument("--settle-ms", type=int, default=20)
    parser.add_argument("--rate", type=int, default=200000)
    parser.add_argument("--pp-limit", type=int, default=180)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--baseline-frames", type=int, default=5, help="send reconbase before live plotting; 0 uses firmware baseline")
    parser.add_argument("--baseline-samples", type=int, default=256)
    parser.add_argument("--baseline-settle-ms", type=int, default=20)
    parser.add_argument("--baseline-rate", type=int, default=200000)
    parser.add_argument("--baseline-pp-limit", type=int, default=180)
    parser.add_argument("--baseline-retries", type=int, default=1)
    parser.add_argument("--fast", action="store_true", help="use reconfast after one full recon frame has supplied node coordinates")
    parser.add_argument("--binary-fast", action="store_true", help="use reconfastbin binary frames after the first full recon frame")
    parser.add_argument("--gain", nargs=2, type=int, metavar=("DRIVE", "MEAS"), default=(512, 6))
    parser.add_argument("--no-power-init", action="store_true", help="do not send 'p 1 0 0' at startup")
    parser.add_argument("--reset", action="store_true", help="reset the MCU with pyOCD before opening serial")
    parser.add_argument("--pyocd", default="/home/fermata/.local/share/pipx/venvs/pyocd/bin/pyocd")
    parser.add_argument("--target", default="r7fa8d1bh")
    parser.add_argument("--uid", default="0F7A117605A6")
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
    parser.add_argument("--save-every", type=int, default=1, help="save PNG/CSV every N frames; 0 disables saving")
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
        "RECONFAST_BEGIN,",
        "RECON_SUMMARY,",
        "RECONFAST_DS,",
        "RECONFAST_DONE",
        "RECON_DONE",
        "ERR:",
        "bad command",
        "node,x,y,ds",
    )
    cleaned = clean_protocol_line(line, markers)
    if cleaned != line.strip():
        return cleaned
    stripped = line.strip()
    if stripped and stripped[0].isdigit():
        return stripped
    return stripped


def send_command_and_drain(ser: "serial.Serial", command: str, args: argparse.Namespace, wait_s: float = 0.4) -> None:
    reader = SerialLineReader(ser)
    write_command(ser, command)
    deadline = time.monotonic() + wait_s
    while True:
        decoded = reader.read_line(deadline)
        if decoded is None:
            return
        if args.debug:
            print("serial:", repr(decoded))


def run_simple_command(ser: "serial.Serial", command: str, end_marker: str, args: argparse.Namespace) -> None:
    drain_idle(ser, idle_s=0.15, max_s=1.0, debug=args.debug)
    write_command(ser, command)
    deadline = time.monotonic() + 5.0
    reader = SerialLineReader(ser, recent_limit=10)
    while True:
        decoded = reader.read_line(deadline)
        if decoded is None:
            break
        line = clean_line(decoded)
        if args.debug:
            print("serial:", repr(decoded))
        if line.startswith(end_marker) or line == end_marker:
            return
        if line.startswith("ERR:") or line.startswith("bad command"):
            raise RuntimeError(line)
    raise TimeoutError("Timed out waiting for {} after {!r}. Recent:\n{}".format(
        end_marker, command, reader.format_recent()
    ))


def write_command(ser: "serial.Serial", command: str) -> None:
    for ch in command.rstrip().encode():
        ser.write(bytes([ch]))
        ser.flush()
        time.sleep(0.003)
    time.sleep(0.05)
    for ch in b"\r\n\r":
        ser.write(bytes([ch]))
        ser.flush()
        time.sleep(0.02)


def drain_idle(ser: "serial.Serial", idle_s: float, max_s: float, debug: bool) -> None:
    deadline = time.monotonic() + max_s
    idle_deadline = time.monotonic() + idle_s
    reader = SerialLineReader(ser)
    while True:
        decoded = reader.read_line(min(deadline, idle_deadline))
        if decoded is None:
            return
        if not decoded:
            if time.monotonic() >= idle_deadline:
                return
            continue
        idle_deadline = time.monotonic() + idle_s
        if debug:
            print("drain:", repr(decoded))


def wait_for_prompt(ser: "serial.Serial", timeout: float, debug: bool) -> None:
    deadline = time.monotonic() + timeout
    data = b""
    saw_ready = False
    first_prompt_time: float | None = None
    while time.monotonic() < deadline:
        chunk = ser.read(256)
        if chunk:
            data += chunk
            data = data[-1024:]
            if debug:
                print("prompt-drain:", repr(chunk.decode("utf-8", errors="replace")))
            if b"ready" in data:
                saw_ready = True
            if b"eit>" in data and saw_ready:
                return
            if b"eit>" in data and first_prompt_time is None:
                first_prompt_time = time.monotonic()
            if first_prompt_time is not None and (time.monotonic() - first_prompt_time) > 3.0:
                return
            continue
        if first_prompt_time is not None and (time.monotonic() - first_prompt_time) > 3.0:
            return
        time.sleep(0.02)
    raise TimeoutError("Timed out waiting for eit> prompt; last bytes:\n{}".format(
        data.decode("utf-8", errors="replace")
    ))


def init_board(ser: "serial.Serial", args: argparse.Namespace) -> None:
    time.sleep(1.0)
    drain_idle(ser, idle_s=0.3, max_s=3.0, debug=args.debug)

    if not args.no_power_init:
        run_simple_command(ser, "p 1 0 0", "power ok", args)
    if args.gain is not None:
        run_simple_command(ser, "g {} {}".format(args.gain[0], args.gain[1]), "gain drive=", args)
    run_simple_command(ser, "recondump", "RECONDUMP,", args)


def pyocd_reset(args: argparse.Namespace) -> None:
    cmd = [args.pyocd, "reset", "--target", args.target, "--uid", args.uid]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10.0)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("warning: pyOCD reset failed ({}); continuing with serial sync".format(exc), flush=True)


def recon_command(args: argparse.Namespace, fast: bool = False) -> str:
    return "{} {} {} {} {} {} {}".format(
        "reconfast" if fast else "recon",
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
        args.baseline_samples,
        args.baseline_settle_ms,
        args.baseline_rate,
        args.baseline_pp_limit,
        args.baseline_retries,
    )


def capture_baseline(ser: "serial.Serial", args: argparse.Namespace) -> None:
    if args.baseline_frames <= 0:
        return

    command = reconbase_command(args)
    print("baseline:", command, flush=True)
    drain_idle(ser, idle_s=0.15, max_s=1.0, debug=args.debug)
    write_command(ser, command)

    reader = SerialLineReader(ser, recent_limit=20)
    deadline = time.monotonic() + max(args.timeout, args.baseline_frames * args.timeout)
    started = False
    start_deadline = time.monotonic() + 8.0
    while True:
        decoded = reader.read_line(min(deadline, start_deadline) if not started else deadline)
        if decoded is None:
            if (not started) and time.monotonic() >= start_deadline:
                raise TimeoutError("Timed out waiting for RECONBASE_BEGIN after {!r}".format(command))
            break
        line = clean_line(decoded)
        if args.debug:
            print("serial:", repr(decoded))
        if not started:
            if line.startswith("RECONBASE_BEGIN,"):
                started = True
            else:
                continue
        if line.startswith("ERR:"):
            raise RuntimeError(line)
        if line.startswith("RECONBASE_FRAME,"):
            print(line, flush=True)
        if line.startswith("RECONBASE_DONE,"):
            print(line, flush=True)
            return

    raise TimeoutError("Timed out waiting for RECONBASE_DONE. Recent serial lines:\n{}".format(reader.format_recent()))


def capture_recon_frame(ser: "serial.Serial", args: argparse.Namespace) -> ReconFrame:
    command = recon_command(args)
    reader = SerialLineReader(ser, recent_limit=20)
    frame_id: int | None = None
    electrodes = args.electrodes
    routes = 0
    expected_nodes: int | None = None
    summary: ReconSummary | None = None
    rows: list[tuple[int, float, float, float]] = []
    reading_nodes = False

    drain_idle(ser, idle_s=0.15, max_s=1.0, debug=args.debug)
    write_command(ser, command)

    deadline = time.monotonic() + args.timeout
    started = False
    start_deadline = time.monotonic() + 8.0
    while True:
        decoded = reader.read_line(min(deadline, start_deadline) if not started else deadline)
        if decoded is None:
            if (not started) and time.monotonic() >= start_deadline:
                raise TimeoutError("Timed out waiting for RECON_BEGIN after {!r}".format(command))
            break
        deadline = time.monotonic() + args.timeout
        line = clean_line(decoded)
        if args.debug:
            print("serial:", repr(decoded))
        if not line:
            continue
        if not started:
            if line.startswith("RECON_BEGIN,"):
                started = True
            else:
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

    raise TimeoutError("Timed out waiting for RECON_DONE. Recent serial lines:\n{}".format(reader.format_recent()))


def capture_reconfast_frame(ser: "serial.Serial", args: argparse.Namespace, template_nodes: np.ndarray) -> ReconFrame:
    command = recon_command(args, fast=True)
    reader = SerialLineReader(ser, recent_limit=20)
    frame_id: int | None = None
    electrodes = args.electrodes
    routes = 0
    expected_nodes: int | None = None
    summary: ReconSummary | None = None
    ds_values: np.ndarray | None = None

    drain_idle(ser, idle_s=0.15, max_s=1.0, debug=args.debug)
    write_command(ser, command)

    deadline = time.monotonic() + args.timeout
    started = False
    start_deadline = time.monotonic() + 8.0
    while True:
        decoded = reader.read_line(min(deadline, start_deadline) if not started else deadline)
        if decoded is None:
            if (not started) and time.monotonic() >= start_deadline:
                raise TimeoutError("Timed out waiting for RECONFAST_BEGIN after {!r}".format(command))
            break
        deadline = time.monotonic() + args.timeout
        line = clean_line(decoded)
        if args.debug:
            print("serial:", repr(decoded))
        if not line:
            continue
        if not started:
            if line.startswith("RECONFAST_BEGIN,"):
                started = True
            else:
                continue
        if line.startswith("ERR:") or line.startswith("bad command"):
            raise RuntimeError(line)
        if line.startswith("RECONFAST_BEGIN,"):
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
        if line.startswith("RECONFAST_DS,"):
            ds_values = np.asarray([float(value) for value in line.split(",")[1:]], dtype=np.float64)
            continue
        if line == "RECONFAST_DONE":
            if frame_id is None or expected_nodes is None or summary is None or ds_values is None:
                raise RuntimeError("RECONFAST_DONE before complete frame data")
            if len(ds_values) != expected_nodes:
                raise RuntimeError("Frame {} has {} ds values, expected {}".format(frame_id, len(ds_values), expected_nodes))
            if len(template_nodes) != expected_nodes:
                raise RuntimeError("Template has {} nodes, expected {}".format(len(template_nodes), expected_nodes))
            nodes = template_nodes.copy()
            nodes[:, 3] = ds_values
            return ReconFrame(frame_id, electrodes, routes, nodes, summary)

    raise TimeoutError("Timed out waiting for RECONFAST_DONE. Recent serial lines:\n{}".format(reader.format_recent()))


def capture_reconfastbin_frame(ser: "serial.Serial", args: argparse.Namespace, template_nodes: np.ndarray) -> ReconFrame:
    command = "{} {} {} {} {} {} {}".format(
        "reconfastbin",
        args.electrodes,
        args.samples,
        args.settle_ms,
        args.rate,
        args.pp_limit,
        args.retries,
    )
    drain_idle(ser, idle_s=0.15, max_s=1.0, debug=args.debug)
    write_command(ser, command)
    frame = read_reconfast_frame(ser, args.timeout)
    if len(template_nodes) != frame.nodes:
        raise RuntimeError("Template has {} nodes, expected {}".format(len(template_nodes), frame.nodes))
    if len(frame.ds_values) != frame.nodes:
        raise RuntimeError("Frame {} has {} ds values, expected {}".format(frame.frame_id, len(frame.ds_values), frame.nodes))

    nodes = template_nodes.copy()
    nodes[:, 3] = np.asarray(frame.ds_values, dtype=np.float64)
    summary = ReconSummary(
        valid=frame.valid,
        invalid=frame.invalid,
        retry=frame.retry,
        ds_min=frame.ds_min,
        ds_max=frame.ds_max,
        ds_abs_p98=frame.ds_abs_p98,
        rel_l2=frame.rel_l2,
    )
    return ReconFrame(frame.frame_id, frame.electrodes, frame.routes, nodes, summary)


def save_nodes(path: Path, frame: ReconFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["frame", "node", "x", "y", "ds"])
        for node, x, y, ds in frame.nodes:
            writer.writerow([frame.frame_id, int(node), "{:.9e}".format(x), "{:.9e}".format(y), "{:.9e}".format(ds)])


def draw_frame(path: Path | None, frame: ReconFrame, args: argparse.Namespace, view: PlotView | None) -> None:
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
            view.ax = None
            view.triangulation = None
            view.image = None
            view.colorbar = None
        fig = view.fig
        if view.ax is None:
            view.ax = fig.add_subplot(111)
        ax = view.ax
    else:
        fig = plt.figure(figsize=(7.0, 6.4))
        ax = fig.add_subplot(111)

    if show and view.triangulation is not None and len(view.triangulation.x) == len(x):
        triangulation = view.triangulation
    else:
        triangulation = mtri.Triangulation(x, y)
        if show:
            view.triangulation = triangulation

    title = "RA8D1 MCU JAC frame {} | valid={} invalid={} retry={} p98={:.3e}".format(
        frame.frame_id,
        frame.summary.valid,
        frame.summary.invalid,
        frame.summary.retry,
        frame.summary.ds_abs_p98,
    )

    if show and view.image is not None:
        view.image.set_array(ds)
        view.image.set_clim(-vmax, vmax)
        ax.set_title(title)
    else:
        ax.clear()
        image = ax.tripcolor(triangulation, ds, shading="gouraud", cmap="coolwarm", vmin=-vmax, vmax=vmax)
        ax.set_aspect("equal")
        ax.set_axis_off()
        ax.set_title(title)
        if not args.no_electrode_labels:
            for index in range(frame.electrodes):
                angle = (np.pi / 2.0) - (2.0 * np.pi * index / frame.electrodes)
                ex = np.cos(angle)
                ey = np.sin(angle)
                ax.plot([ex], [ey], marker="o", markersize=4, color="black", zorder=4)
                ax.text(ex * 1.09, ey * 1.09, "S{}".format(index + 1), ha="center", va="center", fontsize=9)
        if show:
            view.image = image
            if view.colorbar is None:
                view.colorbar = fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04, label="conductivity change")
        else:
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


def main() -> int:
    args = parse_args()
    if args.binary_fast:
        args.fast = True
    if args.reset:
        pyocd_reset(args)
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

    with serial.Serial(args.port, args.baud, timeout=1.0) as ser:
        time.sleep(0.3)
        init_board(ser, args)
        capture_baseline(ser, args)

        frame_index = 0
        template_nodes: np.ndarray | None = None
        while True:
            if args.fast and template_nodes is not None:
                if args.binary_fast:
                    frame = capture_reconfastbin_frame(ser, args, template_nodes)
                else:
                    frame = capture_reconfast_frame(ser, args, template_nodes)
            else:
                frame = capture_recon_frame(ser, args)
                template_nodes = frame.nodes.copy()
            save_enabled = (not args.no_save) and (args.save_every > 0) and ((frame_index % args.save_every) == 0)
            latest_csv = args.out_dir / "{}_latest_nodes.csv".format(args.prefix) if save_enabled else None
            latest_png = args.out_dir / "{}_latest.png".format(args.prefix) if save_enabled else None
            if save_enabled and latest_csv is not None:
                save_nodes(latest_csv, frame)
            if save_enabled and not args.latest_only:
                save_nodes(args.out_dir / "{}_{:06d}_nodes.csv".format(args.prefix, frame.frame_id), frame)

            draw_frame(latest_png, frame, args, view)
            if save_enabled and not args.latest_only:
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
