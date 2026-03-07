import os
import tempfile

import pytest

from p4gitsync.state.state_store import StateStore


@pytest.fixture
def tmp_state_store():
    """임시 StateStore (테스트용)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    store = StateStore(db_path)
    store.initialize()
    yield store
    store.close()
    os.unlink(db_path)


@pytest.fixture
def tmp_git_repo():
    """임시 Git 리포지토리 (테스트용)."""
    import subprocess
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", d], capture_output=True, check=True)
        yield d
