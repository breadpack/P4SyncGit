"""GitHub 저장소 거버넌스를 API로 자동 적용 (GitLab provisioner의 GitHub 짝).

GitHub는 GitLab의 "push rule / protected branch"와 모델이 달라 **Repository
Rulesets API**로 매핑한다(2022-11-28 API 버전):

- Push ruleset    : `max_file_size` 규칙 = LFS 아닌 대용량 차단(F1). GitHub의
  네이티브 대용량 파일 게이트. (org 소유 repo + 유료 플랜/퍼블릭에서 가용)
- Branch ruleset  : `non_fast_forward`(force-push 금지) + `deletion`(삭제 금지)
  + `pull_request`(PR 경유 강제) (F3). 선택적으로 `required_status_checks`,
  `merge_queue`(= GitLab merge train 대응).
- Repo settings   : 허용 merge 방식 + merge 후 브랜치 삭제.

LFS는 GitHub에서 객체 push 시 자동 사용되며 켜고 끄는 REST 토글이 없다(결과에
안내만 남긴다). 토큰은 config에 저장하지 않고 호출부(env/CLI)에서 주입한다.
HTTP 계층은 GitHubClient로 분리해 테스트 시 가짜 클라이언트로 대체한다.

API 노트:
- GitHub Cloud base_url = ``https://api.github.com``.
- GitHub Enterprise Server = ``https://<host>/api/v3``.
- Push ruleset(`target=push`)는 **조직 소유 저장소**에서만 적용된다(개인 repo 미지원).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import quote

# GitHub 하드 한도(참고): 파일 push 100MB, 권장 경고 50MB.
_GITHUB_MAX_FILE_MB = 100


# ── 순수 payload 빌더 (단위 테스트 대상) ──────────────────────────


def _gh_merge_method(merge_method: str) -> str:
    """spec merge_method → GitHub merge_queue/룰셋 표기(대문자)."""
    return {"merge": "MERGE", "squash": "SQUASH", "rebase": "REBASE"}.get(
        merge_method, "MERGE",
    )


def _safe_ref_name(branch: str) -> str:
    """브랜치 패턴을 ruleset 이름에 쓸 수 있는 토큰으로 정규화."""
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")
    return token or "branch"


def _ref_pattern(branch: str) -> str:
    """브랜치명을 ruleset ref 조건 형식으로(`refs/heads/...`)."""
    if branch.startswith("refs/"):
        return branch
    if branch in ("~DEFAULT_BRANCH", "~ALL"):
        return branch
    return f"refs/heads/{branch}"


def push_ruleset_payload(spec: GitHubSpec) -> dict:
    """push ruleset 페이로드. max_file_size 단위는 MB(GitHub 규약, 1~100)."""
    mb = max(1, min(int(spec.max_file_size_mb), _GITHUB_MAX_FILE_MB))
    return {
        "name": f"{spec.ruleset_prefix}-large-file-guard",
        "target": "push",
        "enforcement": spec.enforcement,
        "rules": [
            {"type": "max_file_size", "parameters": {"max_file_size": mb}},
        ],
    }


def branch_ruleset_payload(branch: str, spec: GitHubSpec) -> dict:
    """force-push/삭제 금지 + (선택) PR 강제/status check/merge queue."""
    rules: list[dict] = [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
    ]
    if spec.require_pull_request:
        rules.append({
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": spec.required_approving_review_count,
                "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_review_thread_resolution": False,
            },
        })
    if spec.require_status_checks and spec.status_check_contexts:
        rules.append({
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [
                    {"context": c} for c in spec.status_check_contexts
                ],
            },
        })
    if spec.merge_queue:
        rules.append({
            "type": "merge_queue",
            "parameters": {
                "merge_method": _gh_merge_method(spec.merge_method),
                "grouping_strategy": "ALLGREEN",
                "check_response_timeout_minutes": 60,
                "min_entries_to_merge": 1,
                "max_entries_to_merge": 5,
                "max_entries_to_build": 5,
                "min_entries_to_merge_wait_minutes": 5,
            },
        })
    return {
        "name": f"{spec.ruleset_prefix}-protect-{_safe_ref_name(branch)}",
        "target": "branch",
        "enforcement": spec.enforcement,
        "conditions": {
            "ref_name": {"include": [_ref_pattern(branch)], "exclude": []},
        },
        "rules": rules,
    }


def repo_settings_payload(spec: GitHubSpec) -> dict:
    """선택한 merge 방식만 허용 + merge 후 브랜치 자동 삭제."""
    return {
        "allow_merge_commit": spec.merge_method == "merge",
        "allow_squash_merge": spec.merge_method == "squash",
        "allow_rebase_merge": spec.merge_method == "rebase",
        "delete_branch_on_merge": spec.delete_branch_on_merge,
    }


# ── 설정/결과 구조 ────────────────────────────────────────────


@dataclass
class GitHubSpec:
    max_file_size_mb: float = 5.0
    protected_branches: list[str] = field(default_factory=lambda: ["main"])
    require_pull_request: bool = True
    required_approving_review_count: int = 1
    require_status_checks: bool = False
    status_check_contexts: list[str] = field(default_factory=list)
    merge_queue: bool = False               # = GitLab merge train 대응
    merge_method: str = "merge"             # merge | squash | rebase
    delete_branch_on_merge: bool = True
    enforcement: str = "active"             # active | evaluate | disabled
    ruleset_prefix: str = "p4gitsync"


@dataclass
class ProvisionResult:
    action: str
    ok: bool
    status: int
    detail: str = ""


# ── HTTP 클라이언트 (urllib, 테스트 시 대체 가능) ────────────────


class GitHubClient:
    """GitHub REST API 최소 클라이언트 (stdlib urllib)."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        timeout: int = 30,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None):
        url = self._base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
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


class GitHubProvisioner:
    """GitHub 저장소에 거버넌스 설정을 적용."""

    def __init__(self, client: GitHubClient, repository: str) -> None:
        if "/" not in repository:
            raise ValueError(
                f"repository 는 'owner/repo' 형식이어야 합니다: {repository!r}",
            )
        owner, repo = repository.split("/", 1)
        if not owner or not repo:
            raise ValueError(
                f"repository 는 'owner/repo' 형식이어야 합니다: {repository!r}",
            )
        self._c = client
        self._owner = owner
        self._repo = repo

    def _repo_base(self) -> str:
        return f"/repos/{quote(self._owner, safe='')}/{quote(self._repo, safe='')}"

    def apply(self, spec: GitHubSpec, dry_run: bool = False) -> list[ProvisionResult]:
        results = [
            self._set_repo_settings(spec, dry_run),
            self._ensure_push_ruleset(spec, dry_run),
        ]
        for branch in spec.protected_branches:
            results.append(self._protect_branch(branch, spec, dry_run))
        results.append(self._lfs_note())
        return results

    def _find_ruleset_id(self, name: str) -> int | None:
        """이름이 일치하는 기존 ruleset id(멱등 upsert용)."""
        status, data = self._c.request("GET", self._repo_base() + "/rulesets")
        if status == 200 and isinstance(data, list):
            for rs in data:
                if isinstance(rs, dict) and rs.get("name") == name:
                    return rs.get("id")
        return None

    def _set_repo_settings(self, spec: GitHubSpec, dry_run: bool) -> ProvisionResult:
        path = self._repo_base()
        payload = repo_settings_payload(spec)
        if dry_run:
            return ProvisionResult(
                "repo_settings", True, 0, f"(dry-run) PATCH {path} {payload}",
            )
        status, data = self._c.request("PATCH", path, payload)
        ok = status == 200
        return ProvisionResult(
            "repo_settings", ok, status,
            f"merge={spec.merge_method} delete_branch_on_merge={spec.delete_branch_on_merge}"
            if ok else str(data),
        )

    def _ensure_push_ruleset(self, spec: GitHubSpec, dry_run: bool) -> ProvisionResult:
        base = self._repo_base() + "/rulesets"
        payload = push_ruleset_payload(spec)
        mb = payload["rules"][0]["parameters"]["max_file_size"]
        if dry_run:
            return ProvisionResult(
                "push_ruleset", True, 0,
                f"(dry-run) upsert {base} max_file_size={mb}MB",
            )
        rid = self._find_ruleset_id(payload["name"])
        if rid is not None:
            status, data = self._c.request("PUT", f"{base}/{rid}", payload)
            method = "PUT"
        else:
            status, data = self._c.request("POST", base, payload)
            method = "POST"
        ok = status in (200, 201)
        return ProvisionResult(
            "push_ruleset", ok, status,
            f"{method} max_file_size={mb}MB (非-LFS 대용량 차단)" if ok else str(data),
        )

    def _protect_branch(
        self, branch: str, spec: GitHubSpec, dry_run: bool,
    ) -> ProvisionResult:
        base = self._repo_base() + "/rulesets"
        payload = branch_ruleset_payload(branch, spec)
        if dry_run:
            return ProvisionResult(
                f"protect:{branch}", True, 0,
                f"(dry-run) upsert {base} {payload['name']}",
            )
        rid = self._find_ruleset_id(payload["name"])
        if rid is not None:
            status, data = self._c.request("PUT", f"{base}/{rid}", payload)
        else:
            status, data = self._c.request("POST", base, payload)
        ok = status in (200, 201)
        return ProvisionResult(
            f"protect:{branch}", ok, status,
            "force-push/삭제 금지, PR 경유 강제" if ok else str(data),
        )

    def _lfs_note(self) -> ProvisionResult:
        """GitHub LFS는 API 토글이 없음 — 안내만 남긴다."""
        return ProvisionResult(
            "lfs", True, 0,
            "GitHub LFS는 객체 push 시 자동 사용(API 토글 없음). "
            "Storage/대역폭 쿼터 및 외부 LFS 백엔드 정책 확인 권장.",
        )
