#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import os
import time
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

try:
    import serial
except ImportError:
    serial = None

try:
    import matplotlib.pyplot as plt
except ImportError:
    plt = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manually route ADG731 channels and capture raw ADC waveforms")
    parser.add_argument("--port", required=True, help="USB serial port, for example /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--electrodes", type=int, default=8)
    parser.add_argument("--samples", type=int, default=512)
    parser.add_argument("--rate", type=int, default=200000, help="ADC capture rate in samples/s")
    parser.add_argument("--settle-ms", type=float, default=20.0, help="delay after route setup before ADC capture")
    parser.add_argument("--gain", nargs=2, type=int, metavar=("DRIVE", "MEAS"), default=(512, 6))
    parser.add_argument("--out-dir", type=Path, default=Path("adc_manual_scan"))
    parser.add_argument("--prefix", default="ra8_adc")
    parser.add_argument("--max-routes", type=int, default=0, help="limit number of routes, 0 means all")
    parser.add_argument("--route", nargs=4, type=int, metavar=("SRC", "SINK", "VP", "VN"), help="capture one route only")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--no-power-init", action="store_true", help="do not send p 1 0 0 before capture")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-plot", action="store_true")
    return parser.parse_args()


def clean_line(line: str) -> str:
    for marker in ("ADC_BEGIN", "ADC_END", "ERR:", "bad command", "raw ok", "raw spi_error", "all mux off"):
        index = line.find(marker)
        if index >= 0:
            return line[index:]
    return line.strip()


def read_lines_for(ser: "serial.Serial", seconds: float, debug: bool = False) -> list[str]:
    lines: list[str] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        decoded = raw.decode("utf-8", errors="replace").rstrip()
        if debug:
            print("serial:", repr(decoded))
        lines.append(clean_line(decoded))
    return lines


def send_command(ser: "serial.Serial", command: str, wait_s: float, debug: bool = False) -> list[str]:
    ser.write((command + "\r\n").encode())
    ser.flush()
    time.sleep(wait_s)
    return read_lines_for(ser, 0.05, debug)


def capture_adc(ser: "serial.Serial", samples: int, rate: int, timeout: float, debug: bool = False) -> list[int]:
    values: list[int] = []
    recent: list[str] = []
    in_block = False

    ser.reset_input_buffer()
    ser.write(f"adc {samples} {rate}\r\n".encode())
    ser.flush()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        decoded = raw.decode("utf-8", errors="replace").rstrip()
        line = clean_line(decoded)
        recent.append(decoded)
        recent = recent[-16:]
        if debug:
            print("serial:", repr(decoded))

        if line.startswith("ERR:") or line.startswith("bad command"):
            raise RuntimeError(line)
        if line.startswith("ADC_BEGIN"):
            in_block = True
            continue
        if line.startswith("ADC_END"):
            return values
        if not in_block or line == "i,value":
            continue

        parts = line.split(",")
        if len(parts) == 2:
            values.append(int(parts[1], 0))

    raise TimeoutError(f"timed out waiting for ADC_END; captured {len(values)} samples\n" + "\n".join(recent))


def route_list(electrodes: int, one_route: list[int] | None, max_routes: int) -> list[tuple[int, int, int, int]]:
    if one_route is not None:
        return [tuple(one_route)]  # type: ignore[list-item]

    routes: list[tuple[int, int, int, int]] = []
    for src in range(electrodes):
        sink = (src + 1) % electrodes
        for vp in range(electrodes):
            vn = (vp + 1) % electrodes
            if vp in (src, sink) or vn in (src, sink):
                continue
            routes.append((src, sink, vp, vn))
            if max_routes and len(routes) >= max_routes:
                return routes
    return routes


def setup_route(ser: "serial.Serial", route: tuple[int, int, int, int], settle_ms: float, debug: bool = False) -> None:
    src, sink, vp, vn = route
    for command in (
        "off",
        f"raw src {src} 1",
        f"raw sink {sink} 1",
        f"raw vp {vp} 1",
        f"raw vn {vn} 1",
    ):
        lines = send_command(ser, command, 0.01, debug)
        if any("spi_error" in line or line.startswith("ERR:") for line in lines):
            raise RuntimeError(f"{command}: " + " | ".join(lines))
    if settle_ms > 0.0:
        time.sleep(settle_ms / 1000.0)


def metrics(values: list[int], rate: int, excite_hz: float = 10000.0) -> dict[str, float]:
    count = len(values)
    mean = sum(values) / count
    rms = math.sqrt(sum((value - mean) ** 2 for value in values) / count)
    cos_sum = 0.0
    sin_sum = 0.0
    for i, value in enumerate(values):
        y = value - mean
        phase = 2.0 * math.pi * excite_hz * i / rate
        cos_sum += y * math.cos(phase)
        sin_sum += y * math.sin(phase)
    amp10k = 2.0 * math.sqrt(cos_sum * cos_sum + sin_sum * sin_sum) / count
    return {
        "mean": mean,
        "rms": rms,
        "amp10k": amp10k,
        "min": float(min(values)),
        "max": float(max(values)),
        "pp": float(max(values) - min(values)),
    }


def save_csvs(out_dir: Path, prefix: str, rows: list[dict[str, float]], waves: list[tuple[tuple[int, int, int, int], list[int]]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = out_dir / f"{prefix}_metrics.csv"
    samples_path = out_dir / f"{prefix}_samples.csv"

    with metrics_path.open("w", newline="") as fp:
        fieldnames = ["route", "src", "sink", "vp", "vn", "mean", "rms", "amp10k", "min", "max", "pp"]
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with samples_path.open("w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(("route", "src", "sink", "vp", "vn", "i", "value"))
        for route_index, (route, values) in enumerate(waves):
            src, sink, vp, vn = route
            for i, value in enumerate(values):
                writer.writerow((route_index, src, sink, vp, vn, i, value))


def save_plots(out_dir: Path, prefix: str, rows: list[dict[str, float]], waves: list[tuple[tuple[int, int, int, int], list[int]]], rate: int) -> None:
    if plt is None or not waves:
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    overlay_path = out_dir / f"{prefix}_overlay_dc_removed.png"
    grid_path = out_dir / f"{prefix}_grid.png"

    plt.figure(figsize=(12, 7))
    for index, (route, values) in enumerate(waves):
        mean = sum(values) / len(values)
        x_ms = [i / rate * 1000.0 for i in range(len(values))]
        label = f"{index}: {route[0]}-{route[1]} {route[2]}-{route[3]}"
        plt.plot(x_ms, [value - mean for value in values], linewidth=1.0, label=label)
    plt.xlabel("time (ms)")
    plt.ylabel("ADC code - route mean")
    plt.title("Manual raw ADC scan, DC removed")
    plt.grid(True, alpha=0.3)
    plt.legend(ncol=2, fontsize=8)
    plt.tight_layout()
    plt.savefig(overlay_path, dpi=150)
    plt.close()

    plot_count = min(len(waves), 16)
    cols = 2
    rows_count = math.ceil(plot_count / cols)
    fig, axes = plt.subplots(rows_count, cols, figsize=(12, max(4, rows_count * 2.2)), squeeze=False)
    axes_flat = [axis for row_axes in axes for axis in row_axes]
    for axis in axes_flat[plot_count:]:
        axis.axis("off")
    for index, (axis, (route, values)) in enumerate(zip(axes_flat, waves[:plot_count])):
        x_ms = [i / rate * 1000.0 for i in range(len(values))]
        stat = rows[index]
        axis.plot(x_ms, values, linewidth=1.0)
        axis.set_title(
            f"{index}: s{route[0]} k{route[1]} v{route[2]} n{route[3]} "
            f"rms={stat['rms']:.2f} a10k={stat['amp10k']:.2f}",
            fontsize=9,
        )
        axis.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(grid_path, dpi=150)
    plt.close(fig)


def main() -> int:
    if serial is None:
        raise RuntimeError("pyserial is not installed; install it with: python3 -m pip install pyserial")

    args = parse_args()
    routes = route_list(args.electrodes, args.route, args.max_routes)
    if not routes:
        raise RuntimeError("no routes selected")

    rows: list[dict[str, float]] = []
    waves: list[tuple[tuple[int, int, int, int], list[int]]] = []

    with serial.Serial(args.port, args.baud, timeout=0.2) as ser:
        time.sleep(0.5)
        ser.reset_input_buffer()
        print("ver:", " | ".join(send_command(ser, "ver", 0.05, args.debug)))
        if not args.no_power_init:
            print("power:", " | ".join(send_command(ser, "p 1 0 0", 0.2, args.debug)))
        drive, meas = args.gain
        print("gain:", " | ".join(send_command(ser, f"g {drive} {meas}", 0.2, args.debug)))

        for route_index, route in enumerate(routes):
            setup_route(ser, route, args.settle_ms, args.debug)
            values = capture_adc(ser, args.samples, args.rate, args.timeout, args.debug)
            send_command(ser, "off", 0.02, args.debug)
            stat = metrics(values, args.rate)
            src, sink, vp, vn = route
            row = {
                "route": float(route_index),
                "src": float(src),
                "sink": float(sink),
                "vp": float(vp),
                "vn": float(vn),
                **stat,
            }
            rows.append(row)
            waves.append((route, values))
            print(
                "route {:02d} src={} sink={} vp={} vn={} mean={:.3f} rms={:.3f} amp10k={:.3f} pp={:.0f}".format(
                    route_index,
                    src,
                    sink,
                    vp,
                    vn,
                    stat["mean"],
                    stat["rms"],
                    stat["amp10k"],
                    stat["pp"],
                )
            )

        send_command(ser, "off", 0.02, args.debug)

    save_csvs(args.out_dir, args.prefix, rows, waves)
    if not args.no_plot:
        save_plots(args.out_dir, args.prefix, rows, waves, args.rate)
    print(f"wrote {args.out_dir / (args.prefix + '_metrics.csv')}")
    print(f"wrote {args.out_dir / (args.prefix + '_samples.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
