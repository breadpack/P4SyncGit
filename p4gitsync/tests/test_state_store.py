import os
import tempfile

import pytest

from p4gitsync.state.state_store import StateStore, StreamMapping


@pytest.fixture
def state_store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    store = StateStore(db_path)
    store.initialize()
    yield store
    store.close()
    os.unlink(db_path)


class TestStateStore:
    def test_initialize_creates_tables(self, state_store: StateStore):
        row = state_store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [r["name"] for r in row]
        assert "sync_state" in table_names
        assert "cl_commit_map" in table_names
        assert "user_mappings" in table_names
        assert "sync_errors" in table_names
        assert "stream_registry" in table_names

    def test_wal_mode_enabled(self, state_store: StateStore):
        row = state_store._conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"

    def test_get_last_synced_cl_default(self, state_store: StateStore):
        assert state_store.get_last_synced_cl("//test/main") == 0

    def test_set_and_get_last_synced_cl(self, state_store: StateStore):
        state_store.set_last_synced_cl("//test/main", 100, "abc123")
        assert state_store.get_last_synced_cl("//test/main") == 100

    def test_set_last_synced_cl_updates(self, state_store: StateStore):
        state_store.set_last_synced_cl("//test/main", 100, "abc123")
        state_store.set_last_synced_cl("//test/main", 200, "def456")
        assert state_store.get_last_synced_cl("//test/main") == 200

    def test_record_and_get_commit(self, state_store: StateStore):
        state_store.record_commit(100, "sha100", "//test/main", "main")
        assert state_store.get_commit_sha(100) == "sha100"

    def test_get_commit_sha_with_stream(self, state_store: StateStore):
        state_store.record_commit(100, "sha100", "//test/main", "main")
        state_store.record_commit(100, "sha101", "//test/dev", "dev")
        assert state_store.get_commit_sha(100, "//test/main") == "sha100"
        assert state_store.get_commit_sha(100, "//test/dev") == "sha101"

    def test_get_commit_sha_not_found(self, state_store: StateStore):
        assert state_store.get_commit_sha(999) is None

    def test_user_mapping(self, state_store: StateStore):
        state_store.upsert_user_mapping("john", "John Doe", "john@example.com")
        name, email = state_store.get_git_author("john")
        assert name == "John Doe"
        assert email == "john@example.com"

    def test_user_mapping_default(self, state_store: StateStore):
        name, email = state_store.get_git_author("unknown")
        assert name == "unknown"
        assert email == "unknown@company.com"

    def test_user_mapping_upsert(self, state_store: StateStore):
        state_store.upsert_user_mapping("john", "John", "john@old.com")
        state_store.upsert_user_mapping("john", "John Doe", "john@new.com")
        name, email = state_store.get_git_author("john")
        assert name == "John Doe"
        assert email == "john@new.com"

    def test_bulk_upsert_user_mappings(self, state_store: StateStore):
        mappings = [
            ("user1", "User One", "user1@example.com"),
            ("user2", "User Two", "user2@example.com"),
            ("user3", "User Three", "user3@example.com"),
        ]
        count = state_store.bulk_upsert_user_mappings(mappings)
        assert count == 3
        name, email = state_store.get_git_author("user2")
        assert name == "User Two"

    def test_verify_consistency_initial(self, state_store: StateStore):
        assert state_store.verify_consistency("main", "anything") is True

    def test_verify_consistency_match(self, state_store: StateStore):
        state_store.register_stream(StreamMapping("//test/main", "main"))
        state_store.set_last_synced_cl("//test/main", 100, "sha100")
        assert state_store.verify_consistency("main", "sha100") is True

    def test_verify_consistency_mismatch(self, state_store: StateStore):
        state_store.register_stream(StreamMapping("//test/main", "main"))
        state_store.set_last_synced_cl("//test/main", 100, "sha100")
        assert state_store.verify_consistency("main", "sha999") is False

    def test_get_last_commit_before(self, state_store: StateStore):
        state_store.record_commit(10, "sha10", "//test/main", "main")
        state_store.record_commit(20, "sha20", "//test/main", "main")
        state_store.record_commit(30, "sha30", "//test/main", "main")
        assert state_store.get_last_commit_before("//test/main", 25) == "sha20"
        assert state_store.get_last_commit_before("//test/main", 10) is None

    def test_register_and_get_stream(self, state_store: StateStore):
        mapping = StreamMapping("//test/main", "main", None, None)
        state_store.register_stream(mapping)
        result = state_store.get_stream_mapping("//test/main")
        assert result is not None
        assert result.branch == "main"

    def test_register_stream_with_parent(self, state_store: StateStore):
        mapping = StreamMapping("//test/dev", "dev", "//test/main", 50)
        state_store.register_stream(mapping)
        result = state_store.get_stream_mapping("//test/dev")
        assert result.parent_stream == "//test/main"
        assert result.branch_point_cl == 50

    def test_record_sync_error(self, state_store: StateStore):
        count = state_store.record_sync_error(100, "//test/main", "test error")
        assert count == 1
        count = state_store.record_sync_error(100, "//test/main", "retry error")
        assert count == 2

    def test_resolve_error(self, state_store: StateStore):
        state_store.record_sync_error(100, "//test/main", "error")
        state_store.resolve_error(100, "//test/main")
        errors = state_store.get_unresolved_errors()
        assert len(errors) == 0

    def test_get_unresolved_errors(self, state_store: StateStore):
        state_store.record_sync_error(100, "//test/main", "error1")
        state_store.record_sync_error(200, "//test/main", "error2")
        errors = state_store.get_unresolved_errors()
        assert len(errors) == 2

    def test_push_status(self, state_store: StateStore):
        state_store.record_commit(100, "sha100", "//test/main", "main")
        pending = state_store.get_pending_pushes()
        assert len(pending) == 1
        assert pending[0]["git_push_status"] == "pending"

        state_store.update_push_status(100, "//test/main", "pushed")
        pending = state_store.get_pending_pushes()
        assert len(pending) == 0
