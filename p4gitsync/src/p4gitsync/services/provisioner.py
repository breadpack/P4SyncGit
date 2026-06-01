"""마이그레이션된 git 저장소를 팀이 안전하게 쓰도록 "적절한 설정"을 자동 생성.

대용량 depot→git 시나리오에서 콘텐츠 변환만으로는 부족하고, 팀이 실제로
쓰는 단계(clone/CI/서버 거버넌스)의 설정이 필요하다. 이 모듈은 그 설정들을
파일로 생성한다:

- bootstrap-clone.{sh,ps1} : 역할별(partial clone + scalar + cone sparse +
  LFS 부분 fetch) clone 부트스트랩. 거대 repo를 통째로 받지 않게 한다.
- pre-receive               : 서버측 게이트. LFS 아닌 대용량 blob push 차단.
- recommended.gitconfig     : longpaths/autocrlf/ignorecase/precomposeunicode 등
  플랫폼 공통 권장 설정.
- GITLAB-SETUP.md           : push rule/protected branch/LFS object storage/
  merge train 등 서버 거버넌스 체크리스트(수동 적용용).

모든 함수는 순수 문자열 생성기라 단위 테스트가 쉽다.
"""

from __future__ import annotations

# 역할 → (sparse-checkout 경로, LFS fetchinclude, LFS fetchexclude)
# 프로젝트 경로는 placeholder. provision 시 --code-paths/--asset-paths로 덮어쓴다.
_DEFAULT_ROLES = {
    "code": (["CODE"], "CODE/**", "Art/**,Ui/**"),
    "client": (["CODE", "Art/Common"], "CODE/**", ""),
    "artist": (["Art"], "Art/**", "CODE/**"),
    "full": ([], "*", ""),
}


def _render_role_cases_sh(roles: dict[str, tuple[list[str], str, str]]) -> str:
    lines = []
    for role, (sparse, inc, exc) in roles.items():
        sparse_str = " ".join(f'"{p}"' for p in sparse)
        lines.append(f'  {role})')
        lines.append(f'    SPARSE=({sparse_str}); LFS_INCLUDE="{inc}"; LFS_EXCLUDE="{exc}" ;;')
    return "\n".join(lines)


def _render_role_cases_ps1(roles: dict[str, tuple[list[str], str, str]]) -> str:
    lines = []
    for role, (sparse, inc, exc) in roles.items():
        sparse_str = ", ".join(f"'{p}'" for p in sparse)
        lines.append(f"    '{role}' {{ $Sparse = @({sparse_str}); $LfsInclude = '{inc}'; $LfsExclude = '{exc}' }}")
    return "\n".join(lines)


def generate_bootstrap_sh(
    remote_url: str,
    default_branch: str = "main",
    roles: dict[str, tuple[list[str], str, str]] | None = None,
) -> str:
    """역할별 clone 부트스트랩 (bash/zsh, Linux·macOS)."""
    roles = roles or _DEFAULT_ROLES
    role_names = " | ".join(roles)
    return f"""#!/usr/bin/env bash
# p4gitsync provision — 역할별 git clone 부트스트랩
# 거대 repo를 통째로 받지 않도록 partial clone + sparse-checkout + LFS 부분 fetch.
# 사용: ./bootstrap-clone.sh <role> [dest-dir]
#   role: {role_names}
set -euo pipefail

REMOTE="{remote_url}"
BRANCH="{default_branch}"
ROLE="${{1:-code}}"
DEST="${{2:-repo}}"

# ── 역할별 sparse 경로 / LFS include·exclude (프로젝트에 맞게 수정) ──
case "$ROLE" in
{_render_role_cases_sh(roles)}
  *) echo "unknown role: $ROLE ({role_names})"; exit 1 ;;
esac

# blobless partial clone (객체 lazy fetch)
git clone --filter=blob:none --no-checkout "$REMOTE" "$DEST"
cd "$DEST"

# 대형 repo 성능 스택 (Git 2.38+ 내장 scalar: FSMonitor/commit-graph/sparse-index 등)
scalar register . 2>/dev/null || git maintenance start

# 필요한 경로만 워킹트리에 펼침
git sparse-checkout init --cone
if [ "${{#SPARSE[@]}}" -gt 0 ]; then
  git sparse-checkout set "${{SPARSE[@]}}"
fi

# LFS는 역할 작업영역만 받음 (checkout 시 전체 LFS smudge 방지)
if [ -n "$LFS_INCLUDE" ]; then git config lfs.fetchinclude "$LFS_INCLUDE"; fi
if [ -n "$LFS_EXCLUDE" ]; then git config lfs.fetchexclude "$LFS_EXCLUDE"; fi

git checkout "$BRANCH"
echo "완료: role=$ROLE dest=$DEST (sparse=${{SPARSE[*]:-<all>}})"
"""


def generate_bootstrap_ps1(
    remote_url: str,
    default_branch: str = "main",
    roles: dict[str, tuple[list[str], str, str]] | None = None,
) -> str:
    """역할별 clone 부트스트랩 (PowerShell, Windows)."""
    roles = roles or _DEFAULT_ROLES
    role_names = " | ".join(roles)
    return f"""# p4gitsync provision — 역할별 git clone 부트스트랩 (PowerShell)
# 사용: .\\bootstrap-clone.ps1 -Role code -Dest repo
param(
  [string]$Role = "code",
  [string]$Dest = "repo"
)
$ErrorActionPreference = "Stop"

$Remote = "{remote_url}"
$Branch = "{default_branch}"

switch ($Role) {{
{_render_role_cases_ps1(roles)}
  default {{ Write-Error "unknown role: $Role ({role_names})"; exit 1 }}
}}

git clone --filter=blob:none --no-checkout $Remote $Dest
Set-Location $Dest

# Windows 긴 경로(Unity 깊은 에셋 경로) 대응
git config core.longpaths true

# 대형 repo 성능 스택
try {{ scalar register . }} catch {{ git maintenance start }}

git sparse-checkout init --cone
if ($Sparse.Count -gt 0) {{ git sparse-checkout set @Sparse }}

if ($LfsInclude) {{ git config lfs.fetchinclude $LfsInclude }}
if ($LfsExclude) {{ git config lfs.fetchexclude $LfsExclude }}

git checkout $Branch
Write-Host "완료: role=$Role dest=$Dest"
"""


def generate_pre_receive_hook(max_bytes: int = 5 * 1024 * 1024) -> str:
    """서버측 pre-receive 훅 — LFS 아닌 대용량 blob push 차단(F1 게이트).

    LFS 포인터는 수백 bytes라 통과하고, 추적 누락된 raw 바이너리만 거부된다.
    """
    return f"""#!/usr/bin/env bash
# p4gitsync provision — 대용량 non-LFS blob push 차단 게이트
# GitLab: 프로젝트/그룹 server hook 또는 gitaly custom_hooks/pre-receive 로 설치.
THRESHOLD={max_bytes}
ZERO="0000000000000000000000000000000000000000"
status=0

while read -r old new ref; do
  [ "$new" = "$ZERO" ] && continue
  if [ "$old" = "$ZERO" ]; then
    range="$new"
  else
    range="$old..$new"
  fi
  for obj in $(git rev-list --objects $range 2>/dev/null | awk '{{print $1}}'); do
    [ "$(git cat-file -t "$obj" 2>/dev/null)" = "blob" ] || continue
    size=$(git cat-file -s "$obj" 2>/dev/null || echo 0)
    if [ "$size" -gt "$THRESHOLD" ]; then
      echo "거부: blob $obj ($size bytes > $THRESHOLD) — LFS로 추적해야 합니다 ($ref)" >&2
      status=1
    fi
  done
done

exit $status
"""


def generate_gitconfig_snippet() -> str:
    """플랫폼 공통 권장 git config (개발자/에이전트 머신용)."""
    return """# p4gitsync provision — 권장 git config
# 적용: git config --global include.path <이 파일 절대경로>
#   또는 시스템 gitconfig(MDM 배포)로 강제.
[core]
\tlongpaths = true
\tautocrlf = false
\tignorecase = false
\tprecomposeunicode = true
[lfs]
\tpruneoffsetdays = 7
[fetch]
\tparallel = 0
"""


def generate_gitlab_checklist(max_bytes: int = 5 * 1024 * 1024) -> str:
    """서버 거버넌스(불가우회) 설정 체크리스트 — GitLab에 수동 적용."""
    max_mb = max_bytes / (1024 * 1024)
    return f"""# GitLab 서버 거버넌스 체크리스트 (p4gitsync provision)

대용량 repo의 사고를 서버에서 **불가우회로** 막는 설정. 클라이언트가 어떻든
잘못된 push를 서버가 거부하게 한다.

## 1. Push Rules (Premium/Ultimate)
- [ ] **Maximum file size**: `{max_mb:.0f} MB` — LFS 포인터는 통과, 추적 누락 raw 바이너리 거부 (F1).
- [ ] **Prevent pushing secret files**: 활성화.

## 2. Protected Branches
- [ ] `main`/`release/*`: **force-push 금지**, push는 MR 경유만 (F3).
- [ ] Merge 권한 = Maintainer.

## 3. pre-receive 서버 훅
- [ ] 동봉한 `pre-receive`를 그룹/프로젝트 server hook 또는 Gitaly
      `custom_hooks/pre-receive`에 설치 (Push Rules 미가용 시 대안/보강).

## 4. LFS Object Storage
- [ ] LFS 백엔드를 **만료 정책 없는** 오브젝트 스토리지(S3/R2 등)로 지정.
      (자동삭제 버킷 사용 금지 — 객체 소멸 시 과거 커밋 영구 손상 F2)
- [ ] versioning/복제로 백업, `git lfs fsck` 정례화.

## 5. 대형 repo 성능
- [ ] Gitaly **pack-objects cache** 활성화.
- [ ] (가용 시) **Bundle URI**로 초기 clone offload.
- [ ] CI는 sparse-checkout + `GIT_LFS_SKIP_SMUDGE=1` 후 필요 경로만 `git lfs pull`.

## 6. MR 거버넌스
- [ ] **Merge train** 활성화(직렬화로 broken main 방지).
- [ ] `CODEOWNERS`로 경로별 승인자 지정.
"""
