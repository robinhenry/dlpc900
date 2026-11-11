"""Unit tests for the `dmd` CLI — no hardware, the device layer is stubbed.

Scope: what each subcommand sends to the device, the exit codes, and where the
output goes. The device's own encoding is covered in test_device.py.
"""

import logging
from contextlib import contextmanager

import pytest

import dlpc900.cli as cli
import dlpc900.device as device


class FakeStatus:
    """Hardware status bits, healthy unless a test says otherwise."""

    def __init__(self, initialized=True, incompatible=False):
        self.initialized = initialized
        self.incompatible = incompatible


class FakeDMD:
    """Records commands instead of touching hardware."""

    def __init__(self, status=None, main=None, mode=None, lock_error=None,
                 outcome="reset", woke=0.0):
        self.calls = []
        self.status = status or FakeStatus()
        self.main = main if main is not None else device.MainStatus.from_byte(0b011010)
        self.mode = mode if mode is not None else device.OperatingMode.VIDEO
        self.lock_error = lock_error
        self.outcome = outcome
        self.woke = woke

    def show_test_pattern(self, pattern, foreground=None, **kwargs):
        self.calls.append((pattern, foreground))

    def enter_standby(self, wait=True):
        self.calls.append(("enter_standby", wait))

    def wake(self, wait=True):
        self.calls.append(("wake", wait))
        if isinstance(self.woke, Exception):
            raise self.woke

    def hardware_status(self):
        return self.status

    def prepare_video_input(self, receiver=None, data_port=None, force_reset=False):
        self.calls.append(("prepare_video_input", receiver, data_port, force_reset))
        return self.outcome

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
    @contextmanager
    def fake_connect():
        yield dmd

    monkeypatch.setattr(cli, "connect", fake_connect)
    return dmd


@pytest.fixture
def fake_dmd(monkeypatch):
    return _patch_connect(monkeypatch, FakeDMD())


# --- show -------------------------------------------------------------------


def test_show_covers_all_ten_test_patterns_plus_black():
    # Every 0x1203 value the generator draws (0-9) is reachable by name. "No
    # pattern" (10) is deliberately absent: it idles the generator instead of
    # clearing the array, so the mirrors keep the last image — a no-op.
    selectable = {pattern for pattern, _ in cli.PATTERNS.values()}
    assert selectable == set(device.TestPattern) - {device.TestPattern.NONE}
    assert len(selectable) == 10


@pytest.mark.parametrize(
    "name, pattern, foreground",
    [
        ("checkerboard", device.TestPattern.CHECKERBOARD, cli.WHITE),
        # A dark DMD needs the mirrors actively driven off, not the generator idled.
        ("black", device.TestPattern.SOLID_FIELD, (0, 0, 0)),
    ],
)
def test_show_sends_the_pattern_to_the_device(fake_dmd, name, pattern, foreground):
    assert cli.main(["show", name]) == 0
    assert fake_dmd.calls == [(pattern, foreground)]


def test_show_list_does_not_touch_the_device(fake_dmd, capsys):
    assert cli.main(["show", "--list"]) == 0
    assert fake_dmd.calls == []
    out = capsys.readouterr().out  # readouterr() drains, so capture once
    assert all(name in out for name in cli.PATTERNS)


def test_show_without_pattern_is_a_usage_error(fake_dmd):
    assert cli.main(["show"]) == 2
    assert fake_dmd.calls == []


def test_show_reports_a_missing_device(monkeypatch, capsys):
    @contextmanager
    def failing_connect():
        raise device.DeviceNotFoundError("no DMD connected")
        yield  # pragma: no cover

    monkeypatch.setattr(cli, "connect", failing_connect)
    assert cli.main(["show", "grid"]) == 1
    assert "no DMD connected" in capsys.readouterr().err


# --- status -----------------------------------------------------------------


def test_status_reports_the_device_and_splits_the_streams(monkeypatch, capsys):
    # Results on stdout keeps `dmd ... | ...` usable; problems go to stderr.
    monkeypatch.setattr(
        cli, "find_device", lambda: {"vendor_id": 0x0451, "product_id": 0xC900}
    )
    assert cli.main(["status"]) == 0
    found = capsys.readouterr()
    assert "0451:c900" in found.out and found.err == ""

    monkeypatch.setattr(cli, "find_device", lambda: None)
    assert cli.main(["status"]) == 1
    missing = capsys.readouterr()
    assert "not found" in missing.err and missing.out == ""


def test_no_subcommand_prints_help(capsys):
    assert cli.main([]) == 2
    assert "usage: dmd" in capsys.readouterr().out


# --- stream -----------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, receiver, data_port",
    [
        # DP defaults; 269.89 Mpix/s is over the single-pixel limit so the
        # IT6535 sends two pixels per clock. HDMI's 139.8 Mpix/s sends one.
        # Wrong routing is not a lock failure — it is a half-width image.
        ([], device.IT6535Power.DISPLAYPORT, device.DataPort.PORT12_DUAL),
        (["hdmi"], device.IT6535Power.HDMI, device.DataPort.PORT1_SINGLE),
    ],
)
def test_stream_routing_follows_the_source(fake_dmd, argv, receiver, data_port):
    assert cli.main(["stream", *argv]) == 0
    assert fake_dmd.calls == [
        ("prepare_video_input", receiver, data_port, False),
        ("wait_for_video_lock",),
    ]


def test_stream_confirms_mode_and_lock_to_the_user(fake_dmd, capsys):
    assert cli.main(["stream"]) == 0
    out = capsys.readouterr().out
    assert "VIDEO" in out               # the mode, read back from the controller
    assert "locked in 2.4 s" in out     # the measured lock time, not a guess
    assert "0x1A03 = 0x02" in out       # the routing actually applied
    # 0b011010 = port 2 NOT synced, which is the real board state on a working
    # dual-pixel link. Requiring it would reject a good link.
    assert "streaming" in out


@pytest.mark.parametrize(
    "outcome, expected",
    [
        ("reset", "locked in 2.4 s"),          # link dropped and re-acquired
        ("reconfigured", "locked in 2.4 s"),   # re-acquired without a hotplug
        ("unchanged", "already locked"),       # nothing to do
    ],
)
def test_stream_reports_what_it_actually_did(monkeypatch, capsys, outcome, expected):
    _patch_connect(monkeypatch, FakeDMD(outcome=outcome))
    assert cli.main(["stream"]) == 0
    assert expected in capsys.readouterr().out


def test_stream_can_force_a_link_reset(fake_dmd):
    assert cli.main(["stream", "--reset"]) == 0
    assert fake_dmd.calls[0][3] is True


@pytest.mark.parametrize(
    "argv, hint",
    [([], "DP EDID"), (["hdmi"], "HDMI cable")],
)
def test_stream_fails_when_the_source_never_locks(monkeypatch, capsys, argv, hint):
    _patch_connect(
        monkeypatch, FakeDMD(lock_error=TimeoutError("video source did not lock"))
    )
    assert cli.main(["stream", *argv]) == 1
    err = capsys.readouterr().err
    assert "did not lock" in err
    assert hint in err  # points at the prerequisites for that input


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param(
            {"main": device.MainStatus.from_byte(0b011100)}, id="video-frozen"
        ),
        pytest.param(
            {"mode": device.OperatingMode.PRE_STORED_PATTERN}, id="wrong-mode"
        ),
    ],
)
def test_stream_fails_if_it_did_not_reach_a_streaming_state(
    monkeypatch, capsys, kwargs
):
    _patch_connect(monkeypatch, FakeDMD(**kwargs))
    assert cli.main(["stream"]) == 1
    assert "did not reach a streaming state" in capsys.readouterr().err


@pytest.mark.parametrize(
    "status, message",
    [
        (FakeStatus(incompatible=True), "incompatible"),
        (FakeStatus(initialized=False), "not initialized"),
    ],
)
def test_stream_refuses_an_unhealthy_controller(monkeypatch, capsys, status, message):
    dmd = _patch_connect(monkeypatch, FakeDMD(status))
    assert cli.main(["stream"]) == 1
    assert message in capsys.readouterr().err
    assert dmd.calls == []  # bailed before touching the video path


# --- logging ----------------------------------------------------------------


def test_verbose_enables_debug_logging_without_touching_the_root_logger(fake_dmd):
    assert cli.main(["-v", "stream"]) == 0
    package_logger = logging.getLogger("dlpc900")
    assert package_logger.level == logging.DEBUG
    assert not package_logger.propagate  # importing us must not hijack an app


def test_logging_setup_does_not_stack_handlers(fake_dmd):
    for _ in range(3):
        cli.main(["stream"])
    assert len(logging.getLogger("dlpc900").handlers) == 2  # stdout + stderr


# --- standby / wake -----------------------------------------------------------


@pytest.mark.parametrize("argv, waited", [([], True), (["--no-wait"], False)])
def test_standby_parks_the_mirrors(monkeypatch, fake_dmd, argv, waited):
    # The device call never blocks: the CLI owns the wait so it can draw the
    # countdown, and nothing may be sent to the DMD while it parks.
    counted = []
    monkeypatch.setattr(cli, "_countdown", lambda s, label: counted.append(s))
    assert cli.main(["standby", *argv]) == 0
    assert fake_dmd.calls == [("enter_standby", False)]
    assert counted == ([cli.STANDBY_WAIT_S] if waited else [])


def test_standby_interrupted_warns_that_parking_is_unfinished(
    monkeypatch, fake_dmd, capsys
):
    def interrupt(seconds, label):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_countdown", interrupt)
    assert cli.main(["standby"]) == 130
    assert "still parking" in capsys.readouterr().err


def test_countdown_is_silent_when_not_a_terminal(monkeypatch, capsys):
    # Keeps pipes and log files free of carriage returns and bar characters.
    slept = []
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    cli._countdown(5, "parking")
    assert sum(slept) == 5
    assert capsys.readouterr().out == ""


def test_countdown_draws_a_bar_on_a_terminal(monkeypatch):
    import io

    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    out = FakeTTY()
    monkeypatch.setattr(cli.sys, "stdout", out)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    cli._countdown(4, "parking")
    frames = out.getvalue().split("\r")
    assert "4s" in frames[1] and "parking" in frames[1]
    assert "█" in frames[-2]              # fills as it goes
    assert frames[-1] == "\033[K"        # and clears the line afterwards


def test_standby_no_wait_warns_not_to_cut_power_early(fake_dmd, capsys):
    # Parking is what protects the array; powering off mid-park defeats it.
    assert cli.main(["standby", "--no-wait"]) == 0
    out = capsys.readouterr().out
    assert "do not power it off" in out


def test_wake_returns_to_normal_and_confirms(fake_dmd, capsys):
    assert cli.main(["wake"]) == 0
    assert ("wake", True) in fake_dmd.calls
    assert "awake" in capsys.readouterr().out


def test_wake_fails_if_the_controller_does_not_come_back(monkeypatch, capsys):
    _patch_connect(monkeypatch, FakeDMD(FakeStatus(initialized=False)))
    assert cli.main(["wake"]) == 1
    assert "did not come back initialized" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["stream"], ["show", "grid"]])
def test_image_commands_never_touch_the_power_mode(fake_dmd, argv):
    # Waking is explicit: sending Return to Normal from these paths wedged the
    # controller twice (docs/pattern-mode-design.md). They must not do it.
    cli.main(argv)
    assert not any(call[0] == "wake" for call in fake_dmd.calls if isinstance(call, tuple))


@pytest.mark.parametrize("argv", [["stream"], ["show", "grid"], ["wake"]])
def test_a_wedged_controller_reports_sw1_instead_of_a_traceback(
    monkeypatch, capsys, argv
):
    class Wedged(FakeDMD):
        def hardware_status(self):
            raise device.DeviceError("no response to command 0x1a0a")

        def wake(self, wait=True):
            raise device.DeviceError("no response to command 0x1a0a")

        def show_test_pattern(self, pattern, foreground=None, **kwargs):
            raise device.DeviceError("no response to command 0x1a0a")

    _patch_connect(monkeypatch, Wedged())
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "no response" in err
    # Cannot tell standby from a wedge without a read, so name both.
    assert "SW1" in err and "dmd wake" in err
