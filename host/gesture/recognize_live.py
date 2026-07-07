#!/usr/bin/env python3
"""Real-time EIT gesture recognition with live visualization.

Connects to the RA8D1 MCU, captures reconfastbin frames continuously,
extracts features, classifies gestures, and displays results in a
dual-panel matplotlib window: EIT reconstruction (left) + gesture
probabilities (right).

Usage:
    python -m host.gesture.recognize_live --port /dev/ttyACM0 \
        --model gestures/model.joblib
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

# Ensure host/ is on sys.path
_HOST_DIR = Path(__file__).resolve().parents[1]
if str(_HOST_DIR) not in sys.path:
    sys.path.insert(0, str(_HOST_DIR))

import serial  # noqa: E402

from eit_binary import read_reconfast_frame  # noqa: E402
from serial_lines import SerialLineReader  # noqa: E402

from gesture.features import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    ELECTRODE_ANGLES,
    extract_features,
    get_node_xy,
    get_region_masks,
)
from gesture.model import GestureClassifier  # pyright: ignore[reportMissingImports]

# Matplotlib setup
os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.tri as mtri  # noqa: E402

# ---------------------------------------------------------------------------
# Serial helpers (shared with collect.py)
# ---------------------------------------------------------------------------

def _write_command_mcu(ser: serial.Serial, command: str) -> None:
    for ch in command.rstrip().encode():
        ser.write(bytes([ch]))
        ser.flush()
        time.sleep(0.003)
    time.sleep(0.05)
    for ch in b"\r\n\r":
        ser.write(bytes([ch]))
        ser.flush()
        time.sleep(0.02)


def _drain_idle(ser: serial.Serial, idle_s: float = 0.3, max_s: float = 3.0) -> None:
    deadline = time.monotonic() + max_s
    idle_deadline = time.monotonic() + idle_s
    reader = SerialLineReader(ser)
    while True:
        decoded = reader.read_line(min(deadline, idle_deadline))
        if decoded is None:
            return
        idle_deadline = time.monotonic() + idle_s


def _run_command(
    ser: serial.Serial,
    command: str,
    end_marker: str,
    timeout: float = 10.0,
) -> list[str]:
    _drain_idle(ser)
    _write_command_mcu(ser, command)
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    reader = SerialLineReader(ser)
    while True:
        decoded = reader.read_line(deadline)
        if decoded is None:
            raise TimeoutError(
                f"Timed out waiting for {end_marker!r}. "
                f"Recent:\n{reader.format_recent()}"
            )
        line = decoded.strip()
        lines.append(line)
        if line.startswith(end_marker):
            return lines
        if line.startswith("ERR:") or line.startswith("bad command"):
            raise RuntimeError(line)


def _init_board(ser: serial.Serial, drive_gain: int, meas_gain: int) -> None:
    time.sleep(1.0)
    _drain_idle(ser)
    _run_command(ser, "p 1 0 0", "power ok")
    _run_command(ser, f"g {drive_gain} {meas_gain}", "gain drive=")
    _run_command(ser, "recondump", "RECONDUMP,")


def _capture_baseline(
    ser: serial.Serial,
    electrodes: int = 8,
    frames: int = 5,
    samples: int = 256,
    settle_ms: int = 20,
    rate_hz: int = 200000,
    pp_limit: int = 180,
    retries: int = 1,
    timeout: float = 120.0,
) -> None:
    command = (
        f"reconbase {electrodes} {frames} {samples} "
        f"{settle_ms} {rate_hz} {pp_limit} {retries}"
    )
    _drain_idle(ser)
    _write_command_mcu(ser, command)
    deadline = time.monotonic() + timeout
    reader = SerialLineReader(ser)
    while True:
        decoded = reader.read_line(deadline)
        if decoded is None:
            raise TimeoutError(f"Timed out waiting for RECONBASE_DONE")
        line = decoded.strip()
        if line.startswith("RECONBASE_DONE,"):
            print(f"  baseline: {line}")
            return
        if line.startswith("RECONBASE_FRAME,"):
            print(f"  baseline frame: {line}")
        if line.startswith("ERR:"):
            raise RuntimeError(line)


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

class GestureVisualizer:
    """Dual-panel matplotlib visualization: EIT reconstruction + gesture bar chart."""

    def __init__(
        self,
        triangulation: mtri.Triangulation,
        gesture_labels: list[str],
        vmax: float = 0.0,
        min_vmax: float = 1e-4,
        deadband: float = 0.0,
    ) -> None:
        self.triangulation = triangulation
        self.gesture_labels = gesture_labels
        self.vmax = vmax
        self.min_vmax = min_vmax
        self.deadband = deadband

        self.fig, (self.ax_recon, self.ax_bar) = plt.subplots(
            1, 2,
            figsize=(12, 5),
            gridspec_kw={"width_ratios": [1, 0.7]},
        )
        self.fig.canvas.manager.set_window_title("EIT Gesture Recognition")

        # Reconstruction panel
        self.ax_recon.set_aspect("equal")
        self.ax_recon.set_title("EIT Reconstruction")
        self.recon_image = None
        self.colorbar = None

        # Electrode markers
        angles = ELECTRODE_ANGLES
        for ei, ang in enumerate(angles):
            ex, ey = np.cos(ang), np.sin(ang)
            self.ax_recon.plot(ex, ey, "ko", markersize=6)
            self.ax_recon.text(ex * 1.08, ey * 1.08, f"S{ei+1}",
                               ha="center", va="center", fontsize=7)

        self.ax_recon.set_xlim(-1.15, 1.15)
        self.ax_recon.set_ylim(-1.15, 1.15)
        self.ax_recon.set_xticks([])
        self.ax_recon.set_yticks([])

        # Gesture bar chart
        self.ax_bar.set_title("Gesture Probabilities")
        self.ax_bar.set_xlabel("Confidence")
        self.ax_bar.set_ylim(-0.5, len(gesture_labels) - 0.5)
        self.bar_container = None
        self.pred_text = self.ax_bar.text(
            0.5, 0.95, "", transform=self.ax_bar.transAxes,
            ha="center", va="top", fontsize=14, fontweight="bold",
        )

        # Frame history (last 20 predictions)
        self.history: list[str] = []
        self.history_text = self.ax_bar.text(
            0.5, 0.02, "", transform=self.ax_bar.transAxes,
            ha="center", va="bottom", fontsize=7, color="gray",
        )

        plt.ion()
        plt.tight_layout()
        plt.show(block=False)

    def update(
        self,
        ds_node: np.ndarray,
        frame_id: int,
        gesture_label: str,
        confidence: float,
        all_probas: dict[str, float],
    ) -> None:
        """Update both panels with new frame data."""
        # --- Reconstruction ---
        ds_display = np.array(ds_node, dtype=np.float64).copy()
        if self.deadband > 0:
            ds_display[np.abs(ds_display) < self.deadband] = 0.0

        vmax = self.vmax if self.vmax > 0 else max(
            float(np.percentile(np.abs(ds_display), 98)),
            self.min_vmax,
        )

        if self.recon_image is None:
            self.recon_image = self.ax_recon.tripcolor(
                self.triangulation, ds_display,
                shading="gouraud", cmap="coolwarm",
                vmin=-vmax, vmax=vmax,
            )
            self.colorbar = self.fig.colorbar(
                self.recon_image, ax=self.ax_recon, shrink=0.8,
            )
            self.colorbar.set_label(r"$\Delta\sigma$")
        else:
            self.recon_image.set_array(ds_display)
            self.recon_image.set_clim(-vmax, vmax)

        self.ax_recon.set_title(f"Frame #{frame_id}  [{gesture_label}]")

        # --- Gesture bar chart ---
        labels_ordered = [
            l for l in self.gesture_labels if l != "unknown"
        ]
        probs = [all_probas.get(l, 0.0) for l in labels_ordered]
        colors = [
            "#2ecc71" if l == gesture_label else "#bdc3c7"
            for l in labels_ordered
        ]

        if self.bar_container is None:
            self.bar_container = self.ax_bar.barh(
                labels_ordered, probs, color=colors, height=0.6,
            )
            self.ax_bar.set_xlim(0, 1.05)
        else:
            for rect, p, c in zip(self.bar_container, probs, colors):
                rect.set_width(p)
                rect.set_color(c)

        # Prediction text
        if gesture_label == "unknown":
            self.pred_text.set_text(f"Unknown\n(max {confidence:.1%})")
            self.pred_text.set_color("gray")
        else:
            self.pred_text.set_text(f"{gesture_label}\n{confidence:.1%}")
            self.pred_text.set_color("#27ae60" if confidence >= 0.7 else "#e67e22")

        # History
        self.history.append(gesture_label[:4])
        if len(self.history) > 30:
            self.history = self.history[-30:]
        self.history_text.set_text(
            "History: " + " → ".join(self.history[-15:])
        )

        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()


# ---------------------------------------------------------------------------
# Main recognition loop
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Real-time EIT gesture recognition"
    )
    p.add_argument("--port", required=True, help="Serial port, e.g. /dev/ttyACM0")
    p.add_argument("--baud", type=int, default=460800)
    p.add_argument("--model", required=True, help="Path to model.joblib")
    p.add_argument("--electrodes", type=int, default=8)
    p.add_argument("--samples", type=int, default=128)
    p.add_argument("--settle-ms", type=int, default=5)
    p.add_argument("--rate", type=int, default=200000)
    p.add_argument("--pp-limit", type=int, default=180)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--baseline-frames", type=int, default=5)
    p.add_argument("--drive-gain", type=int, default=512)
    p.add_argument("--meas-gain", type=int, default=6)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--vmax", type=float, default=0.0,
                   help="Fixed color scale; 0 = auto per-frame")
    p.add_argument("--min-vmax", type=float, default=1e-4)
    p.add_argument("--deadband", type=float, default=0.0)
    p.add_argument("--log", type=Path, default=None,
                   help="CSV path to log predictions")
    p.add_argument("--no-plot", action="store_true")
    p.add_argument("--threshold", type=float, default=0.6,
                   help="Confidence threshold for 'unknown'")
    return p.parse_args()


def build_triangulation(node_xy: np.ndarray) -> mtri.Triangulation:
    """Build matplotlib Triangulation from node coordinates.

    Uses Delaunay triangulation on the node positions.
    """
    x, y = node_xy[:, 0], node_xy[:, 1]
    return mtri.Triangulation(x, y)


def recognize_live(args: argparse.Namespace) -> None:
    """Run the real-time recognition loop."""

    # Load model
    print(f"Loading model: {args.model}")
    clf = GestureClassifier.load(args.model)
    clf.confidence_threshold = args.threshold
    gesture_labels = [str(l) for l in clf.label_encoder.classes_]
    if "unknown" not in gesture_labels:
        gesture_labels.append("unknown")

    # Preload node coordinates and regions
    node_xy = get_node_xy()
    regions = get_region_masks()

    # Build triangulation
    triangulation = build_triangulation(node_xy)
    print(f"Triangulation: {triangulation.triangles.shape[0]} triangles")

    # Setup visualization
    viz: GestureVisualizer | None = None
    if not args.no_plot:
        viz = GestureVisualizer(
            triangulation=triangulation,
            gesture_labels=gesture_labels,
            vmax=args.vmax,
            min_vmax=args.min_vmax,
            deadband=args.deadband,
        )

    # Setup logging
    log_fp = None
    log_writer = None
    if args.log is not None:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        log_fp = args.log.open("w", newline="")
        log_writer = csv.writer(log_fp)
        log_writer.writerow([
            "timestamp", "frame_id", "gesture", "confidence",
            "valid_count", "ds_abs_p98", "rel_l2",
        ])

    # Connect
    print(f"Opening {args.port} @ {args.baud}...")
    ser = serial.Serial(args.port, args.baud, timeout=0.1)

    try:
        # Initialize
        print("Initializing MCU...")
        _init_board(ser, drive_gain=args.drive_gain, meas_gain=args.meas_gain)
        _capture_baseline(
            ser,
            electrodes=args.electrodes,
            frames=args.baseline_frames,
            samples=args.samples,
            settle_ms=args.settle_ms,
            rate_hz=args.rate,
            pp_limit=args.pp_limit,
            retries=args.retries,
        )
        print("Ready — starting recognition loop.\n")

        print("Commands: [q]uit  [s]ave baseline  [0-9] label frame")
        print(f"{'Frame':>6} {'Prediction':>12} {'Conf':>7} {'Valid':>6} {'p98':>10}")
        print("-" * 55)

        prev_ds: np.ndarray | None = None
        frame_count = 0

        while True:
            # Send reconfastbin and read binary frame
            command = (
                f"reconfastbin {args.electrodes} {args.samples} "
                f"{args.settle_ms} {args.rate} {args.pp_limit} {args.retries}"
            )
            _drain_idle(ser)
            _write_command_mcu(ser, command)

            try:
                frame = read_reconfast_frame(ser, args.timeout)
            except (TimeoutError, RuntimeError) as exc:
                print(f"  [err] frame read failed: {exc}", file=sys.stderr)
                continue

            frame_count += 1
            ds = np.array(frame.ds_values, dtype=np.float64)

            summary_dict = {
                "valid_count": frame.valid,
                "invalid_count": frame.invalid,
                "retry_count": frame.retry,
                "ds_min": frame.ds_min,
                "ds_max": frame.ds_max,
                "ds_abs_p98": frame.ds_abs_p98,
                "rel_l2": frame.rel_l2,
            }

            # Extract features and classify
            feat = extract_features(
                ds, summary_dict,
                prev_ds_node=prev_ds,
                regions=regions,
                node_xy=node_xy,
            )
            label, confidence, all_probas = clf.predict(feat.values)

            # Console output
            print(
                f"{frame_count:>6} {label:>12} {confidence:>6.1%} "
                f"{frame.valid:>6} {frame.ds_abs_p98:>10.5f}",
                flush=True,
            )

            # Update visualization
            if viz is not None:
                viz.update(ds, frame_count, label, confidence, all_probas)

            # Log
            if log_writer is not None:
                log_writer.writerow([
                    datetime.now().isoformat(),
                    frame_count, label, f"{confidence:.4f}",
                    frame.valid, f"{frame.ds_abs_p98:.6f}",
                    f"{frame.rel_l2:.6f}",
                ])
                assert log_fp is not None
                log_fp.flush()

            # Check for keyboard input
            if viz is not None and not plt.fignum_exists(viz.fig.number):
                print("\nFigure closed — exiting.")
                break

            prev_ds = ds

    except KeyboardInterrupt:
        print("\n\nInterrupted.")
    finally:
        ser.close()
        if log_fp is not None:
            log_fp.close()
        if viz is not None and plt.fignum_exists(viz.fig.number):
            plt.close(viz.fig)
        print("Done.")


def main() -> None:
    args = parse_args()
    recognize_live(args)


if __name__ == "__main__":
    main()
