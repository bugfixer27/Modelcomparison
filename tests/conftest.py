import os, sys, shutil
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def cfg(tmp_path):
    """Config bound to a temporary root that shares the real config/ but has an empty store."""
    from scoreboard import config as c
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    for f in ("scoreboard.yaml", "sites.yaml"):
        shutil.copy(ROOT / "config" / f, root / "config" / f)
    (root / ".env").write_text("")
    c.load.cache_clear()
    cfg = c.load(root)
    yield cfg
    c.load.cache_clear()
