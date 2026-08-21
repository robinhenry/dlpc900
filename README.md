# dmd-client

HID client for the DMD (digital micromirror device), built on [`hidapi`](https://pypi.org/project/hidapi/).

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

## Usage

```bash
uv run dmd status                    # find the DMD and report its details
uv run dmd show checkerboard         # static test pattern, for optical alignment
uv run dmd show black                # all mirrors off (a zeroed solid field)
uv run dmd show --list               # list the pattern names
uv run dmd stream-hdmi               # show the live HDMI signal on the DMD
uv run dmd stream-dp                 # same over DisplayPort, dual-pixel @ 60 Hz
uv run python -m dmd_client status   # same CLI, as a module
```

`dmd show` uses the DLPC900's **internal** test-pattern generator: the
controller draws the image itself, so it needs no HDMI source, GPU, pattern LUT,
or camera sync, and the image never changes — a stable reference for aligning
and focusing the optics. All ten generator patterns are available (`solid`,
`horizontal-ramp`, `vertical-ramp`, `horizontal-lines`, `diagonal-lines`,
`vertical-lines`, `grid`, `checkerboard`, `rgb-ramp`, `color-bars`) plus
`black`. The pattern stays up until you change modes (e.g. start an acquisition
sequence) or power-cycle, and while it is up the DMD ignores HDMI.

`dmd stream-hdmi` is the other source: plain VIDEO mode, the DMD acting as a
projector for whatever the GPU streams over HDMI (native resolution, single-pixel
input — the hardware-validated path). It polls the controller's main status until
the source locks, then reads back the mode and frame state, so it *confirms* the
DMD is streaming instead of asking you to go look at it:

```
DMD is streaming the live HDMI signal — confirmed by the controller:
  mode           VIDEO
  source         locked in 0.8 s
  port 1 syncs   valid
  frames         changing (video not frozen)
```

It exits non-zero if the source never locks (dead cable, GPU not driving the
output) instead of silently claiming success.

`dmd stream-dp` is the same thing over DisplayPort at **2716×1600 @ 60 Hz**, the
full-resolution path (~270 Mpix/s). One DLPC900 could not take that on a single
port, but this is a dual-controller board: the Video Splitter FPGA gives each
controller half the array (~135 Mpix/s), inside the single-pixel limit — so the
routing stays `PORT1_SINGLE` like the HDMI path. `--dual-pixel` selects
`0x1A03 = 0x02` instead; both lock identically here. Requires the DP EDID to be
flashed — see [`docs/edid/README.md`](docs/edid/README.md).
`scripts/check_edid.py` verifies which EDID is live:

```bash
uv run scripts/check_edid.py                       # decode the live link
uv run scripts/check_edid.py --file docs/edid/...  # decode a TI batch file offline
```

The library locates the DMD automatically by its USB vendor/product ID
(`0451:c900`), so it does not matter which USB port or bus the device is on —
plugging it into a different port needs no reconfiguration. From code:

```python
import dmd_client

with dmd_client.connect() as dmd:    # find + open; closes automatically
    hw = dmd.hardware_status()       # fresh snapshot of the status bits
    if hw.incompatible:
        raise RuntimeError("controller/DMD mismatch")
```

## Project structure

```
src/dmd_client/     importable package (installed as `dmd-client`)
  device.py         device discovery/connection (find_device, open_device)
  cli.py            the `dmd` command — status, show, stream-hdmi
  __main__.py       enables `python -m dmd_client`
scripts/            standalone helper scripts, run directly with `uv run`
udev/               udev rule granting non-root access to the DMD
tests/unit/         fast tests, no hardware (hid mocked)
tests/hardware/     tests needing a connected DMD (marked `hardware`)
```

## Testing

```bash
uv run pytest                 # unit tests only — safe anywhere, no hardware
uv run pytest -m hardware     # hardware tests — needs a connected DMD
```

Hardware tests are marked `hardware` and deselected by default (see
`addopts` in `pyproject.toml`), so a bare `pytest` never runs anythinbg on real device.
When run with `-m hardware`, they *skip* if no DMD is connected and *fail* if
one is connected but not openable (e.g. the udev rule below isn't installed).

## Device permissions on Linux

The DMD's `/dev/hidraw*` node is root-only by default, so the library can't
open it. Install the shipped udev rule once per machine:

```bash
sudo cp udev/99-dmd.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then unplug and replug the DMD.
