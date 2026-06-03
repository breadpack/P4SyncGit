"""github_provisioner 테스트 — payload 빌더 + 가짜 클라이언트로 호출 검증."""

import pytest

from p4gitsync.services.github_provisioner import (
    GitHubProvisioner,
    GitHubSpec,
    branch_ruleset_payload,
    push_ruleset_payload,
    repo_settings_payload,
)


def _rule_types(payload: dict) -> set[str]:
    return {r["type"] for r in payload["rules"]}


class TestPayloadBuilders:
    def test_push_ruleset_max_file_size(self):
        p = push_ruleset_payload(GitHubSpec(max_file_size_mb=5.0))
        assert p["target"] == "push"
        assert p["enforcement"] == "active"
        rule = p["rules"][0]
        assert rule["type"] == "max_file_size"
        assert rule["parameters"]["max_file_size"] == 5

    def test_push_ruleset_clamped_to_github_limit(self):
        # GitHub 하드 한도 100MB 로 클램프, 0 이하는 1 로
        assert push_ruleset_payload(GitHubSpec(max_file_size_mb=999))[
            "rules"][0]["parameters"]["max_file_size"] == 100
        assert push_ruleset_payload(GitHubSpec(max_file_size_mb=0))[
            "rules"][0]["parameters"]["max_file_size"] == 1

    def test_branch_ruleset_blocks_force_and_deletion_and_requires_pr(self):
        p = branch_ruleset_payload("main", GitHubSpec())
        assert p["target"] == "branch"
        assert p["conditions"]["ref_name"]["include"] == ["refs/heads/main"]
        types = _rule_types(p)
        assert "non_fast_forward" in types   # force-push 금지 (F3)
        assert "deletion" in types
        assert "pull_request" in types

    def test_branch_ruleset_pr_can_be_disabled(self):
        p = branch_ruleset_payload("main", GitHubSpec(require_pull_request=False))
        assert "pull_request" not in _rule_types(p)

    def test_branch_ruleset_merge_queue_optional(self):
        off = branch_ruleset_payload("main", GitHubSpec(merge_queue=False))
        assert "merge_queue" not in _rule_types(off)
        on = branch_ruleset_payload(
            "main", GitHubSpec(merge_queue=True, merge_method="squash"),
        )
        mq = next(r for r in on["rules"] if r["type"] == "merge_queue")
        assert mq["parameters"]["merge_method"] == "SQUASH"

    def test_branch_ruleset_status_checks(self):
        p = branch_ruleset_payload(
            "main",
            GitHubSpec(require_status_checks=True, status_check_contexts=["ci/build"]),
        )
        rsc = next(r for r in p["rules"] if r["type"] == "required_status_checks")
        assert rsc["parameters"]["required_status_checks"] == [{"context": "ci/build"}]

    def test_branch_ruleset_name_sanitized_for_patterns(self):
        p = branch_ruleset_payload("release/*", GitHubSpec())
        assert p["conditions"]["ref_name"]["include"] == ["refs/heads/release/*"]
        # 이름에는 슬래시/별표가 안전 토큰으로
        assert "/" not in p["name"] and "*" not in p["name"]

    def test_repo_settings_single_merge_method(self):
        p = repo_settings_payload(GitHubSpec(merge_method="squash"))
        assert p["allow_squash_merge"] is True
        assert p["allow_merge_commit"] is False
        assert p["allow_rebase_merge"] is False
        assert p["delete_branch_on_merge"] is True


class _FakeClient:
    """요청을 기록하는 가짜 GitHub 클라이언트."""

    def __init__(self, responses=None):
        self.calls = []
        self._responses = responses or {}

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return self._responses.get((method, path), (200, {}))


class TestProvisionerConstruction:
    def test_invalid_repository_raises(self):
        with pytest.raises(ValueError):
            GitHubProvisioner(_FakeClient(), "no-slash")
        with pytest.raises(ValueError):
            GitHubProvisioner(_FakeClient(), "owner/")


class TestProvisionerApply:
    def test_dry_run_makes_no_real_calls(self):
        client = _FakeClient()
        prov = GitHubProvisioner(client, "octo/repo")
        results = prov.apply(GitHubSpec(protected_branches=["main"]), dry_run=True)
        assert client.calls == []                       # dry-run은 호출 없음
        assert all(r.ok for r in results)
        assert any("dry-run" in r.detail for r in results)

    def test_repo_path_in_url(self):
        client = _FakeClient()
        prov = GitHubProvisioner(client, "octo-org/my-repo")
        prov.apply(GitHubSpec(protected_branches=[]), dry_run=False)
        assert any("/repos/octo-org/my-repo" in path for _, path, _ in client.calls)

    def test_repo_settings_patched(self):
        client = _FakeClient()
        prov = GitHubProvisioner(client, "octo/repo")
        prov.apply(GitHubSpec(protected_branches=[]), dry_run=False)
        methods = [(m, p) for m, p, _ in client.calls]
        assert ("PATCH", "/repos/octo/repo") in methods

    def test_push_ruleset_creates_when_absent(self):
        # GET rulesets → 빈 목록 → POST 로 생성
        client = _FakeClient(responses={
            ("GET", "/repos/octo/repo/rulesets?per_page=100"): (200, []),
        })
        prov = GitHubProvisioner(client, "octo/repo")
        prov.apply(GitHubSpec(protected_branches=[]), dry_run=False)
        methods = [(m, p) for m, p, _ in client.calls]
        assert ("POST", "/repos/octo/repo/rulesets") in methods

    def test_push_ruleset_updates_when_present(self):
        # GET rulesets 에 동일 이름 존재 → PUT /rulesets/{id}
        client = _FakeClient(responses={
            ("GET", "/repos/octo/repo/rulesets?per_page=100"): (
                200, [{"id": 77, "name": "p4gitsync-large-file-guard"}],
            ),
        })
        prov = GitHubProvisioner(client, "octo/repo")
        prov.apply(GitHubSpec(protected_branches=[]), dry_run=False)
        methods = [(m, p) for m, p, _ in client.calls]
        assert ("PUT", "/repos/octo/repo/rulesets/77") in methods

    def test_ruleset_list_requests_per_page_100(self):
        # 멱등 upsert: ruleset 조회는 per_page=100 으로 한 페이지에 최대한 담아
        # 기존 항목이 페이지네이션으로 누락돼 중복 생성되는 것을 막는다.
        client = _FakeClient(responses={
            ("GET", "/repos/octo/repo/rulesets?per_page=100"): (200, []),
        })
        prov = GitHubProvisioner(client, "octo/repo")
        prov.apply(GitHubSpec(protected_branches=["main"]), dry_run=False)
        get_paths = [p for m, p, _ in client.calls if m == "GET"]
        assert get_paths  # ruleset 조회가 실제로 발생
        assert all("per_page=100" in p for p in get_paths)
        assert all(p == "/repos/octo/repo/rulesets?per_page=100" for p in get_paths)

    def test_branch_protect_creates_ruleset(self):
        client = _FakeClient(responses={
            ("GET", "/repos/octo/repo/rulesets?per_page=100"): (200, []),
        })
        prov = GitHubProvisioner(client, "octo/repo")
        results = prov.apply(GitHubSpec(protected_branches=["main"]), dry_run=False)
        # branch ruleset 생성 호출이 있어야 한다(POST /rulesets, branch target body)
        branch_posts = [
            body for m, p, body in client.calls
            if m == "POST" and p == "/repos/octo/repo/rulesets"
            and body and body.get("target") == "branch"
        ]
        assert branch_posts
        assert any(r.action == "protect:main" and r.ok for r in results)

    def test_lfs_note_present(self):
        client = _FakeClient()
        prov = GitHubProvisioner(client, "octo/repo")
        results = prov.apply(GitHubSpec(protected_branches=[]), dry_run=False)
        lfs = next(r for r in results if r.action == "lfs")
        assert lfs.ok is True
        assert "LFS" in lfs.detail

    def test_failure_reported(self):
        client = _FakeClient(responses={
            ("PATCH", "/repos/octo/repo"): (403, {"message": "forbidden"}),
        })
        prov = GitHubProvisioner(client, "octo/repo")
        results = prov.apply(GitHubSpec(protected_branches=[]), dry_run=False)
        settings = next(r for r in results if r.action == "repo_settings")
        assert settings.ok is False
        assert settings.status == 403
