"""Unit tests for the `dmd` CLI — no hardware, the device layer is stubbed."""

import pytest

import dmd_client.cli as cli
import dmd_client.device as device


class FakeStatus:
    """Hardware status bits, healthy unless a test says otherwise."""

    def __init__(self, initialized=True, incompatible=False):
        self.initialized = initialized
        self.incompatible = incompatible


class FakeDMD:
    """Records commands instead of touching hardware."""

    def __init__(self, status=None, main=None, mode=None, lock_error=None):
        self.calls = []
        self.status = status or FakeStatus()
        self.main = main if main is not None else device.MainStatus.from_byte(0b011000)
        self.mode = mode if mode is not None else device.OperatingMode.VIDEO
        self.lock_error = lock_error

    def show_test_pattern(self, pattern, foreground=None, **kwargs):
        self.calls.append((pattern, foreground))

    def hardware_status(self):
        return self.status

    def prepare_video_input(self, receiver=None, data_port=None):
        self.calls.append(("prepare_video_input", receiver, data_port))

    def wait_for_video_lock(self, **kwargs):
        self.calls.append(("wait_for_video_lock",))
        if self.lock_error:
            raise self.lock_error
        return 2.4

    def operating_mode(self):
        return self.mode

    def main_status(self):
        return self.main


def _patch_connect(monkeypatch, dmd):
    from contextlib import contextmanager

    @contextmanager
    def fake_connect():
        yield dmd

    monkeypatch.setattr(cli, "connect", fake_connect)
    return dmd


@pytest.fixture
def fake_dmd(monkeypatch):
    return _patch_connect(monkeypatch, FakeDMD())


def test_show_covers_all_ten_test_patterns_plus_black():
    # Every 0x1203 value the generator draws (0-9) is reachable by name; "no
    # pattern" (10) is not offered, since it freezes rather than clears.
    selectable = {pattern for pattern, _ in cli.PATTERNS.values()}
    assert selectable == set(device.TestPattern) - {device.TestPattern.NONE}
    assert len(selectable) == 10
    assert "black" in cli.PATTERNS


def test_show_sends_pattern_to_device(fake_dmd):
    assert cli.main(["show", "checkerboard"]) == 0
    assert fake_dmd.calls == [(device.TestPattern.CHECKERBOARD, cli.WHITE)]


def test_show_black_is_a_zeroed_solid_field(fake_dmd):
    # A dark DMD needs the mirrors actively driven off, not the generator idled.
    assert cli.main(["show", "black"]) == 0
    assert fake_dmd.calls == [(device.TestPattern.SOLID_FIELD, (0, 0, 0))]


def test_show_list_does_not_touch_the_device(fake_dmd, capsys):
    assert cli.main(["show", "--list"]) == 0
    assert fake_dmd.calls == []
    out = capsys.readouterr().out
    assert all(name in out for name in cli.PATTERNS)


def test_show_without_pattern_is_a_usage_error(fake_dmd):
    assert cli.main(["show"]) == 2
    assert fake_dmd.calls == []


def test_show_reports_a_missing_device(monkeypatch, capsys):
    from contextlib import contextmanager

    @contextmanager
    def failing_connect():
        raise device.DeviceNotFoundError("no DMD connected")
        yield  # pragma: no cover

    monkeypatch.setattr(cli, "connect", failing_connect)
    assert cli.main(["show", "grid"]) == 1
    assert "no DMD connected" in capsys.readouterr().err


def test_status_reports_found_device(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "find_device", lambda: {"vendor_id": 0x0451, "product_id": 0xC900}
    )
    assert cli.main(["status"]) == 0
    assert "0451:c900" in capsys.readouterr().out


def test_status_reports_missing_device(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_device", lambda: None)
    assert cli.main(["status"]) == 1
    assert "not found" in capsys.readouterr().out


def test_no_subcommand_prints_help(capsys):
    assert cli.main([]) == 2
    assert "usage: dmd" in capsys.readouterr().out


def test_stream_hdmi_enters_video_mode_and_waits_for_lock(fake_dmd):
    assert cli.main(["stream-hdmi"]) == 0
    assert fake_dmd.calls == [
        ("prepare_video_input", device.IT6535Power.HDMI, device.DataPort.PORT1_SINGLE),
        ("wait_for_video_lock",),
    ]


def test_stream_hdmi_confirms_mode_and_lock_to_the_user(fake_dmd, capsys):
    assert cli.main(["stream-hdmi"]) == 0
    out = capsys.readouterr().out
    assert "VIDEO" in out                 # the mode, read back from the controller
    assert "locked in 2.4 s" in out       # the measured lock time, not a guess
    assert "streaming" in out


def test_stream_hdmi_fails_when_the_source_never_locks(monkeypatch, capsys):
    _patch_connect(
        monkeypatch, FakeDMD(lock_error=TimeoutError("HDMI source did not lock"))
    )
    assert cli.main(["stream-hdmi"]) == 1
    err = capsys.readouterr().err
    assert "did not lock" in err
    assert "HDMI cable" in err  # points at the usual cause


def test_stream_hdmi_fails_on_a_frozen_video_path(monkeypatch, capsys):
    # Locked and synced, but the frame buffer is frozen: not actually streaming.
    frozen = device.MainStatus.from_byte(0b011100)
    _patch_connect(monkeypatch, FakeDMD(main=frozen))
    assert cli.main(["stream-hdmi"]) == 1
    assert "did not reach a streaming state" in capsys.readouterr().err


def test_stream_hdmi_fails_if_the_mode_read_back_is_wrong(monkeypatch, capsys):
    _patch_connect(
        monkeypatch, FakeDMD(mode=device.OperatingMode.PRE_STORED_PATTERN)
    )
    assert cli.main(["stream-hdmi"]) == 1
    assert "did not reach a streaming state" in capsys.readouterr().err


def test_stream_hdmi_refuses_an_incompatible_controller(monkeypatch, capsys):
    dmd = _patch_connect(monkeypatch, FakeDMD(FakeStatus(incompatible=True)))
    assert cli.main(["stream-hdmi"]) == 1
    assert "incompatible" in capsys.readouterr().err
    assert dmd.calls == []  # bailed before touching the video path


def test_stream_hdmi_refuses_an_uninitialized_controller(monkeypatch, capsys):
    dmd = _patch_connect(monkeypatch, FakeDMD(FakeStatus(initialized=False)))
    assert cli.main(["stream-hdmi"]) == 1
    assert "not initialized" in capsys.readouterr().err
    assert dmd.calls == []
