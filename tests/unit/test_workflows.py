"""Every bundled workflow can find the extension workflows it includes."""

from __future__ import annotations

import re

import pytest

from msw_flir_bonsai.bonsai_workflows import WORKFLOW_DIR

_INCLUDE = re.compile(r'IncludeWorkflow"\s+Path="([^"]+\.bonsai)"')
_WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.bonsai"))


def test_workflows_found() -> None:
    assert _WORKFLOWS


@pytest.mark.parametrize("workflow", _WORKFLOWS, ids=lambda p: p.stem)
def test_included_workflows_are_bundled(workflow) -> None:
    includes = _INCLUDE.findall(workflow.read_text(encoding="utf-8"))
    missing = [
        inc for inc in includes if not (WORKFLOW_DIR / inc.replace("\\", "/")).is_file()
    ]
    assert not missing, (
        f"{workflow.name} includes files not in the package: {sorted(set(missing))}"
    )
