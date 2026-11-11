"""Unit tests for device.py — no hardware, hid is mocked.

Scope: the things that break silently. Wire framing, register bit-packing, and
the command *orders* that hardware debugging established (see
docs/pattern-mode-design.md). Not covered: anything only the DMD can answer.
"""

import struct

import pytest

import dlpc900.device as device

INFO = {"path": b"1-4:1.0", "vendor_id": 0x0451, "product_id": 0xC900}


class FakeHid:
    """Stand-in for hid.device(): records close(), can fail to open."""

    def __init__(self, open_error=None):
        self.open_error = open_error
        self.closed = False

    def open_path(self, path):
        if self.open_error:
            raise self.open_error

    def close(self):
        self.closed = True


class FakeReadDev:
    """Records every written packet and returns a canned response."""

    def __init__(self, response=None):
        self.response = response
        self.writes = []

    @property
    def written(self):
        return self.writes[-1] if self.writes else None

    def write(self, data):
        self.writes.append(list(data))

    def read(self, size, timeout_ms=0):
        return self.response


def _cmd(packet):
    """USB command from a written packet (bytes 5,6 = command LSB, MSB)."""
    return packet[5] | (packet[6] << 8)


def _data(packet):
    """Payload bytes of a written packet (after the 2-byte command)."""
    return packet[7:]


# --- transport --------------------------------------------------------------


def test_open_device_permission_error(monkeypatch):
    monkeypatch.setattr(device.hid, "enumerate", lambda *_: [INFO])
    monkeypatch.setattr(device.hid, "device", lambda: FakeHid(OSError("open failed")))
    with pytest.raises(device.DevicePermissionError):
        with device.open_device():
            pass


def test_read_encodes_command_and_parses_payload():
    # response: flag=0xC0, seq=1, length=2, payload=[0xAB, 0xCD]
    dev = FakeReadDev([0xC0, 1, 0x02, 0x00, 0xAB, 0xCD] + [0] * 58)
    assert device.read(dev, 0x1A0A) == bytes([0xAB, 0xCD])
    # command 0x1A0A goes out LSB-first at bytes 5,6 (after report id + flag).
    assert dev.written[5] == 0x0A and dev.written[6] == 0x1A


def test_write_encodes_flag_length_command_and_data():
    dev = FakeReadDev(None)
    device.write(dev, 0x1A1B, bytes([0x02]))
    p = dev.written
    assert p[0] == 0x00 and p[1] == 0x00           # report id, flag (write/no reply)
    assert p[3] == 0x03 and p[4] == 0x00           # length = 2 command + 1 data
    assert p[5] == 0x1B and p[6] == 0x1A           # command LSB, MSB
    assert p[7] == 0x02                            # data byte


@pytest.mark.parametrize(
    "response",
    [
        pytest.param([0xE0, 1, 0, 0] + [0] * 60, id="error-flag"),   # bit 5 set
        pytest.param(None, id="no-response"),                        # wedged
    ],
)
def test_read_raises_device_error(response):
    # A wedged controller returns nothing; that must surface as DeviceError, not
    # as a TypeError from touching the missing response.
    with pytest.raises(device.DeviceError):
        device.read(FakeReadDev(response), 0x1A0A)


# --- status decoding --------------------------------------------------------


def test_hardware_status_decodes_bits():
    # payload byte 0b1000_0011 -> bits 0, 1, 7; read through DMD to cover wiring.
    dev = FakeReadDev([0xC0, 1, 0x01, 0x00, 0b1000_0011] + [0] * 59)
    hw = device.DMD(dev).hardware_status()
    assert hw.initialized and hw.incompatible and hw.sequencer_error
    assert not hw.secondary_ready


def test_main_status_decodes_bits():
    # 0b011010 — the byte the board returns while streaming.
    s = device.MainStatus.from_byte(0b011010)
    assert s.sequencer_running and s.source_locked and s.port1_syncs_valid
    assert not s.parked and not s.video_frozen and not s.port2_syncs_valid


@pytest.mark.parametrize(
    "byte, streaming, why",
    [
        (0b011010, True, "locked, port 1 synced, frames changing"),
        # port 2 stays False on a *working* dual-pixel link — the bit is not
        # reported on this board, so gating on it would reject a good link.
        (0b111010, True, "port 2 also synced"),
        (0b011100, False, "frozen: locked but stuck on one frame"),
        (0b000000, False, "not locked"),
    ],
)
def test_streaming_truth_table(byte, streaming, why):
    assert device.MainStatus.from_byte(byte).streaming is streaming, why


def test_operating_mode_reads_the_mode_byte():
    dev = FakeReadDev([0xC0, 1, 0x01, 0x00, int(device.OperatingMode.VIDEO)] + [0] * 59)
    assert device.DMD(dev).operating_mode() is device.OperatingMode.VIDEO
    assert _cmd(dev.written) == 0x1A1B


# --- register encoding ------------------------------------------------------


@pytest.mark.parametrize(
    "call, command, expected",
    [
        (
            lambda d: d.set_input_source(
                device.InputSource.PARALLEL, device.ParallelBitDepth.BITS_24
            ),
            0x1A00,
            0 | (1 << 3),                                  # source | depth<<3
        ),
        (
            lambda d: d.set_port_clock(device.DataPort.PORT12_DUAL),
            0x1A03,
            0x02,                                          # dual-pixel
        ),
        (
            lambda d: d.set_test_pattern(device.TestPattern.CHECKERBOARD),
            0x1203,
            7,                                             # DLPU018 Table 2-58
        ),
        (
            lambda d: d.set_operating_mode(device.OperatingMode.VIDEO_PATTERN),
            0x1A1B,
            2,
        ),
    ],
)
def test_single_byte_commands(call, command, expected):
    dev = FakeReadDev()
    call(device.DMD(dev))
    assert _cmd(dev.written) == command
    assert _data(dev.written)[0] == expected


def test_test_pattern_color_packs_six_le_words():
    dev = FakeReadDev()
    device.DMD(dev).set_test_pattern_color(
        foreground=(1023, 512, 0), background=(0, 0, 1023)
    )
    assert _cmd(dev.written) == 0x1204
    assert struct.unpack("<6H", bytes(_data(dev.written)[:12])) == (
        1023, 512, 0, 0, 0, 1023,
    )


def test_test_pattern_color_rejects_out_of_range():
    with pytest.raises(ValueError):
        device.DMD(FakeReadDev()).set_test_pattern_color(foreground=(1024, 0, 0))


def test_pattern_entry_encodes_lut_definition():
    b = device.PatternEntry(
        exposure_us=500, bit_depth=8, frame_bit_position=0, dark_time_us=100
    ).encode(index=3)
    assert len(b) == 12
    assert int.from_bytes(b[0:2], "little") == 3             # LUT position
    assert int.from_bytes(b[2:5], "little") == 500           # exposure
    assert (b[5] >> 1) & 0x07 == 7                           # bit depth 8 -> code 7
    assert (b[5] >> 4) & 0x07 == 0                           # LED NONE
    assert b[5] & 0x01 == 0 and b[5] >> 7 == 0               # clear/wait off
    assert int.from_bytes(b[6:9], "little") == 100           # dark time
    assert b[9] == 0                                         # TRIG_OUT_2 enabled
    assert int.from_bytes(b[10:12], "little") >> 11 == 0     # frame bit position


def test_pattern_entry_packs_depth_code_and_frame_position():
    b = device.PatternEntry(
        exposure_us=100, bit_depth=1, frame_bit_position=23
    ).encode(0)
    assert (int.from_bytes(b[10:12], "little") >> 11) & 0x1F == 23
    assert (b[5] >> 1) & 0x07 == 0                           # 1-bit -> code 0


def test_pattern_entry_validates_bit_depth():
    with pytest.raises(ValueError):
        device.PatternEntry(
            exposure_us=100, bit_depth=9, frame_bit_position=0
        ).encode(0)


# --- frame-filling maths ----------------------------------------------------
# The one invariant that matters: total on-time == the HDMI frame period. If it
# drifts, the sequencer runs against the video and the controller flickers or
# freezes (the E2E "Video Pattern Mode Frozen" failure).


@pytest.mark.parametrize(
    "bit_depth, refresh_hz, count, first_us, last_us",
    [
        (8, 30, 3, 11111, 11111),   # the known-good E2E reference case
        (6, 30, 4, 8333, 8334),     # remainder folded into the last entry
        (1, 30, 24, 1388, 1409),    # every bit-plane of the frame
        (8, 60, 3, 5555, 5557),     # halved frame period at 60 Hz
    ],
)
def test_frame_filling_fills_exactly_one_frame(
    bit_depth, refresh_hz, count, first_us, last_us
):
    seq = device.PatternSequence.frame_filling(bit_depth, refresh_hz)
    assert len(seq.entries) == count
    assert [e.frame_bit_position for e in seq.entries] == [
        i * bit_depth for i in range(count)
    ]
    assert all(e.bit_depth == bit_depth for e in seq.entries)
    assert all(e.led_select is device.LedSelect.NONE for e in seq.entries)
    assert seq.entries[0].exposure_us == first_us
    assert seq.entries[-1].exposure_us == last_us
    assert sum(e.exposure_us for e in seq.entries) == round(1_000_000 / refresh_hz)


@pytest.mark.parametrize(
    "kwargs",
    [{"bit_depth": 0}, {"bit_depth": 9}, {"bit_depth": 8, "refresh_hz": 0}],
)
def test_frame_filling_rejects_bad_args(kwargs):
    with pytest.raises(ValueError):
        device.PatternSequence.frame_filling(**kwargs)


# --- command sequences ------------------------------------------------------
# Order here is hard-won, not arbitrary; the comments say what breaks if it is
# changed. See docs/pattern-mode-design.md.


@pytest.mark.parametrize("repeat, expected_np", [(False, 2), (True, 0)])
def test_configure_pattern_sequence_definitions_then_config(repeat, expected_np):
    dev = FakeReadDev()
    seq = device.PatternSequence(
        entries=[
            device.PatternEntry(exposure_us=100, bit_depth=1, frame_bit_position=0),
            device.PatternEntry(exposure_us=100, bit_depth=1, frame_bit_position=1),
        ],
        repeat=repeat,
    )
    device.DMD(dev).configure_pattern_sequence(seq)
    # stop, set VIDEO_PATTERN, define, define, configure (Table 5-1, writes only)
    assert [_cmd(w) for w in dev.writes] == [0x1A24, 0x1A1B, 0x1A34, 0x1A34, 0x1A31]
    assert _data(dev.writes[1])[0] == int(device.OperatingMode.VIDEO_PATTERN)
    config = _data(dev.writes[-1])
    assert config[0] | (config[1] << 8) == 2                          # NL = 2 entries
    assert int.from_bytes(bytes(config[2:6]), "little") == expected_np  # NP


def test_show_test_pattern_writes_only_in_order():
    dev = FakeReadDev()
    device.DMD(dev).show_test_pattern(device.TestPattern.GRID, settle_s=0)
    # VIDEO mode precedes the source select: the mode write re-inits the video
    # pipeline, and 0x1203/0x1204 are not accepted in the pattern modes (DLPU018
    # Table A-2). Writes only — no status reads.
    assert [_cmd(w) for w in dev.writes] == [0x1A24, 0x1A1B, 0x1A00, 0x1203, 0x1204]
    by_cmd = {_cmd(w): _data(w) for w in dev.writes}
    assert by_cmd[0x1A00][0] & 0x07 == int(device.InputSource.TEST_PATTERN)
    assert by_cmd[0x1203][0] == int(device.TestPattern.GRID)
    assert by_cmd[0x1A1B][0] == int(device.OperatingMode.VIDEO)


def test_prepare_video_input_command_order(monkeypatch):
    monkeypatch.setattr(device.time, "sleep", lambda _s: None)
    dev = FakeReadDev()
    device.DMD(dev).prepare_video_input(force_reset=True)
    # stop, receiver OFF, receiver on, input source, port/clock, gamma, mode
    assert [_cmd(w) for w in dev.writes] == [
        0x1A24, 0x1A01, 0x1A01, 0x1A00, 0x1A03, 0x1A3B, 0x1A1B,
    ]
    # The receiver goes down first so the link drops and the port/clock routing
    # re-latches; an already-locked source otherwise keeps the old routing.
    assert [_data(w)[0] for w in dev.writes if _cmd(w) == 0x1A01] == [
        int(device.IT6535Power.POWER_DOWN),
        int(device.IT6535Power.HDMI),
    ]
    # Enters VIDEO mode: the precondition, not VIDEO_PATTERN itself.
    assert _data(dev.writes[-1])[0] == int(device.OperatingMode.VIDEO)


@pytest.mark.parametrize(
    "ready, link_live, outcome, resets",
    [
        (True, True, "unchanged", False),      # nothing to do
        (False, False, "reconfigured", False),  # e.g. back from `dmd show`
        (False, True, "reset", True),           # routing must re-latch
    ],
)
def test_prepare_video_input_resets_only_when_needed(
    monkeypatch, ready, link_live, outcome, resets
):
    # The reset drops the link, which the host sees as a monitor unplug/replug,
    # so it must fire only when a live link's routing has to change.
    monkeypatch.setattr(device.time, "sleep", lambda _s: None)
    monkeypatch.setattr(device.DMD, "video_input_ready", lambda *_: ready)
    monkeypatch.setattr(device.DMD, "_parallel_link_live", lambda self: link_live)
    dev = FakeReadDev()
    assert device.DMD(dev).prepare_video_input() == outcome
    powered_down = int(device.IT6535Power.POWER_DOWN) in [
        _data(w)[0] for w in dev.writes if _cmd(w) == 0x1A01
    ]
    assert powered_down is resets


def test_run_pattern_sequence_writes_only_in_order(monkeypatch):
    monkeypatch.setattr(device.time, "sleep", lambda _s: None)
    monkeypatch.setattr(device.DMD, "video_input_ready", lambda *_: False)
    monkeypatch.setattr(device.DMD, "_parallel_link_live", lambda self: True)
    dev = FakeReadDev()
    seq = device.PatternSequence(
        entries=[
            device.PatternEntry(exposure_us=11000, bit_depth=8, frame_bit_position=0),
            device.PatternEntry(exposure_us=11000, bit_depth=8, frame_bit_position=8),
        ]
    )
    device.DMD(dev).run_pattern_sequence(seq, lock_wait_s=0)
    # prepare (stop, receiver off/on, input, port/clock, gamma, VIDEO), configure
    # (stop, VIDEO_PATTERN, define x2, config), start — writes only, no reads.
    assert [_cmd(w) for w in dev.writes] == [
        0x1A24, 0x1A01, 0x1A01, 0x1A00, 0x1A03, 0x1A3B, 0x1A1B,
        0x1A24, 0x1A1B, 0x1A34, 0x1A34, 0x1A31, 0x1A24,
    ]
    assert [_data(w)[0] for w in dev.writes if _cmd(w) == 0x1A1B] == [
        int(device.OperatingMode.VIDEO),
        int(device.OperatingMode.VIDEO_PATTERN),
    ]
    assert _data(dev.writes[-1])[0] == 2  # start


def test_enter_standby_stops_then_parks():
    dev = FakeReadDev()
    device.DMD(dev).enter_standby(wait=False)
    # stop display, then Power Standby (parks the mirrors) — writes only. No
    # status reads: in standby the only accepted command is Return to Normal.
    assert [_cmd(w) for w in dev.writes] == [0x1A24, 0x0200]
    assert _data(dev.writes[0])[0] == 0
    assert _data(dev.writes[1])[0] == int(device.PowerMode.STANDBY)


def test_wake_returns_to_normal_then_waits(monkeypatch):
    # Blind wait, no polling: reading across this transition wedged the
    # controller twice in testing (docs/pattern-mode-design.md).
    slept = []
    monkeypatch.setattr(device.time, "sleep", slept.append)
    dev = FakeReadDev()
    device.DMD(dev).wake()
    assert [_cmd(w) for w in dev.writes] == [0x0200]
    assert _data(dev.writes[0])[0] == int(device.PowerMode.NORMAL)
    assert sum(slept) >= device.WAKE_SETTLE_S


# --- lock polling -----------------------------------------------------------


def test_wait_for_video_lock_returns_once_locked(monkeypatch):
    dmd = device.DMD(FakeReadDev())
    states = iter([0b000000, 0b000000, 0b011010])  # not locked, not locked, locked
    monkeypatch.setattr(
        dmd, "main_status", lambda: device.MainStatus.from_byte(next(states))
    )
    monkeypatch.setattr(device.time, "sleep", lambda _s: None)
    assert dmd.wait_for_video_lock(timeout_s=5) >= 0


def test_wait_for_video_lock_times_out_on_a_dead_source(monkeypatch):
    dmd = device.DMD(FakeReadDev())
    monkeypatch.setattr(dmd, "main_status", lambda: device.MainStatus.from_byte(0))
    monkeypatch.setattr(device.time, "sleep", lambda _s: None)
    with pytest.raises(TimeoutError, match="did not lock"):
        dmd.wait_for_video_lock(timeout_s=0.01, interval_s=0)
