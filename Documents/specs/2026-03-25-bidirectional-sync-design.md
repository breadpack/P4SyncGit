# 양방향 동기화 설계 사양서

**날짜**: 2026-03-25
**상태**: 승인됨

## 개요

P4GitSync에 Git → P4 역방향 동기화를 추가하여 마이그레이션 과도기 동안 양쪽에서 본격적인 개발이 가능하도록 한다.

## 요구사항

- 마이그레이션 과도기 병행 운영 (최종 목표는 P4 종료)
- Git에서도 본격적인 코드 개발 (branch, merge, PR)
- 실시간 양방향 동기화
- branch별 동기화 방향 설정 (`p4_to_git` / `git_to_p4` / `bidirectional`)
- 충돌 시 자동 중단 → Git에서 merge로 해결
- Git author ↔ P4 user 역매핑으로 submit

## 접근 방식

단일 프로세스(SyncOrchestrator) 확장. 기존 P4→Git 파이프라인에 Git→P4 파이프라인을 추가한다.

## 설정 확장

### stream_policy.sync_directions

```toml
[[stream_policy.sync_directions]]
stream = "//depot/main"
branch = "main"
direction = "bidirectional"

[[stream_policy.sync_directions]]
stream = "//depot/develop"
branch = "develop"
direction = "p4_to_git"
```

기본값: `"p4_to_git"` (기존 동작 유지)

### git 설정 추가

```toml
[git]
watch_remote = "origin"
reverse_sync_interval_seconds = 30
```

### p4 설정 추가

```toml
[p4]
submit_workspace = "p4sync-submit"
submit_as_user = true
```

## 루프 방지

### 마커 (Git trailer 형식)

P4→Git commit message:
```
원본 설명

P4CL: 12345
```

Git→P4 changelist description:
```
원본 설명

GitCommit: abc123def456
```

### 감지 로직

- Git commit에 `P4CL:` trailer → P4에서 온 commit → 역방향 스킵
- P4 CL에 `GitCommit:` 마커 → Git에서 온 CL → 순방향 스킵
- StateStore의 `sync_direction` 필드로 이중 확인

### 하위 호환

파싱 시 기존 `[P4CL: NNN]` 형식과 새 `P4CL: NNN` trailer 형식 모두 인식.

## 역방향 파이프라인

```
Git Remote → git fetch → GitChangeDetector → ReverseCommitBuilder → P4Submitter → StateStore
```

### GitChangeDetector

- 매 `reverse_sync_interval_seconds` 간격으로 `git fetch {watch_remote}`
- `git log {last_sha}..origin/{branch} --reverse`로 새 commit 수집
- `P4CL:` trailer 있는 commit 스킵
- `conflict/` prefix branch 삭제 감지 → 충돌 해결 판정

### ReverseCommitBuilder

- `git diff-tree`로 파일 변경 목록 추출 (add/edit/delete)
- 각 파일의 content를 bytes로 읽기
- Git author → P4 user 역매핑 (`user_mappings` 테이블)
- changelist description 생성 + `GitCommit: {sha}` trailer 삽입

### P4Submitter

- 별도 workspace(`submit_workspace`) 사용
- `submit_as_user=true` 시 대상 P4 user로 submit
- 파일별: add → `p4 add`, edit → `p4 edit`, delete → `p4 delete`
- `p4 submit` 실행
- StateStore에 `sync_direction='git_to_p4'` 기록

## 충돌 감지 및 처리

### 감지

양방향 동기화 사이클 시작 시:
1. P4 새 CL의 변경 파일 → `Set<P4Files>`
2. Git 새 commit의 변경 파일 → `Set<GitFiles>`
3. 교집합 비어있지 않으면 → 충돌

### 처리 흐름

1. 해당 branch 양방향 동기화 일시 중단
2. P4 변경사항으로 `conflict/{branch}/CL{number}` Git branch 생성 & push
3. Slack ERROR 알림 (충돌 파일, 작성자, 충돌 branch 이름)
4. 사용자가 Git에서 merge로 해결 후 충돌 branch 삭제
5. GitChangeDetector가 충돌 branch 삭제 감지 → 동기화 재개
6. merge 결과를 P4에 submit

### branch별 독립 상태

branch A가 충돌 중이어도 branch B는 정상 동기화 계속.

## SyncOrchestrator 확장

```python
while running:
    for branch in registered_branches:
        if branch.has_conflict:
            _check_conflict_resolved(branch)
            continue

        direction = branch.direction
        p4_changes = _collect_p4_changes(branch) if direction != 'git_to_p4' else []
        git_changes = _collect_git_changes(branch) if direction != 'p4_to_git' else []

        conflicts = _detect_conflicts(p4_changes, git_changes)
        if conflicts:
            _handle_conflicts(branch, conflicts)
        else:
            _sync_p4_to_git(p4_changes)
            _sync_git_to_p4(git_changes)

    sleep(interval)
```

## 신규 컴포넌트

| 컴포넌트 | 위치 | 역할 |
|----------|------|------|
| GitChangeDetector | `git/git_change_detector.py` | git fetch → 새 commit 감지, 충돌 branch 삭제 감지 |
| ReverseCommitBuilder | `services/reverse_commit_builder.py` | Git commit → P4 changelist 데이터 변환 |
| P4Submitter | `p4/p4_submitter.py` | P4 workspace에 파일 반영 + submit |
| ConflictDetector | `services/conflict_detector.py` | 양방향 변경 파일 교집합, 충돌 branch 생성 |

## 변경 컴포넌트

| 컴포넌트 | 변경 |
|----------|------|
| GitOperator | `fetch()`, `get_log_after()`, `get_commit_files()`, `delete_branch()` 추가 |
| P4Client | `create_changelist()`, `add()`, `edit()`, `delete()`, `submit()` 추가 |
| SyncOrchestrator | 양방향 루프, ConflictDetector 통합 |
| StateStore | `sync_direction` 컬럼, `conflict_state` 테이블 |
| CommitMetadata | trailer 형식 전환 (하위 호환) |
| SyncConfig / StreamPolicy | 방향 설정 추가 |
| SlackNotifier | 충돌 알림 |
| ApiServer | `GET /api/conflicts` |
