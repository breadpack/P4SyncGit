"""CutoverManager 배선 테스트 — verify_cutover 전략 호출 + 실패 전파(가짜 주입)."""

from p4gitsync.config.sync_config import (
    AppConfig,
    CutoverConfig,
    GitConfig,
    P4Config,
    StateConfig,
)
from p4gitsync.services.cutover import CutoverManager, CutoverPhase
from p4gitsync.services.integrity_checker import IntegrityResult


class _FakeState:
    def __init__(self):
        self.pushed = []

    def get_last_synced_cl(self, stream):
        return 0

    def get_pending_pushes(self):
        return []

    def get_all_registered_streams(self):
        return []

    def update_push_status(self, cl, stream, status):
        pass

    def close(self):
        pass


class _FakeP4:
    def get_changes_after(self, stream, cl):
        return []          # freeze 확인 통과 + 잔여 CL 0

    def disconnect(self):
        pass


class _FakeGit:
    def __init__(self):
        self.pushes = []

    def push(self, branch):
        self.pushes.append(branch)


class _RecChecker:
    """verify_cutover 호출 인자를 기록하고 미리 지정한 결과를 반환."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def verify_cutover(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def verify_sample(self, n):
        return self.result


def _config(cutover: CutoverConfig | None = None) -> AppConfig:
    return AppConfig(
        p4=P4Config(stream="//depot/main"),
        git=GitConfig(repo_path="repo", default_branch="main"),
        state=StateConfig(db_path=":memory:"),
        cutover=cutover or CutoverConfig(),
    )


def _wire(mgr: CutoverManager, checker, git=None, state=None, p4=None):
    git = git or _FakeGit()
    state = state or _FakeState()
    p4 = p4 or _FakeP4()

    def fake_init():
        mgr._state_store = state
        mgr._p4_client = p4
        mgr._git_operator = git
        mgr._notifier = None
        mgr._integrity_checker = checker

    mgr._initialize = fake_init
    mgr._cleanup = lambda: None
    return git, state, p4


class TestConfigWiring:
    def test_cutover_defaults_present(self):
        cfg = _config()
        assert cfg.cutover.verify_mode == "smart"
        assert cfg.cutover.verify_workers == 4
        assert cfg.cutover.verify_sample_count == 1000

    def test_from_dict_reads_cutover_section(self):
        cfg = AppConfig.from_dict({
            "p4": {"stream": "//d/m"},
            "git": {"repo_path": "r"},
            "state": {"db_path": "s"},
            "cutover": {"verify_mode": "full", "verify_workers": 8},
        })
        assert cfg.cutover.verify_mode == "full"
        assert cfg.cutover.verify_workers == 8


class TestExecuteIntegrityWiring:
    def test_verify_cutover_called_with_config_strategy(self):
        cfg = _config(CutoverConfig(
            verify_mode="smart", verify_sample_count=500,
            verify_large_threshold_bytes=1234,
        ))
        checker = _RecChecker(IntegrityResult(
            passed=True, checked_files=10, total_files=10, strategy="smart",
        ))
        mgr = CutoverManager(cfg)
        git, _, _ = _wire(mgr, checker)

        result = mgr.execute()
        assert result.success is True
        assert result.phase == CutoverPhase.COMPLETED
        # config 전략이 그대로 전달됐는지
        assert checker.calls == [{
            "mode": "smart",
            "sample_count": 500,
            "large_threshold_bytes": 1234,
        }]
        assert "main" in git.pushes

    def test_integrity_failure_aborts_at_verify_phase(self):
        cfg = _config()
        checker = _RecChecker(IntegrityResult(
            passed=False, checked_files=10, mismatched_files=["a.bin", "b.bin"],
            total_files=10, strategy="smart",
        ))
        mgr = CutoverManager(cfg)
        git, _, _ = _wire(mgr, checker)

        result = mgr.execute()
        assert result.success is False
        assert result.phase == CutoverPhase.INTEGRITY_VERIFY
        assert "2개 파일 불일치" in result.message
        # 검증 실패 → push 로 진행하지 않아야 한다
        assert git.pushes == []
