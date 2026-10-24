import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """Nothing in the real environment may influence a test."""
    for name in [k for k in os.environ if k.startswith('TRADOVATE_')]:
        monkeypatch.delenv(name, raising=False)
