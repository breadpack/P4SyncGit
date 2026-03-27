import logging
import time
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import TypeVar, Callable, ParamSpec

from P4 import P4, P4Exception

from p4gitsync.p4.p4_change_info import P4ChangeInfo
from p4gitsync.p4.p4_file_action import P4FileAction

logger = logging.getLogger("p4gitsync.p4")

P = ParamSpec("P")
T = TypeVar("T")

_MAX_RECONNECT_RETRIES = 3
_BASE_RECONNECT_DELAY = 1.0


_MAX_AUTO_RECONNECT_ATTEMPTS = 3


def _auto_reconnect(func: Callable[P, T]) -> Callable[P, T]:
    """P4 연결 끊김 시 자동 재연결 후 재시도하는 데코레이터 (최대 3회)."""

    @wraps(func)
    def wrapper(self: "P4Client", *args: P.args, **kwargs: P.kwargs) -> T:
        self._ensure_connected()
        last_error: P4Exception | None = None
        for attempt in range(1, _MAX_AUTO_RECONNECT_ATTEMPTS + 1):
            try:
                return func(self, *args, **kwargs)
            except P4Exception as e:
                if not self._p4.connected():
                    logger.warning(
                        "P4 연결 끊김 감지, 재연결 시도 %d/%d: %s",
                        attempt, _MAX_AUTO_RECONNECT_ATTEMPTS, e,
                    )
                    last_error = e
                    self._reconnect_with_backoff()
                else:
                    raise
        raise last_error  # type: ignore[misc]

    return wrapper


class P4Client:
    def __init__(self, port: str, user: str, workspace: str, password: str = "") -> None:
        self._p4 = P4()
        self._p4.port = port
        self._p4.user = user
        self._p4.client = workspace
        self._password = password

    def connect(self) -> None:
        self._p4.connect()
        if self._password:
            self._p4.password = self._password
            self._p4.run_login()
        logger.info("P4 연결 성공: %s@%s", self._p4.user, self._p4.port)

    def disconnect(self) -> None:
        if self._p4.connected():
            self._p4.disconnect()
            logger.info("P4 연결 해제")

    def _ensure_connected(self) -> None:
        """연결이 끊어져 있으면 재연결."""
        if not self._p4.connected():
            logger.info("P4 연결 끊어짐 — 재연결 시도")
            self._p4.connect()
            if self._password:
                self._p4.password = self._password
                self._p4.run_login()
            logger.info("P4 재연결 성공: %s@%s", self._p4.user, self._p4.port)

    def _reconnect_with_backoff(self) -> None:
        """exponential backoff 기반 재연결."""
        for attempt in range(1, _MAX_RECONNECT_RETRIES + 1):
            delay = _BASE_RECONNECT_DELAY * (2 ** (attempt - 1))
            try:
                logger.info("P4 재연결 시도 %d/%d (%.1fs 후)", attempt, _MAX_RECONNECT_RETRIES, delay)
                time.sleep(delay)
                if self._p4.connected():
                    self._p4.disconnect()
                self._p4.connect()
                if self._password:
                    self._p4.password = self._password
                    self._p4.run_login()
                logger.info("P4 재연결 성공")
                return
            except P4Exception as e:
                logger.warning("P4 재연결 실패 (%d/%d): %s", attempt, _MAX_RECONNECT_RETRIES, e)
        raise P4Exception("P4 재연결 실패: 최대 재시도 횟수 초과")

    @_auto_reconnect
    def get_changes_after(self, stream: str, after_cl: int) -> list[int]:
        """지정 CL 이후의 submitted changelist 목록 조회 (오름차순)."""
        results = self._p4.run_changes(
            "-s", "submitted",
            "-e", str(after_cl + 1),
            f"{stream}/...",
        )
        return sorted(int(r["change"]) for r in results)

    @_auto_reconnect
    def get_all_changes(self, stream: str) -> list[int]:
        """stream의 전체 submitted changelist 목록 조회 (오름차순)."""
        results = self._p4.run_changes(
            "-s", "submitted",
            f"{stream}/...",
        )
        return sorted(int(r["change"]) for r in results)

    @staticmethod
    def _parse_describe_result(desc: dict) -> P4ChangeInfo:
        """P4 describe 결과 dict를 P4ChangeInfo로 변환."""
        files = [
            P4FileAction(
                depot_path=desc["depotFile"][i],
                action=desc["action"][i],
                file_type=desc["type"][i],
                revision=int(desc["rev"][i]),
            )
            for i in range(len(desc.get("depotFile", [])))
        ]
        return P4ChangeInfo(
            changelist=int(desc["change"]),
            user=desc["user"],
            description=desc["desc"],
            timestamp=int(desc["time"]),
            files=files,
            workspace=desc.get("client", ""),
        )

    @_auto_reconnect
    def describe(self, changelist: int) -> P4ChangeInfo:
        """changelist 상세 정보 (파일 목록, action, 설명, 작성자)."""
        results = self._p4.run_describe("-s", str(changelist))
        return self._parse_describe_result(results[0])

    @_auto_reconnect
    def describe_batch(self, changelists: list[int]) -> list[P4ChangeInfo]:
        """다중 changelist를 단일 호출로 describe. 순서 보장."""
        if not changelists:
            return []
        results = self._p4.run_describe("-s", *[str(cl) for cl in changelists])
        return [self._parse_describe_result(desc) for desc in results]

    @_auto_reconnect
    def print_file(self, depot_path: str, revision: int, output_path: str) -> None:
        """특정 리비전의 파일 내용을 로컬 경로에 출력."""
        self._p4.run_print(
            "-o", output_path,
            f"{depot_path}#{revision}",
        )

    def print_file_safe(self, depot_path: str, revision: int, output_path: str) -> bool:
        """파일 추출 시도. obliterate 등으로 실패하면 False 반환."""
        try:
            self.print_file(depot_path, revision, output_path)
            return True
        except P4Exception as e:
            logger.warning(
                "파일 추출 실패 (obliterate?): %s#%d — %s",
                depot_path, revision, e,
            )
            return False

    @_auto_reconnect
    def print_file_to_bytes(self, depot_path: str, revision: int) -> bytes | None:
        """특정 리비전의 파일 내용을 bytes로 반환. 실패 시 None."""
        try:
            results = self._p4.run_print(f"{depot_path}#{revision}")
            if len(results) >= 2:
                content = results[1]
                if isinstance(content, bytes):
                    return content
                return content.encode("utf-8")
            return None
        except P4Exception as e:
            logger.warning(
                "파일 내용 추출 실패: %s#%d — %s",
                depot_path, revision, e,
            )
            return None

    @_auto_reconnect
    def print_file_to_disk(
        self, depot_path: str, revision: int, dest_dir: Path
    ) -> Path:
        """p4 print -o 로 파일을 디스크에 직접 출력. 메모리 로드 없음."""
        filename = PurePosixPath(depot_path).name
        dest_path = Path(dest_dir) / filename
        try:
            self._p4.run_print(
                "-o", str(dest_path), f"{depot_path}#{revision}",
            )
        except P4Exception as e:
            raise RuntimeError(
                f"p4 print -o 실패: {depot_path}#{revision}: {e}"
            ) from e
        if not dest_path.exists():
            raise RuntimeError(
                f"p4 print -o 후 파일 미생성: {dest_path}"
            )
        return dest_path

    @_auto_reconnect
    def print_file_to_bytes_head(self, depot_path: str) -> bytes | None:
        """최신 리비전(#head)의 파일 내용을 bytes로 반환. 실패 시 None."""
        try:
            results = self._p4.run_print(f"{depot_path}#head")
            if len(results) >= 2:
                content = results[1]
                if isinstance(content, bytes):
                    return content
                return content.encode("utf-8")
            return None
        except P4Exception as e:
            logger.warning("파일 내용 추출 실패: %s#head — %s", depot_path, e)
            return None

    @_auto_reconnect
    def print_files_batch(
        self, file_specs: list[str],
    ) -> dict[str, bytes | None]:
        """다중 파일을 단일 p4 print 호출로 일괄 추출.

        Args:
            file_specs: "depot_path#revision" 형식의 파일 스펙 목록.

        Returns:
            {depot_path: bytes | None} 딕셔너리.
        """
        result_map: dict[str, bytes | None] = {}
        if not file_specs:
            return result_map

        try:
            results = self._p4.run_print(*file_specs)
        except P4Exception as e:
            logger.warning("batch print 실패, 개별 추출로 fallback: %s", e)
            for spec in file_specs:
                path = spec.split("#")[0]
                rev = int(spec.split("#")[1]) if "#" in spec else 0
                result_map[path] = self.print_file_to_bytes(path, rev)
            return result_map

        # p4 print 결과: [metadata_dict, content, metadata_dict, content, ...]
        i = 0
        while i < len(results):
            if isinstance(results[i], dict):
                depot_file = results[i].get("depotFile", "")
                content = None
                if i + 1 < len(results) and not isinstance(results[i + 1], dict):
                    raw = results[i + 1]
                    content = raw if isinstance(raw, bytes) else raw.encode("utf-8")
                    i += 2
                else:
                    i += 1
                result_map[depot_file] = content
            else:
                i += 1

        return result_map

    @_auto_reconnect
    def sync(self, changelist: int) -> None:
        """워크스페이스를 특정 CL 시점으로 sync."""
        self._p4.run_sync(f"//...@{changelist}")

    @_auto_reconnect
    def get_users(self) -> list[dict]:
        """전체 P4 사용자 목록 조회."""
        return self._p4.run_users()

    @_auto_reconnect
    def resolve_virtual_stream(self, stream: str) -> tuple[str, list[str]]:
        """virtual stream이면 (parent_stream, exclude_patterns) 반환.

        일반 stream이면 (stream, []) 반환.
        exclude_patterns: parent stream prefix를 strip한 후 매칭할 경로 접두사 목록.
        """
        info = self.get_stream_info(stream)
        if not info or info.get("Type") != "virtual":
            return stream, []

        parent = info.get("Parent", "")
        if not parent or parent == "none":
            logger.warning("virtual stream %s에 parent 없음, 일반 stream으로 처리", stream)
            return stream, []

        paths = info.get("Paths", [])
        excludes: list[str] = []
        for path_entry in paths:
            stripped = path_entry.strip()
            if stripped.startswith("exclude "):
                pattern = stripped[len("exclude "):].strip()
                # "Foo/..." → "Foo/" 형태로 정규화
                if pattern.endswith("/..."):
                    pattern = pattern[:-3]  # "Foo/..." → "Foo/"
                elif pattern.endswith("..."):
                    pattern = pattern[:-3]  # "Foo..." → "Foo"
                excludes.append(pattern)

        logger.info(
            "virtual stream 감지: %s -> parent=%s, excludes=%d개",
            stream, parent, len(excludes),
        )
        return parent, excludes

    @_auto_reconnect
    def get_stream_info(self, stream: str) -> dict:
        """stream 상세 정보 조회."""
        results = self._p4.run_stream("-o", stream)
        return results[0] if results else {}

    @_auto_reconnect
    def get_streams(self, depot: str) -> list[dict]:
        """depot 하위 전체 stream 목록 조회."""
        return self._p4.run_streams(f"{depot}/...")

    @_auto_reconnect
    def run_filelog(self, depot_paths: list[str], batch_size: int = 200) -> list:
        """다중 파일 filelog 배치 조회. batch_size 단위로 분할."""
        all_results = []
        for i in range(0, len(depot_paths), batch_size):
            chunk = depot_paths[i:i + batch_size]
            results = self._p4.run_filelog(*chunk)
            all_results.extend(results)
        return all_results

    @_auto_reconnect
    def check_server_load(self, threshold: int = 50) -> bool:
        """P4 서버 부하 확인. 과부하 시 True 반환."""
        try:
            monitors = self._p4.run_monitor("show")
            active_commands = len([m for m in monitors if m.get("status") == "R"])
            return active_commands > threshold
        except P4Exception:
            return False

    def build_initial_user_mapping(self, company_domain: str) -> list[tuple[str, str, str]]:
        """P4 전체 사용자 목록을 조회하여 (p4_user, full_name, email) 튜플 리스트 반환."""
        users = self.get_users()
        mappings = []
        for u in users:
            p4_user = u["User"]
            full_name = u.get("FullName", p4_user)
            email = u.get("Email", f"{p4_user}@{company_domain}")
            mappings.append((p4_user, full_name, email))
        return mappings

    # ── Git→P4 역방향 동기화용 쓰기 메서드 ──────────────────────────

    @_auto_reconnect
    def create_changelist(self, description: str, user: str | None = None) -> int:
        """새 pending changelist 생성. changelist 번호 반환."""
        change_spec = self._p4.fetch_change()
        change_spec["Description"] = description
        if user:
            change_spec["User"] = user
        result = self._p4.save_change(change_spec)
        # result: ["Change 12345 created."]
        cl_str = result[0].split()[1]
        return int(cl_str)

    @_auto_reconnect
    def p4_add(self, file_path: str, changelist: int) -> None:
        """파일을 changelist에 add."""
        self._p4.run_add("-c", str(changelist), file_path)

    @_auto_reconnect
    def p4_edit(self, file_path: str, changelist: int) -> None:
        """파일을 changelist에 edit (checkout)."""
        self._p4.run_edit("-c", str(changelist), file_path)

    @_auto_reconnect
    def p4_delete(self, file_path: str, changelist: int) -> None:
        """파일을 changelist에 delete."""
        self._p4.run_delete("-c", str(changelist), file_path)

    @_auto_reconnect
    def submit_changelist(self, changelist: int) -> int:
        """pending changelist를 submit. 최종 CL 번호 반환."""
        result = self._p4.run_submit("-c", str(changelist))
        # submit 결과에서 submittedChange 추출
        for item in result:
            if isinstance(item, dict) and "submittedChange" in item:
                return int(item["submittedChange"])
        raise RuntimeError(f"submit 결과에서 CL 번호를 찾을 수 없음: {result}")

    @_auto_reconnect
    def revert_changelist(self, changelist: int) -> None:
        """changelist의 모든 파일을 revert."""
        try:
            self._p4.run_revert("-c", str(changelist), "//...")
        except P4Exception:
            pass  # 파일이 없으면 무시

    @_auto_reconnect
    def delete_changelist(self, changelist: int) -> None:
        """빈 pending changelist 삭제."""
        try:
            self._p4.run_change("-d", str(changelist))
        except P4Exception:
            pass  # 이미 삭제됐거나 파일이 남아있으면 무시

    @_auto_reconnect
    def ensure_workspace(self, name: str, stream: str, root: str) -> bool:
        """workspace가 없으면 생성, 있으면 stream 매핑 확인/업데이트.

        Returns:
            True면 새로 생성됨, False면 기존 workspace 사용.
        """
        spec = self._p4.fetch_client(name)
        existing_root = spec.get("Root", "")
        existing_stream = spec.get("Stream", "")

        if existing_root and existing_stream == stream:
            logger.info("workspace 이미 존재: %s (stream=%s)", name, stream)
            return False

        spec["Root"] = root
        spec["Stream"] = stream
        spec["Options"] = spec.get("Options", "noallwrite noclobber nocompress unlocked nomodtime normdir")
        self._p4.save_client(spec)

        if existing_root:
            logger.info("workspace 업데이트: %s (stream=%s, root=%s)", name, stream, root)
        else:
            logger.info("workspace 생성: %s (stream=%s, root=%s)", name, stream, root)
        return not bool(existing_root)

    def get_workspace_root(self, workspace: str | None = None) -> str:
        """workspace의 root 경로 반환."""
        ws = workspace or self._p4.client
        client_spec = self._p4.fetch_client(ws)
        return client_spec.get("Root", "")

    @_auto_reconnect
    def file_exists(self, depot_path: str) -> bool:
        """depot에 파일이 존재하는지 확인."""
        try:
            result = self._p4.run_fstat(depot_path)
            return len(result) > 0
        except P4Exception:
            return False
