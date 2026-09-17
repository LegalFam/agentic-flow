import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def code_excerpts_count_as_articulado(monkeypatch):
    monkeypatch.setattr(settings, "locator_min_articulado_articles", 1)
