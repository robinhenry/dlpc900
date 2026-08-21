"""Minimal unit tests for device.py — no hardware, hid is mocked."""

import pytest

import dmd_client.device as device

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


def test_open_device_permission_error(monkeypatch):
    monkeypatch.setattr(device.hid, "enumerate", lambda *_: [INFO])
    monkeypatch.setattr(device.hid, "device", lambda: FakeHid(OSError("open failed")))
    with pytest.raises(device.DevicePermissionError):
        with device.open_device():
            pass


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


def test_read_encodes_command_and_parses_payload():
    # response: flag=0xC0, seq=1, length=2, payload=[0xAB, 0xCD]
    dev = FakeReadDev([0xC0, 1, 0x02, 0x00, 0xAB, 0xCD] + [0] * 58)
    assert device.read(dev, 0x1A0A) == bytes([0xAB, 0xCD])
    # command 0x1A0A goes out LSB-first at bytes 5,6 (after report id + flag).
    assert dev.written[5] == 0x0A and dev.written[6] == 0x1A


def test_read_raises_on_error_flag():
    dev = FakeReadDev([0xE0, 1, 0, 0] + [0] * 60)  # bit 5 (0x20) set
    with pytest.raises(device.DeviceError):
        device.read(dev, 0x1A0A)


def test_hardware_status_decodes_bits():
    hw = device.HardwareStatus.from_byte(0b1000_0011)  # bits 0, 1, 7
    assert hw.initialized and hw.incompatible and hw.sequencer_error
    assert not hw.secondary_ready


def test_dmd_reads_and_decodes_status():
    # response payload = one byte 0x01 -> only `initialized` set
    dev = FakeReadDev([0xC0, 1, 0x01, 0x00, 0x01] + [0] * 59)
    assert device.DMD(dev).hardware_status().initialized is True


def test_write_encodes_flag_length_command_and_data():
    dev = FakeReadDev(None)
    device.write(dev, 0x1A1B, bytes([0x02]))
    p = dev.written
    assert p[0] == 0x00 and p[1] == 0x00           # report id, flag (write/no reply)
    assert p[3] == 0x03 and p[4] == 0x00           # length = 2 command + 1 data
    assert p[5] == 0x1B and p[6] == 0x1A           # command LSB, MSB
    assert p[7] == 0x02                            # data byte


def _cmd(packet):
    """USB command from a written packet (bytes 5,6 = command LSB, MSB)."""
    return packet[5] | (packet[6] << 8)


def _data(packet):
    """Payload bytes of a written packet (after the 2-byte command)."""
    return packet[7:]


def test_pattern_entry_encodes_lut_definition():
    e = device.PatternEntry(
        exposure_us=500, bit_depth=8, frame_bit_position=0, dark_time_us=100
    )
    b = e.encode(index=3)
    assert len(b) == 12
    assert int.from_bytes(b[0:2], "little") == 3            # LUT position
    assert int.from_bytes(b[2:5], "little") == 500          # exposure
    assert (b[5] >> 1) & 0x07 == 7                           # bit depth 8 -> code 7
    assert (b[5] >> 4) & 0x07 == 0                           # LED NONE
    assert b[5] & 0x01 == 0 and b[5] >> 7 == 0               # clear/wait off
    assert int.from_bytes(b[6:9], "little") == 100           # dark time
    assert b[9] == 0                                          # TRIG_OUT_2 enabled
    assert int.from_bytes(b[10:12], "little") >> 11 == 0      # frame bit position


def test_pattern_entry_frame_bit_position_and_bit_depth():
    b = device.PatternEntry(
        exposure_us=100, bit_depth=1, frame_bit_position=23
    ).encode(0)
    assert (int.from_bytes(b[10:12], "little") >> 11) & 0x1F == 23
    assert (b[5] >> 1) & 0x07 == 0                            # 1-bit -> code 0


def test_pattern_entry_validates_bit_depth():
    with pytest.raises(ValueError):
        device.PatternEntry(
            exposure_us=100, bit_depth=9, frame_bit_position=0
        ).encode(0)


def test_frame_filling_8bit_30hz_matches_e2e_reference():
    # The known-good case from the E2E "Video Pattern Mode Frozen" fix:
    # 3 patterns/frame at 30 Hz, ~11 ms each, exactly filling the 33.33 ms frame.
    seq = device.PatternSequence.frame_filling(bit_depth=8, refresh_hz=30)
    assert len(seq.entries) == 3
    assert seq.repeat is True
    assert [e.frame_bit_position for e in seq.entries] == [0, 8, 16]
    assert all(e.bit_depth == 8 for e in seq.entries)
    assert all(e.led_select is device.LedSelect.NONE for e in seq.entries)
    # Total on-time lands exactly on the 33333 µs frame period (no drift).
    assert sum(e.exposure_us for e in seq.entries) == round(1_000_000 / 30)
    assert seq.entries[0].exposure_us == 11111


def test_frame_filling_folds_remainder_into_last_entry():
    # 6-bit -> 4 patterns/frame; 33333 // 4 = 8333 with 1 µs remainder, which
    # goes to the final entry so the sum stays exactly on the frame period.
    seq = device.PatternSequence.frame_filling(bit_depth=6, refresh_hz=30)
    assert len(seq.entries) == 4
    assert [e.frame_bit_position for e in seq.entries] == [0, 6, 12, 18]
    assert seq.entries[0].exposure_us == 8333
    assert seq.entries[-1].exposure_us == 8334
    assert sum(e.exposure_us for e in seq.entries) == round(1_000_000 / 30)


def test_frame_filling_1bit_uses_all_24_planes():
    seq = device.PatternSequence.frame_filling(bit_depth=1, refresh_hz=30)
    assert len(seq.entries) == 24
    assert [e.frame_bit_position for e in seq.entries] == list(range(24))
    assert sum(e.exposure_us for e in seq.entries) == round(1_000_000 / 30)


def test_frame_filling_scales_with_refresh():
    # Doubling refresh halves the per-pattern exposure (frame period halves).
    at30 = device.PatternSequence.frame_filling(bit_depth=8, refresh_hz=30)
    at60 = device.PatternSequence.frame_filling(bit_depth=8, refresh_hz=60)
    assert sum(e.exposure_us for e in at60.entries) == round(1_000_000 / 60)
    assert at60.entries[0].exposure_us < at30.entries[0].exposure_us


def test_frame_filling_rejects_bad_args():
    with pytest.raises(ValueError):
        device.PatternSequence.frame_filling(bit_depth=0)
    with pytest.raises(ValueError):
        device.PatternSequence.frame_filling(bit_depth=9)
    with pytest.raises(ValueError):
        device.PatternSequence.frame_filling(bit_depth=8, refresh_hz=0)


def test_configure_pattern_sequence_definitions_then_config():
    dev = FakeReadDev()
    seq = device.PatternSequence(
        entries=[
            device.PatternEntry(exposure_us=100, bit_depth=1, frame_bit_position=0),
            device.PatternEntry(exposure_us=100, bit_depth=1, frame_bit_position=1),
        ],
        repeat=False,
    )
    device.DMD(dev).configure_pattern_sequence(seq)
    # stop, set VIDEO_PATTERN, define, define, configure (Table 5-1, writes only)
    assert [_cmd(w) for w in dev.writes] == [0x1A24, 0x1A1B, 0x1A34, 0x1A34, 0x1A31]
    assert _data(dev.writes[1])[0] == int(device.OperatingMode.VIDEO_PATTERN)
    config = _data(dev.writes[-1])
    assert config[0] | (config[1] << 8) == 2                 # NL = 2 entries
    assert int.from_bytes(bytes(config[2:6]), "little") == 2  # NP = 2 (play once)


def test_configure_pattern_sequence_repeat_sets_np_zero():
    dev = FakeReadDev()
    seq = device.PatternSequence(
        entries=[device.PatternEntry(exposure_us=100, bit_depth=1, frame_bit_position=0)],
        repeat=True,
    )
    device.DMD(dev).configure_pattern_sequence(seq)
    config = _data(dev.writes[-1])
    assert int.from_bytes(bytes(config[2:6]), "little") == 0  # NP = 0 (loop)


def test_set_input_source_packs_source_and_bit_depth():
    dev = FakeReadDev()
    device.DMD(dev).set_input_source(
        device.InputSource.PARALLEL, device.ParallelBitDepth.BITS_24
    )
    w = dev.writes[-1]
    assert _cmd(w) == 0x1A00
    assert _data(w)[0] == (0 | (1 << 3))                     # PARALLEL | BITS_24<<3


def test_run_pattern_sequence_writes_only_in_order():
    dev = FakeReadDev()
    seq = device.PatternSequence(
        entries=[
            device.PatternEntry(exposure_us=11000, bit_depth=8, frame_bit_position=0),
            device.PatternEntry(exposure_us=11000, bit_depth=8, frame_bit_position=8),
        ]
    )
    device.DMD(dev).run_pattern_sequence(seq, lock_wait_s=0)
    # prepare (stop, it6535, input, port/clock, gamma, VIDEO), configure (stop,
    # VIDEO_PATTERN, define x2, config), start — all writes, no status reads
    assert [_cmd(w) for w in dev.writes] == [
        0x1A24, 0x1A01, 0x1A00, 0x1A03, 0x1A3B, 0x1A1B,
        0x1A24, 0x1A1B, 0x1A34, 0x1A34, 0x1A31, 0x1A24,
    ]
    mode_writes = [_data(w)[0] for w in dev.writes if _cmd(w) == 0x1A1B]
    assert mode_writes == [
        int(device.OperatingMode.VIDEO),
        int(device.OperatingMode.VIDEO_PATTERN),
    ]
    assert _data(dev.writes[-1])[0] == 2  # start


def test_set_test_pattern_encoding():
    dev = FakeReadDev()
    device.DMD(dev).set_test_pattern(device.TestPattern.CHECKERBOARD)
    w = dev.writes[-1]
    assert _cmd(w) == 0x1203
    assert _data(w)[0] == 7  # Checkerboard = 7 (Table 2-58)


def test_set_test_pattern_color_packs_six_le_words():
    dev = FakeReadDev()
    device.DMD(dev).set_test_pattern_color(
        foreground=(1023, 512, 0), background=(0, 0, 1023)
    )
    w = dev.writes[-1]
    assert _cmd(w) == 0x1204
    import struct
    assert struct.unpack("<6H", bytes(_data(w)[:12])) == (1023, 512, 0, 0, 0, 1023)


def test_set_test_pattern_color_rejects_out_of_range():
    with pytest.raises(ValueError):
        device.DMD(FakeReadDev()).set_test_pattern_color(foreground=(1024, 0, 0))


def test_show_test_pattern_writes_only_in_order():
    dev = FakeReadDev()
    device.DMD(dev).show_test_pattern(device.TestPattern.GRID, settle_s=0)
    # stop, VIDEO mode, input source (test pattern gen), pattern select, colors —
    # all writes, no status reads (avoids the video-mode read hang). VIDEO mode
    # precedes the source select: the mode write re-inits the video pipeline, and
    # 0x1203/0x1204 are not accepted in the pattern modes (DLPU018 Table A-2).
    assert [_cmd(w) for w in dev.writes] == [
        0x1A24, 0x1A1B, 0x1A00, 0x1203, 0x1204,
    ]
    by_cmd = {_cmd(w): _data(w) for w in dev.writes}
    assert by_cmd[0x1A00][0] & 0x07 == int(device.InputSource.TEST_PATTERN)
    assert by_cmd[0x1203][0] == int(device.TestPattern.GRID)
    assert by_cmd[0x1A1B][0] == int(device.OperatingMode.VIDEO)


def test_prepare_for_shutdown_stops_then_standby():
    dev = FakeReadDev()
    device.DMD(dev).prepare_for_shutdown(wait=False)
    # stop display, then Power Standby (parks mirrors) — writes only, no reads.
    assert [_cmd(w) for w in dev.writes] == [0x1A24, 0x0200]
    assert _data(dev.writes[0])[0] == 0                          # stop
    assert _data(dev.writes[1])[0] == int(device.PowerMode.STANDBY)


def test_set_port_clock_writes_port_byte():
    dev = FakeReadDev()
    device.DMD(dev).set_port_clock(device.DataPort.PORT12_DUAL)
    w = dev.writes[-1]
    assert _cmd(w) == 0x1A03
    assert _data(w)[0] == int(device.DataPort.PORT12_DUAL)  # dual-pixel = 0x02


def test_prepare_video_input_command_order():
    dev = FakeReadDev()
    device.DMD(dev).prepare_video_input()
    # stop, it6535, input source, port/clock, gamma, operating mode
    assert [_cmd(w) for w in dev.writes] == [
        0x1A24, 0x1A01, 0x1A00, 0x1A03, 0x1A3B, 0x1A1B,
    ]
    # defaults: IT6535 = HDMI, dual-pixel port/clock; enters VIDEO mode (the
    # precondition — the VIDEO_PATTERN switch happens in configure_pattern_sequence)
    by_cmd = {_cmd(w): _data(w) for w in dev.writes}
    assert by_cmd[0x1A01][0] == int(device.IT6535Power.HDMI)
    assert by_cmd[0x1A03][0] == int(device.DataPort.PORT1_SINGLE)
    assert by_cmd[0x1A1B][0] == int(device.OperatingMode.VIDEO)


def test_main_status_decodes_the_lock_bits():
    # 0b011010 — the byte the board actually returns while streaming HDMI.
    status = device.MainStatus.from_byte(0b011010)
    assert not status.parked
    assert status.sequencer_running
    assert not status.video_frozen
    assert status.source_locked
    assert status.port1_syncs_valid
    assert not status.port2_syncs_valid
    assert status.streaming


def test_main_status_frozen_video_is_not_streaming():
    # Locked and synced, but stuck on one frame — the DMD shows a still image.
    assert not device.MainStatus.from_byte(0b011100).streaming


def test_main_status_unlocked_is_not_streaming():
    assert not device.MainStatus.from_byte(0b000000).streaming


def test_wait_for_video_lock_returns_once_locked(monkeypatch):
    dmd = device.DMD(FakeReadDev())
    # Not locked, not locked, then locked — the real bring-up shape.
    bytes_ = iter([0b000000, 0b000000, 0b011010])
    monkeypatch.setattr(dmd, "main_status", lambda: device.MainStatus.from_byte(next(bytes_)))
    monkeypatch.setattr(device.time, "sleep", lambda _s: None)
    assert dmd.wait_for_video_lock(timeout_s=5) >= 0


def test_wait_for_video_lock_times_out_on_a_dead_source(monkeypatch):
    dmd = device.DMD(FakeReadDev())
    monkeypatch.setattr(dmd, "main_status", lambda: device.MainStatus.from_byte(0))
    monkeypatch.setattr(device.time, "sleep", lambda _s: None)
    with pytest.raises(TimeoutError, match="did not lock"):
        dmd.wait_for_video_lock(timeout_s=0.01, interval_s=0)


def test_operating_mode_reads_the_mode_byte():
    dev = FakeReadDev([0xC0, 1, 0x01, 0x00, int(device.OperatingMode.VIDEO)] + [0] * 59)
    assert device.DMD(dev).operating_mode() is device.OperatingMode.VIDEO
    assert _cmd(dev.written) == 0x1A1B
