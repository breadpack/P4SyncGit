import logging

from P4 import P4, P4Exception

from p4gitsync.p4.p4_change_info import P4ChangeInfo
from p4gitsync.p4.p4_file_action import P4FileAction

logger = logging.getLogger("p4gitsync.p4")


class P4Client:
    def __init__(self, port: str, user: str, workspace: str) -> None:
        self._p4 = P4()
        self._p4.port = port
        self._p4.user = user
        self._p4.client = workspace

    def connect(self) -> None:
        self._p4.connect()
        logger.info("P4 연결 성공: %s@%s", self._p4.user, self._p4.port)

    def disconnect(self) -> None:
        if self._p4.connected():
            self._p4.disconnect()
            logger.info("P4 연결 해제")

    def get_changes_after(self, stream: str, after_cl: int) -> list[int]:
        """지정 CL 이후의 submitted changelist 목록 조회 (오름차순)."""
        results = self._p4.run_changes(
            "-s", "submitted",
            "-e", str(after_cl + 1),
            f"{stream}/...",
        )
        return sorted(int(r["change"]) for r in results)

    def get_all_changes(self, stream: str) -> list[int]:
        """stream의 전체 submitted changelist 목록 조회 (오름차순)."""
        results = self._p4.run_changes(
            "-s", "submitted",
            f"{stream}/...",
        )
        return sorted(int(r["change"]) for r in results)

    def describe(self, changelist: int) -> P4ChangeInfo:
        """changelist 상세 정보 (파일 목록, action, 설명, 작성자)."""
        results = self._p4.run_describe("-s", str(changelist))
        desc = results[0]
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
        )

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

    def sync(self, changelist: int) -> None:
        """워크스페이스를 특정 CL 시점으로 sync."""
        self._p4.run_sync(f"//...@{changelist}")

    def get_users(self) -> list[dict]:
        """전체 P4 사용자 목록 조회."""
        return self._p4.run_users()

    def get_stream_info(self, stream: str) -> dict:
        """stream 상세 정보 조회."""
        results = self._p4.run_stream("-o", stream)
        return results[0] if results else {}

    def get_streams(self, depot: str) -> list[dict]:
        """depot 하위 전체 stream 목록 조회."""
        return self._p4.run_streams(f"{depot}/...")

    def run_filelog(self, depot_paths: list[str], batch_size: int = 200) -> list:
        """다중 파일 filelog 배치 조회. batch_size 단위로 분할."""
        all_results = []
        for i in range(0, len(depot_paths), batch_size):
            chunk = depot_paths[i:i + batch_size]
            results = self._p4.run_filelog(*chunk)
            all_results.extend(results)
        return all_results

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
