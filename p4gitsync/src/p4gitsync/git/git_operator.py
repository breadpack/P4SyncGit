from typing import Protocol, Sequence

from p4gitsync.git.commit_metadata import CommitMetadata


class GitOperator(Protocol):
    """Git 조작 인터페이스 (Protocol 기반 structural subtyping)."""

    def init_repo(self) -> None:
        """리포지토리 초기화 또는 열기."""
        ...

    def create_commit(
        self,
        branch: str,
        parent_sha: str | None,
        metadata: CommitMetadata,
        file_changes: list[tuple[str, bytes]],
        deletes: list[str] | None = None,
    ) -> str:
        """일반 commit 생성 (parent 1개). commit SHA 반환."""
        ...

    def create_merge_commit(
        self,
        branch: str,
        parent_shas: Sequence[str],
        metadata: CommitMetadata,
        file_changes: list[tuple[str, bytes]],
        deletes: list[str] | None = None,
    ) -> str:
        """merge commit 생성 (parent 2개 이상). commit SHA 반환."""
        ...

    def create_branch(self, branch: str, start_sha: str) -> None:
        """지정 commit에서 새 branch 생성."""
        ...

    def create_orphan_branch(self, branch: str) -> None:
        """parent 없는 orphan branch 생성 (mainline용)."""
        ...

    def push(self, branch: str) -> None:
        """remote push."""
        ...

    def get_head_sha(self, branch: str) -> str | None:
        """branch의 HEAD commit SHA 반환. 없으면 None."""
        ...

    def maybe_run_gc(self, gc_interval: int) -> None:
        """설정된 간격마다 git gc 실행."""
        ...

    def fetch(self, remote: str = "origin") -> None:
        """remote에서 fetch."""
        ...

    def get_log_after(self, branch: str, after_sha: str | None, remote: str = "origin") -> list[dict]:
        """after_sha 이후의 remote branch commit 목록 반환 (오래된 순).

        반환: [{"sha": str, "message": str, "author_name": str, "author_email": str, "timestamp": int, "parents": list[str]}]
        after_sha가 None이면 전체 log.
        """
        ...

    def get_commit_files(self, commit_sha: str) -> tuple[list[tuple[str, bytes]], list[str]]:
        """commit의 파일 변경사항 반환.

        반환: (file_changes: [(path, content)], deletes: [path])
        """
        ...

    def delete_branch(self, branch: str) -> None:
        """branch 삭제."""
        ...

    def list_remote_branches(self, remote: str = "origin", prefix: str = "") -> list[str]:
        """remote branch 목록 반환. prefix 지정 시 해당 prefix로 시작하는 것만."""
        ...
