# RA8D1 EIT Port

This firmware keeps the Pico EIT serial protocol while using the RA8D1 Vision
Board RPI connector. The 10-bit external ADC bus is rewired onto RA Port 0 so a
sample can be captured from one `R_PORT0->PIDR` read.

## ADC Bus Rewire

Configure these pins as GPIO inputs in e2studio/FSP. For pins that can also be
analog inputs, make sure analog mode is disabled.

| ADC bit | RA pin | RPI label | FSP mode               |
| ------- | ------ | --------- | ---------------------- |
| ADC_0   | P001   | RPI-P001  | GPIO input             |
| ADC_1   | P002   | RPI-AN102 | GPIO input, not analog |
| ADC_2   | P003   | RPI-P003  | GPIO input             |
| ADC_3   | P004   | RPI-AN000 | GPIO input, not analog |
| ADC_4   | P005   | RPI-AN001 | GPIO input, not analog |
| ADC_5   | P006   | RPI-P006  | GPIO input             |
| ADC_6   | P007   | RPI-P007  | GPIO input             |
| ADC_7   | P008   | RPI-P008  | GPIO input             |
| ADC_8   | P009   | RPI-P009  | GPIO input             |
| ADC_9   | P011   | RPI-P011  | GPIO input             |

The capture path packs samples as:

```c
code = ((R_PORT0->PIDR >> 1) & 0x01ff) | ((R_PORT0->PIDR >> 2) & 0x0200);
```

Electrode numbering follows the Pico firmware directly. There is no firmware
remap table:

```text
S1  -> ADG731 channel 0 -> command 0x00
S2  -> ADG731 channel 1 -> command 0x01
...
S32 -> ADG731 channel 31 -> command 0x1f
off -> command 0x80
```

This is confirmed against `../pico_pio_dma_eit/src/main.c`: Pico sends
`channel & 0x1f` for enabled channels and `0x80` for off. The ADG731 data sheet
Table II maps address 0..31 to S1..S32.

## Sub-board ADG731 Mapping

These connections are from `../副板.pdf` and `../副板.tel`.

Functional role mapping:

| Firmware role | Sub-board CS | ADG731 | Drain net | RA pin |
| --- | --- | --- | --- | --- |
| `sink` | CS1 | U7 | D1 | PA05 |
| `vp`   | CS2 | U5 | D2 | P507 |
| `src`  | CS3 | U8 | D3 | P508 |
| `vn`   | CS4 | U6 | D4 | P509 |

The ADG731 command byte is Pico-compatible even though the RA8D1 adapter uses
the confirmed drain roles above. Do not add an S-channel remap for the
PIO-compatible `raw`/`scanraw` path; `channel & 0x1f` must reach the ADG731
unchanged.

All four ADG731 chips share the same electrode source nets. The command channel
therefore maps directly to the silk/net label regardless of role:

| Command channel | ADG731 switch | Sub-board net | Header pin |
| --- | --- | --- | --- |
| 0 | S1 | S1 | H1.1 |
| 1 | S2 | S2 | H2.1 |
| 2 | S3 | S3 | H1.2 |
| 3 | S4 | S4 | H2.2 |
| 4 | S5 | S5 | H1.3 |
| 5 | S6 | S6 | H2.3 |
| 6 | S7 | S7 | H1.4 |
| 7 | S8 | S8 | H2.4 |
| 8 | S9 | S9 | H1.5 |
| 9 | S10 | S10 | H2.5 |
| 10 | S11 | S11 | H1.6 |
| 11 | S12 | S12 | H2.6 |
| 12 | S13 | S13 | H1.7 |
| 13 | S14 | S14 | H2.7 |
| 14 | S15 | S15 | H1.8 |
| 15 | S16 | S16 | H2.8 |
| 16 | S17 | S17 | H1.9 |
| 17 | S18 | S18 | H2.9 |
| 18 | S19 | S19 | H1.10 |
| 19 | S20 | S20 | H2.10 |
| 20 | S21 | S21 | H1.11 |
| 21 | S22 | S22 | H2.11 |
| 22 | S23 | S23 | H1.12 |
| 23 | S24 | S24 | H2.12 |
| 24 | S25 | S25 | H1.13 |
| 25 | S26 | S26 | H2.13 |
| 26 | S27 | S27 | H1.14 |
| 27 | S28 | S28 | H2.14 |
| 28 | S29 | S29 | H1.15 |
| 29 | S30 | S30 | H2.15 |
| 30 | S31 | S31 | H1.16 |
| 31 | S32 | S32 | H2.16 |

The sub-board headers are split by odd/even electrode labels: H1 carries
S1,S3,...,S31 and H2 carries S2,S4,...,S32. This is only the physical header
layout; it is not a firmware remap.

## Control Pins

ADG731 uses hardware SPI on SCI2. AD5270 stays on bit-banged GPIO SPI.

Configure these as GPIO outputs in e2studio/FSP.

| Signal         | RA pin | RPI label | Initial level |
| -------------- | ------ | --------- | ------------- |
| EN             | P510   | RPI-P510  | low           |
| PWR/PWDN       | P801   | RPI-RXD2  | low           |
| OE_N           | P802   | RPI-TXD2  | low           |
| ADG731 CS1/SINK | PA05   | RPI-CS2   | high          |
| ADG731 CS2/VP   | P507   | RPI-P507  | high          |
| ADG731 CS3/SRC  | P508   | RPI-P508  | high          |
| ADG731 CS4/VN   | P509   | RPI-P509  | high          |
| V_DAT          | P409   | RPI-SDA0  | low           |
| V_SCLK         | P408   | RPI-SCL0  | low           |
| CS_DRIVE       | P505   | RPI-P505  | high          |
| CS_MEAS        | P804   | RPI-P804  | high          |

Configure these as SCI2 Simple SPI peripheral pins:

| Signal | RA pin | RPI label | FSP mode |
| --- | --- | --- | --- |
| H_DAT | PA03 | RPI-MOSI2 | SCI2 TXD2 |
| H_SCLK | PA04 | RPI-SCK2 | SCI2 SCK2 |
| H_RX unused | PA02 | RPI optional | SCI2 RXD2, leave unconnected if required |

Keep SCI9 UART on P208/P209 for the CLI. The RA8D1 firmware uses 460800 baud
for the host CLI. Rates above 460800 should be tested explicitly with long
`scanstat`/`reconfast` captures before using them for reconstruction data.

## e2studio/FSP Checklist

- Pins tab:
  - Set P001, P002, P003, P004, P005, P006, P007, P008, P009, P011 to GPIO input.
  - Disable analog mode for P002, P004, and P005.
  - Set P510, P801, P802, P409, P408 to GPIO output low.
  - Set PA05, P505, P507, P508, P509, P804 to GPIO output high.
  - Set PA03 to SCI2 TXD2 and PA04 to SCI2 SCK2.
  - If the FSP pin tool requires RXD2, set PA02 to SCI2 RXD2 and leave it unconnected.
  - Do not configure PA05 as SCI2 CTS/RTS; PA05 is manual ADG731 CS1/SINK.
  - Keep P208 RXD9 and P209 TXD9 as SCI9 UART peripheral pins.
- Stacks tab:
  - Keep SCI9 UART enabled at 460800 baud.
  - Add/keep `SPI on SCI_B_SPI` named `g_sci_spi_h` on channel 2.
  - Configure `g_sci_spi_h`: master, CPOL low, CPHA edge even, MSB first, 100000 baud, no DTC/DMAC.
  - Add/keep `Timer, General PWM (r_gpt)` named `g_adc_sample_timer` on GPT0.
    Configure periodic mode, 5 us default period, no output pins, no callback, cycle-end interrupt disabled.
  - Add/keep `Transfer on DMAC (r_dmac)` named `g_adc_port_dma` on DMAC channel 0.
    Configure normal mode, 2-byte transfer, fixed source, incremented destination, activation event
    `GPT0_COUNTER_OVERFLOW`, end interrupt, priority 4, callback `adc_dma_callback`.
  - No RA ADC peripheral is required.
- BSP tab:
  - Set main stack to at least `0x2000`. `scanstat` uses math/FSP callbacks and should not run with the default `0x400` stack.
- After changing pins, regenerate FSP files, then rebuild with CMake.

## CMake Build Notes

The CMake build does not depend on the e2studio `Debug/` directory. The FSP
linker support files used by CMake are kept in `script/`:

```text
script/fsp_gen.ld
script/memory_regions.ld
script/bsp_linker_info.h
```

If e2studio regenerates these files after BSP/memory changes, copy the updated
versions into `script/` before running the CMake build.

## ADC Capture Notes

- The external ADC bus is read from Port0 GPIO input, not from the RA ADC peripheral.
- `eit_adc_capture()` uses GPT0-triggered DMAC to copy `R_PORT0->PIDR` into the sample buffer. The requested sample
  rate is applied at runtime with `periodSet(PCLKD / rate_hz)`.
- Physical bit order matches the Pico firmware's GPIO sampling order, so the 10-bit word must be passed through `reverse10()` after reading P001..P009/P011.
- After decode, firmware removes isolated single-sample spikes. If both neighbor
  samples are valid and close to each other, a single `0/1023` rail sample or a
  jump larger than 128 codes is replaced with the neighbor average. Consecutive
  clipping is left unchanged.
- `scanraw` prints decoded raw samples after this bit reversal. If the reversal is removed, raw values look like full-scale random jumps even at low sample rates.
- `scanstat` keeps Pico-compatible field semantics: `mean_code` is the ADC DC
  mean, `rms_code` is the raw AC RMS around that mean, and `pp_code` is the
  trimmed peak-to-peak diagnostic. The reconstruction host uses
  `rms_code * sqrt(2)` as the voltage amplitude.
- `scanstatbin` sends the same route statistics as a binary frame. All integer
  fields are little-endian. The 32-byte header is:
  `magic[4]="EITB"`, `version:u8=1`, `type:u8=1`, `header_len:u16=32`,
  `payload_len:u32`, `frame_id:u32`, `electrodes:u16`, `samples:u16`,
  `rate_hz:u32`, `route_count:u16`, `row_stride:u16=32`,
  `payload_crc16_ccitt:u16`, `reserved:u16`.
  Each 32-byte route row is:
  `route_index:u16`, `src:u8`, `sink:u8`, `vp:u8`, `vn:u8`,
  `mean_milli:u32`, `rms_milli:u32`, `min_code:u16`, `max_code:u16`,
  `pp_code:u16`, `overrange_count:u16`, `valid_count:u16`, `flags:u16`,
  `raw_flags:u16`, `retry_count:u8`, `reserved[3]`.
- `reconfastbin` uses binary frame type `2` after MCU-side JAC reconstruction.
  The 32-byte common header uses `meta0=electrodes`, `meta1=node_count`,
  `meta2=route_count`, `item_count=node_count`, and `item_stride=4`.
  The payload starts with a 32-byte summary:
  `valid:u16`, `invalid:u16`, `retry:u16`, `reserved:u16`,
  `ds_min:f32`, `ds_max:f32`, `ds_abs_p98:f32`, `rel_l2:f32`,
  `reserved[8]`, followed by `node_count` little-endian float32 `ds` values.

## Bring-up Commands

```text
ver
p 1 0 0
g 512 6
rawonly src 0 1
off
adc 2048 200000
scanraw 8 256 20 200000 0 0
scanstat 8 256 20 200000 180 1
scanstatbin 8 256 20 200000 180 1
reconfastbin 8 256 20 200000 180 1
```

Low-output route-quality diagnosis from the host:

```bash
.venv/bin/python3 host/diagnose_invalid_routes.py \
  --port /dev/ttyACM0 \
  --baud 460800 \
  --samples 256 \
  --settle-ms 20 \
  --rate 200000 \
  --pp-limit 180 \
  --retries 1 \
  --gain 512 6 \
  --skip-raw \
  --out-dir diagnostics/scanstat_g512_6
```

This command saves the full `scanstat` log and a route summary under
`diagnostics/...`, but prints only aggregate quality and invalid-route lines.
If routes show `overrange` or rail-heavy values near 0/1023, reduce analog gain
before trusting reconstruction output; for example try `--gain 512 128`,
`--gain 512 512`, or lower drive. Only omit `--skip-raw` when raw samples are
needed for waveform inspection.

For gain tuning, capture a few representative raw routes across one or more
gain settings and plot them side by side:

```bash
MPLCONFIGDIR=/tmp/mpl .venv/bin/python3 host/compare_gain_waveforms.py \
  --reset \
  --port /dev/ttyACM0 \
  --baud 460800 \
  --samples 512 \
  --rate 200000 \
  --settle-ms 20 \
  --gain 512 6 \
  --gain 512 128 \
  --route-index 1 \
  --route-index 19 \
  --route-index 37 \
  --route-index 20 \
  --out-dir diagnostics/gain_waveforms \
  --prefix gain_compare
```

The default routes are `1,19,37,20`, so the `--route-index` lines can be
omitted for the same comparison. The script writes `<prefix>_metrics.csv`,
`<prefix>_samples.csv`, `<prefix>_grid.png`, and
`<prefix>_dc_overlay.png`; terminal output is limited to one metrics line per
route/gain pair.

Manual ADC route scan from the host, without using firmware `scanraw`:

```bash
python host/manual_adc_scan.py \
  --port /dev/ttyACM0 \
  --baud 460800 \
  --electrodes 8 \
  --samples 512 \
  --rate 200000 \
  --settle-ms 20 \
  --gain 512 6 \
  --max-routes 8 \
  --out-dir diagnostics/manual_adc_scan_g512_6 \
  --prefix quick8
```

The script sends `off`, then `raw src/sink/vp/vn`, then `adc` for every
selected route. It writes `<prefix>_metrics.csv`, `<prefix>_samples.csv`, and
plots when `matplotlib` is available.

## MCU-side Reconstruction

The RA8D1 firmware now contains the fixed 8-electrode JAC reconstruction model.
Host Python is not needed for the reconstruction math during normal operation:
the MCU captures the 40 adjacent-drive/adjacent-measurement routes, normalizes
against the active baseline, multiplies by the generated float32 node matrix,
and prints CSV-style node values.

The model constants are generated offline by:

```bash
MPLCONFIGDIR=/tmp/mpl .venv/bin/python3 host/generate_eit_recon_model.py
```

The generated files are `src/eit_recon_model.h` and
`src/eit_recon_model.c`. They include the 40 route signatures, initial
baseline amplitudes, mesh nodes/elements, and the final node-space matrix
equivalent to:

```python
ds_elem = solver.solve(v1, v0, normalize=True)
ds_node = sim2pts(mesh_obj.node, mesh_obj.element, ds_elem)
```

Runtime commands:

```text
recondump
reconbase 8 5 256 20 200000 180 1
recon 8 256 20 200000 180 1
```

`reconbase` averages valid routes from N empty-tank frames into the RAM
baseline. If a route never becomes valid, that route keeps the previous
baseline and the command reports `ram_partial,updated,missing`. It does not
write data flash; reset returns to the compiled baseline.
`recon` prints:

```text
RECON_BEGIN,frame,electrodes,routes,nodes
RECON_SUMMARY,valid,invalid,retry,ds_min,ds_max,ds_abs_p98,rel_l2
node,x,y,ds
0,...
...
RECON_DONE
```

Coordinates are already rotated for display with S1 at the top, S3 right, S5
bottom, and S7 left. `scanstat` remains available as the debugging baseline and
uses the same 40-route order as the reconstruction model.

Live plot MCU reconstruction frames directly from serial:

```bash
MPLCONFIGDIR=/tmp/mpl .venv/bin/python3 host/plot_recon_live.py \
  --port /dev/ttyACM0 \
  --baud 460800 \
  --reset \
  --baseline-frames 5 \
  --baseline-samples 256 \
  --baseline-settle-ms 20 \
  --samples 128 \
  --settle-ms 5 \
  --rate 200000 \
  --gain 512 6 \
  --fast \
  --latest-only
```

The script sends `p 1 0 0`, `g DRIVE MEAS`, optional `reconbase`, then loops on
`recon`. `--reset` asks pyOCD to reset the target first; if the CMSIS-DAP probe
is not visible, the script warns and continues with serial synchronization.
Keep baseline parameters conservative (`--baseline-samples 256`
and `--baseline-settle-ms 20`) even when live frames use faster settings. With
`--fast`, the first frame uses `recon` to get fixed node coordinates and later
frames use `reconfast` to send only node `ds` values. It updates
`<out-dir>/<prefix>_latest.png` and
`<out-dir>/<prefix>_latest_nodes.csv` while also showing a matplotlib window
when a GUI backend is available.

Verify the generated float32 matrix against the current pyEIT/JAC path with a
captured baseline/features pair:

```bash
MPLCONFIGDIR=/tmp/mpl .venv/bin/python3 host/verify_eit_recon_model.py \
  --baseline-csv eit_reconstruct_ra8d1_live/ra8_baseline.csv \
  --features-csv eit_reconstruct_ra8d1_live/ra8_latest_features.csv
```

The older host reconstruction script can still be used as a live comparison
tool with the RA8D1 serial port:

```bash
../pico2_eit_validation/.venv/bin/python -u ../pico_pio_dma_eit/host/reconstruct_eit_live.py \
  --port /dev/ttyACM0 \
  --baud 460800 \
  --electrodes 8 \
  --samples 256 \
  --settle-ms 20 \
  --rate 200000 \
  --gain 512 6
```

## ADG731 SPI Timing Check

The RA8D1 ADG731 path is intended to match the working Pico timing:

| Item | Pico reference | RA8D1 setting |
| --- | --- | --- |
| SPI peripheral | SPI0 | SCI2 simple SPI, `g_sci_spi_h` |
| Bitrate | 100 kHz | 100 kHz |
| CPOL | 0 | low |
| CPHA | 1 | edge even |
| Bit order | MSB first | MSB first |
| CS setup | 5 us | 5 us |
| CS hold | 5 us | 5 us |
| CS high gap | 10 us | 10 us |
| Off command | `0x80` | `0x80` |

Scope test sequence:

```text
off
rawonly src 0 1
rawonly vp 0 1
rawonly sink 0 1
rawonly vn 0 1
off
```

For each `rawonly`, verify:

- Only the selected ADG731 CS goes low.
- CS low contains exactly 8 SCK pulses.
- `H_DAT` changes on SCI2 TXD2 / PA03 and `H_SCLK` toggles on SCI2 SCK2 / PA04.
- CS stays low until after the last clock, then returns high.
- `cmd=0x..` printed by firmware matches the byte on `H_DAT`.
- The firmware prints `ok`, not `spi_error`. A `spi_error` means the SCI2 SPI
  transfer did not complete before CS was released.

If SCK toggles but ADG731 channel numbers are shifted or scrambled, verify that
`g_sci_spi_h` uses clock phase edge even. FSP's `edge odd` means sampling on the
first edge and does not match Pico `SPI_CPHA_1`. If no SCK toggles, the issue is
pin muxing or wiring, not ADG731 command content.
