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
            ["git", "fast-import", "--force"],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
        self._write_bytes(msg_bytes + b"\n")

        for path, content in files:
            self._write(f"M 100644 inline {path}\n")
            self._write(f"data {len(content)}\n")
            self._write_bytes(content + b"\n")

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
        self._write_bytes(msg_bytes + b"\n")

        if merge_from_mark:
            self._write(f"merge :{merge_from_mark}\n")
        elif merge_from_ref:
            self._write(f"merge {merge_from_ref}\n")

        for path, content in files:
            self._write(f"M 100644 inline {path}\n")
            self._write(f"data {len(content)}\n")
            self._write_bytes(content + b"\n")

        for path in (deletes or []):
            self._write(f"D {path}\n")

        self._write("\n")
        return self._mark

    def begin_commit(
        self,
        branch: str,
        metadata: CommitMetadata,
    ) -> int:
        """스트리밍 commit 시작. mark 번호 반환.

        이후 write_file(), write_delete()를 호출하고,
        end_commit()으로 마무리한다.
        """
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
        self._write_bytes(msg_bytes + b"\n")
        return self._mark

    def write_file(self, path: str, content: bytes) -> None:
        """현재 commit에 파일 추가. begin_commit() 이후에 호출."""
        self._write(f"M 100644 inline {path}\n")
        self._write(f"data {len(content)}\n")
        self._write_bytes(content + b"\n")

    def write_delete(self, path: str) -> None:
        """현재 commit에서 파일 삭제. begin_commit() 이후에 호출."""
        self._write(f"D {path}\n")

    def end_commit(self) -> None:
        """스트리밍 commit 종료."""
        self._write("\n")

    def checkpoint(self) -> None:
        """체크포인트 생성."""
        self._write("checkpoint\n\n")
        if self._proc and self._proc.stdin and not self._proc.stdin.closed:
            self._proc.stdin.flush()

    def finish(self) -> int:
        """fast-import 프로세스 종료. returncode 반환 (정상=0, 프로세스 없음=0)."""
        if not self._proc:
            return 0
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()
        # stderr 출력
        stderr_output = ""
        if self._proc.stderr:
            try:
                stderr_output = self._proc.stderr.read().decode(errors="replace").strip()
            except Exception:
                pass
            finally:
                try:
                    self._proc.stderr.close()
                except OSError:
                    pass
        rc = self._proc.returncode or 0
        if rc != 0:
            logger.error(
                "fast-import 비정상 종료 (exit=%d): %s",
                rc,
                stderr_output[-500:] if stderr_output else "stderr 없음",
            )
        else:
            logger.info("fast-import 완료 (총 %d marks)", self._mark)
        self._proc = None
        return rc

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def current_mark(self) -> int:
        return self._mark

    def _write_bytes(self, data: bytes) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.stdin.closed:
            raise OSError("fast-import 프로세스가 실행 중이 아닙니다")
        self._proc.stdin.write(data)

    def _write(self, data: str) -> None:
        self._write_bytes(data.encode("utf-8"))
