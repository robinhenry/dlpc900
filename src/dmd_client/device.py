"""Discovery and connection helpers for the TI DLPC900 DMD controller."""

import struct
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum

import hid

# USB identifiers for the Texas Instruments DLPC900
# (DLP LightCrafter 6500 / 9000 evaluation modules).
DLPC900_VENDOR_ID = 0x0451
DLPC900_PRODUCT_ID = 0xC900

# Settle/wait times after mode changes (DLPU018 §2.4.1, §2.3.1.1). Setters don't
# block; the caller waits.
VIDEO_PATTERN_SETTLE_S = 0.3  # video-pattern transition
STANDBY_WAIT_S = 123          # standby: ~2 min to park the array + 3 s

# Pace between USB commands (TI's batch files use 5 ms; we use 10). Buffer
# hygiene; does not fix the video-mode read hang (see docs).
COMMAND_INTERVAL_S = 0.010

# Read timeout. Reads normally answer in ~11 ms; generous since commands aren't
# time-sensitive. Does not affect the video-mode hang (see docs).
READ_TIMEOUT_MS = 5000

# HDMI sync-lock poll. Lock took 3.6 s on the factory EDID; 10 s leaves margin
# without hanging the caller forever on a dead cable or an idle GPU.
LOCK_TIMEOUT_S = 10.0
LOCK_POLL_INTERVAL_S = 0.1

# Shown whenever the device is present but not accessible — almost always
# because the udev permission rule has not been installed yet.
UDEV_HINT = (
    "On Linux this usually means the udev permission rule has not been "
    "installed. See the 'Device permissions on Linux' section of the README."
)


class DeviceNotFoundError(RuntimeError):
    """Raised when no matching DMD controller is connected."""


class DevicePermissionError(RuntimeError):
    """Raised when the DMD is connected but cannot be opened (usually udev)."""


class DeviceError(RuntimeError):
    """Raised when a DLPC900 command gets no reply or an error response."""


def find_device(
    vendor_id: int = DLPC900_VENDOR_ID,
    product_id: int = DLPC900_PRODUCT_ID,
) -> dict | None:
    """Return the info dict for the first connected DMD controller, or ``None``.

    The DLPC900 exposes more than one HID interface; this returns the first.
    """
    devices = hid.enumerate(vendor_id, product_id)
    return devices[0] if devices else None


@contextmanager
def open_device(
    vendor_id: int = DLPC900_VENDOR_ID,
    product_id: int = DLPC900_PRODUCT_ID,
) -> Generator["hid.device"]:
    """Open the first connected DMD controller as a context manager.

    Yields an open ``hid.device`` and closes it automatically on exit, even if
    an exception is raised inside the ``with`` block::

        with open_device() as dev:
            dev.write(...)

    :raises DeviceNotFoundError: if no matching device is present.
    :raises DevicePermissionError: if the device is present but cannot be
        opened (on Linux, usually because the udev rule is not installed).
    """
    info = find_device(vendor_id, product_id)
    if info is None:
        raise DeviceNotFoundError(
            f"No DMD controller found (looking for "
            f"{vendor_id:04x}:{product_id:04x}). Is it plugged in and powered on?"
        )

    dev = hid.device()
    try:
        # Open by path so we deterministically pick the enumerated interface
        # (opening by vid/pid alone is ambiguous when several interfaces match).
        dev.open_path(info["path"])
    except OSError as exc:
        raise DevicePermissionError(
            f"Found the DMD controller ({vendor_id:04x}:{product_id:04x}) but "
            f"could not open it ({exc}).\n{UDEV_HINT}"
        ) from exc

    try:
        yield dev
    finally:
        dev.close()


def read(dev: "hid.device", command: int, seq: int = 1) -> bytes:
    """Send a DLPC900 read command and return the response payload bytes.

    ``dev`` is an open handle from :func:`open_device`. ``command`` is the
    16-bit USB command (e.g. ``0x1A0A`` for hardware status); see the DLPC900
    Programmer's Guide (DLPU018). Response framing: byte 0 flag, byte 1 seq,
    bytes 2-3 payload length (little-endian), bytes 4.. payload.

    :raises DeviceError: on no reply or an error response.
    """
    packet = [0x00, 0xC0, seq, 0x02, 0x00, command & 0xFF, (command >> 8) & 0xFF]
    dev.write(packet + [0x00] * (65 - len(packet)))
    time.sleep(COMMAND_INTERVAL_S)  # let the controller prepare the response

    resp = dev.read(64, timeout_ms=READ_TIMEOUT_MS)
    time.sleep(COMMAND_INTERVAL_S)  # pace subsequent commands
    if not resp:
        raise DeviceError(f"no response to command {command:#06x}")
    if resp[0] & 0x20:  # bit 5 of the flag byte = error
        raise DeviceError(f"command {command:#06x} failed (flag {resp[0]:#04x})")

    length = resp[2] | (resp[3] << 8)
    return bytes(resp[4:4 + length])


def write(dev: "hid.device", command: int, data: bytes = b"", seq: int = 1) -> None:
    """Send a DLPC900 write command with an optional data payload.

    ``dev`` is an open handle from :func:`open_device`. ``command`` is the
    16-bit USB command; ``data`` is its parameter bytes (LSB first, per the
    command's definition in the Programmer's Guide). Uses flag 0x00 (write, no
    reply) per DLPU018 §1.2.3. The payload must fit a single 64-byte HID report
    (at most 58 data bytes).
    """
    payload = [command & 0xFF, (command >> 8) & 0xFF, *data]
    length = len(payload)
    packet = [0x00, 0x00, seq, length & 0xFF, (length >> 8) & 0xFF, *payload]
    if len(packet) > 65:
        raise ValueError("payload too large for a single HID transfer")
    dev.write(packet + [0x00] * (65 - len(packet)))
    time.sleep(COMMAND_INTERVAL_S)  # pace subsequent commands (see COMMAND_INTERVAL_S)


# --- Status snapshot --------------------------------------------------------
# Immutable decoding of the DLPC900 hardware status byte at a single instant,
# read fresh from the device. Bit meanings follow the DLPC900 Programmer's
# Guide (DLPU018).


@dataclass(frozen=True)
class HardwareStatus:
    """Decoded hardware status byte (command 0x1A0A)."""

    initialized: bool           # bit 0: internal initialization done
    incompatible: bool          # bit 1: incompatible controller/DMD (error)
    reset_controller_error: bool  # bit 2
    forced_swap_error: bool     # bit 3
    secondary_ready: bool       # bit 4: secondary controller present & ready
    sequencer_abort: bool       # bit 6
    sequencer_error: bool       # bit 7

    @classmethod
    def from_byte(cls, b: int) -> "HardwareStatus":
        return cls(
            initialized=bool(b & 0x01),
            incompatible=bool(b & 0x02),
            reset_controller_error=bool(b & 0x04),
            forced_swap_error=bool(b & 0x08),
            secondary_ready=bool(b & 0x10),
            sequencer_abort=bool(b & 0x40),
            sequencer_error=bool(b & 0x80),
        )


@dataclass(frozen=True)
class MainStatus:
    """Decoded main status byte (command 0x1A0C, DLPU018 Table 2-10).

    ``source_locked`` + ``port1_syncs_valid`` are the two bits that say the HDMI
    input has actually synced — the difference between assuming a blind wait was
    long enough and knowing the controller is consuming the video.
    """

    parked: bool              # bit 0: micromirrors parked
    sequencer_running: bool   # bit 1
    video_frozen: bool        # bit 2: displaying a single frame, not new ones
    source_locked: bool       # bit 3: external video source locked
    port1_syncs_valid: bool   # bit 4
    port2_syncs_valid: bool   # bit 5

    @classmethod
    def from_byte(cls, b: int) -> "MainStatus":
        return cls(
            parked=bool(b & 0x01),
            sequencer_running=bool(b & 0x02),
            video_frozen=bool(b & 0x04),
            source_locked=bool(b & 0x08),
            port1_syncs_valid=bool(b & 0x10),
            port2_syncs_valid=bool(b & 0x20),
        )

    @property
    def streaming(self) -> bool:
        """True when locked to the source and showing new frames.

        Port 2 is deliberately not required, even at dual-pixel/60 Hz. This is a
        *dual-controller* board (DLP670S behind two DLPC900s): the Video Splitter
        FPGA hands each controller half the array, ~135 Mpix/s at 2716x1600@60 —
        under the 175 MHz single-pixel limit — so each controller reads its half
        on port 1 alone. Status is also read from the primary controller only
        (DLPU102 3.7), so the secondary's state never appears here regardless.

        Measured on our board over the DP input: ``port2_syncs_valid`` is False
        with 0x1A03 = 0x02 (dual) *and* 0x00 (single), identically. Requiring it
        only produces false failures. On a single-controller EVM taking the whole
        stream, dual-pixel would genuinely need both ports.
        """
        return self.source_locked and self.port1_syncs_valid and not self.video_frozen


class _NamedIntEnum(IntEnum):
    """IntEnum that prints its member name instead of the raw number."""

    def __str__(self) -> str:
        return self.name


class PowerMode(_NamedIntEnum):
    """DLPC900 power state (command 0x0200)."""

    NORMAL = 0
    STANDBY = 1


class OperatingMode(_NamedIntEnum):
    """DLPC900 display/operating mode (command 0x1A1B)."""

    VIDEO = 0
    PRE_STORED_PATTERN = 1
    VIDEO_PATTERN = 2
    PATTERN_ON_THE_FLY = 3


class LedSelect(_NamedIntEnum):
    """Which LED(s) illuminate a pattern (LUT byte 5, bits 6:4).

    NONE (all LEDs disabled) is the choice for external illumination — the
    mirrors still display the pattern; the DLPC900's own LED driver stays off.
    """

    NONE = 0b000
    RED = 0b001
    GREEN = 0b010
    YELLOW = 0b011
    BLUE = 0b100
    MAGENTA = 0b101
    CYAN = 0b110
    WHITE = 0b111


class IT6535Power(_NamedIntEnum):
    """IT6535 digital-receiver power state (command 0x1A01)."""

    POWER_DOWN = 0
    HDMI = 1
    DISPLAYPORT = 2


class InputSource(_NamedIntEnum):
    """DLPC900 input source / interface mode (command 0x1A00, byte 0 bits 2:0)."""

    PARALLEL = 0        # parallel RGB (what the IT6535 feeds from HDMI/DP)
    TEST_PATTERN = 1    # internal test pattern generator
    FLASH = 2           # still images from flash
    CURTAIN = 3         # solid color curtain


class TestPattern(_NamedIntEnum):
    """Internal test-pattern generator selection (command 0x1203, byte 0 bits 3:0).

    Displayed only when the input source is ``InputSource.TEST_PATTERN``. These
    are generated on-controller (no HDMI/GPU needed) at the DMD's native
    resolution — ideal as a fixed reference for optical alignment. GRID and
    CHECKERBOARD have a foreground/background split (see set_test_pattern_color).
    """

    SOLID_FIELD = 0
    HORIZONTAL_RAMP = 1
    VERTICAL_RAMP = 2
    HORIZONTAL_LINES = 3
    DIAGONAL_LINES = 4
    VERTICAL_LINES = 5
    GRID = 6
    CHECKERBOARD = 7
    RGB_RAMP = 8
    COLOR_BARS = 9
    NONE = 10  # blank — keeps displaying whatever was displaying before


class ParallelBitDepth(_NamedIntEnum):
    """Parallel-interface bit depth (command 0x1A00, byte 0 bits 4:3)."""

    BITS_30 = 0
    BITS_24 = 1
    BITS_20 = 2
    BITS_16 = 3


class DataPort(_NamedIntEnum):
    """Parallel data port / pixel mode (command 0x1A03, byte 0 bits 1:0)."""

    PORT1_SINGLE = 0
    PORT2_SINGLE = 1
    PORT12_DUAL = 2   # dual pixel, even pixel on port 1
    PORT21_DUAL = 3   # dual pixel, even pixel on port 2


@dataclass(frozen=True)
class PatternEntry:
    """One entry in the Pattern Display LUT for Video Pattern Mode.

    Encodes to the Pattern Display LUT Definition command (0x1A34, DLPU018
    Table 2-143). In Video Pattern Mode the pixel data arrives over HDMI;
    ``frame_bit_position`` (0-23) selects which bit-plane of the incoming frame
    this entry shows, and ``bit_depth`` (1-8) how many consecutive planes it
    spans.
    """

    exposure_us: int                        # on-time (bytes 4:2)
    bit_depth: int                          # 1-8 (byte 5, bits 3:1)
    frame_bit_position: int                 # 0-23 (byte 11, bits 15:11)
    dark_time_us: int = 0                   # off-time after exposure (bytes 8:6)
    led_select: LedSelect = LedSelect.NONE  # illumination (byte 5, bits 6:4)

    def encode(self, index: int) -> bytes:
        """Return the 12-byte LUT Definition payload for LUT position ``index``."""
        if not 1 <= self.bit_depth <= 8:
            raise ValueError("bit_depth must be 1..8")
        if not 0 <= self.frame_bit_position <= 23:
            raise ValueError("frame_bit_position must be 0..23")
        if not 0 <= self.exposure_us < (1 << 24):
            raise ValueError("exposure_us out of range (24-bit µs)")
        if not 0 <= self.dark_time_us < (1 << 24):
            raise ValueError("dark_time_us out of range (24-bit µs)")

        b = bytearray(12)
        b[0:2] = struct.pack("<H", index)               # pattern index = position
        b[2:5] = self.exposure_us.to_bytes(3, "little")
        b[5] = ((self.bit_depth - 1) << 1) | (int(self.led_select) << 4)
        b[6:9] = self.dark_time_us.to_bytes(3, "little")
        b[9] = 0                                         # TRIG_OUT_2 enabled (spare)
        b[10:12] = struct.pack("<H", (self.frame_bit_position & 0x1F) << 11)
        return bytes(b)


# A 24-bit RGB HDMI frame is 24 stacked 1-bit planes; an N-bit pattern consumes
# N of them, so one frame carries 24 // N patterns (DLPU018 §2.4.1).
HDMI_FRAME_BITPLANES = 24


@dataclass(frozen=True)
class PatternSequence:
    """An ordered list of :class:`PatternEntry`, displayed in order.

    ``repeat=True`` loops forever (NP=0); ``repeat=False`` plays once (NP=len).
    """

    entries: list[PatternEntry]
    repeat: bool = True

    @classmethod
    def frame_filling(
        cls,
        bit_depth: int,
        refresh_hz: float = 30.0,
        led_select: LedSelect = LedSelect.NONE,
        repeat: bool = True,
    ) -> "PatternSequence":
        """Build an N-bit sequence whose exposures exactly fill one HDMI frame.

        In Video Pattern Mode the sequencer must consume incoming frames at the
        rate they arrive: the LUT's total on-time has to equal the HDMI frame
        period, or the sequencer drifts against the video and the controller
        flickers/freezes (DLPU018 §2.4.1; matches the TI E2E "Video Pattern Mode
        Frozen" fix — 3 × 8-bit at 30 Hz ⇒ ~11 ms each, not the ~1.5 ms that
        caused the freeze). This packs ``24 // bit_depth`` patterns into the
        frame, each reading the next ``bit_depth`` bit-planes, with exposure
        ``frame_period / patterns_per_frame`` — the rounding remainder folded
        into the last entry so the total lands exactly on the frame period.

        ``refresh_hz`` is the refresh the GPU actually outputs (30 Hz for the
        stock native-resolution single-pixel EDID). Free-running, no dark time,
        externally illuminated (``LedSelect.NONE``) by default — no triggers.
        """
        if not 1 <= bit_depth <= 8:
            raise ValueError("bit_depth must be 1..8")
        if refresh_hz <= 0:
            raise ValueError("refresh_hz must be positive")

        patterns_per_frame = HDMI_FRAME_BITPLANES // bit_depth
        frame_us = round(1_000_000 / refresh_hz)
        exposure_us = frame_us // patterns_per_frame
        remainder_us = frame_us - exposure_us * patterns_per_frame

        entries = [
            PatternEntry(
                exposure_us=exposure_us
                + (remainder_us if i == patterns_per_frame - 1 else 0),
                bit_depth=bit_depth,
                frame_bit_position=i * bit_depth,
                led_select=led_select,
            )
            for i in range(patterns_per_frame)
        ]
        return cls(entries=entries, repeat=repeat)


class DMD:
    """A connected DLPC900 controller.

    Wraps an open ``hid.device`` handle and exposes commands. It holds the
    *connection*, not device state: each status method reads a fresh value.
    Use via :func:`connect`::

        with connect() as dmd:
            if dmd.hardware_status().sequencer_error:
                ...
    """

    def __init__(self, dev: "hid.device"):
        self._dev = dev

    def read(self, command: int, seq: int = 1) -> bytes:
        """Send a read command and return its payload (see :func:`read`)."""
        return read(self._dev, command, seq)

    def write(self, command: int, data: bytes = b"", seq: int = 1) -> None:
        """Send a write command with optional data (see :func:`write`)."""
        write(self._dev, command, data, seq)

    def firmware_tag(self) -> str:
        """Firmware build tag string, e.g. ``"dlp670S_6.2.0_OOB"`` (0x0206)."""
        return self.read(0x0206)[1:].split(b"\x00")[0].decode("ascii", "replace")

    def firmware_version(self) -> tuple[int, int, int]:
        """Application firmware version as ``(major, minor, patch)`` (0x0205)."""
        w = int.from_bytes(self.read(0x0205)[0:4], "little")
        return ((w >> 24) & 0xFF, (w >> 16) & 0xFF, w & 0xFFFF)

    def dmd_type(self) -> int:
        """DMD/chipset type byte, e.g. ``0x03`` for the DLP670S (0x0206)."""
        return self.read(0x0206)[0]

    def hardware_status(self) -> HardwareStatus:
        return HardwareStatus.from_byte(self.read(0x1A0A)[0])

    def main_status(self) -> MainStatus:
        """Park/sequencer/frame-buffer status, incl. the HDMI sync-lock bits (0x1A0C)."""
        return MainStatus.from_byte(self.read(0x1A0C)[0])

    def operating_mode(self) -> OperatingMode:
        """Read back the current display mode (0x1A1B).

        Safe in a settled VIDEO mode. Do NOT call it right after switching to
        VIDEO_PATTERN: the guide gives the read ~300 ms of unreliability there,
        and in practice that read hung the controller (docs/pattern-mode-design.md).
        """
        return OperatingMode(self.read(0x1A1B)[0] & 0x03)

    # --- Power / safe shutdown -------------------------------------------------

    def set_power_mode(self, mode: PowerMode) -> None:
        """Set the power state — NORMAL or STANDBY (0x0200).

        Entering STANDBY runs a ~2-minute park precondition (wait
        ``STANDBY_WAIT_S``); this call does not block. Once in standby only
        NORMAL is accepted (DLPU018 §2.3.1.1). For a safe power-off use
        :meth:`prepare_for_shutdown`.
        """
        self.write(0x0200, bytes([int(mode)]))

    def prepare_for_shutdown(self, wait: bool = True) -> None:
        """Park the mirrors and enter Standby so the DMD can be safely powered off.

        Required before any planned power-down (DLPU018 §4.2, §2.3.1.1): Standby
        parks the micromirrors and disables the front-end input interfaces,
        protecting the DMD array. Stops any active display first, then issues
        Power Standby (0x0200 = 1).

        Standby runs a ~2-minute precondition to park the array (plus ~3 s); with
        ``wait=True`` (default) this blocks ``STANDBY_WAIT_S`` until parking is
        complete, after which it is safe to remove power. Writes only — no status
        reads (which can hang the controller). A power-cycle also clears standby.
        """
        self.stop_patterns()
        self.set_power_mode(PowerMode.STANDBY)
        if wait:
            time.sleep(STANDBY_WAIT_S)

    def set_operating_mode(self, mode: OperatingMode) -> None:
        """Set the display/operating mode (0x1A1B). To enter VIDEO_PATTERN use
        configure_pattern_sequence instead; reading the mode back right after
        switching can hang the controller.
        """
        self.write(0x1A1B, bytes([int(mode)]))

    # --- Input / illumination config ------------------------------------------

    def disable_gamma(self) -> None:
        """Disable gamma so pixel value maps linearly to duty cycle (0x1A3B).

        Recommended for quantitative/flat-field use — see docs/pattern-mode-design.md.
        """
        self.write(0x1A3B, bytes([0, 0]))

    def set_it6535_power(self, mode: IT6535Power) -> None:
        """Power the IT6535 HDMI/DisplayPort receiver (0x1A01).

        Use ``IT6535Power.HDMI`` to accept pattern data streamed from the GPU.
        Ignored if the IT6535 is absent.
        """
        self.write(0x1A01, bytes([int(mode)]))

    def set_input_source(
        self,
        source: InputSource,
        bit_depth: ParallelBitDepth = ParallelBitDepth.BITS_24,
    ) -> None:
        """Select the input source and parallel bit depth (0x1A00).

        For GPU-over-HDMI patterns use ``PARALLEL`` + ``BITS_24`` (pattern modes
        use at most 24 bits). ``bit_depth`` applies only to the parallel source.
        Note: the port/clock routing (0x1A03) is board-specific (see the EVM
        guide) and is not set here.
        """
        self.write(0x1A00, bytes([(int(source) & 0x07) | (int(bit_depth) << 3)]))

    def set_port_clock(self, data_port: DataPort = DataPort.PORT1_SINGLE) -> None:
        """Select the parallel data port / pixel mode (0x1A03).

        BOARD-SPECIFIC. Single- vs dual-pixel depends on the HDMI video timing.
        ``PORT1_SINGLE`` (the reset value, 0x00) is the validated HDMI lock on
        our board; dual-pixel (``PORT12_DUAL``, 0x02) needs the matching EDID.
        The pixel-clock/data-enable/sync select bits are 0 for both, so only the
        port field is set here.
        """
        self.write(0x1A03, bytes([int(data_port) & 0x03]))

    def prepare_video_input(
        self,
        receiver: IT6535Power = IT6535Power.HDMI,
        data_port: DataPort = DataPort.PORT1_SINGLE,
    ) -> None:
        """Set up the HDMI/DP input and enter VIDEO mode, the precondition for
        VIDEO_PATTERN. Writes only: stop, power receiver, parallel 24-bit input,
        port/clock, gamma off, VIDEO mode. Then wait blind for lock ->
        configure_pattern_sequence -> start_patterns.

        data_port defaults to single-pixel (dual needs the matching EDID).
        Status reads in a video-active mode can intermittently hang the
        controller (SW1 to recover); avoid them — confirm lock externally.
        """
        self.stop_patterns()
        self.set_it6535_power(receiver)
        self.set_input_source(InputSource.PARALLEL, ParallelBitDepth.BITS_24)
        self.set_port_clock(data_port)
        self.disable_gamma()
        self.set_operating_mode(OperatingMode.VIDEO)

    def wait_for_video_lock(
        self,
        timeout_s: float = LOCK_TIMEOUT_S,
        interval_s: float = LOCK_POLL_INTERVAL_S,
    ) -> float:
        """Poll main status until the video source locks; return the seconds it took.

        Replaces a blind wait in plain VIDEO mode: it returns as soon as the
        source is actually locked, and distinguishes "not locked yet" from "never
        going to lock" (dead cable, GPU not driving the output). Waits on port 1
        only — see :attr:`MainStatus.streaming` for why port 2 never comes up on
        this board.

        Only for VIDEO mode. Status reads in a video-active mode were long treated
        as unsafe (docs/pattern-mode-design.md), but that no longer reproduces on
        the reduced-blanking EDID: 93,422 reads across 6 VIDEO-mode entries, up to
        479 reads/s, zero wedges. The VIDEO_PATTERN path is *not* covered by that
        evidence — keep it read-free.

        :raises TimeoutError: if the source has not locked within ``timeout_s``.
        """
        deadline = time.monotonic() + timeout_s
        start = time.monotonic()
        while True:
            status = self.main_status()
            if status.source_locked and status.port1_syncs_valid:
                return time.monotonic() - start
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"video source did not lock within {timeout_s:g} s "
                    f"(source_locked={status.source_locked}, "
                    f"port1_syncs_valid={status.port1_syncs_valid}, "
                    f"port2_syncs_valid={status.port2_syncs_valid})"
                )
            time.sleep(interval_s)

    # --- Internal test pattern (static, for optical alignment) ----------------

    def set_test_pattern(self, pattern: TestPattern) -> None:
        """Select the internal test pattern (0x1203, DLPU018 Table 2-58).

        Takes effect only while the input source is ``InputSource.TEST_PATTERN``.
        """
        self.write(0x1203, bytes([int(pattern) & 0x0F]))

    def set_test_pattern_color(
        self,
        foreground: tuple[int, int, int] = (1023, 1023, 1023),
        background: tuple[int, int, int] = (0, 0, 0),
    ) -> None:
        """Set internal test-pattern colors (0x1204, DLPU018 Table 2-60).

        ``foreground``/``background`` are (R, G, B), each 0-1023. Foreground
        applies to all patterns; background only to split patterns (lines, grid,
        checkerboard). Defaults are the reset values — white on black.
        """
        values = (*foreground, *background)
        if any(not 0 <= v <= 1023 for v in values):
            raise ValueError("color intensities must be 0..1023")
        self.write(0x1204, struct.pack("<6H", *values))

    def show_test_pattern(
        self,
        pattern: TestPattern = TestPattern.GRID,
        foreground: tuple[int, int, int] = (1023, 1023, 1023),
        background: tuple[int, int, int] = (0, 0, 0),
        settle_s: float = VIDEO_PATTERN_SETTLE_S,
    ) -> None:
        """Display a static internal test pattern for optical alignment.

        Brings the DMD from any state to a fixed, controller-generated pattern
        on the mirrors — no HDMI source, GPU, LUT, or camera sync involved, so
        the image never changes. One call: stop any running sequence, enter
        VIDEO mode, then switch the input to the internal generator and select
        the pattern + colors.

        Order matters. VIDEO mode goes first, before the source select, for two
        reasons: the 0x1A1B write re-initializes the video pipeline (selecting
        the source first lets it fall back to the parallel/HDMI input, so the
        HDMI image shows instead of the pattern), and per the DLPU018 command
        matrix (Table A-2) Test Pattern Select/Color are not accepted in
        VIDEO_PATTERN or the flash pattern modes, which is where a previous
        acquisition run leaves the controller.

        Writes only, then a blind settle wait — no status reads, since reads in a
        video-active mode can hang the controller (SW1 to recover; see
        docs/pattern-mode-design.md). Confirm the result by looking at the DMD,
        not by polling USB status.
        """
        self.stop_patterns()
        self.set_operating_mode(OperatingMode.VIDEO)
        time.sleep(settle_s)  # let the video pipeline re-init before selecting
        self.set_input_source(InputSource.TEST_PATTERN)
        self.set_test_pattern(pattern)
        self.set_test_pattern_color(foreground, background)
        time.sleep(settle_s)

    # --- Pattern display (Video Pattern Mode) ---------------------------------

    def configure_pattern_sequence(self, sequence: PatternSequence) -> None:
        """Switch to VIDEO_PATTERN and load the LUT (DLPU018 Table 5-1). Call in
        locked VIDEO mode, then start_patterns. Writes only, no reads: stop, set
        VIDEO_PATTERN, one Definition (0x1A34) per entry, Configuration (0x1A31).
        Don't read status while patterns run — it can hang the controller.
        """
        self.stop_patterns()
        self.set_operating_mode(OperatingMode.VIDEO_PATTERN)
        for index, entry in enumerate(sequence.entries):
            self.write(0x1A34, entry.encode(index))
        n = len(sequence.entries)
        num_to_display = 0 if sequence.repeat else n
        self.write(0x1A31, struct.pack("<HI", n, num_to_display))

    def start_patterns(self) -> None:
        """Start the pattern display sequence (0x1A24)."""
        self.write(0x1A24, bytes([2]))

    def stop_patterns(self) -> None:
        """Stop the pattern display sequence (0x1A24)."""
        self.write(0x1A24, bytes([0]))

    def run_pattern_sequence(
        self,
        sequence: PatternSequence,
        receiver: IT6535Power = IT6535Power.HDMI,
        data_port: DataPort = DataPort.PORT1_SINGLE,
        lock_wait_s: float = 3.0,
    ) -> None:
        """Bring the DMD from any state to running `sequence` in Video Pattern
        Mode. One call: stop + set up HDMI input + VIDEO mode, wait lock_wait_s
        for the source to sync-lock, switch to VIDEO_PATTERN + load the LUT,
        start.

        Writes only, so it can't hang the controller (a blind wait replaces the
        status poll). Assumes NORMAL power (the power-on default) and a live HDMI
        source; confirm it's actually running with a scope on TRIG_OUT_1.
        """
        self.prepare_video_input(receiver, data_port)
        time.sleep(lock_wait_s)
        self.configure_pattern_sequence(sequence)
        self.start_patterns()


@contextmanager
def connect(
    vendor_id: int = DLPC900_VENDOR_ID,
    product_id: int = DLPC900_PRODUCT_ID,
) -> Generator["DMD"]:
    """Open the DMD and yield a :class:`DMD`, closing it automatically on exit.

    Raises the same errors as :func:`open_device`.
    """
    with open_device(vendor_id, product_id) as dev:
        yield DMD(dev)
