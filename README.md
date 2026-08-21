# dmd-client

HID client for the DMD (digital micromirror device), built on
[`hidapi`](https://pypi.org/project/hidapi/). Drives a TI DLPC900 controller over
USB — display mode, video input, internal test patterns — from the command line
or as a library.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python ≥ 3.14 (uv will fetch it if missing)
- System HID libraries (usually preinstalled). On Debian/Ubuntu:
  ```bash
  sudo apt install libhidapi-hidraw0 libhidapi-libusb0
  ```

## Setup

```bash
uv sync
```

## Device permissions on Linux

The DMD's `/dev/hidraw*` node is root-only by default, so the library can't open
it. Install the shipped udev rule once per machine:

```bash
sudo cp udev/99-dmd.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then unplug and replug the DMD.

## Usage

```bash
uv run dmd status                   # find the DMD and report its details
uv run dmd show checkerboard        # static internal test pattern (useful during alignment)
uv run dmd show --list              # list the pattern names
uv run dmd stream                   # live video from the GPU (default: DisplayPort, 60 Hz)
uv run dmd stream hdmi              # ... over HDMI (30 Hz) instead
uv run dmd -v stream                # ... logging every USB command sent
uv run dmd standby                  # park the mirrors, ready to power off
uv run dmd wake                     # return to normal power
```

`show` and `stream` correspond to two different image sources, and they are mutually exclusive. Both stay up until you change
modes or power-cycle the DMD.

### `dmd show` — internal test pattern

The DLPC900 draws the image itself, so it needs no GPU, video link or pattern
LUT, and it never changes: this is useful when aligning the optics.
Ten generator supports options (`full`, `grid`, `checkerboard`, `horizontal-lines`,
`vertical-lines`, `diagonal-lines`, `horizontal-ramp`, `vertical-ramp`,
`rgb-ramp`, `color-bars`) plus `black`, which drives every mirror off.

### `dmd stream` — live video from the GPU

Plain VIDEO mode, the DMD acting as a projector. The default is DisplayPort at 2716×1600 @ 60 Hz. HDMI (30 Hz) is
an alternative option. Each input has its own EDID, which must be flashed first — see [`edid-files/README.md`](edid-files/README.md).

It polls the controller until the source locks and reads the mode back, so it
*confirms* the DMD has correctly entered this mode:

```
DMD is streaming the live DisplayPort signal — confirmed by the controller:
  mode           VIDEO
  source         locked in 2.8 s
  port 1 syncs   valid
  frames         changing (video not frozen)
  routing        dual-pixel (0x1A03 = 0x02)
Native resolution. Stays up until you change modes.
```

### `dmd standby` / `dmd wake` — the resting state

Standby parks the micromirrors, disables the input interfaces and powers down
part of the DMD. Parking is what protects the array when it is not projecting,
so run it when an experiment ends — and **always before a planned power-off**.

Parking takes about two minutes, so `dmd standby` blocks until it is done —
showing a countdown — and then tells you it is safe to cut power:

```
  ███████░░░░░░░░░░░░░░░░░░░░░  93s  parking the mirrors
```

`--no-wait` returns immediately instead. 
While in standby, Return to Normal is the only command the controller accepts, so
`dmd standby` deliberately reads no status back — it is the one command that
cannot confirm its own result.

**The most reliable way back is a power-cycle**, not `dmd wake`:

```bash
uv run dmd standby   # park the mirrors (writes only — safe)
                     # ... power the EVM off, and on again later
uv run dmd stream    # the controller boots clean; carry on
```

## As a library

The DMD is located by USB vendor/product ID (`0451:c900`), so it does not matter
which port or bus it is on — moving it needs no reconfiguration.

```python
import dmd_client

with dmd_client.connect() as dmd:    # find + open; closes automatically
    hw = dmd.hardware_status()       # fresh snapshot of the status bits
    if hw.incompatible:
        raise RuntimeError("controller/DMD mismatch")
```

Logging follows the usual library convention: `dmd_client` logs to a
`logging.getLogger(__name__)` and configures nothing else. The CLI is what attaches
handlers.

## Project structure

```
src/dmd_client/     importable package (distribution name: dmd-client)
  device.py         discovery, connection, and the DLPC900 command set
  cli.py            the `dmd` command — status, show, stream
  __main__.py       enables `python -m dmd_client`
scripts/            standalone helper scripts, run directly with `uv run`
edid-files/         EDIDs for the DMD's HDMI/DisplayPort inputs
docs/               TI reference PDFs and design notes
udev/               udev rule granting non-root access to the DMD
tests/unit/         fast tests, no hardware (hid mocked)
tests/hardware/     tests needing a connected DMD (marked `hardware`)
```

## Testing

```bash
uv run pytest                 # unit tests only — safe anywhere, no hardware
uv run pytest -m hardware     # hardware tests — needs a connected DMD
```

Hardware tests are deselected by default (see `addopts` in `pyproject.toml`), so
a bare `pytest` never touches a real device. With `-m hardware` they *skip* if no
DMD is connected, and *fail* if one is connected but not openable — e.g. the
udev rule above isn't installed.
