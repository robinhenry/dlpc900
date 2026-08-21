"""The `dmd` command.

    dmd status                  # find the DMD and report its details
    dmd show checkerboard       # static internal test pattern (optical alignment)
    dmd stream                  # live video signal, DisplayPort by default
    dmd -v stream hdmi          # ... over HDMI, logging every USB command

`show` and `stream` are mutually exclusive input sources — each switches the
controller away from the other, and both stay up until you change modes or
power-cycle. See docs/pattern-mode-design.md for the mode background.
"""

import argparse
import logging
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

logger = logging.getLogger(__name__)

WHITE = (1023, 1023, 1023)
BLACK = (0, 0, 0)

# CLI name -> (test pattern, foreground). All ten generator patterns (0x1203
# values 0-9, DLPU018 Table 2-58) plus "black", a zeroed solid field — see
# docs/pattern-mode-design.md for why value 10 is not exposed.
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

# CLI name -> (receiver, port/clock routing, label, what to check if it never
# locks). DP is the default: 60 Hz vs HDMI's 30 at the same resolution. The
# routing is a property of the timing, not a choice: DP's 269.89 MHz is over the
# 175 MHz single-pixel limit so the IT6535 sends two pixels per clock, while
# HDMI's 139.8 MHz sends one. See docs/pattern-mode-design.md ("Port/clock").
SOURCES = {
    "dp": (
        IT6535Power.DISPLAYPORT,
        DataPort.PORT12_DUAL,
        "DisplayPort",
        "Check the DP cable is in J2, that the DP EDID is flashed "
        "(edid-files/README.md), and that the GPU is driving 2716x1600 @ 60 Hz.",
    ),
    "hdmi": (
        IT6535Power.HDMI,
        DataPort.PORT1_SINGLE,
        "HDMI",
        "Check the HDMI cable and that the GPU is driving the DMD output.",
    ),
}


class _MaxLevel(logging.Filter):
    """Keep records below ``level`` — routes info/debug to stdout, not stderr."""

    def __init__(self, level: int):
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.level


def configure_logging(verbose: bool = False) -> None:
    """Send package logs to the console: results to stdout, problems to stderr.

    Configures the ``dmd_client`` logger rather than the root logger, so
    importing this package never hijacks an application's logging.
    """
    package_logger = logging.getLogger("dmd_client")
    for handler in list(package_logger.handlers):
        package_logger.removeHandler(handler)

    fmt = "%(levelname)s %(name)s: %(message)s" if verbose else "%(message)s"
    out = logging.StreamHandler(sys.stdout)
    out.addFilter(_MaxLevel(logging.WARNING))
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    for handler in (out, err):
        handler.setFormatter(logging.Formatter(fmt))
        package_logger.addHandler(handler)

    package_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    package_logger.propagate = False


def cmd_status(_args: argparse.Namespace) -> int:
    """Report whether a DMD controller is connected."""
    info = find_device()
    if info is None:
        logger.error("DMD not found")
        return 1
    logger.info("Found DMD %04x:%04x", info["vendor_id"], info["product_id"])
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """Display a static internal test pattern on the DMD."""
    if args.list:
        logger.info("Available patterns:\n  %s", "\n  ".join(PATTERNS))
        return 0
    if args.pattern is None:
        logger.error("a pattern name is required (see --list)")
        return 2

    pattern, foreground = PATTERNS[args.pattern]
    try:
        with connect() as dmd:
            dmd.show_test_pattern(pattern, foreground=foreground)
    except (DeviceNotFoundError, DevicePermissionError) as exc:
        logger.error("%s", exc)
        return 1

    logger.info("Showing static '%s' pattern on the DMD.", args.pattern)
    return 0


def cmd_stream(args: argparse.Namespace) -> int:
    """Display the live video signal on the DMD (plain VIDEO mode)."""
    receiver, data_port, source, hint = SOURCES[args.source]

    try:
        with connect() as dmd:
            # Catch a controller/DMD mismatch before touching the video path.
            hw = dmd.hardware_status()
            if hw.incompatible:
                logger.error("controller/DMD incompatible")
                return 1
            if not hw.initialized:
                logger.error("controller not initialized yet")
                return 1

            outcome = dmd.prepare_video_input(
                receiver=receiver, data_port=data_port, force_reset=args.reset
            )
            try:
                lock_s = dmd.wait_for_video_lock()
            except TimeoutError as exc:
                logger.error("%s\n%s", exc, hint)
                return 1
            mode = dmd.operating_mode()
            status = dmd.main_status()
    except (DeviceNotFoundError, DevicePermissionError) as exc:
        logger.error("%s", exc)
        return 1

    if mode is not OperatingMode.VIDEO or not status.streaming:
        logger.error(
            "DMD did not reach a streaming state (mode=%s, locked=%s, "
            "port1_syncs_valid=%s, port2_syncs_valid=%s, video_frozen=%s)",
            mode, status.source_locked, status.port1_syncs_valid,
            status.port2_syncs_valid, status.video_frozen,
        )
        return 1

    dual = data_port in (DataPort.PORT12_DUAL, DataPort.PORT21_DUAL)
    logger.info(
        "DMD is streaming the live %s signal — confirmed by the controller:\n"
        "  mode           %s\n"
        "  source         %s\n"
        "  port 1 syncs   valid\n"
        "  frames         changing (video not frozen)\n"
        "  routing        %s (0x1A03 = %#04x)\n"
        "Native resolution. Stays up until you change modes.",
        source, mode,
        "already locked" if outcome == "unchanged" else f"locked in {lock_s:.1f} s",
        "dual-pixel" if dual else "single-pixel", int(data_port),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dmd", description=__doc__.splitlines()[0])
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="log every USB command sent to the controller",
    )
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
        "stream",
        help="display the live video signal on the DMD (plain VIDEO mode)",
    )
    stream.add_argument(
        "source",
        nargs="?",
        default="dp",
        choices=list(SOURCES),
        metavar="SOURCE",
        help="which video input to display: dp (default, 60 Hz) or hdmi (30 Hz)",
    )
    stream.add_argument(
        "--reset",
        action="store_true",
        help="drop and re-acquire the link even if it is already streaming "
        "(the host sees a monitor unplug/replug)",
    )
    stream.set_defaults(func=cmd_stream)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    if args.command is None:
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
