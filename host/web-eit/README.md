# RA8D1 EIT Web Console

Browser console for the RA8D1 EIT firmware. The recommended mode uses a local
Python serial bridge, so Chrome does not directly own the ART-Link CDC device.
It uses the existing MCU-side commands:

- `p 1 0 0`
- `g drive meas`
- `reconbase electrodes frames samples settle_ms rate pp_limit retries`
- `recon electrodes samples settle_ms rate pp_limit retries`
- `reconfast electrodes samples settle_ms rate pp_limit retries`

Recommended bridge mode:

```bash
python3 host/web-eit/serial_bridge.py --serial-port /dev/ttyACM0 --baud 460800
```

Open:

```text
http://127.0.0.1:8765/?bridge=1
```

The first live frame uses `recon` to receive node coordinates; later frames can
use `reconfast` and reuse those coordinates.

Direct Web Serial mode is still available, but ART-Link CMSIS-DAP CDC devices
can be less stable in browser-owned serial sessions:

```bash
python3 -m http.server 8000
```

```text
http://localhost:8000/host/web-eit/
```
