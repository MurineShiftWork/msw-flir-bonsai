from __future__ import annotations

import msw_flir_bonsai


def test_version() -> None:
    assert msw_flir_bonsai.__version__ is not None
    assert isinstance(msw_flir_bonsai.__version__, str)
