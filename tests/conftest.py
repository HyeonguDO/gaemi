import pytest

from assetlog import config


@pytest.fixture(autouse=True)
def _no_real_data_or_keys(tmp_path, monkeypatch):
    """No test writes to the real data/ (sync.sync records every run there) or reads the real .env (keys would make
    a sync through the app call live broker APIs)."""
    monkeypatch.setattr(config, "DATA", tmp_path / "data")
    monkeypatch.setattr(config, "load", lambda *a, **k: {})
