"""Command-line entry point for dmd-client (the `dmd` command).

Subcommands::

    dmd status                  # find the DMD and report its details
    dmd show checkerboard       # put a static internal test pattern on the DMD
    dmd show --list             # list the pattern names
    dmd stream-hdmi             # display the live HDMI signal on the DMD

``show`` drives the DLPC900's internal test-pattern generator: the controller
draws the image itself, so nothing depends on HDMI, the GPU, a pattern LUT, or
camera sync, and the image on the mirrors never changes. That makes it the
reference to align and focus the optics against.

``stream-hdmi`` is the opposite source: plain VIDEO mode, mirrors showing
whatever the GPU streams over HDMI. The two are mutually exclusive — each
switches the input source away from the other — and both stay up until you
change modes (e.g. start an acquisition sequence) or power-cycle.
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


def cmd_stream_hdmi(_args: argparse.Namespace) -> int:
    """Display the live HDMI signal on the DMD (plain VIDEO mode)."""
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

            # Stop, power the HDMI receiver, single-pixel 24-bit parallel input,
            # gamma off, enter VIDEO mode.
            dmd.prepare_video_input(
                receiver=IT6535Power.HDMI, data_port=DataPort.PORT1_SINGLE
            )

            # Then confirm it worked, rather than waiting blind and hoping: poll
            # until the source locks, and read back the mode and frame state.
            try:
                lock_s = dmd.wait_for_video_lock()
            except TimeoutError as exc:
                print(f"error: {exc}", file=sys.stderr)
                print(
                    "Check the HDMI cable and that the GPU is driving the DMD "
                    "output.",
                    file=sys.stderr,
                )
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
            f"video_frozen={status.video_frozen})",
            file=sys.stderr,
        )
        return 1

    print("DMD is streaming the live HDMI signal — confirmed by the controller:")
    print(f"  mode           {mode}")
    print(f"  source         locked in {lock_s:.1f} s")
    print("  port 1 syncs   valid")
    print("  frames         changing (video not frozen)")
    print("Native resolution, single-pixel. Stays up until you change modes.")
    return 0


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
