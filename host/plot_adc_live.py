#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")

import matplotlib.pyplot as plt

try:
    import serial
except ImportError:
    serial = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Continuously capture and plot Pico PIO DMA ADC data")
    parser.add_argument("--port", required=True, help="USB serial port, for example /dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--samples", type=int, default=2048)
    parser.add_argument("--rate", type=int, default=200000, help="PIO ADC readout rate in samples/s")
    parser.add_argument("--csv", type=Path, default=Path("adc_live.csv"))
    parser.add_argument("--png", type=Path, default=Path("adc_live.png"))
    parser.add_argument("--vref", type=float, default=3.3, help="ADC reference voltage used for displayed voltage estimate")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--interval", type=float, default=0.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--auto-y", action="store_true", help="auto-scale the plot Y axis around the captured data")
    parser.add_argument("--no-power-init", action="store_true", help="do not send 'p 1 0 0' before the first capture")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def clean_line(line: str) -> str:
    for marker in ("ADC_BEGIN", "ADC_END", "ERR:", "bad command"):
        index = line.find(marker)
        if index >= 0:
            return line[index:]
    return line.strip()


def capture_adc(ser: "serial.Serial", args: argparse.Namespace) -> tuple[list[int], int]:
    command = "adc {} {}\r\n".format(args.samples, args.rate).encode()
    values: list[int] = []
    in_block = False
    reported_rate = args.rate
    recent: list[str] = []

    ser.reset_input_buffer()
    ser.write(b"\r\n")
    ser.flush()
    time.sleep(0.02)
    ser.reset_input_buffer()
    ser.write(command)
    ser.flush()

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        decoded = raw.decode("utf-8", errors="replace").rstrip()
        line = clean_line(decoded)
        recent.append(decoded)
        recent = recent[-16:]
        if args.debug:
            print("serial:", repr(decoded))

        if line.startswith("ERR:") or line.startswith("bad command"):
            raise RuntimeError(line)
        if line.startswith("ADC_BEGIN"):
            parts = line.split(",")
            if len(parts) >= 3:
                reported_rate = int(parts[2])
            in_block = True
            continue
        if line.startswith("ADC_END"):
            return values, reported_rate
        if not in_block or line == "i,value":
            continue

        parts = line.split(",")
        if len(parts) == 2:
            values.append(int(parts[1], 0))

    raise TimeoutError("Timed out before ADC_END; captured {} samples\n{}".format(len(values), "\n".join(recent)))


def save_csv(path: Path, values: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(("i", "value"))
        for i, value in enumerate(values):
            writer.writerow((i, value))


def send_power_init(ser: "serial.Serial", args: argparse.Namespace) -> None:
    ser.reset_input_buffer()
    ser.write(b"p 1 0 0\r\n")
    ser.flush()

    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        decoded = raw.decode("utf-8", errors="replace").rstrip()
        if args.debug:
            print("serial:", repr(decoded))
        if "power ok" in decoded:
            return


def update_plot(fig, ax, line, values: list[int], rate: int, args: argparse.Namespace) -> None:
    if rate <= 0:
        x = list(range(len(values)))
        xlabel = "sample"
    else:
        x = [i / rate * 10.0 for i in range(len(values))]
        xlabel = "time"

    min_code = min(values)
    max_code = max(values)
    mean_code = sum(values) / len(values)
    pp_code = max_code - min_code
    mean_v = mean_code / 1023.0 * args.vref
    pp_v = pp_code / 1023.0 * args.vref

    line.set_data(x, values)
    ax.set_xlim(x[0] if x else 0, x[-1] if x else 1)
    if args.auto_y:
        margin = max(8, pp_code * 0.15)
        ax.set_ylim(max(0, min_code - margin), min(1023, max_code + margin))
    else:
        ax.set_ylim(-10, 1033)
    ax.set_title(
        "ADC live: n={}, rate={} S/s, mean={:.1f} ({:.3f} V), pp={} ({:.3f} Vpp)".format(
            len(values),
            rate,
            mean_code,
            mean_v,
            pp_code,
            pp_v,
        )
    )
    ax.set_xlabel(xlabel)
    ax.set_ylabel("ADC code")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()


def main() -> int:
    if serial is None:
        raise RuntimeError("pyserial is not installed; install it with: python3 -m pip install pyserial")

    args = parse_args()
    plt.ion()
    fig, ax = plt.subplots(figsize=(10, 4))
    (line,) = ax.plot([], [], linewidth=1.0)

    with serial.Serial(args.port, args.baud, timeout=0.2) as ser:
        time.sleep(0.2)
        frame = 0
        try:
            while True:
                values, rate = capture_adc(ser, args)
                if not values:
                    print("No samples captured", file=sys.stderr)
                    return 1
                save_csv(args.csv, values)
                update_plot(fig, ax, line, values, rate, args)
                args.png.parent.mkdir(parents=True, exist_ok=True)
                fig.savefig(args.png, dpi=150)
                fig.canvas.draw_idle()
                fig.canvas.flush_events()
                plt.pause(0.001)
                frame += 1
                print(
                    "frame {}: {} samples at {} S/s, min={}, max={}, pp={} -> {}, {}".format(
                        frame,
                        len(values),
                        rate,
                        min(values),
                        max(values),
                        max(values) - min(values),
                        args.csv,
                        args.png,
                    )
                )
                if args.once:
                    break
                if args.interval > 0.0:
                    time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstop")

    plt.close(fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
