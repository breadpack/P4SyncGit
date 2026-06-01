"""gitlab_provisioner 테스트 — payload 빌더 + 가짜 클라이언트로 호출 검증."""

from p4gitsync.services.gitlab_provisioner import (
    ACCESS_MAINTAINER,
    ACCESS_NO_ONE,
    GitLabProvisioner,
    ProvisionSpec,
    project_settings_payload,
    protected_branch_payload,
    push_rule_payload,
)


class TestPayloadBuilders:
    def test_push_rule(self):
        p = push_rule_payload(ProvisionSpec(max_file_size_mb=5.0))
        assert p == {"max_file_size": 5, "prevent_secrets": True}

    def test_project_settings_without_merge_train(self):
        p = project_settings_payload(ProvisionSpec(merge_trains=False))
        assert p["lfs_enabled"] is True
        assert p["only_allow_merge_if_pipeline_succeeds"] is True
        assert "merge_trains_enabled" not in p

    def test_project_settings_with_merge_train(self):
        p = project_settings_payload(ProvisionSpec(merge_trains=True))
        assert p["merge_trains_enabled"] is True
        assert p["merge_pipelines_enabled"] is True

    def test_protected_branch_forbids_force_and_direct_push(self):
        p = protected_branch_payload("main", ProvisionSpec())
        assert p["name"] == "main"
        assert p["allow_force_push"] is False
        assert p["push_access_level"] == ACCESS_NO_ONE
        assert p["merge_access_level"] == ACCESS_MAINTAINER


class _FakeClient:
    """요청을 기록하는 가짜 GitLab 클라이언트."""

    def __init__(self, responses=None):
        self.calls = []
        self._responses = responses or {}

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        # push_rule GET 은 기본 404(미존재) → POST 경로
        return self._responses.get((method, path), (200, {}))


class TestProvisionerApply:
    def test_dry_run_makes_no_real_calls(self):
        client = _FakeClient()
        prov = GitLabProvisioner(client, "org/repo")
        results = prov.apply(ProvisionSpec(protected_branches=["main"]), dry_run=True)
        assert client.calls == []                      # dry-run은 호출 없음
        assert all(r.ok for r in results)
        assert any("dry-run" in r.detail for r in results)

    def test_project_path_url_encoded(self):
        client = _FakeClient()
        prov = GitLabProvisioner(client, "group/sub/proj")
        prov.apply(ProvisionSpec(protected_branches=[]), dry_run=False)
        # 경로가 URL 인코딩되어 /projects/group%2Fsub%2Fproj 로
        assert any("group%2Fsub%2Fproj" in path for _, path, _ in client.calls)

    def test_push_rule_creates_when_absent(self):
        # GET push_rule → 404 → POST 로 생성
        client = _FakeClient(responses={
            ("GET", "/api/v4/projects/org%2Frepo/push_rule"): (404, {}),
        })
        prov = GitLabProvisioner(client, "org/repo")
        prov.apply(ProvisionSpec(protected_branches=[]), dry_run=False)
        methods = [(m, p) for m, p, _ in client.calls]
        assert ("GET", "/api/v4/projects/org%2Frepo/push_rule") in methods
        assert ("POST", "/api/v4/projects/org%2Frepo/push_rule") in methods

    def test_push_rule_updates_when_present(self):
        client = _FakeClient(responses={
            ("GET", "/api/v4/projects/org%2Frepo/push_rule"): (200, {"id": 1}),
        })
        prov = GitLabProvisioner(client, "org/repo")
        prov.apply(ProvisionSpec(protected_branches=[]), dry_run=False)
        methods = [(m, p) for m, p, _ in client.calls]
        assert ("PUT", "/api/v4/projects/org%2Frepo/push_rule") in methods

    def test_protect_branch_unprotects_then_protects(self):
        client = _FakeClient()
        prov = GitLabProvisioner(client, "org/repo")
        prov.apply(ProvisionSpec(protected_branches=["main"]), dry_run=False)
        methods = [(m, p) for m, p, _ in client.calls]
        assert ("DELETE", "/api/v4/projects/org%2Frepo/protected_branches/main") in methods
        assert ("POST", "/api/v4/projects/org%2Frepo/protected_branches") in methods

    def test_failure_reported(self):
        client = _FakeClient(responses={
            ("PUT", "/api/v4/projects/org%2Frepo"): (403, {"message": "forbidden"}),
        })
        prov = GitLabProvisioner(client, "org/repo")
        results = prov.apply(ProvisionSpec(protected_branches=[]), dry_run=False)
        settings = next(r for r in results if r.action == "project_settings")
        assert settings.ok is False
        assert settings.status == 403
