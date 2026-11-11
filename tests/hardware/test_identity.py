"""Hardware identity tests — assert the connected board's firmware and type.

Run: uv run pytest -m hardware
Values are specific to the DLP670S board used in this project; update them if the board or its
firmware changes.
"""

import pytest

pytestmark = pytest.mark.hardware

EXPECTED_FIRMWARE_TAG = "dlp670S_6.2.0_OOB"
EXPECTED_FIRMWARE_VERSION = (6, 2, 0)
EXPECTED_DMD_TYPE = 0x03


def test_firmware_tag(dmd):
    assert dmd.firmware_tag() == EXPECTED_FIRMWARE_TAG


def test_firmware_version(dmd):
    assert dmd.firmware_version() == EXPECTED_FIRMWARE_VERSION


def test_dmd_type(dmd):
    assert dmd.dmd_type() == EXPECTED_DMD_TYPE
