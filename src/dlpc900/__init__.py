"""dlpc900 — HID client for the TI DLPC900 DMD controller."""

from dlpc900.cli import main
from dlpc900.device import (
    DMD,
    DeviceError,
    DeviceNotFoundError,
    DevicePermissionError,
    MainStatus,
    TestPattern,
    connect,
)

__all__ = [
    "main",
    "connect",
    "DMD",
    "TestPattern",
    "MainStatus",
    "DeviceNotFoundError",
    "DevicePermissionError",
    "DeviceError",
]
