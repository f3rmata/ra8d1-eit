#!/usr/bin/env python3
"""
Sweep gain settings across electrode excitation pairs and plot ADC waveforms.

Use this to tune AD5270 drive/meas gain by visually comparing signal quality:
- Too hot (clipping at 0/1023) → reduce drive gain or increase meas gain
- Too weak (small AC swing)          → increase drive gain or reduce meas gain

Typical usage:
  # Compare a few excitation pairs at default gain
  .venv/bin/python3 host/gain_sweep.py --port /dev/ttyACM0

  # Sweep gain on all 8 excitation pairs
  .venv/bin/python3 host/gain_sweep.py --port /dev/ttyACM0 --excitation all \
      --gain 512 6 --gain 512 64 --gain 512 128 --gain 512 512

  # Focus on one excitation pair, fine-sweep drive gain
  .venv/bin/python3 host/gain_sweep.py --port /dev/ttyACM0 \
      --excitation 0 1 --excitation 4 5 \
      --gain 256 6 --gain 384 6 --gain 512 6 --gain 640 6 --gain 768 6

  # Use a fixed measurement pair
  .venv/bin/python3 host/gain_sweep.py --port /dev/ttyACM0 \
      --excitation all --measure 2 3 --gain 512 6 --gain 512 64
"""

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
    raise RuntimeError("matplotlib is required; use the project .venv") from exc

try:
    import serial
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("pyserial is required; use the project .venv") from exc

from serial_lines import SerialLineReader, clean_protocol_line


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExPair:
    """An excitation pair: src drives current, sink drains it."""
    src: int
    sink: int


@dataclass(frozen=True)
class MeasPair:
    """A measurement pair: vp is sense+, vn is sense-."""
    vp: int
    vn: int


@dataclass(frozen=True)
class Capture:
    gain_drive: int
    gain_meas: int
    excitation: ExPair
    measure: MeasPair
    samples: np.ndarray
    mean: float
    rms: float
    amp10k: float
    min_code: int
    max_code: int
    pp_code: int
    rail_count: int


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sweep gain across excitation pairs and plot ADC waveforms for gain tuning",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick check: all 8 excitation pairs at one gain
  %(prog)s --port /dev/ttyACM0

  # Compare two gain settings across all excitation pairs
  %(prog)s --port /dev/ttyACM0 --excitation all --gain 512 6 --gain 512 128

  # Sweep drive gain on a single excitation pair
  %(prog)s --port /dev/ttyACM0 --excitation 0 1 \\
      --gain 128 6 --gain 256 6 --gain 384 6 --gain 512 6 --gain 768 6

  # Fixed measurement pair, sweep excitation pairs
  %(prog)s --port /dev/ttyACM0 --excitation all --measure 2 3 \\
      --gain 512 6 --gain 512 128
        """,
    )
    p.add_argument("--port", default="/dev/ttyACM0", help="serial port")
    p.add_argument("--baud", type=int, default=460800)
    p.add_argument("--electrodes", type=int, default=8)
    p.add_argument("--samples", type=int, default=512, help="ADC samples per capture")
    p.add_argument("--rate", type=int, default=200000, help="sample rate (Hz)")
    p.add_argument("--settle-ms", type=float, default=20.0, help="delay after mux setup before ADC capture")
    p.add_argument("--excite-hz", type=float, default=10000.0, help="excitation frequency for amp10k metric")
    p.add_argument(
        "--all-excitations", action="store_true",
        help="sweep all adjacent excitation pairs (0,1), (1,2), ..., (N-1,0)",
    )
    p.add_argument(
        "--excitation", nargs=2, action="append", type=int, metavar=("SRC", "SINK"),
        help="excitation pair; repeat for multiple",
    )
    p.add_argument(
        "--measure", nargs=2, type=int, metavar=("VP", "VN"),
        default=None,
        help="fixed measurement pair; default: auto-pick next adjacent pair for each excitation",
    )
    p.add_argument(
        "--gain", nargs=2, action="append", type=int, metavar=("DRIVE", "MEAS"),
        help="gain pair (0-1023); repeat for sweep. Default: 512 6",
    )
    p.add_argument("--out-dir", type=Path, default=Path("diagnostics/gain_sweep"))
    p.add_argument("--prefix", default="sweep")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--no-power-init", action="store_true")
    p.add_argument("--reset", action="store_true", help="pyOCD reset before capture")
    p.add_argument("--pyocd", default="/home/fermata/.local/share/pipx/venvs/pyocd/bin/pyocd")
    p.add_argument("--target", default="r7fa8d1bh")
    p.add_argument("--uid", default="0F7A117605A6")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Excitation / measurement pair helpers
# ---------------------------------------------------------------------------

def all_excitation_pairs(electrodes: int) -> list[ExPair]:
    """Adjacent-drive excitation pairs: (0,1), (1,2), ..., (N-1, 0)."""
    return [ExPair(src, (src + 1) % electrodes) for src in range(electrodes)]


def default_measure_pair(excitation: ExPair, electrodes: int) -> MeasPair:
    """Pick the next adjacent pair that does not overlap with excitation."""
    for offset in range(1, electrodes):
        vp = (excitation.sink + offset) % electrodes
        vn = (vp + 1) % electrodes
        if vp not in (excitation.src, excitation.sink) and vn not in (excitation.src, excitation.sink):
            return MeasPair(vp, vn)
    # fallback — should not happen with >=4 electrodes
    return MeasPair((excitation.sink + 1) % electrodes, (excitation.sink + 2) % electrodes)


def resolve_excitations(args: argparse.Namespace) -> list[ExPair]:
    """Parse --excitation / --all-excitations arguments."""
    if args.all_excitations:
        return all_excitation_pairs(args.electrodes)

    if args.excitation is None:
        # Default: sweep all adjacent pairs
        return all_excitation_pairs(args.electrodes)

    pairs: list[ExPair] = []
    seen: set[tuple[int, int]] = set()
    for src, sink in args.excitation:
        if (src, sink) not in seen:
            seen.add((src, sink))
            pairs.append(ExPair(src, sink))
    return pairs


def resolve_measures(args: argparse.Namespace, excitations: list[ExPair]) -> list[MeasPair]:
    """If --measure is given, use it for all; otherwise auto-pick."""
    if args.measure is not None:
        return [MeasPair(args.measure[0], args.measure[1])] * len(excitations)
    return [default_measure_pair(ex, args.electrodes) for ex in excitations]


# ---------------------------------------------------------------------------
# Serial helpers
# ---------------------------------------------------------------------------

def clean_line(line: str) -> str:
    markers = (
        "ADC_BEGIN", "ADC_END", "ERR:", "bad command",
        "raw ok", "raw spi_error", "all mux off", "power ok", "gain drive=",
    )
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
            if start_marker is not None and line.startswith(start_marker):
                started = True
            else:
                continue
        lines.append(line)
        if line.startswith("ERR:") or line.startswith("bad command"):
            raise RuntimeError(line)
        if line == end_marker or line.startswith(end_marker):
            return lines
    raise TimeoutError(
        "timed out waiting for {} after {!r}; recent:\n{}".format(
            end_marker, command, "\n".join(lines[-12:])
        )
    )


def pyocd_reset(args: argparse.Namespace) -> None:
    cmd = [args.pyocd, "reset", "--target", args.target, "--uid", args.uid]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10.0)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("warning: pyOCD reset failed ({}); continuing".format(exc), flush=True)


# ---------------------------------------------------------------------------
# Board control
# ---------------------------------------------------------------------------

def init_board(ser: "serial.Serial", args: argparse.Namespace) -> None:
    time.sleep(1.0)
    drain_idle(ser, idle_s=0.3, max_s=3.0, debug=args.debug)
    if not args.no_power_init:
        run_until(ser, "p 1 0 0", "power ok", 5.0, args.debug)


def set_gain(ser: "serial.Serial", drive: int, meas: int, args: argparse.Namespace) -> None:
    run_until(ser, "g {} {}".format(drive, meas), "gain drive=", 5.0, args.debug)


def setup_route(
    ser: "serial.Serial",
    excitation: ExPair,
    measure: MeasPair,
    settle_ms: float,
    args: argparse.Namespace,
) -> None:
    for command in (
        "off",
        "raw src {} 1".format(excitation.src),
        "raw sink {} 1".format(excitation.sink),
        "raw vp {} 1".format(measure.vp),
        "raw vn {} 1".format(measure.vn),
    ):
        lines = run_until(
            ser, command,
            "raw ok" if command.startswith("raw ") else "all mux off",
            5.0, args.debug,
        )
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
        raise RuntimeError(
            "captured {} ADC samples, expected {}".format(len(values), args.samples)
        )
    return np.asarray(values, dtype=np.float64)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(
    samples: np.ndarray, rate: int, excite_hz: float
) -> tuple[float, float, float, int, int, int, int]:
    mean = float(np.mean(samples))
    centered = samples - mean
    rms = float(np.sqrt(np.mean(centered * centered)))

    # 10 kHz lock-in amplitude
    indices = np.arange(len(samples), dtype=np.float64)
    phase = 2.0 * math.pi * excite_hz * indices / float(rate)
    cos_sum = float(np.sum(centered * np.cos(phase)))
    sin_sum = float(np.sum(centered * np.sin(phase)))
    amp10k = 2.0 * math.sqrt(cos_sum * cos_sum + sin_sum * sin_sum) / float(len(samples))

    min_code = int(np.min(samples))
    max_code = int(np.max(samples))
    pp_code = max_code - min_code
    rail_count = int(np.count_nonzero((samples <= 2.0) | (samples >= 1021.0)))
    return mean, rms, amp10k, min_code, max_code, pp_code, rail_count


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def ex_label(ex: ExPair) -> str:
    return "S{}+ S{}-".format(ex.src + 1, ex.sink + 1)


def meas_label(meas: MeasPair) -> str:
    return "V{}+ V{}-".format(meas.vp + 1, meas.vn + 1)


def short_label(gain_drive: int, gain_meas: int) -> str:
    return "g{},{}".format(gain_drive, gain_meas)


def plot_waveform_grid(
    captures: list[Capture],
    excitations: list[ExPair],
    measures: list[MeasPair],
    gains: list[tuple[int, int]],
    out_dir: Path,
    prefix: str,
    rate: int,
) -> Path:
    """Grid: rows = excitation pair, columns = gain setting."""
    by_key: dict[tuple[int, int, int, int, int, int], Capture] = {}
    for cap in captures:
        key = (cap.gain_drive, cap.gain_meas, cap.excitation.src, cap.excitation.sink,
               cap.measure.vp, cap.measure.vn)
        by_key[key] = cap

    n_rows = len(excitations)
    n_cols = len(gains)

    fig, axes = plt.subplots(
        n_rows, n_cols,
        figsize=(max(5.5, n_cols * 4.2), max(3.0, n_rows * 2.6)),
        squeeze=False, sharex=True, sharey=True,
    )

    for row, (ex, meas) in enumerate(zip(excitations, measures)):
        for col, (drive, meas_gain) in enumerate(gains):
            ax = axes[row][col]
            key = (drive, meas_gain, ex.src, ex.sink, meas.vp, meas.vn)
            cap = by_key.get(key)

            if cap is None:
                ax.text(0.5, 0.5, "no data", transform=ax.transAxes,
                        ha="center", va="center", fontsize=10, color="gray")
                ax.set_title("{} | {}".format(ex_label(ex), short_label(drive, meas_gain)),
                             fontsize=7.5)
                continue

            x_ms = np.arange(len(cap.samples), dtype=np.float64) / float(rate) * 1000.0
            ax.plot(x_ms, cap.samples, lw=1.0, color="#1f77b4")

            # Rail markers
            ax.axhline(2, color="red", lw=0.6, alpha=0.4, ls="--")
            ax.axhline(1021, color="red", lw=0.6, alpha=0.4, ls="--")
            # Mid-scale
            ax.axhline(512, color="gray", lw=0.4, alpha=0.3, ls=":")

            ax.grid(True, alpha=0.2)
            ax.set_title(
                "{} | {} — pp={} rms={:.1f}".format(
                    ex_label(ex), short_label(drive, meas_gain),
                    cap.pp_code, cap.rms,
                ),
                fontsize=7.5,
            )

            # Color title red if clipping
            if cap.rail_count > 0:
                ax.set_title(
                    "{} | {} — pp={} rms={:.1f} RAILS={}".format(
                        ex_label(ex), short_label(drive, meas_gain),
                        cap.pp_code, cap.rms, cap.rail_count,
                    ),
                    fontsize=7.5, color="red",
                )

        # Row label
        axes[row][0].set_ylabel(
            "{}\n{}\nADC code".format(ex_label(ex), meas_label(meas)),
            fontsize=8,
        )

    for col in range(n_cols):
        axes[-1][col].set_xlabel("time (ms)")

    fig.tight_layout()
    path = out_dir / "{}_grid.png".format(prefix)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def plot_overlay(
    captures: list[Capture],
    excitations: list[ExPair],
    measures: list[MeasPair],
    gains: list[tuple[int, int]],
    out_dir: Path,
    prefix: str,
    rate: int,
) -> Path:
    """DC-removed overlay: one subplot per excitation, all gains overlaid."""
    by_key: dict[tuple[int, int, int, int, int, int], Capture] = {}
    for cap in captures:
        key = (cap.gain_drive, cap.gain_meas, cap.excitation.src, cap.excitation.sink,
               cap.measure.vp, cap.measure.vn)
        by_key[key] = cap

    n_rows = len(excitations)
    fig, axes = plt.subplots(n_rows, 1, figsize=(12.0, max(3.2, n_rows * 2.8)),
                              squeeze=False, sharex=True)

    for row, (ex, meas) in enumerate(zip(excitations, measures)):
        ax = axes[row][0]
        for drive, meas_gain in gains:
            key = (drive, meas_gain, ex.src, ex.sink, meas.vp, meas.vn)
            cap = by_key.get(key)
            if cap is None:
                continue
            x_ms = np.arange(len(cap.samples), dtype=np.float64) / float(rate) * 1000.0
            label = "{} pp={} rms={:.1f} rails={}".format(
                short_label(drive, meas_gain), cap.pp_code, cap.rms, cap.rail_count,
            )
            ax.plot(x_ms, cap.samples - cap.mean, lw=1.0, label=label)
        ax.set_title("{}  |  {}".format(ex_label(ex), meas_label(meas)), fontsize=9)
        ax.set_ylabel("code - mean")
        ax.grid(True, alpha=0.2)
        ax.legend(fontsize=8, ncol=min(3, len(gains)))
    axes[-1][0].set_xlabel("time (ms)")
    fig.tight_layout()
    path = out_dir / "{}_overlay.png".format(prefix)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def plot_summary_chart(
    captures: list[Capture],
    excitations: list[ExPair],
    gains: list[tuple[int, int]],
    out_dir: Path,
    prefix: str,
) -> Path:
    """Summary bar charts: pp and rms per excitation, grouped by gain."""
    n_ex = len(excitations)
    n_gains = len(gains)
    if n_gains < 2:
        return Path()  # skip — no comparison needed

    # Build matrix: [gain_idx, ex_idx]
    pp_matrix = np.zeros((n_gains, n_ex))
    rms_matrix = np.zeros((n_gains, n_ex))
    rail_matrix = np.zeros((n_gains, n_ex), dtype=int)

    by_key: dict[tuple[int, int, int, int], Capture] = {}
    for cap in captures:
        key = (cap.gain_drive, cap.gain_meas, cap.excitation.src, cap.excitation.sink)
        by_key[key] = cap

    for gi, (drive, meas_g) in enumerate(gains):
        for ei, ex in enumerate(excitations):
            cap = by_key.get((drive, meas_g, ex.src, ex.sink))
            if cap is not None:
                pp_matrix[gi, ei] = cap.pp_code
                rms_matrix[gi, ei] = cap.rms
                rail_matrix[gi, ei] = cap.rail_count

    fig, (ax_pp, ax_rms) = plt.subplots(2, 1, figsize=(max(6, n_ex * 1.2), 7))

    x_pos = np.arange(n_ex)
    bar_width = 0.8 / n_gains
    cmap = plt.get_cmap("viridis")
    colors = cmap(np.linspace(0.1, 0.9, n_gains))

    for gi, (drive, meas_g) in enumerate(gains):
        offset = (gi - n_gains / 2.0 + 0.5) * bar_width
        label = short_label(drive, meas_g)
        bars = ax_pp.bar(x_pos + offset, pp_matrix[gi],
                         bar_width * 0.85, label=label, color=colors[gi])
        # Hatch bars that have rail hits
        for bi, bar in enumerate(bars):
            if rail_matrix[gi, ei := bi] > 0:  # type: ignore[name-defined]
                bar.set_hatch("///")
                bar.set_edgecolor("red")
        ax_rms.bar(x_pos + offset, rms_matrix[gi],
                   bar_width * 0.85, label=label, color=colors[gi])

    ax_pp.set_ylabel("peak-to-peak (codes)")
    ax_pp.set_title("Peak-to-peak amplitude by excitation pair")
    ax_pp.set_xticks(x_pos)
    ax_pp.set_xticklabels([ex_label(ex) for ex in excitations], fontsize=8)
    ax_pp.legend(fontsize=8)
    ax_pp.grid(True, alpha=0.2, axis="y")

    ax_rms.set_ylabel("RMS (codes)")
    ax_rms.set_title("AC RMS by excitation pair")
    ax_rms.set_xticks(x_pos)
    ax_rms.set_xticklabels([ex_label(ex) for ex in excitations], fontsize=8)
    ax_rms.legend(fontsize=8)
    ax_rms.grid(True, alpha=0.2, axis="y")

    fig.tight_layout()
    path = out_dir / "{}_summary.png".format(prefix)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def save_csvs(captures: list[Capture], out_dir: Path, prefix: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = out_dir / "{}_metrics.csv".format(prefix)
    samples_path = out_dir / "{}_samples.csv".format(prefix)

    fieldnames = [
        "gain_drive", "gain_meas", "src", "sink", "vp", "vn",
        "mean", "rms", "amp10k", "min", "max", "pp", "rail_count",
    ]
    with metrics_path.open("w", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        for cap in captures:
            writer.writerow({
                "gain_drive": cap.gain_drive,
                "gain_meas": cap.gain_meas,
                "src": cap.excitation.src,
                "sink": cap.excitation.sink,
                "vp": cap.measure.vp,
                "vn": cap.measure.vn,
                "mean": "{:.6f}".format(cap.mean),
                "rms": "{:.6f}".format(cap.rms),
                "amp10k": "{:.6f}".format(cap.amp10k),
                "min": cap.min_code,
                "max": cap.max_code,
                "pp": cap.pp_code,
                "rail_count": cap.rail_count,
            })

    with samples_path.open("w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["gain_drive", "gain_meas", "src", "sink", "vp", "vn", "i", "value"])
        for cap in captures:
            for i, value in enumerate(cap.samples.astype(int)):
                writer.writerow([
                    cap.gain_drive, cap.gain_meas,
                    cap.excitation.src, cap.excitation.sink,
                    cap.measure.vp, cap.measure.vn,
                    i, value,
                ])

    return metrics_path, samples_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    gains: list[tuple[int, int]] = (
        [tuple(g) for g in args.gain] if args.gain else [(512, 6)]
    )
    excitations = resolve_excitations(args)
    measures = resolve_measures(args, excitations)

    print("Excitation pairs ({}):".format(len(excitations)))
    for ex, meas in zip(excitations, measures):
        print("  {} -> {}".format(ex_label(ex), meas_label(meas)))
    print("Gain sweep ({}): {}".format(len(gains), [short_label(d, m) for d, m in gains]))
    print()

    if args.reset:
        pyocd_reset(args)

    captures: list[Capture] = []
    with serial.Serial(args.port, args.baud, timeout=0.5) as ser:
        init_board(ser, args)

        for drive, meas_g in gains:
            set_gain(ser, drive, meas_g, args)
            for ex, meas in zip(excitations, measures):
                setup_route(ser, ex, meas, args.settle_ms, args)
                samples = capture_adc(ser, args)
                mean, rms, amp10k, min_code, max_code, pp_code, rail_count = (
                    compute_metrics(samples, args.rate, args.excite_hz)
                )
                cap = Capture(
                    drive, meas_g, ex, meas, samples,
                    mean, rms, amp10k, min_code, max_code, pp_code, rail_count,
                )
                captures.append(cap)

                # Color-code terminal output
                status = ""
                if rail_count > 0:
                    status = " CLIP"
                elif pp_code < 10:
                    status = " WEAK"
                elif pp_code > 900:
                    status = " HOT"
                else:
                    status = "  OK"

                print(
                    "{} {} {:<9s} {:<9s}: mean={:7.2f} rms={:7.2f} pp={:4d} rails={:3d}/{}{}".format(
                        short_label(drive, meas_g),
                        status,
                        ex_label(ex),
                        meas_label(meas),
                        mean, rms, pp_code, rail_count, args.samples, status,
                    ),
                    flush=True,
                )

        # All off
        run_until(ser, "off", "all mux off", 5.0, args.debug)

    # Save
    metrics_path, samples_path = save_csvs(captures, args.out_dir, args.prefix)
    print("\nwrote {}".format(metrics_path))
    print("wrote {}".format(samples_path))

    # Plot
    grid_path = plot_waveform_grid(captures, excitations, measures, gains,
                                   args.out_dir, args.prefix, args.rate)
    print("wrote {}".format(grid_path))

    overlay_path = plot_overlay(captures, excitations, measures, gains,
                                 args.out_dir, args.prefix, args.rate)
    print("wrote {}".format(overlay_path))

    if len(gains) >= 2:
        summary_path = plot_summary_chart(captures, excitations, gains,
                                          args.out_dir, args.prefix)
        if summary_path != Path():
            print("wrote {}".format(summary_path))

    # Gain tuning guidance
    clip_excitations = set()
    weak_excitations = set()
    for cap in captures:
        if cap.rail_count > 0:
            clip_excitations.add((cap.gain_drive, cap.gain_meas, cap.excitation.src, cap.excitation.sink))
        if cap.pp_code < 10:
            weak_excitations.add((cap.gain_drive, cap.gain_meas, cap.excitation.src, cap.excitation.sink))

    if clip_excitations:
        print("\n⚠  Clipping detected (rail hits). Consider:")
        print("   - Reduce drive gain, or increase meas gain")
        for drive, meas_g, src, sink in sorted(clip_excitations):
            print("     g={},{}  excitation S{}+ S{}-".format(drive, meas_g, src + 1, sink + 1))

    if weak_excitations:
        print("\n⚠  Weak signal detected (pp < 10). Consider:")
        print("   - Increase drive gain, or reduce meas gain")
        for drive, meas_g, src, sink in sorted(weak_excitations):
            print("     g={},{}  excitation S{}+ S{}-".format(drive, meas_g, src + 1, sink + 1))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
