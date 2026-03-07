import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass

logger = logging.getLogger("p4gitsync.state")

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS sync_state (
    stream      TEXT PRIMARY KEY,
    last_cl     INTEGER NOT NULL,
    commit_sha  TEXT NOT NULL,
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cl_commit_map (
    changelist      INTEGER NOT NULL,
    commit_sha      TEXT NOT NULL,
    stream          TEXT NOT NULL,
    branch          TEXT NOT NULL,
    has_integration INTEGER DEFAULT 0,
    git_push_status TEXT DEFAULT 'pending',
    created_at      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (changelist, stream)
);

CREATE INDEX IF NOT EXISTS idx_cl_stream
    ON cl_commit_map(stream, changelist);
CREATE INDEX IF NOT EXISTS idx_push_status
    ON cl_commit_map(git_push_status);

CREATE TABLE IF NOT EXISTS user_mappings (
    p4_user     TEXT PRIMARY KEY,
    git_name    TEXT NOT NULL,
    git_email   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    changelist  INTEGER NOT NULL,
    stream      TEXT NOT NULL,
    error_msg   TEXT,
    retry_count INTEGER DEFAULT 0,
    resolved    INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stream_registry (
    stream              TEXT PRIMARY KEY,
    branch              TEXT NOT NULL,
    parent_stream       TEXT,
    branch_point_cl     INTEGER
);

CREATE TABLE IF NOT EXISTS cl_commit_map_archive (
    changelist      INTEGER NOT NULL,
    commit_sha      TEXT NOT NULL,
    stream          TEXT NOT NULL,
    branch          TEXT NOT NULL,
    has_integration INTEGER DEFAULT 0,
    created_at      TEXT,
    archived_at     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (changelist, stream)
);
"""


@dataclass
class StreamMapping:
    stream: str
    branch: str
    parent_stream: str | None = None
    branch_point_cl: int | None = None


class StateStore:
    """SQLite 기반 상태 관리."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._in_transaction: bool = False
        self._author_cache: dict[str, tuple[str, str]] = {}
        self._author_cache_time: float = 0.0
        self._author_cache_ttl: float = 300.0  # 5분

    def initialize(self) -> None:
        """스키마 생성 + WAL 모드 설정."""
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        logger.info("StateStore 초기화 완료: %s", self._db_path)

    @contextmanager
    def transaction(self):
        """트랜잭션 내에서 개별 commit을 억제하고, 종료 시 일괄 commit."""
        if self._in_transaction:
            yield
            return
        self._in_transaction = True
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            self._in_transaction = False

    def _auto_commit(self) -> None:
        """트랜잭션 중이 아닐 때만 commit."""
        if not self._in_transaction:
            self._conn.commit()

    def get_last_synced_cl(self, stream: str) -> int:
        """마지막 동기화 CL. 없으면 0 반환."""
        row = self._conn.execute(
            "SELECT last_cl FROM sync_state WHERE stream = ?", (stream,)
        ).fetchone()
        return row["last_cl"] if row else 0

    def set_last_synced_cl(self, stream: str, cl: int, commit_sha: str) -> None:
        self._conn.execute(
            """INSERT INTO sync_state (stream, last_cl, commit_sha)
               VALUES (?, ?, ?)
               ON CONFLICT(stream) DO UPDATE SET
                   last_cl = excluded.last_cl,
                   commit_sha = excluded.commit_sha,
                   updated_at = datetime('now')""",
            (stream, cl, commit_sha),
        )
        self._auto_commit()

    def get_commit_sha(self, changelist: int, stream: str | None = None) -> str | None:
        """CL -> SHA 조회. stream 지정 시 해당 stream의 SHA만 조회."""
        if stream:
            row = self._conn.execute(
                "SELECT commit_sha FROM cl_commit_map WHERE changelist = ? AND stream = ?",
                (changelist, stream),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT commit_sha FROM cl_commit_map WHERE changelist = ?",
                (changelist,),
            ).fetchone()
        return row["commit_sha"] if row else None

    def record_commit(
        self,
        cl: int,
        sha: str,
        stream: str,
        branch: str,
        has_integration: bool = False,
    ) -> None:
        self._conn.execute(
            """INSERT INTO cl_commit_map (changelist, commit_sha, stream, branch, has_integration)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(changelist, stream) DO UPDATE SET
                   commit_sha = excluded.commit_sha,
                   has_integration = excluded.has_integration""",
            (cl, sha, stream, branch, int(has_integration)),
        )
        self._auto_commit()

    def update_push_status(self, changelist: int, stream: str, status: str) -> None:
        """git_push_status 업데이트 (pending / pushed / failed)."""
        self._conn.execute(
            "UPDATE cl_commit_map SET git_push_status = ? WHERE changelist = ? AND stream = ?",
            (status, changelist, stream),
        )
        self._auto_commit()

    def get_pending_pushes(self) -> list[dict]:
        """push 대기/실패 건 조회."""
        rows = self._conn.execute(
            """SELECT changelist, commit_sha, stream, branch, git_push_status
               FROM cl_commit_map
               WHERE git_push_status IN ('pending', 'failed')
               ORDER BY changelist""",
        ).fetchall()
        return [dict(r) for r in rows]

    def get_git_author(self, p4_user: str, default_domain: str = "company.com") -> tuple[str, str]:
        """P4 사용자 -> (name, email) 조회. TTL 캐시 적용."""
        now = time.monotonic()
        if now - self._author_cache_time > self._author_cache_ttl:
            self._author_cache.clear()
            self._author_cache_time = now

        if p4_user in self._author_cache:
            return self._author_cache[p4_user]

        row = self._conn.execute(
            "SELECT git_name, git_email FROM user_mappings WHERE p4_user = ?",
            (p4_user,),
        ).fetchone()
        if row:
            result = (row["git_name"], row["git_email"])
        else:
            result = (p4_user, f"{p4_user}@{default_domain}")
        self._author_cache[p4_user] = result
        return result

    def upsert_user_mapping(self, p4_user: str, git_name: str, git_email: str) -> None:
        """사용자 매핑 등록/갱신."""
        self._conn.execute(
            """INSERT INTO user_mappings (p4_user, git_name, git_email)
               VALUES (?, ?, ?)
               ON CONFLICT(p4_user) DO UPDATE SET
                   git_name = excluded.git_name,
                   git_email = excluded.git_email""",
            (p4_user, git_name, git_email),
        )
        self._auto_commit()
        self._author_cache.pop(p4_user, None)

    def bulk_upsert_user_mappings(self, mappings: list[tuple[str, str, str]]) -> int:
        """사용자 매핑 일괄 등록. (p4_user, git_name, git_email) 튜플 리스트."""
        self._conn.executemany(
            """INSERT INTO user_mappings (p4_user, git_name, git_email)
               VALUES (?, ?, ?)
               ON CONFLICT(p4_user) DO UPDATE SET
                   git_name = excluded.git_name,
                   git_email = excluded.git_email""",
            mappings,
        )
        self._auto_commit()
        self._author_cache.clear()
        return len(mappings)

    def verify_consistency(self, branch: str, git_head_sha: str) -> bool:
        """서비스 시작 시 Git 최신 commit의 SHA와 StateStore 교차 검증."""
        row = self._conn.execute(
            "SELECT commit_sha FROM sync_state WHERE stream IN "
            "(SELECT stream FROM stream_registry WHERE branch = ?)",
            (branch,),
        ).fetchone()
        if row is None:
            return True
        return row["commit_sha"] == git_head_sha

    def get_last_commit_before(self, stream: str, before_cl: int) -> str | None:
        """특정 CL 직전의 commit SHA (분기점 매핑용)."""
        row = self._conn.execute(
            """SELECT commit_sha FROM cl_commit_map
               WHERE stream = ? AND changelist < ?
               ORDER BY changelist DESC LIMIT 1""",
            (stream, before_cl),
        ).fetchone()
        return row["commit_sha"] if row else None

    def register_stream(self, mapping: StreamMapping) -> None:
        """stream 등록 (분기점 포함)."""
        self._conn.execute(
            """INSERT INTO stream_registry (stream, branch, parent_stream, branch_point_cl)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(stream) DO UPDATE SET
                   branch = excluded.branch,
                   parent_stream = excluded.parent_stream,
                   branch_point_cl = excluded.branch_point_cl""",
            (mapping.stream, mapping.branch, mapping.parent_stream, mapping.branch_point_cl),
        )
        self._auto_commit()

    def get_all_registered_streams(self) -> list[StreamMapping]:
        """등록된 모든 stream 매핑 목록 조회."""
        rows = self._conn.execute(
            "SELECT stream, branch, parent_stream, branch_point_cl FROM stream_registry"
        ).fetchall()
        return [
            StreamMapping(
                stream=row["stream"],
                branch=row["branch"],
                parent_stream=row["parent_stream"],
                branch_point_cl=row["branch_point_cl"],
            )
            for row in rows
        ]

    def is_stream_synced(self, stream: str) -> bool:
        """stream이 한 번이라도 동기화된 적이 있는지 확인."""
        row = self._conn.execute(
            "SELECT last_cl FROM sync_state WHERE stream = ?", (stream,)
        ).fetchone()
        return row is not None

    def get_stream_mapping(self, stream: str) -> StreamMapping | None:
        """stream 매핑 정보 조회."""
        row = self._conn.execute(
            "SELECT stream, branch, parent_stream, branch_point_cl FROM stream_registry WHERE stream = ?",
            (stream,),
        ).fetchone()
        if row is None:
            return None
        return StreamMapping(
            stream=row["stream"],
            branch=row["branch"],
            parent_stream=row["parent_stream"],
            branch_point_cl=row["branch_point_cl"],
        )

    def record_sync_error(self, changelist: int, stream: str, error_msg: str) -> int:
        """동기화 에러 기록. retry_count 반환 (1부터 시작)."""
        row = self._conn.execute(
            "SELECT id, retry_count FROM sync_errors WHERE changelist = ? AND stream = ? AND resolved = 0",
            (changelist, stream),
        ).fetchone()
        if row:
            new_count = row["retry_count"] + 1
            self._conn.execute(
                "UPDATE sync_errors SET retry_count = ?, error_msg = ? WHERE id = ?",
                (new_count, error_msg, row["id"]),
            )
            self._auto_commit()
            return new_count
        else:
            self._conn.execute(
                "INSERT INTO sync_errors (changelist, stream, error_msg, retry_count) VALUES (?, ?, ?, 1)",
                (changelist, stream, error_msg),
            )
            self._auto_commit()
            return 1

    def resolve_error(self, changelist: int, stream: str) -> None:
        """에러 해결 처리."""
        self._conn.execute(
            "UPDATE sync_errors SET resolved = 1 WHERE changelist = ? AND stream = ? AND resolved = 0",
            (changelist, stream),
        )
        self._auto_commit()

    def get_unresolved_errors(self) -> list[dict]:
        """미해결 에러 목록."""
        rows = self._conn.execute(
            """SELECT changelist, stream, error_msg, retry_count, created_at
               FROM sync_errors WHERE resolved = 0
               ORDER BY created_at DESC""",
        ).fetchall()
        return [dict(r) for r in rows]

    def cleanup_resolved_errors(self, retention_days: int = 90) -> int:
        """resolved=1인 sync_errors를 retention_days 경과 후 자동 삭제."""
        cursor = self._conn.execute(
            "DELETE FROM sync_errors WHERE resolved = 1 "
            "AND created_at < datetime('now', ?)",
            (f"-{retention_days} days",),
        )
        count = cursor.rowcount
        self._auto_commit()
        if count > 0:
            logger.info("해결된 sync_errors %d건 정리 (보존: %d일)", count, retention_days)
        return count

    def archive_old_commit_maps(self, retention_days: int = 365) -> int:
        """pushed 후 retention_days 경과한 cl_commit_map을 아카이브 테이블로 이동."""
        cursor = self._conn.execute(
            """INSERT INTO cl_commit_map_archive
               (changelist, commit_sha, stream, branch, has_integration, created_at)
               SELECT changelist, commit_sha, stream, branch, has_integration, created_at
               FROM cl_commit_map
               WHERE git_push_status = 'pushed'
               AND created_at < datetime('now', ?)""",
            (f"-{retention_days} days",),
        )
        archived = cursor.rowcount
        if archived > 0:
            self._conn.execute(
                """DELETE FROM cl_commit_map
                   WHERE git_push_status = 'pushed'
                   AND created_at < datetime('now', ?)""",
                (f"-{retention_days} days",),
            )
            self._auto_commit()
            logger.info("cl_commit_map %d건 아카이브 (보존: %d일)", archived, retention_days)
        return archived

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
