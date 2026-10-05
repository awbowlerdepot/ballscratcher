"""The plotter estimator (v3) is copied into public_api/service.py and
admin_api/service.py -- each Lambda is its own deployment package, so it
can't be imported across. A visitor's plotter page and an admin's
re-estimate run must never disagree, so this fails if the two copies
drift: the marked blocks must be byte-identical, and both must produce
the same answers over a grid of inputs."""
import importlib.util
import itertools
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
BLOCK_RE = re.compile(r"# >>> PLOTTER ESTIMATOR v3 >>>.*?# <<< PLOTTER ESTIMATOR v3 <<<", re.DOTALL)


def _read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_estimator_blocks_are_identical():
    public = BLOCK_RE.search(_read("src/public_api/service.py"))
    admin = BLOCK_RE.search(_read("src/admin_api/service.py"))
    assert public and admin, "PLOTTER ESTIMATOR v3 markers missing"
    assert public.group(0) == admin.group(0)


def test_estimators_agree_over_an_input_grid():
    pub = _load("plotter_sync_public", "src/public_api/service.py")
    adm = _load("plotter_sync_admin", "src/admin_api/service.py")
    grid = itertools.product(
        ["reactive_resin", "urethane", "polyester_plastic", None],
        ["solid", "pearl", "hybrid", None],
        ["symmetric", "asymmetric", None],
        ["polished", "satin", "dull", None],
        [None, 2.47, 2.55],
        [None, 0.020, 0.050],
        [None, 120.0, 190.0],
    )
    for material, cover, core, finish, rg, diff, price in grid:
        kwargs = dict(coverstock_material=material, coverstock_type=cover, core_type=core, finish_category=finish,
                      rg15=rg, diff15=diff, mass_bias15=0.015 if core == "asymmetric" else None, price=price)
        assert pub.estimate_oil_motion(**kwargs) == adm.estimate_oil_motion(**kwargs), kwargs
