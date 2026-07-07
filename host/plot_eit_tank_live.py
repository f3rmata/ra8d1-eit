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

from eit_binary import read_scanstat_frame, scanstat_rows_as_dicts
from serial_lines import SerialLineReader, clean_protocol_line, write_command

try:
    import serial
except ImportError:
    serial = None


@dataclass(frozen=True)
class Route:
    src: int
    sink: int
    vp: int
    vn: int


@dataclass
class Block:
    route: Route
    samples: list[int]
    overrange: list[int]


@dataclass
class Frame:
    frame_id: int
    electrodes: int
    samples_per_route: int
    sample_rate_hz: float
    blocks: list[Block]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Continuously capture scanraw frames and draw an 8-electrode EIT tank image")
    parser.add_argument("--port", required=True, help="USB serial port, for example /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--electrodes", type=int, default=8, help="physical S1..S_n electrode count")
    parser.add_argument("--samples", type=int, default=256, help="ADC samples per route")
    parser.add_argument("--settle-ms", type=int, default=2)
    parser.add_argument("--rate", type=int, default=200000, help="PIO ADC readout rate in samples/s")
    parser.add_argument("--excitation-hz", type=float, default=10000.0)
    parser.add_argument("--vref", type=float, default=2.5, help="external ADC full-scale voltage used for code-to-volt conversion")
    parser.add_argument("--field", choices=("amp_v", "pp_v", "rms_v"), default="amp_v")
    parser.add_argument("--out-dir", type=Path, default=Path("eit_capture"))
    parser.add_argument("--prefix", default="tank")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--interval", type=float, default=0.0)
    parser.add_argument("--frames", type=int, default=0, help="stop after this many frames; 0 means run until interrupted")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--baseline", action="store_true", help="capture a baseline and plot later frames as delta")
    parser.add_argument("--baseline-warmup", type=int, default=0, help="discard this many frames before baseline capture")
    parser.add_argument("--baseline-frames", type=int, default=1, help="number of valid frames to median into the baseline")
    parser.add_argument("--stats-only", action="store_true", help="use Pico scanstat output instead of streaming raw samples")
    parser.add_argument("--binary-stat", action="store_true", help="use RA8D1 scanstatbin binary frames")
    parser.add_argument("--raw-pp-limit", type=int, default=180, help="scanraw pp_code threshold for Pico-side retry; 0 disables")
    parser.add_argument("--raw-retries", type=int, default=2, help="scanraw retry count for abnormal raw routes")
    parser.add_argument("--stat-pp-limit", type=int, default=180, help="scanstat pp_code threshold; 0 disables absolute pp filtering")
    parser.add_argument("--stat-retries", type=int, default=1, help="scanstat retry count for abnormal routes")
    parser.add_argument("--allow-partial", action="store_true", help="save a partial frame if serial capture times out")
    parser.add_argument("--save-raw", action="store_true", help="also save raw route samples for each latest frame")
    parser.add_argument("--latest-only", action="store_true", help="only update latest CSV/PNG instead of archiving every frame")
    parser.add_argument("--gain", nargs=2, type=int, metavar=("DRIVE", "MEAS"), help="optionally send 'g DRIVE MEAS' at startup")
    parser.add_argument("--no-power-init", action="store_true", help="do not send 'p 1 0 0' at startup")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def clean_line(line: str) -> str:
    return clean_protocol_line(
        line,
        ("FRAME_BEGIN", "STAT_BEGIN", "ROUTE", "END", "SCAN_DONE", "STAT_DONE", "ERR:", "bad command"),
    )


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


def capture_frame(ser: "serial.Serial", args: argparse.Namespace) -> Frame:
    command = "scanraw {} {} {} {} {} {}".format(
        args.electrodes,
        args.samples,
        args.settle_ms,
        args.rate,
        args.raw_pp_limit,
        args.raw_retries,
    )
    reader = SerialLineReader(ser, recent_limit=20)
    frame_id: int | None = None
    electrodes = args.electrodes
    sample_rate_hz = float(args.rate)
    samples_per_route = args.samples
    blocks: list[Block] = []
    current_route: Route | None = None
    current_samples: list[int] = []
    current_overrange: list[int] = []

    def expected_route_count() -> int:
        return max(0, electrodes * (electrodes - 3))

    ser.reset_input_buffer()
    write_command(ser, command)

    deadline = time.monotonic() + args.timeout
    while True:
        decoded = reader.read_line(deadline)
        if decoded is None:
            break
        deadline = time.monotonic() + args.timeout
        line = clean_line(decoded)
        if args.debug:
            print("serial:", repr(decoded))
        if not line:
            continue

        if line.startswith("ERR:") or line.startswith("bad command"):
            raise RuntimeError(line)

        if line.startswith("FRAME_BEGIN,"):
            parts = line.split(",")
            frame_id = int(parts[1])
            if len(parts) >= 5:
                electrodes = int(parts[2])
                samples_per_route = int(parts[3])
                sample_rate_hz = float(parts[4]) if float(parts[4]) > 0 else float(args.rate)
            continue

        if line.startswith("ROUTE,"):
            parts = line.split(",")
            current_route = Route(*(int(part) for part in parts[1:5]))
            current_samples = []
            current_overrange = []
            continue

        if line == "END":
            if current_route is not None:
                blocks.append(Block(current_route, current_samples, current_overrange))
            current_route = None
            current_samples = []
            current_overrange = []
            continue

        if line == "SCAN_DONE":
            if frame_id is None:
                raise RuntimeError("SCAN_DONE before FRAME_BEGIN")
            return Frame(frame_id, electrodes, samples_per_route, sample_rate_hz, blocks)

        if current_route is None:
            continue

        parts = line.split(",")
        if len(parts) == 3:
            current_samples.append(int(parts[1]))
            current_overrange.append(int(parts[2]))

    if args.allow_partial and frame_id is not None and blocks:
        print(
            "warning: timed out with partial frame: {}/{} routes".format(
                len(blocks),
                expected_route_count(),
            ),
            file=sys.stderr,
        )
        return Frame(frame_id, electrodes, samples_per_route, sample_rate_hz, blocks)

    detail = "received {}/{} routes".format(len(blocks), expected_route_count())
    if current_route is not None:
        detail += ", current route {}-{} {}-{} has {} samples".format(
            current_route.src,
            current_route.sink,
            current_route.vp,
            current_route.vn,
            len(current_samples),
        )
    raise TimeoutError(
        "Timed out waiting for SCAN_DONE; {}. Recent serial lines:\n{}".format(
            detail,
            reader.format_recent(),
        )
    )


def capture_stat_frame(ser: "serial.Serial", args: argparse.Namespace) -> tuple[Frame, list[dict[str, float | int]]]:
    command = "scanstat {} {} {} {} {} {}".format(
        args.electrodes,
        args.samples,
        args.settle_ms,
        args.rate,
        args.stat_pp_limit,
        args.stat_retries,
    )
    reader = SerialLineReader(ser, recent_limit=20)
    frame_id: int | None = None
    electrodes = args.electrodes
    samples_per_route = args.samples
    sample_rate_hz = float(args.rate)
    rows: list[dict[str, float | int]] = []
    scale = args.vref / 1023.0

    def expected_route_count() -> int:
        return max(0, electrodes * (electrodes - 3))

    ser.reset_input_buffer()
    write_command(ser, command)

    deadline = time.monotonic() + args.timeout
    while True:
        decoded = reader.read_line(deadline)
        if decoded is None:
            break
        deadline = time.monotonic() + args.timeout
        line = clean_line(decoded)
        if args.debug:
            print("serial:", repr(decoded))
        if not line:
            continue

        if line.startswith("ERR:") or line.startswith("bad command"):
            raise RuntimeError(line)

        if line.startswith("STAT_BEGIN,"):
            parts = line.split(",")
            frame_id = int(parts[1])
            if len(parts) >= 5:
                electrodes = int(parts[2])
                samples_per_route = int(parts[3])
                sample_rate_hz = float(parts[4]) if float(parts[4]) > 0 else float(args.rate)
            continue

        if line == "STAT_DONE":
            if frame_id is None:
                raise RuntimeError("STAT_DONE before STAT_BEGIN")
            return Frame(frame_id, electrodes, samples_per_route, sample_rate_hz, []), rows

        if line.startswith("route,"):
            continue

        parts = line.split(",")
        if len(parts) >= 11 and frame_id is not None:
            route_index = int(parts[0])
            src = int(parts[1])
            sink = int(parts[2])
            vp = int(parts[3])
            vn = int(parts[4])
            mean_code = float(parts[5])
            min_code = int(parts[6])
            max_code = int(parts[7])
            pp_code = float(parts[8])
            rms_code = float(parts[9])
            overrange_count = int(parts[10])
            valid_count = int(parts[11]) if len(parts) >= 12 else samples_per_route
            flags = int(parts[12]) if len(parts) >= 13 else 0
            retry_count = int(parts[13]) if len(parts) >= 14 else 0
            raw_flags = int(parts[14]) if len(parts) >= 15 else flags
            dc_v = mean_code * scale
            rms_v = rms_code * scale
            pp_v = pp_code * scale
            rows.append(
                {
                    "frame": frame_id,
                    "route_index": route_index,
                    "src": src,
                    "sink": sink,
                    "vp": vp,
                    "vn": vn,
                    "mean_code": mean_code,
                    "min_code": min_code,
                    "max_code": max_code,
                    "pp_code": pp_code,
                    "rms_code": rms_code,
                    "dc_v": dc_v,
                    "amp_v": rms_v * math.sqrt(2.0),
                    "phase_rad": 0.0,
                    "rms_v": rms_v,
                    "pp_v": pp_v,
                    "overrange_count": overrange_count,
                    "valid_count": valid_count,
                    "flags": flags,
                    "retry_count": retry_count,
                    "raw_flags": raw_flags,
                }
            )

    if args.allow_partial and frame_id is not None and rows:
        print(
            "warning: timed out with partial stat frame: {}/{} routes".format(len(rows), expected_route_count()),
            file=sys.stderr,
        )
        return Frame(frame_id, electrodes, samples_per_route, sample_rate_hz, []), rows

    raise TimeoutError(
        "Timed out waiting for STAT_DONE; received {}/{} routes. Recent serial lines:\n{}".format(
            len(rows),
            expected_route_count(),
            reader.format_recent(),
        )
    )


def capture_statbin_frame(ser: "serial.Serial", args: argparse.Namespace) -> tuple[Frame, list[dict[str, float | int]]]:
    command = "scanstatbin {} {} {} {} {} {}".format(
        args.electrodes,
        args.samples,
        args.settle_ms,
        args.rate,
        args.stat_pp_limit,
        args.stat_retries,
    )
    ser.reset_input_buffer()
    write_command(ser, command)
    binary_frame = read_scanstat_frame(ser, args.timeout)
    rows = scanstat_rows_as_dicts(binary_frame, args.vref)
    frame = Frame(
        binary_frame.frame_id,
        binary_frame.electrodes,
        binary_frame.samples,
        float(binary_frame.rate_hz),
        [],
    )
    return frame, rows


def sine_feature(samples: np.ndarray, sample_rate_hz: float, excitation_hz: float) -> tuple[float, float, float]:
    t = np.arange(samples.size, dtype=np.float64) / sample_rate_hz
    omega = 2.0 * math.pi * excitation_hz
    design = np.column_stack(
        (
            np.ones(samples.size, dtype=np.float64),
            np.sin(omega * t),
            np.cos(omega * t),
        )
    )
    beta = np.linalg.lstsq(design, samples.astype(np.float64), rcond=None)[0]
    dc = float(beta[0])
    amp = float(math.hypot(beta[1], beta[2]))
    phase = float(math.atan2(beta[2], beta[1]))
    return dc, amp, phase


def compute_features(frame: Frame, args: argparse.Namespace) -> list[dict[str, float | int]]:
    rows: list[dict[str, float | int]] = []
    scale = args.vref / 1023.0
    for route_index, block in enumerate(frame.blocks):
        samples = np.asarray(block.samples, dtype=np.float64)
        if samples.size == 0:
            continue

        volts = samples * scale
        centered = volts - float(np.mean(volts))
        dc, amp, phase = sine_feature(volts, frame.sample_rate_hz, args.excitation_hz)
        r = block.route
        rows.append(
            {
                "frame": frame.frame_id,
                "route_index": route_index,
                "src": r.src,
                "sink": r.sink,
                "vp": r.vp,
                "vn": r.vn,
                "mean_code": float(np.mean(samples)),
                "min_code": int(np.min(samples)),
                "max_code": int(np.max(samples)),
                "pp_code": float(np.max(samples) - np.min(samples)),
                "rms_code": float(np.sqrt(np.mean((samples - np.mean(samples)) ** 2))),
                "dc_v": dc,
                "amp_v": amp,
                "phase_rad": phase,
                "rms_v": float(np.sqrt(np.mean(centered * centered))),
                "pp_v": float(np.max(volts) - np.min(volts)),
                "overrange_count": int(sum(block.overrange)),
            }
        )
    return rows


def route_key(row: dict[str, float | int]) -> tuple[int, int, int, int]:
    return int(row["src"]), int(row["sink"]), int(row["vp"]), int(row["vn"])


def row_is_valid(row: dict[str, float | int]) -> bool:
    return (
        int(row.get("flags", 0)) == 0
        and int(row.get("raw_flags", 0)) == 0
        and int(row.get("retry_count", 0)) == 0
        and int(row.get("overrange_count", 0)) == 0
    )


def collect_baseline_values(
    samples: dict[tuple[int, int, int, int], list[float]],
    rows: list[dict[str, float | int]],
    field: str,
) -> int:
    added = 0
    for row in rows:
        if row_is_valid(row):
            samples.setdefault(route_key(row), []).append(float(row[field]))
            added += 1
    return added


def median_baseline(samples: dict[tuple[int, int, int, int], list[float]]) -> dict[tuple[int, int, int, int], float]:
    return {key: float(np.median(values)) for key, values in samples.items() if values}


def electrode_xy(index: int, electrodes: int) -> tuple[float, float]:
    angle = math.pi / 2.0 - 2.0 * math.pi * index / electrodes
    return math.cos(angle), math.sin(angle)


def tank_grid(
    rows: list[dict[str, float | int]],
    electrodes: int,
    field: str,
    baseline: dict[tuple[int, int, int, int], float] | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, float]:
    values = []
    centers = []
    for row in rows:
        if not row_is_valid(row):
            continue
        value = float(row[field])
        if baseline is not None:
            value -= baseline.get(route_key(row), value)
        values.append(value)

        sx, sy = electrode_xy(int(row["src"]), electrodes)
        kx, ky = electrode_xy(int(row["sink"]), electrodes)
        vx, vy = electrode_xy(int(row["vp"]), electrodes)
        nx, ny = electrode_xy(int(row["vn"]), electrodes)

        # Quick-look sensitivity center: midpoint between drive-pair center and measure-pair center.
        drive_x = 0.5 * (sx + kx)
        drive_y = 0.5 * (sy + ky)
        meas_x = 0.5 * (vx + nx)
        meas_y = 0.5 * (vy + ny)
        centers.append((0.5 * (drive_x + meas_x), 0.5 * (drive_y + meas_y)))

    if not values:
        empty = np.full((2, 2), np.nan, dtype=np.float64)
        axis = np.linspace(-1.05, 1.05, 2)
        return axis, axis, empty, 0.0, 1.0

    values_array = np.asarray(values, dtype=np.float64)
    center_value = float(np.median(values_array))
    if baseline is None:
        relative = values_array - center_value
    else:
        relative = values_array
    spread = float(np.nanpercentile(np.abs(relative), 95))
    if not math.isfinite(spread) or spread <= 0.0:
        spread = float(np.max(np.abs(relative))) if values_array.size else 1.0
    if spread <= 0.0:
        spread = 1.0

    grid_n = 240
    axis = np.linspace(-1.05, 1.05, grid_n)
    xx, yy = np.meshgrid(axis, axis)
    rr = np.sqrt(xx * xx + yy * yy)
    image = np.full_like(xx, np.nan, dtype=np.float64)
    acc = np.zeros_like(xx, dtype=np.float64)
    weight = np.zeros_like(xx, dtype=np.float64)
    mask = rr <= 1.0
    sigma = 0.32

    for (cx, cy), value in zip(centers, relative):
        normalized = value / spread
        gaussian = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2.0 * sigma * sigma)))
        acc += normalized * gaussian
        weight += gaussian

    image[mask] = acc[mask] / np.maximum(weight[mask], 1e-9)
    image = np.clip(image, -1.0, 1.0)
    return axis, axis, image, center_value, spread


def draw_tank(
    fig,
    rows: list[dict[str, float | int]],
    frame: Frame,
    args: argparse.Namespace,
    baseline: dict[tuple[int, int, int, int], float] | None,
) -> None:
    fig.clf()
    ax = fig.add_subplot(111)
    x_axis, y_axis, image, center_value, spread = tank_grid(rows, frame.electrodes, args.field, baseline)
    im = ax.imshow(
        image,
        extent=[x_axis[0], x_axis[-1], y_axis[0], y_axis[-1]],
        origin="lower",
        cmap="coolwarm",
        vmin=-1.0,
        vmax=1.0,
    )
    ax.add_patch(plt.Circle((0, 0), 1.0, edgecolor="black", facecolor="none", linewidth=2))

    for idx in range(frame.electrodes):
        x, y = electrode_xy(idx, frame.electrodes)
        ax.plot([0.92 * x, 1.08 * x], [0.92 * y, 1.08 * y], color="black", linewidth=2)
        ax.text(1.18 * x, 1.18 * y, "S{}".format(idx + 1), ha="center", va="center", fontsize=10)

    values = np.asarray([float(row[args.field]) for row in rows], dtype=np.float64)
    overrange = sum(int(row["overrange_count"]) for row in rows)
    invalid = sum(1 for row in rows if not row_is_valid(row))
    mode = "delta from baseline" if baseline is not None else "relative to frame median"
    ax.set_title(
        "EIT tank frame {} | {} | routes={} invalid={} | {} median={:.3e}, spread={:.3e}, overrange={}".format(
            frame.frame_id,
            mode,
            len(rows),
            invalid,
            args.field,
            float(np.median(values)) if values.size else 0.0,
            spread,
            overrange,
        )
    )
    ax.set_aspect("equal")
    ax.set_xlim(-1.3, 1.3)
    ax.set_ylim(-1.3, 1.3)
    ax.axis("off")
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="normalized {}".format(args.field))
    fig.tight_layout()


def save_features(path: Path, rows: list[dict[str, float | int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with path.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def save_raw(path: Path, frame: Frame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(("frame", "route_index", "src", "sink", "vp", "vn", "sample", "value", "overrange"))
        for route_index, block in enumerate(frame.blocks):
            r = block.route
            for i, value in enumerate(block.samples):
                overrange = block.overrange[i] if i < len(block.overrange) else 0
                writer.writerow((frame.frame_id, route_index, r.src, r.sink, r.vp, r.vn, i, value, overrange))


def main() -> int:
    if serial is None:
        raise RuntimeError("pyserial is not installed; install it with: python3 -m pip install pyserial")

    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    plt.ion()
    fig = plt.figure(figsize=(7.5, 7.2))
    baseline: dict[tuple[int, int, int, int], float] | None = None
    baseline_samples: dict[tuple[int, int, int, int], list[float]] = {}
    baseline_warmup_seen = 0
    baseline_frames_seen = 0
    frame_count = 0

    with serial.Serial(args.port, args.baud, timeout=0.2) as ser:
        time.sleep(0.2)
        init_board(ser, args)
        try:
            while True:
                if args.binary_stat:
                    frame, rows = capture_statbin_frame(ser, args)
                elif args.stats_only:
                    frame, rows = capture_stat_frame(ser, args)
                else:
                    frame = capture_frame(ser, args)
                    rows = compute_features(frame, args)

                if args.baseline and baseline is None:
                    if baseline_warmup_seen < args.baseline_warmup:
                        baseline_warmup_seen += 1
                        print(
                            "baseline warmup frame {}/{}: frame {}".format(
                                baseline_warmup_seen,
                                args.baseline_warmup,
                                frame.frame_id,
                            )
                        )
                    else:
                        baseline_frames_seen += 1
                        added = collect_baseline_values(baseline_samples, rows, args.field)
                        print(
                            "baseline frame {}/{}: frame {}, valid routes added={}".format(
                                baseline_frames_seen,
                                args.baseline_frames,
                                frame.frame_id,
                                added,
                            )
                        )
                        if baseline_frames_seen >= args.baseline_frames:
                            baseline = median_baseline(baseline_samples)
                            print(
                                "baseline captured from {} frame(s), routes={}".format(
                                    args.baseline_frames,
                                    len(baseline),
                                )
                            )

                draw_tank(fig, rows, frame, args, baseline)
                frame_prefix = "{}_{:06d}".format(args.prefix, frame.frame_id)
                frame_png = args.out_dir / "{}_tank.png".format(frame_prefix)
                frame_features = args.out_dir / "{}_features.csv".format(frame_prefix)
                latest_png = args.out_dir / "{}_latest_tank.png".format(args.prefix)
                latest_features = args.out_dir / "{}_latest_features.csv".format(args.prefix)
                if not args.latest_only:
                    fig.savefig(frame_png, dpi=150)
                fig.savefig(latest_png, dpi=150)
                if not args.latest_only:
                    save_features(frame_features, rows)
                save_features(latest_features, rows)
                if args.save_raw and frame.blocks:
                    if not args.latest_only:
                        save_raw(args.out_dir / "{}_raw.csv".format(frame_prefix), frame)
                    save_raw(args.out_dir / "{}_latest_raw.csv".format(args.prefix), frame)

                fig.canvas.draw_idle()
                fig.canvas.flush_events()
                plt.pause(0.001)

                amps = np.asarray([float(row[args.field]) for row in rows], dtype=np.float64)
                invalid = sum(1 for row in rows if not row_is_valid(row))
                overrange = sum(int(row["overrange_count"]) for row in rows)
                retries = sum(int(row.get("retry_count", 0)) for row in rows)
                print(
                    "frame {}: routes={}, invalid={}, retries={}, overrange={}, {} min/median/max={:.3e}/{:.3e}/{:.3e} -> {}".format(
                        frame.frame_id,
                        len(rows),
                        invalid,
                        retries,
                        overrange,
                        args.field,
                        float(np.min(amps)) if amps.size else 0.0,
                        float(np.median(amps)) if amps.size else 0.0,
                        float(np.max(amps)) if amps.size else 0.0,
                        latest_png if args.latest_only else frame_png,
                    )
                )

                frame_count += 1
                if args.once or (args.frames > 0 and frame_count >= args.frames):
                    break
                if args.interval > 0.0:
                    time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstop")
        except TimeoutError as exc:
            print("\ntimeout: {}".format(exc), file=sys.stderr)
            return 2

    plt.close(fig)
    return 0 if frame_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
