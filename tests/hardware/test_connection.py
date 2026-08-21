"""Hardware test — requires a connected DMD. Run: uv run pytest -m hardware"""

import pytest

pytestmark = pytest.mark.hardware


def test_can_open_device(dmd):
    assert dmd is not None
