"""dmd-client — HID client for the DMD (digital micromirror device)."""

from dmd_client.cli import main
from dmd_client.device import (
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
