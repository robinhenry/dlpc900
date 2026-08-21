"""Shared pytest fixtures for the dmd-client test suite."""

import pytest

from dmd_client.device import connect, find_device


@pytest.fixture(scope="session")
def dmd():
    """Yield a connected DMD, for hardware tests.

    Skips the test if no device is connected, so the hardware suite degrades
    gracefully to "skipped" on machines without the board (laptops, CI). If the
    device *is* present but cannot be opened (e.g. the udev rule is missing),
    the underlying DevicePermissionError propagates as a real failure.
    """
    if find_device() is None:
        pytest.skip("no DMD connected")
    with connect() as dmd:
        yield dmd
