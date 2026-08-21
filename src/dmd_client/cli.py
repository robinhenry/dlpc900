"""Command-line entry point for dmd-client (the `dmd` command).

Subcommands::

    dmd status                  # find the DMD and report its details
    dmd show checkerboard       # put a static internal test pattern on the DMD
    dmd show --list             # list the pattern names
    dmd stream-hdmi             # display the live HDMI signal on the DMD
    dmd stream-dp               # same, over DisplayPort at 60 Hz

``show`` drives the DLPC900's internal test-pattern generator: the controller
draws the image itself, so nothing depends on HDMI, the GPU, a pattern LUT, or
camera sync, and the image on the mirrors never changes. That makes it the
reference to align and focus the optics against.

``stream-hdmi`` / ``stream-dp`` are the opposite source: plain VIDEO mode,
mirrors showing whatever the GPU streams over the video link. All of these are
mutually exclusive — each switches the input source away from the others — and
they stay up until you change modes (e.g. start an acquisition sequence) or
power-cycle.
"""

import argparse
import sys

from dmd_client.device import (
    DataPort,
    DeviceNotFoundError,
    DevicePermissionError,
    IT6535Power,
    OperatingMode,
    TestPattern,
    connect,
    find_device,
)

WHITE = (1023, 1023, 1023)
BLACK = (0, 0, 0)

# CLI name -> (test pattern, foreground color). These are all ten patterns the
# generator offers (0x1203 values 0-9, DLPU018 Table 2-58), plus "black": a
# solid field with the foreground driven to zero, which is the way to get a dark
# DMD. (Value 10, "no pattern", is deliberately not exposed — it stops the
# generator rather than clearing the array, so the mirrors keep holding whatever
# was displayed last, which reads as a no-op.)
PATTERNS: dict[str, tuple[TestPattern, tuple[int, int, int]]] = {
    "full": (TestPattern.SOLID_FIELD, WHITE),
    "black": (TestPattern.SOLID_FIELD, BLACK),
    "horizontal-ramp": (TestPattern.HORIZONTAL_RAMP, WHITE),
    "vertical-ramp": (TestPattern.VERTICAL_RAMP, WHITE),
    "horizontal-lines": (TestPattern.HORIZONTAL_LINES, WHITE),
    "diagonal-lines": (TestPattern.DIAGONAL_LINES, WHITE),
    "vertical-lines": (TestPattern.VERTICAL_LINES, WHITE),
    "grid": (TestPattern.GRID, WHITE),
    "checkerboard": (TestPattern.CHECKERBOARD, WHITE),
    "rgb-ramp": (TestPattern.RGB_RAMP, WHITE),
    "color-bars": (TestPattern.COLOR_BARS, WHITE),
}


def cmd_status(_args: argparse.Namespace) -> int:
    """Report whether a DMD controller is connected."""
    info = find_device()
    if info is None:
        print("DMD not found")
        return 1
    print(f"Found DMD {info['vendor_id']:04x}:{info['product_id']:04x}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Display a static internal test pattern on the DMD."""
    if args.list:
        print("Available patterns:")
        for name in PATTERNS:
            print(f"  {name}")
        return 0
    if args.pattern is None:
        print("error: a pattern name is required (see --list)", file=sys.stderr)
        return 2

    pattern, foreground = PATTERNS[args.pattern]
    try:
        with connect() as dmd:
            dmd.show_test_pattern(pattern, foreground=foreground)
    except (DeviceNotFoundError, DevicePermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Showing static '{args.pattern}' pattern on the DMD.")
    return 0


def _stream(
    receiver: IT6535Power,
    data_port: DataPort,
    source: str,
    pixel_mode: str,
    hint: str,
) -> int:
    """Enter VIDEO mode on one video input and confirm the DMD is streaming it.

    Shared by stream-hdmi and stream-dp: the sequence is identical, only the
    receiver (0x1A01) and the port/clock routing (0x1A03) differ.
    """
    try:
        with connect() as dmd:
            # Catches a controller/DMD mismatch before touching the video path.
            hw = dmd.hardware_status()
            if hw.incompatible:
                print("error: controller/DMD incompatible", file=sys.stderr)
                return 1
            if not hw.initialized:
                print("error: controller not initialized yet", file=sys.stderr)
                return 1

            # Stop, power the receiver, 24-bit parallel input, the requested
            # port/clock routing, gamma off, enter VIDEO mode.
            dmd.prepare_video_input(receiver=receiver, data_port=data_port)

            # Then confirm it worked, rather than waiting blind and hoping: poll
            # until the source locks, and read back the mode and frame state.
            try:
                lock_s = dmd.wait_for_video_lock()
            except TimeoutError as exc:
                print(f"error: {exc}", file=sys.stderr)
                print(hint, file=sys.stderr)
                return 1
            mode = dmd.operating_mode()
            status = dmd.main_status()
    except (DeviceNotFoundError, DevicePermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if mode is not OperatingMode.VIDEO or not status.streaming:
        print(
            f"error: DMD did not reach a streaming state "
            f"(mode={mode}, locked={status.source_locked}, "
            f"port1_syncs_valid={status.port1_syncs_valid}, "
            f"port2_syncs_valid={status.port2_syncs_valid}, "
            f"video_frozen={status.video_frozen})",
            file=sys.stderr,
        )
        return 1

    print(f"DMD is streaming the live {source} signal — confirmed by the controller:")
    print(f"  mode           {mode}")
    print(f"  source         locked in {lock_s:.1f} s")
    print("  port 1 syncs   valid")
    print("  frames         changing (video not frozen)")
    print(f"  routing        {pixel_mode} (0x1A03 = {int(data_port):#04x})")
    print("Native resolution. Stays up until you change modes.")
    return 0


def cmd_stream_hdmi(_args: argparse.Namespace) -> int:
    """Display the live HDMI signal on the DMD (plain VIDEO mode)."""
    return _stream(
        receiver=IT6535Power.HDMI,
        data_port=DataPort.PORT1_SINGLE,
        source="HDMI",
        pixel_mode="single-pixel",
        hint="Check the HDMI cable and that the GPU is driving the DMD output.",
    )


def cmd_stream_dp(args: argparse.Namespace) -> int:
    """Display the live DisplayPort signal on the DMD (plain VIDEO mode).

    The full-resolution 60 Hz path: 2716x1600 @ 60 is ~270 Mpix/s, which a single
    DLPC900 could not take on one port — but the Video Splitter FPGA gives each
    of the two controllers half the array (~135 Mpix/s), inside the single-pixel
    limit. So the default routing is PORT1_SINGLE, same as the HDMI path; both
    routings lock identically on our board (see MainStatus.streaming).
    ``--dual-pixel`` selects 0x1A03 = 0x02 for comparison.

    Requires the DP EDID to be flashed (docs/edid/, DLPU102 3.13) — without it
    the GPU will not offer the timing and the source never locks.
    """
    dual = getattr(args, "dual_pixel", False)
    return _stream(
        receiver=IT6535Power.DISPLAYPORT,
        data_port=DataPort.PORT12_DUAL if dual else DataPort.PORT1_SINGLE,
        source="DisplayPort",
        pixel_mode="dual-pixel" if dual else "single-pixel per controller",
        hint=(
            "Check the DP cable is in J2, that the DP EDID is flashed "
            "(docs/edid/README.md), and that the GPU is actually driving "
            "2716x1600 @ 60 Hz."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dmd", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command")

    status = subparsers.add_parser("status", help="find the DMD and report its details")
    status.set_defaults(func=cmd_status)

    show = subparsers.add_parser(
        "show",
        help="display a static internal test pattern (for optical alignment)",
    )
    show.add_argument(
        "pattern",
        nargs="?",
        choices=list(PATTERNS),
        metavar="PATTERN",
        help="which pattern to show; see --list for the names",
    )
    show.add_argument(
        "--list",
        action="store_true",
        help="list the available pattern names and exit",
    )
    show.set_defaults(func=cmd_show)

    stream = subparsers.add_parser(
        "stream-hdmi",
        help="display the live HDMI signal on the DMD (plain VIDEO mode)",
    )
    stream.set_defaults(func=cmd_stream_hdmi)

    stream_dp = subparsers.add_parser(
        "stream-dp",
        help="display the live DisplayPort signal at 60 Hz (needs the DP EDID)",
    )
    stream_dp.add_argument(
        "--dual-pixel",
        action="store_true",
        help="route as dual-pixel (0x1A03 = 0x02) instead of the default port 1",
    )
    stream_dp.set_defaults(func=cmd_stream_dp)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
