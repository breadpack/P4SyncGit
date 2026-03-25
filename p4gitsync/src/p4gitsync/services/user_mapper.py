from __future__ import annotations

import importlib.util
import logging
import types
from pathlib import Path

from p4gitsync.config.sync_config import UserMappingConfig
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.services.user_mapper")


class UserMapper:
    """사용자 매핑을 처리한다.

    script가 설정되면 사용자 정의 스크립트의 p4_to_git() / git_to_p4()를 호출한다.
    미설정이면 StateStore의 user_mappings 테이블을 사용한다 (기존 동작).
    """

    def __init__(
        self,
        config: UserMappingConfig,
        state_store: StateStore,
    ) -> None:
        self._state = state_store
        self._module: types.ModuleType | None = None

        if config.script:
            self._module = self._load_script(config.script)

    def p4_to_git(self, changelist_info: dict) -> dict:
        """P4 changelist 정보 → Git author 매핑.

        Args:
            changelist_info: {
                "user": str,          # P4 user (예: "CODE")
                "workspace": str,     # workspace 이름
                "description": str,   # CL 설명
                "changelist": int,    # CL 번호
            }

        Returns:
            {"name": str, "email": str}
        """
        if self._module and hasattr(self._module, "p4_to_git"):
            try:
                result = self._module.p4_to_git(changelist_info)
                if isinstance(result, dict) and "name" in result and "email" in result:
                    return result
                logger.warning(
                    "p4_to_git 스크립트 반환값 형식 오류, fallback: %s", result,
                )
            except Exception:
                logger.exception("p4_to_git 스크립트 실행 실패, fallback")

        # fallback: StateStore user_mappings
        p4_user = changelist_info.get("user", "")
        name, email = self._state.get_git_author(p4_user)
        return {"name": name, "email": email}

    def git_to_p4(self, commit_info: dict) -> dict:
        """Git commit 정보 → P4 submit 정보 매핑.

        Args:
            commit_info: {
                "author_name": str,   # Git author 이름
                "author_email": str,  # Git author 이메일
                "message": str,       # commit message
            }

        Returns:
            {
                "user": str | None,        # P4 user (None이면 기본 계정)
                "workspace": str | None,   # workspace (None이면 설정값)
                "description": str,        # changelist 설명
            }
        """
        if self._module and hasattr(self._module, "git_to_p4"):
            try:
                result = self._module.git_to_p4(commit_info)
                if isinstance(result, dict) and "description" in result:
                    return result
                logger.warning(
                    "git_to_p4 스크립트 반환값 형식 오류, fallback: %s", result,
                )
            except Exception:
                logger.exception("git_to_p4 스크립트 실행 실패, fallback")

        # fallback: StateStore 역매핑
        email = commit_info.get("author_email", "")
        p4_user = self._state.get_p4_user(email)
        return {
            "user": p4_user,
            "workspace": None,
            "description": commit_info.get("message", ""),
        }

    @staticmethod
    def _load_script(script_path: str) -> types.ModuleType:
        """사용자 정의 Python 스크립트를 모듈로 로드."""
        path = Path(script_path)
        if not path.exists():
            raise FileNotFoundError(
                f"user_mapping 스크립트를 찾을 수 없음: {script_path}"
            )

        spec = importlib.util.spec_from_file_location("user_mapper_plugin", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        logger.info("사용자 매핑 스크립트 로드 완료: %s", script_path)

        # 인터페이스 검증
        if not hasattr(module, "p4_to_git"):
            logger.warning("스크립트에 p4_to_git() 없음: %s", script_path)
        if not hasattr(module, "git_to_p4"):
            logger.warning("스크립트에 git_to_p4() 없음: %s", script_path)

        return module
