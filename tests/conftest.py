import os

import pytest


def pytest_addoption(parser):
    parser.addoption("--unit-shard", metavar="K/N", help="Run sorted unit partition K of N")


def pytest_collection_modifyitems(config, items):
    # A CLI option reaches explicit xdist workers, but not pytest children started by tests.
    spec = config.getoption("unit_shard")
    if spec is None:
        return
    index, count = unit_partition(spec)
    kept = set(sorted(item.nodeid for item in items)[index - 1 :: count])
    config.hook.pytest_deselected(items=[item for item in items if item.nodeid not in kept])
    items[:] = [item for item in items if item.nodeid in kept]


def unit_partition(spec):
    try:
        index, count = (int(part) for part in spec.split("/"))
        if not 1 <= index <= count:
            raise ValueError
    except ValueError as error:
        raise pytest.UsageError("shard must be K/N with 1 <= K <= N") from error
    return index, count


@pytest.fixture(autouse=True)
def _clean_settings_environment(monkeypatch):
    """Remove every NCI_SIP_* variable for each test.

    A developer's own settings (a fixture upstream mode, a licence key, a credential) must not
    decide a unit test's result. A test that needs a setting sets it itself, and monkeypatch
    restores the variables afterwards.
    """
    for name in [name for name in os.environ if name.startswith("NCI_SIP_")]:
        monkeypatch.delenv(name)
    # Git hooks export repository-local paths. Tests that create their own repositories must
    # not redirect `git init`, commits, refs or installed hooks into the caller's checkout.
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
