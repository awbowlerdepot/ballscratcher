"""The plotter neighbor logic is copied into public_api (Learn plotter) and
partner_api (/v1/plotter) -- separate Lambda packages, so it can't be
imported across. Learn and partners must recommend the same balls, so
this fails if the marked blocks drift (runbook 6cj)."""
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
BLOCK_RE = re.compile(r"# >>> PLOTTER NEIGHBORS v1 >>>.*?# <<< PLOTTER NEIGHBORS v1 <<<", re.DOTALL)


def _block(rel):
    with open(os.path.join(ROOT, rel)) as f:
        m = BLOCK_RE.search(f.read())
    assert m, f"PLOTTER NEIGHBORS v1 markers missing in {rel}"
    return m.group(0)


def test_neighbor_blocks_are_identical():
    assert _block("src/public_api/service.py") == _block("src/partner_api/service.py")
