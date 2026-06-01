"""GitLab 서버 거버넌스를 API로 자동 적용 (Layer 1 — 불가우회 게이트).

`provision`이 생성하는 GITLAB-SETUP.md 체크리스트를 사람이 수동 적용하는 대신,
GitLab REST API(v4)로 직접 설정한다:

- Push Rule        : max_file_size(=LFS 아닌 대용량 차단 F1), prevent_secrets
- Project Settings : lfs_enabled, merge_method, merge train, pipeline 성공 강제
- Protected Branch : force-push 금지 + 직접 push 금지(MR 경유) (F3)

토큰은 config에 저장하지 않고 호출부(env/CLI)에서 주입한다. HTTP 계층은
GitLabClient로 분리해 테스트 시 가짜 클라이언트로 대체할 수 있다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import quote

# GitLab access level 상수
ACCESS_NO_ONE = 0
ACCESS_MAINTAINER = 40


# ── 순수 payload 빌더 (단위 테스트 대상) ──────────────────────────


def push_rule_payload(spec: ProvisionSpec) -> dict:
    """push rule 페이로드. max_file_size 단위는 MB(GitLab 규약)."""
    return {
        "max_file_size": int(spec.max_file_size_mb),
        "prevent_secrets": spec.prevent_secrets,
    }


def project_settings_payload(spec: ProvisionSpec) -> dict:
    payload: dict = {
        "lfs_enabled": spec.lfs_enabled,
        "merge_method": spec.merge_method,
        "only_allow_merge_if_pipeline_succeeds": spec.require_pipeline_success,
    }
    if spec.merge_trains:
        # merge train은 merge pipeline 활성화가 전제
        payload["merge_pipelines_enabled"] = True
        payload["merge_trains_enabled"] = True
    return payload


def protected_branch_payload(name: str, spec: ProvisionSpec) -> dict:
    """force-push 금지 + 직접 push 금지(MR 경유), merge는 maintainer."""
    return {
        "name": name,
        "allow_force_push": spec.allow_force_push,
        "push_access_level": spec.push_access_level,
        "merge_access_level": spec.merge_access_level,
    }


# ── 설정/결과 구조 ────────────────────────────────────────────


@dataclass
class ProvisionSpec:
    max_file_size_mb: float = 5.0
    prevent_secrets: bool = True
    lfs_enabled: bool = True
    merge_method: str = "merge"          # merge | rebase_merge | ff
    require_pipeline_success: bool = True
    merge_trains: bool = False
    protected_branches: list[str] = field(default_factory=lambda: ["main"])
    allow_force_push: bool = False
    push_access_level: int = ACCESS_NO_ONE       # 직접 push 금지 → MR 경유
    merge_access_level: int = ACCESS_MAINTAINER


@dataclass
class ProvisionResult:
    action: str
    ok: bool
    status: int
    detail: str = ""


# ── HTTP 클라이언트 (urllib, 테스트 시 대체 가능) ────────────────


class GitLabClient:
    """GitLab REST API v4 최소 클라이언트 (stdlib urllib)."""

    def __init__(self, base_url: str, token: str, timeout: int = 30) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        url = self._base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("PRIVATE-TOKEN", self._token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"error": raw}
            return e.code, parsed
        except urllib.error.URLError as e:
            return 0, {"error": str(e)}


# ── 프로비저너 ────────────────────────────────────────────────


class GitLabProvisioner:
    """GitLab 프로젝트에 거버넌스 설정을 적용."""

    def __init__(self, client: GitLabClient, project: str) -> None:
        self._c = client
        self._project_enc = quote(str(project), safe="")

    def _base(self) -> str:
        return f"/api/v4/projects/{self._project_enc}"

    def apply(self, spec: ProvisionSpec, dry_run: bool = False) -> list[ProvisionResult]:
        results = [
            self._ensure_push_rule(spec, dry_run),
            self._set_project_settings(spec, dry_run),
        ]
        for branch in spec.protected_branches:
            results.append(self._protect_branch(branch, spec, dry_run))
        return results

    def _ensure_push_rule(self, spec: ProvisionSpec, dry_run: bool) -> ProvisionResult:
        path = self._base() + "/push_rule"
        payload = push_rule_payload(spec)
        if dry_run:
            return ProvisionResult(
                "push_rule", True, 0, f"(dry-run) upsert {path} {payload}",
            )
        get_status, _ = self._c.request("GET", path)
        method = "PUT" if get_status == 200 else "POST"
        status, data = self._c.request(method, path, payload)
        ok = status in (200, 201)
        return ProvisionResult(
            "push_rule", ok, status,
            f"{method} max_file_size={payload['max_file_size']}MB" if ok else str(data),
        )

    def _set_project_settings(self, spec: ProvisionSpec, dry_run: bool) -> ProvisionResult:
        path = self._base()
        payload = project_settings_payload(spec)
        if dry_run:
            return ProvisionResult(
                "project_settings", True, 0, f"(dry-run) PUT {path} {payload}",
            )
        status, data = self._c.request("PUT", path, payload)
        ok = status == 200
        return ProvisionResult(
            "project_settings", ok, status,
            f"lfs={payload['lfs_enabled']} merge_train={spec.merge_trains}" if ok else str(data),
        )

    def _protect_branch(self, name: str, spec: ProvisionSpec, dry_run: bool) -> ProvisionResult:
        base = self._base()
        payload = protected_branch_payload(name, spec)
        if dry_run:
            return ProvisionResult(
                f"protect:{name}", True, 0,
                f"(dry-run) POST {base}/protected_branches {payload}",
            )
        # 멱등성: 기존 보호 규칙 제거 후 재생성
        self._c.request("DELETE", f"{base}/protected_branches/{quote(name, safe='')}")
        status, data = self._c.request("POST", f"{base}/protected_branches", payload)
        ok = status in (200, 201)
        return ProvisionResult(
            f"protect:{name}", ok, status,
            "force-push 금지, 직접 push 금지(MR 경유)" if ok else str(data),
        )
