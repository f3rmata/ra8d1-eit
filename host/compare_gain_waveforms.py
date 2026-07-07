#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

try:
    import matplotlib.pyplot as plt
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("matplotlib is required for plotting") from exc

try:
    import serial
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("pyserial is required. Use the project .venv or install pyserial.") from exc

from serial_lines import SerialLineReader, clean_protocol_line


@dataclass(frozen=True)
class Route:
    index: int
    src: int
    sink: int
    vp: int
    vn: int


@dataclass(frozen=True)
class Capture:
    gain_drive: int
    gain_meas: int
    route: Route
    samples: np.ndarray
    mean: float
    rms: float
    amp10k: float
    min_code: int
    max_code: int
    pp_code: int
    rail_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture and plot raw EIT ADC waveforms for gain tuning")
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--electrodes", type=int, default=8)
    parser.add_argument("--samples", type=int, default=512)
    parser.add_argument("--rate", type=int, default=200000)
    parser.add_argument("--settle-ms", type=float, default=20.0)
    parser.add_argument(
        "--gain",
        nargs=2,
        action="append",
        type=int,
        metavar=("DRIVE", "MEAS"),
        help="gain pair to test; may be repeated. Default: 512 6",
    )
    parser.add_argument(
        "--route-index",
        action="append",
        type=int,
        help="40-route adjacent protocol index to capture; may be repeated",
    )
    parser.add_argument(
        "--route",
        nargs=4,
        action="append",
        type=int,
        metavar=("SRC", "SINK", "VP", "VN"),
        help="explicit zero-based route tuple; may be repeated",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("diagnostics/gain_waveforms"))
    parser.add_argument("--prefix", default="gain_compare")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--excite-hz", type=float, default=10000.0)
    parser.add_argument("--reset", action="store_true", help="reset target with pyOCD before capture")
    parser.add_argument("--pyocd", default="/home/fermata/.local/share/pipx/venvs/pyocd/bin/pyocd")
    parser.add_argument("--target", default="r7fa8d1bh")
    parser.add_argument("--uid", default="0F7A117605A6")
    parser.add_argument("--no-power-init", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def protocol_routes(electrodes: int) -> list[Route]:
    routes: list[Route] = []
    for src in range(electrodes):
        sink = (src + 1) % electrodes
        for vp in range(electrodes):
            vn = (vp + 1) % electrodes
            if vp in (src, sink) or vn in (src, sink):
                continue
            routes.append(Route(len(routes), src, sink, vp, vn))
    return routes


def selected_routes(args: argparse.Namespace) -> list[Route]:
    all_routes = protocol_routes(args.electrodes)
    selected: list[Route] = []

    route_indices = args.route_index if args.route_index else [1, 19, 37, 20]
    for index in route_indices:
        if index < 0 or index >= len(all_routes):
            raise ValueError("route index {} is outside 0..{}".format(index, len(all_routes) - 1))
        selected.append(all_routes[index])

    if args.route:
        for route in args.route:
            src, sink, vp, vn = route
            matched = next(
                (item for item in all_routes if (item.src, item.sink, item.vp, item.vn) == (src, sink, vp, vn)),
                None,
            )
            selected.append(matched if matched is not None else Route(-1, src, sink, vp, vn))

    deduped: list[Route] = []
    seen: set[tuple[int, int, int, int]] = set()
    for route in selected:
        key = (route.src, route.sink, route.vp, route.vn)
        if key not in seen:
            seen.add(key)
            deduped.append(route)
    return deduped


def clean_line(line: str) -> str:
    markers = ("ADC_BEGIN", "ADC_END", "ERR:", "bad command", "raw ok", "raw spi_error", "all mux off", "power ok", "gain drive=")
    return clean_protocol_line(line, markers)


def drain_idle(ser: "serial.Serial", idle_s: float, max_s: float, debug: bool) -> None:
    deadline = time.monotonic() + max_s
    idle_deadline = time.monotonic() + idle_s
    reader = SerialLineReader(ser)
    while True:
        decoded = reader.read_line(min(deadline, idle_deadline))
        if decoded is None:
            return
        idle_deadline = time.monotonic() + idle_s
        if debug:
            print("drain:", repr(decoded))


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


def run_until(
    ser: "serial.Serial",
    command: str,
    end_marker: str,
    timeout: float,
    debug: bool,
    start_marker: str | None = None,
) -> list[str]:
    drain_idle(ser, idle_s=0.08, max_s=0.5, debug=debug)
    write_command(ser, command)
    lines: list[str] = []
    started = start_marker is None
    start_deadline = time.monotonic() + min(5.0, timeout)
    deadline = time.monotonic() + timeout
    reader = SerialLineReader(ser)
    while True:
        decoded = reader.read_line(min(deadline, start_deadline) if not started else deadline)
        if decoded is None:
            if (not started) and time.monotonic() >= start_deadline:
                break
            break
        line = clean_line(decoded)
        if debug:
            print("serial:", repr(decoded))
        if not line:
            continue
        if not started:
            if line.startswith(start_marker):
                started = True
            else:
                continue
        lines.append(line)
        if line.startswith("ERR:") or line.startswith("bad command"):
            raise RuntimeError(line)
        if line == end_marker or line.startswith(end_marker):
            return lines
    raise TimeoutError("timed out waiting for {} after {!r}; recent lines:\n{}".format(
        end_marker, command, "\n".join(lines[-12:])
    ))


def pyocd_reset(args: argparse.Namespace) -> None:
    cmd = [args.pyocd, "reset", "--target", args.target, "--uid", args.uid]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10.0)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("warning: pyOCD reset failed ({}); continuing with serial sync".format(exc), flush=True)


def init_board(ser: "serial.Serial", args: argparse.Namespace) -> None:
    time.sleep(1.0)
    drain_idle(ser, idle_s=0.3, max_s=3.0, debug=args.debug)
    if not args.no_power_init:
        run_until(ser, "p 1 0 0", "power ok", 5.0, args.debug)


def set_gain(ser: "serial.Serial", drive: int, meas: int, args: argparse.Namespace) -> None:
    run_until(ser, "g {} {}".format(drive, meas), "gain drive=", 5.0, args.debug)


def setup_route(ser: "serial.Serial", route: Route, settle_ms: float, args: argparse.Namespace) -> None:
    for command in (
        "off",
        "raw src {} 1".format(route.src),
        "raw sink {} 1".format(route.sink),
        "raw vp {} 1".format(route.vp),
        "raw vn {} 1".format(route.vn),
    ):
        lines = run_until(ser, command, "raw ok" if command.startswith("raw ") else "all mux off", 5.0, args.debug)
        if any("spi_error" in line for line in lines):
            raise RuntimeError("{} failed: {}".format(command, " | ".join(lines)))
    if settle_ms > 0.0:
        time.sleep(settle_ms / 1000.0)


def capture_adc(ser: "serial.Serial", args: argparse.Namespace) -> np.ndarray:
    lines = run_until(
        ser,
        "adc {} {}".format(args.samples, args.rate),
        "ADC_END",
        args.timeout,
        args.debug,
        start_marker="ADC_BEGIN",
    )
    values: list[int] = []
    for line in lines:
        parts = line.split(",")
        if len(parts) == 2 and parts[0].isdigit():
            values.append(int(parts[1], 0))
    if len(values) != args.samples:
        raise RuntimeError("captured {} ADC samples, expected {}".format(len(values), args.samples))
    return np.asarray(values, dtype=np.float64)


def compute_metrics(samples: np.ndarray, rate: int, excite_hz: float) -> tuple[float, float, float, int, int, int, int]:
    mean = float(np.mean(samples))
    centered = samples - mean
    rms = float(np.sqrt(np.mean(centered * centered)))
    indices = np.arange(len(samples), dtype=np.float64)
    phase = 2.0 * math.pi * excite_hz * indices / float(rate)
    cos_sum = float(np.sum(centered * np.cos(phase)))
    sin_sum = float(np.sum(centered * np.sin(phase)))
    amp10k = 2.0 * math.sqrt((cos_sum * cos_sum) + (sin_sum * sin_sum)) / float(len(samples))
    min_code = int(np.min(samples))
    max_code = int(np.max(samples))
    pp_code = max_code - min_code
    rail_count = int(np.count_nonzero((samples <= 2.0) | (samples >= 1021.0)))
    return mean, rms, amp10k, min_code, max_code, pp_code, rail_count


def save_csvs(captures: list[Capture], out_dir: Path, prefix: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = out_dir / "{}_metrics.csv".format(prefix)
    samples_path = out_dir / "{}_samples.csv".format(prefix)

    with metrics_path.open("w", newline="") as fp:
        fieldnames = [
            "gain_drive",
            "gain_meas",
            "route",
            "src",
            "sink",
            "vp",
            "vn",
            "mean",
            "rms",
            "amp10k",
            "min",
            "max",
            "pp",
            "rails",
        ]
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        for cap in captures:
            writer.writerow(
                {
                    "gain_drive": cap.gain_drive,
                    "gain_meas": cap.gain_meas,
                    "route": cap.route.index,
                    "src": cap.route.src,
                    "sink": cap.route.sink,
                    "vp": cap.route.vp,
                    "vn": cap.route.vn,
                    "mean": "{:.6f}".format(cap.mean),
                    "rms": "{:.6f}".format(cap.rms),
                    "amp10k": "{:.6f}".format(cap.amp10k),
                    "min": cap.min_code,
                    "max": cap.max_code,
                    "pp": cap.pp_code,
                    "rails": cap.rail_count,
                }
            )

    with samples_path.open("w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["gain_drive", "gain_meas", "route", "src", "sink", "vp", "vn", "i", "value"])
        for cap in captures:
            for i, value in enumerate(cap.samples.astype(int)):
                writer.writerow([cap.gain_drive, cap.gain_meas, cap.route.index, cap.route.src, cap.route.sink, cap.route.vp, cap.route.vn, i, value])

    return metrics_path, samples_path


def route_label(route: Route) -> str:
    route_id = "r{}".format(route.index) if route.index >= 0 else "custom"
    return "{} {}-{} / {}-{}".format(route_id, route.src, route.sink, route.vp, route.vn)


def plot_grid(captures: list[Capture], routes: list[Route], gains: list[tuple[int, int]], out_dir: Path, prefix: str, rate: int) -> Path:
    by_key = {(cap.gain_drive, cap.gain_meas, cap.route.src, cap.route.sink, cap.route.vp, cap.route.vn): cap for cap in captures}
    rows = len(routes)
    cols = len(gains)
    fig, axes = plt.subplots(rows, cols, figsize=(max(5.0, cols * 4.4), max(3.0, rows * 2.5)), squeeze=False, sharex=True, sharey=True)
    for row, route in enumerate(routes):
        for col, gain in enumerate(gains):
            ax = axes[row][col]
            cap = by_key[(gain[0], gain[1], route.src, route.sink, route.vp, route.vn)]
            x_ms = np.arange(len(cap.samples), dtype=np.float64) / float(rate) * 1000.0
            ax.plot(x_ms, cap.samples, lw=1.0)
            ax.axhline(2, color="red", lw=0.7, alpha=0.45)
            ax.axhline(1021, color="red", lw=0.7, alpha=0.45)
            ax.grid(True, alpha=0.25)
            ax.set_title(
                "{} | g {} {}\npp={} rms={:.1f} rails={}".format(
                    route_label(route), gain[0], gain[1], cap.pp_code, cap.rms, cap.rail_count
                ),
                fontsize=8,
            )
            if col == 0:
                ax.set_ylabel("ADC code")
            if row == rows - 1:
                ax.set_xlabel("time (ms)")
    fig.tight_layout()
    path = out_dir / "{}_grid.png".format(prefix)
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def plot_overlay(captures: list[Capture], routes: list[Route], gains: list[tuple[int, int]], out_dir: Path, prefix: str, rate: int) -> Path:
    by_key = {(cap.gain_drive, cap.gain_meas, cap.route.src, cap.route.sink, cap.route.vp, cap.route.vn): cap for cap in captures}
    rows = len(routes)
    fig, axes = plt.subplots(rows, 1, figsize=(12.0, max(3.2, rows * 2.5)), squeeze=False, sharex=True)
    for row, route in enumerate(routes):
        ax = axes[row][0]
        for gain in gains:
            cap = by_key[(gain[0], gain[1], route.src, route.sink, route.vp, route.vn)]
            x_ms = np.arange(len(cap.samples), dtype=np.float64) / float(rate) * 1000.0
            label = "g {} {} pp={} rails={}".format(gain[0], gain[1], cap.pp_code, cap.rail_count)
            ax.plot(x_ms, cap.samples - cap.mean, lw=1.0, label=label)
        ax.set_title(route_label(route), fontsize=9)
        ax.set_ylabel("code - mean")
        ax.grid(True, alpha=0.25)
        ax.legend(fontsize=8, ncol=min(3, len(gains)))
    axes[-1][0].set_xlabel("time (ms)")
    fig.tight_layout()
    path = out_dir / "{}_dc_overlay.png".format(prefix)
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def main() -> int:
    args = parse_args()
    gains = [tuple(gain) for gain in args.gain] if args.gain else [(512, 6)]
    routes = selected_routes(args)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    if args.reset:
        pyocd_reset(args)

    captures: list[Capture] = []
    with serial.Serial(args.port, args.baud, timeout=0.5) as ser:
        init_board(ser, args)
        for drive, meas in gains:
            set_gain(ser, drive, meas, args)
            for route in routes:
                setup_route(ser, route, args.settle_ms, args)
                samples = capture_adc(ser, args)
                mean, rms, amp10k, min_code, max_code, pp_code, rail_count = compute_metrics(samples, args.rate, args.excite_hz)
                cap = Capture(drive, meas, route, samples, mean, rms, amp10k, min_code, max_code, pp_code, rail_count)
                captures.append(cap)
                print(
                    "g {:4d} {:4d} {:>14s}: mean={:7.2f} rms={:7.2f} amp10k={:7.2f} pp={:4d} rails={:3d}/{}".format(
                        drive,
                        meas,
                        route_label(route),
                        mean,
                        rms,
                        amp10k,
                        pp_code,
                        rail_count,
                        args.samples,
                    ),
                    flush=True,
                )
        run_until(ser, "off", "all mux off", 5.0, args.debug)

    metrics_path, samples_path = save_csvs(captures, args.out_dir, args.prefix)
    grid_path = plot_grid(captures, routes, gains, args.out_dir, args.prefix, args.rate)
    overlay_path = plot_overlay(captures, routes, gains, args.out_dir, args.prefix, args.rate)
    print("wrote {}".format(metrics_path))
    print("wrote {}".format(samples_path))
    print("wrote {}".format(grid_path))
    print("wrote {}".format(overlay_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
