# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — p4gitsync 단일 실행 파일 빌드.

사용법:
  pip install pyinstaller
  cd p4gitsync
  pyinstaller p4gitsync.spec

결과: dist/p4gitsync.exe (Windows) 또는 dist/p4gitsync (Linux/macOS)

주의:
  - 빌드 환경에 p4python, pygit2가 설치되어 있어야 합니다.
  - 실행 환경에 git CLI가 설치되어 있어야 합니다 (push, gc 등에 사용).
"""

import importlib
import os
import sys
from pathlib import Path

block_cipher = None

# p4python 네이티브 라이브러리 수집
p4_binaries = []
try:
    import P4
    p4_dir = Path(P4.__file__).parent
    for f in p4_dir.glob("*.so*"):
        p4_binaries.append((str(f), "P4"))
    for f in p4_dir.glob("*.pyd"):
        p4_binaries.append((str(f), "P4"))
    for f in p4_dir.glob("*.dll"):
        p4_binaries.append((str(f), "P4"))
except ImportError:
    print("WARNING: p4python not found, binary may not work")

# pygit2 + libgit2 네이티브 라이브러리 수집
pygit2_binaries = []
try:
    import pygit2
    pygit2_dir = Path(pygit2.__file__).parent
    for f in pygit2_dir.glob("*.so*"):
        pygit2_binaries.append((str(f), "pygit2"))
    for f in pygit2_dir.glob("*.pyd"):
        pygit2_binaries.append((str(f), "pygit2"))
    for f in pygit2_dir.glob("*.dll"):
        pygit2_binaries.append((str(f), "pygit2"))
    # libgit2 공유 라이브러리
    for f in pygit2_dir.glob("libgit2*"):
        pygit2_binaries.append((str(f), "pygit2"))
    for f in pygit2_dir.glob("git2*"):
        pygit2_binaries.append((str(f), "pygit2"))
except ImportError:
    print("WARNING: pygit2 not found, binary may not work")

a = Analysis(
    ["src/p4gitsync/__main__.py"],
    pathex=["src"],
    binaries=p4_binaries + pygit2_binaries,
    datas=[],
    hiddenimports=[
        "p4gitsync",
        "p4gitsync.config",
        "p4gitsync.config.sync_config",
        "p4gitsync.config.lfs_config",
        "p4gitsync.config.logging_config",
        "p4gitsync.p4",
        "p4gitsync.p4.p4_client",
        "p4gitsync.p4.p4_submitter",
        "p4gitsync.p4.merge_analyzer",
        "p4gitsync.p4.workspace_manager",
        "p4gitsync.git",
        "p4gitsync.git.pygit2_git_operator",
        "p4gitsync.git.git_cli_operator",
        "p4gitsync.git.git_change_detector",
        "p4gitsync.git.fast_importer",
        "p4gitsync.services",
        "p4gitsync.services.sync_orchestrator",
        "p4gitsync.services.commit_builder",
        "p4gitsync.services.reverse_commit_builder",
        "p4gitsync.services.conflict_detector",
        "p4gitsync.services.user_mapper",
        "p4gitsync.services.initial_importer",
        "p4gitsync.services.multi_stream_sync",
        "p4gitsync.services.event_consumer",
        "p4gitsync.services.event_collector",
        "p4gitsync.services.changelist_poller",
        "p4gitsync.services.integrity_checker",
        "p4gitsync.services.circuit_breaker",
        "p4gitsync.services.recovery",
        "p4gitsync.services.cutover",
        "p4gitsync.state",
        "p4gitsync.state.state_store",
        "p4gitsync.api",
        "p4gitsync.api.api_server",
        "p4gitsync.notifications",
        "p4gitsync.notifications.notifier",
        "P4",
        "pygit2",
        "fastapi",
        "uvicorn",
        "slack_sdk",
        "redis",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="p4gitsync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
