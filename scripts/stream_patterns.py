"""Set up a freshly powered-on DMD to stream N-bit patterns over HDMI.

Brings the DLPC900 from its power-on (out-of-box) state into Video Pattern Mode,
free-running at the DMD's native mirror resolution, ready to display whatever
bit-planes the GPU streams over HDMI. Back-to-basics setup: single-pixel input
at the native resolution advertised by the stock EDID (2716×1600 @ 30 Hz on the
DLP670S — Option A in docs/pattern-mode-design.md), which is the validated,
reliably-locking path. No dual-pixel, no custom EDID, no hardware triggers.

Run it (from the repo root)::

    uv run scripts/stream_patterns.py                 # 8-bit patterns @ 30 Hz
    uv run scripts/stream_patterns.py --bit-depth 4   # faster, coarser grayscale
    uv run scripts/stream_patterns.py --refresh-hz 60 # if the GPU/EDID drives 60

Bit depth is the speed↔flatness knob: a 24-bit HDMI frame carries 24 bit-planes,
so an N-bit pattern gives 24/N patterns per frame. 8-bit is the flat-field
grayscale case; fewer bits trade grayscale levels for a higher pattern rate.

The exposures are computed to fill exactly one HDMI frame period — the LUT's
total on-time must equal the frame time or the sequencer drifts against the
video and the controller flickers/freezes (the failure mode from last time; see
docs/pattern-mode-design.md).

This is writes-only (no USB status polling once video is active, which can hang
the controller). It assumes a live HDMI source and NORMAL power (the power-on
default). Confirm it's actually streaming by looking at the DMD / a scope on
TRIG_OUT_1 — not by reading USB status.
"""

import argparse
import sys

import dmd_client
from dmd_client.device import HDMI_FRAME_BITPLANES, PatternSequence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--bit-depth",
        type=int,
        default=8,
        choices=range(1, 9),
        metavar="N",
        help="grayscale bit depth per pattern, 1-8 (default: 8)",
    )
    parser.add_argument(
        "--refresh-hz",
        type=float,
        default=30.0,
        help="HDMI refresh the GPU outputs (default: 30, the stock native EDID)",
    )
    args = parser.parse_args(argv)

    sequence = PatternSequence.frame_filling(args.bit_depth, args.refresh_hz)
    patterns_per_frame = HDMI_FRAME_BITPLANES // args.bit_depth
    pattern_hz = patterns_per_frame * args.refresh_hz
    exposure_us = sequence.entries[0].exposure_us

    try:
        with dmd_client.connect() as dmd:
            # Safe to read here — still in the power-on (pre-stored) state, before
            # any video-active mode. Catches a controller/DMD mismatch early.
            hw = dmd.hardware_status()
            if hw.incompatible:
                print("error: controller/DMD incompatible", file=sys.stderr)
                return 1
            if not hw.initialized:
                print("error: controller not initialized yet", file=sys.stderr)
                return 1

            # Writes-only, single-pixel native-resolution HDMI -> VIDEO ->
            # (blind lock wait) -> VIDEO_PATTERN + LUT -> start.
            dmd.run_pattern_sequence(sequence)
    except dmd_client.DeviceNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except dmd_client.DevicePermissionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Streaming {args.bit_depth}-bit patterns over HDMI, free-running.\n"
        f"  {patterns_per_frame} patterns/frame × {args.refresh_hz:g} Hz "
        f"= {pattern_hz:g} patterns/s, {exposure_us} µs each.\n"
        "  Single-pixel, native resolution, LEDs off (external illumination).\n"
        "The DMD now displays the bit-planes the GPU streams over HDMI. Confirm\n"
        "with a scope on TRIG_OUT_1 or the camera — do not poll USB status."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
