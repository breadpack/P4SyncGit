"""file_mode: P4 파일 타입 → Git 모드 매핑 및 change 항목 정규화 테스트."""

from p4gitsync.git.file_mode import (
    GIT_MODE_EXEC,
    GIT_MODE_FILE,
    GIT_MODE_SYMLINK,
    git_mode_from_p4_type,
    unpack_change,
)


class TestGitModeFromP4Type:
    def test_plain_text_and_binary(self):
        assert git_mode_from_p4_type("text") == GIT_MODE_FILE
        assert git_mode_from_p4_type("binary") == GIT_MODE_FILE
        assert git_mode_from_p4_type("unicode") == GIT_MODE_FILE
        assert git_mode_from_p4_type("utf16") == GIT_MODE_FILE

    def test_executable_base_type(self):
        # xtext / xbinary = 실행 비트
        assert git_mode_from_p4_type("xtext") == GIT_MODE_EXEC
        assert git_mode_from_p4_type("xbinary") == GIT_MODE_EXEC

    def test_executable_modifier(self):
        # base+x 형태
        assert git_mode_from_p4_type("binary+x") == GIT_MODE_EXEC
        assert git_mode_from_p4_type("text+lx") == GIT_MODE_EXEC

    def test_symlink(self):
        assert git_mode_from_p4_type("symlink") == GIT_MODE_SYMLINK

    def test_lockable_modifier_is_not_executable(self):
        # +l (lockable)은 실행과 무관
        assert git_mode_from_p4_type("text+l") == GIT_MODE_FILE
        assert git_mode_from_p4_type("binary+l") == GIT_MODE_FILE

    def test_case_insensitive_and_empty(self):
        assert git_mode_from_p4_type("XTEXT") == GIT_MODE_EXEC
        assert git_mode_from_p4_type("") == GIT_MODE_FILE


class TestUnpackChange:
    def test_two_tuple_defaults_to_file_mode(self):
        path, content, mode = unpack_change(("a.txt", b"hi"))
        assert (path, content, mode) == ("a.txt", b"hi", GIT_MODE_FILE)

    def test_three_tuple_preserves_mode(self):
        path, content, mode = unpack_change(("run.sh", b"#!/bin/sh", GIT_MODE_EXEC))
        assert mode == GIT_MODE_EXEC
