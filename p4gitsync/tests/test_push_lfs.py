from unittest.mock import MagicMock, patch


class TestLfsPushOrdering:
    def test_lfs_push_before_git_push(self):
        calls = []

        def track_subprocess(*args, **kwargs):
            cmd = args[0]
            calls.append(list(cmd))
            return MagicMock(returncode=0, stderr="")

        with patch("subprocess.run", side_effect=track_subprocess):
            from subprocess import run

            run(["git", "lfs", "push", "--all", "origin", "main"])
            run(["git", "push", "origin", "main"])

        assert "lfs" in calls[0]
        assert calls[1] == ["git", "push", "origin", "main"]

    def test_lfs_push_failure_blocks_git_push(self):
        call_count = 0

        def fail_lfs_push(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            cmd = args[0]
            if "lfs" in cmd:
                return MagicMock(returncode=1, stderr="LFS error")
            return MagicMock(returncode=0, stderr="")

        with patch("subprocess.run", side_effect=fail_lfs_push):
            from subprocess import run

            lfs_result = run(["git", "lfs", "push", "--all", "origin", "main"])
            if lfs_result.returncode != 0:
                pass
            else:
                run(["git", "push", "origin", "main"])

        assert call_count == 1
