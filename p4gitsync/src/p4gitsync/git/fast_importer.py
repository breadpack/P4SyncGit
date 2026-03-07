import logging
import subprocess

from p4gitsync.git.commit_metadata import CommitMetadata

logger = logging.getLogger("p4gitsync.git.fast_import")


class FastImporter:
    """git fast-import를 통한 대규모 히스토리 일괄 import."""

    def __init__(self, repo_path: str) -> None:
        self._repo_path = repo_path
        self._proc: subprocess.Popen | None = None
        self._mark = 0

    def start(self) -> None:
        self._proc = subprocess.Popen(
            ["git", "fast-import", "--force", "--quiet"],
            stdin=subprocess.PIPE,
            cwd=self._repo_path,
        )
        logger.info("fast-import 프로세스 시작")

    def add_commit(
        self,
        branch: str,
        metadata: CommitMetadata,
        files: list[tuple[str, bytes]],
        deletes: list[str] | None = None,
    ) -> int:
        """commit 추가. mark 번호 반환."""
        self._mark += 1
        lines = [
            f"commit refs/heads/{branch}",
            f"mark :{self._mark}",
            f"author {metadata.author_name} <{metadata.author_email}> {metadata.author_timestamp} +0000",
            f"committer {metadata.author_name} <{metadata.author_email}> {metadata.author_timestamp} +0000",
        ]
        msg = metadata.format_message()
        msg_bytes = msg.encode("utf-8")
        lines.append(f"data {len(msg_bytes)}")

        self._write("\n".join(lines) + "\n")
        self._proc.stdin.write(msg_bytes + b"\n")

        for path, content in files:
            self._write(f"M 100644 inline {path}\n")
            self._write(f"data {len(content)}\n")
            self._proc.stdin.write(content + b"\n")

        for path in (deletes or []):
            self._write(f"D {path}\n")

        self._write("\n")
        return self._mark

    def add_merge_commit(
        self,
        branch: str,
        merge_from_mark: int | None,
        merge_from_ref: str | None,
        metadata: CommitMetadata,
        files: list[tuple[str, bytes]],
        deletes: list[str] | None = None,
    ) -> int:
        """merge commit 추가."""
        self._mark += 1
        lines = [
            f"commit refs/heads/{branch}",
            f"mark :{self._mark}",
            f"author {metadata.author_name} <{metadata.author_email}> {metadata.author_timestamp} +0000",
            f"committer {metadata.author_name} <{metadata.author_email}> {metadata.author_timestamp} +0000",
        ]
        msg = metadata.format_message()
        msg_bytes = msg.encode("utf-8")
        lines.append(f"data {len(msg_bytes)}")

        self._write("\n".join(lines) + "\n")
        self._proc.stdin.write(msg_bytes + b"\n")

        if merge_from_mark:
            self._write(f"merge :{merge_from_mark}\n")
        elif merge_from_ref:
            self._write(f"merge {merge_from_ref}\n")

        for path, content in files:
            self._write(f"M 100644 inline {path}\n")
            self._write(f"data {len(content)}\n")
            self._proc.stdin.write(content + b"\n")

        for path in (deletes or []):
            self._write(f"D {path}\n")

        self._write("\n")
        return self._mark

    def checkpoint(self) -> None:
        """체크포인트 생성."""
        self._write("checkpoint\n\n")
        self._proc.stdin.flush()

    def finish(self) -> None:
        """fast-import 프로세스 종료."""
        if self._proc and self._proc.stdin:
            self._proc.stdin.close()
            self._proc.wait()
            if self._proc.returncode != 0:
                logger.error("fast-import 프로세스 비정상 종료: %d", self._proc.returncode)
            else:
                logger.info("fast-import 완료 (총 %d marks)", self._mark)
            self._proc = None

    @property
    def current_mark(self) -> int:
        return self._mark

    def _write(self, data: str) -> None:
        self._proc.stdin.write(data.encode("utf-8"))
