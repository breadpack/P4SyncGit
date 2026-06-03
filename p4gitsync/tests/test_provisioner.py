"""provisioner 설정 생성기 테스트 — 생성물에 핵심 설정 토큰이 포함되는지."""

from p4gitsync.services import provisioner


class TestBootstrapSh:
    def test_contains_core_settings(self):
        out = provisioner.generate_bootstrap_sh("git@host:org/repo.git", "main")
        assert "git@host:org/repo.git" in out
        assert "--filter=blob:none" in out          # partial clone
        assert "scalar register" in out              # 성능 스택
        assert "sparse-checkout" in out              # 부분 체크아웃
        assert "lfs.fetchinclude" in out             # LFS 부분 fetch
        assert 'BRANCH="main"' in out

    def test_roles_present(self):
        out = provisioner.generate_bootstrap_sh("r", "main")
        for role in ("code)", "client)", "artist)", "full)"):
            assert role in out

    def test_bundle_uri_support(self):
        out = provisioner.generate_bootstrap_sh("r", "main")
        assert "BUNDLE_URI" in out
        assert "--bundle-uri=" in out


class TestBootstrapPs1:
    def test_contains_core_settings(self):
        out = provisioner.generate_bootstrap_ps1("git@host:org/repo.git", "develop")
        assert "--filter=blob:none" in out
        assert "core.longpaths true" in out          # Windows 긴 경로
        assert "sparse-checkout" in out
        assert '$Branch = "develop"' in out
        assert "BUNDLE_URI" in out
        assert "--bundle-uri=" in out


class TestPreReceiveHook:
    def test_threshold_and_logic(self):
        out = provisioner.generate_pre_receive_hook(max_bytes=1048576)
        assert "THRESHOLD=1048576" in out
        assert "git rev-list" in out
        assert "git cat-file -s" in out
        assert "exit $status" in out


class TestGitconfigSnippet:
    def test_platform_settings(self):
        out = provisioner.generate_gitconfig_snippet()
        assert "longpaths = true" in out
        assert "autocrlf = false" in out
        assert "ignorecase = false" in out
        assert "precomposeunicode = true" in out


class TestGitlabChecklist:
    def test_governance_items(self):
        out = provisioner.generate_gitlab_checklist(max_bytes=5 * 1024 * 1024)
        assert "Maximum file size" in out
        assert "5 MB" in out
        assert "Protected Branches" in out
        assert "LFS Object Storage" in out
        assert "Merge train" in out
