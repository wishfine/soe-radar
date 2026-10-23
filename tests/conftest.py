import pytest

from soe_radar.config import load_settings
from soe_radar.db import initialize


@pytest.fixture
def cfg(tmp_path):
    settings = load_settings(tmp_path / "absent.yaml")
    settings.data_dir = tmp_path / "data"
    settings.request_delay = 0
    settings.llm.providers = []
    return settings


@pytest.fixture
def db(cfg):
    return initialize(cfg)
