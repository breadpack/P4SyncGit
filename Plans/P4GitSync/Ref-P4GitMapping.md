# P4 ↔ Git 개념 매핑 및 한계

## 개념 매핑 테이블

| P4 개념 | Git 대응 | 매핑 정확도 | 비고 |
|---------|---------|------------|------|
| Depot | Repository | 높음 | |
| Stream | Branch | 높음 | |
| Changelist | Commit | 높음 | 1:1 매핑 |
| CL 번호 | Commit SHA | - | commit message에 CL 번호 기록 |
| Submit | Commit + Push | 높음 | |
| P4 User | Git Author | 높음 | 매핑 테이블 필요 |
| Stream 생성 | Branch 생성 | 높음 | parent stream → source branch |
| Copy-up (promote) | Merge (child→parent) | 중간 | 아래 상세 설명 |
| Copy-down | Merge (parent→child) | 중간 | |
| Cherry-pick integrate | 근사 매핑만 가능 | 낮음 | 아래 상세 설명 |
| Shelve | Stash | - | 동기화 대상 아님 |
| Label | Tag | 높음 | 선택적 동기화 |
| Virtual stream | 매핑 불가 | - | 물리적 실체 없음 |
| Task stream | Short-lived branch | 중간 | 수명 관리 필요 |
| Import path | 매핑 불가 | - | Git에 대응 개념 없음 |
| Stream path exclude | .gitignore와 유사하나 다름 | 낮음 | |
| Obliterate | 대응 없음 | - | Git에서 히스토리 삭제 불가, 해당 CL/파일 건너뛰기 |
| Stream path remap | 경로 변환 필요 | 낮음 | depot path ≠ workspace path 시 매핑 로직 필요 |
| P4 Timestamp | Git AuthorDate | 높음 | P4 서버 timezone 설정 필요 (ServerTimezone) |

## Changelist → Commit 변환 규칙

### 메타데이터 매핑

```
P4 Changelist:
  Number:      12345
  User:        kwonsanggoo
  Timestamp:   2026/03/06 10:30:00
  Description: "Fix player respawn logic\n\nDetail explanation..."
  Workspace:   ksg-dev

Git Commit:
  Author:      Sanggoo Kwon <kwonsanggoo@example.com>   ← user_mapping 조회
  AuthorDate:  2026-03-06T10:30:00+09:00                ← P4 timestamp
  Committer:   P4GitSync <p4sync@service>               ← 서비스 계정 고정
  CommitDate:  (실제 동기화 시점)
  Message:     "Fix player respawn logic\n\nDetail explanation...\n\nP4CL: 12345"
```

### Timezone 처리

P4 timestamp는 서버 timezone 기준이다.
Git의 AuthorDate에 올바른 timezone을 설정하려면 P4 서버의 timezone을 알아야 한다.

```
설정에서 P4 서버 timezone 지정:
  "P4": {
    "ServerTimezone": "Asia/Seoul"   // IANA timezone
  }

또는 p4 info에서 자동 감지:
  p4 info | grep "Server date"
  # Server date: 2026/03/06 10:30:00 +0900 KST
```

### File Action 매핑

| P4 Action | Git 동작 |
|-----------|---------|
| add | 파일 추가 (new file) |
| edit | 파일 수정 |
| delete | 파일 삭제 |
| move/add + move/delete | 파일 이동 (rename) |
| branch | 파일 추가 (integration source 정보는 commit에 기록) |
| integrate | merge 분석 대상 (아래 참조) |

## Integration → Merge 변환 상세

### 판별 알고리즘

```
CL을 분석할 때:

1. p4 describe -s {CL} 로 파일 목록과 action 확인
2. integrate/branch action이 있는 파일들의 source를 p4 filelog로 조회
3. source stream 집합을 구함

판별:
  source가 없음 (integrate 없음)
    → 일반 commit (parent 1개)

  source가 1개 stream
    → merge commit (parent 2개: target HEAD + source의 최근 commit)

  source가 2개 이상 stream
    → octopus merge (parent N개) 또는 primary source만 merge로 기록
```

### Merge Commit 생성 (git commit-tree)

```bash
# Tree: P4에서 가져온 파일 상태 그대로
git add -A
TREE=$(git write-tree)

# Parent 결정
PARENT1=$(git rev-parse main)              # target branch HEAD
PARENT2=$(state-db lookup //dev → SHA)     # source branch의 최근 동기화 commit

# Merge commit 생성
COMMIT=$(GIT_AUTHOR_NAME="Sanggoo Kwon" \
  GIT_AUTHOR_EMAIL="kwonsanggoo@example.com" \
  GIT_AUTHOR_DATE="2026-03-06T10:30:00+09:00" \
  git commit-tree $TREE \
    -p $PARENT1 \
    -p $PARENT2 \
    -m "Merge from dev to main

P4CL: 12345
P4Integration: //dev → //main")

# Branch ref 업데이트
git update-ref refs/heads/main $COMMIT
```

### pygit2 동등 코드

```python
import pygit2
from datetime import datetime, timezone

repo = pygit2.Repository("/path/to/repo")

# Tree 구성: P4에서 가져온 파일 변경사항을 반영
tb = repo.TreeBuilder(repo.head.peel().tree)
# ... P4 파일 변경사항 반영 (tb.insert / tb.remove) ...
tree_oid = tb.write()

# Parent 결정
parent1 = repo.branches["main"].peel().id   # target branch HEAD
parent2 = pygit2.Oid(hex=source_commit_sha)  # source branch의 최근 동기화 commit

# Signature 생성
author = pygit2.Signature(git_name, git_email, int(cl_timestamp.timestamp()), cl_tz_offset)
committer = pygit2.Signature("P4GitSync", "p4sync@service", int(datetime.now(timezone.utc).timestamp()), 0)

# Merge commit 생성 (parent 직접 지정)
merge_commit_oid = repo.create_commit(
    None,                                      # ref 업데이트 없이 commit만 생성
    author,
    committer,
    f"Merge from dev to main\n\nP4CL: {cl}",
    tree_oid,
    [parent1, parent2],                        # parent 목록
)

# Branch ref 업데이트
repo.references.create("refs/heads/main", merge_commit_oid, force=True)
```

## Cherry-pick Merge 처리

### 문제

```
//dev에 CL 100~110 존재
//main으로 CL 103, 107만 integrate

P4: 파일 단위로 source 기록 → 정상
Git: merge parent를 어디로?
  - dev HEAD (CL 110 commit) → 100~110 전부 merge한 것으로 보임
  - dev의 CL 107 commit      → 100~107이 merge한 것으로 보임 (103만은 표현 불가)
```

### 채택한 전략

```
2nd parent = source branch에서 integrate된 changelist 중 가장 큰 CL에 대응하는 commit

이유:
  - Git merge는 "이 시점까지의 변경을 통합했다"는 의미
  - 가장 큰 CL의 commit을 parent로 잡으면 git log 그래프가 자연스러움
  - Tree(실제 파일 내용)는 P4 결과 그대로이므로 파일 정확성은 100%

한계:
  - git log main..dev 에서 "아직 merge 안 된 commit" 목록이 부정확할 수 있음
  - 이것은 P4→Git 구조적 한계이며, 어떤 도구도 해결 불가
```

## Evil Merge 한계

### 문제

```
P4의 integration resolve 과정에서 개발자가 수동으로 내용을 수정하면,
Git 관점에서 "3-way merge 결과와 다른 tree"를 가진 merge commit이 생성된다.
이를 "evil merge"라 한다.

영향:
  - git diff가 예상과 다를 수 있음
  - git rebase, git cherry-pick에서 비정상적 conflict 발생 가능
  - GitHub/GitLab PR diff 표시가 비직관적
```

### 완화 방안

```
1. 감지 시 commit message에 경고 기록
2. 전환 후 merge-only workflow 권장 (rebase 사용 자제)
3. 구조적 한계임을 팀에 사전 교육
```

## Stream 타입별 처리

| Stream 타입 | 처리 방식 |
|------------|----------|
| mainline | `main` branch, 항상 동기화 |
| development | feature/dev branch, 항상 동기화 |
| release | release/* branch, 항상 동기화 |
| virtual | **동기화 제외** — 물리적 파일 없음 |
| task | 설정에 따라 동기화 여부 결정 (기본: 제외) |

## Stream Path Remap 처리

P4 Stream에서 `remap` 지시자를 사용하면 depot path와 workspace 파일 경로가 달라진다.

```
예시:
  Stream spec에 remap 지시자:
    remap src/legacy/... src/...

  결과:
    depot: //stream/src/legacy/foo.cs
    workspace: {root}/src/foo.cs

동기화 영향:
  - p4 print의 depot path와 Git의 파일 경로가 불일치
  - Stream spec의 remap 규칙을 파싱하여 경로 변환 필요

현재 정책:
  - scope 외로 분류 (README.md 비목표 참조)
  - 대상 stream에 remap이 사용되는지 사전 조사 필요
  - remap이 있는 경우 경로 변환 로직을 CommitBuilder에 추가
```

## Stream 분기점 탐지 알고리즘

Git으로 완전 전환하려면 Stream의 부모-자식 관계를 branch 분기점으로 정확히 재현해야 한다.
단순히 "동기화 시점의 parent HEAD"에서 branch를 생성하면 분기 지점이 틀어진다.

### P4 Stream 계층 예시

```
//ProjectSTAR/main (mainline)
  ├── //ProjectSTAR/dev (development, parent=main)
  │     └── //ProjectSTAR/feature-x (development, parent=dev)
  └── //ProjectSTAR/release-1 (release, parent=main)
```

### 분기점 결정 방법

```
Stream이 생성된 시점 = 해당 stream에 최초로 submit된 CL의 직전 상태

알고리즘:
1. p4 stream -o {stream} → Parent 필드 확인
2. p4 changes -m1 {stream}/... → 해당 stream의 최초 CL 번호 (firstCL) 확인
3. parent stream에서 firstCL 직전까지 동기화된 commit SHA 조회
   → parentCommitSha = stateStore.GetLastCommitBefore(parentStream, firstCL)
4. 이 parentCommitSha에서 Git branch 생성

예시:
  //dev의 최초 CL = 1050
  //main에서 CL 1049까지 동기화된 commit = abc123
  → git branch dev abc123
  → CL 1050부터 dev branch에 commit 시작
```

### p4 명령어 상세

```bash
# 1. Stream 부모 확인
p4 stream -o //ProjectSTAR/dev
# Parent: //ProjectSTAR/main

# 2. Stream 최초 CL 확인 (역순 조회 후 마지막 = 최초)
p4 changes -s submitted //ProjectSTAR/dev/...
# 마지막 줄이 최초 CL

# 또는 정순 조회 (가능한 경우)
p4 changes -s submitted -r //ProjectSTAR/dev/... | head -1
# -r 플래그: 역순 (oldest first)

# 3. parent stream에서 분기 직전 CL 확인
# firstCL 직전에 parent stream에 submit된 CL
p4 changes -s submitted -m1 -e 1 -E {firstCL - 1} //ProjectSTAR/main/...
```

### 전역 CL 정렬에서 Stream 생성 이벤트 처리

```
초기 히스토리 import 또는 실시간 동기화 시:

전역 CL 순서로 처리하되, Stream 생성 이벤트를 끼워넣어야 한다.

예시 (시간순):
  CL 1000: //main submit      → main branch에 commit
  CL 1001: //main submit      → main branch에 commit
  [CL 1002 이전: //dev stream 생성 감지]
    → main의 CL 1001 commit에서 dev branch 생성
  CL 1002: //dev submit       → dev branch에 commit
  CL 1003: //main submit      → main branch에 commit

처리 순서:
  1. 모든 활성 stream의 CL 목록을 수집
  2. 각 stream의 최초 CL(firstCL)을 확인
  3. firstCL 직전에 "branch 생성" 이벤트를 삽입
  4. 전체를 CL 번호순으로 정렬
  5. 순차 처리
```

### Stream Reparent 처리

```
P4에서 stream의 parent를 변경하는 경우:
  - Git에는 branch의 "parent"라는 메타데이터가 없음
  - 이후 merge commit의 방향이 자연스럽게 새 관계를 반영
  - 별도 처리 불필요 (허용 가능한 한계)
```

### 단일 Repository 원칙

```
모든 Stream은 하나의 Git repository 안에서 branch로 표현한다.

이유:
  - Stream 간 merge가 Git merge commit으로 자연스럽게 표현됨
  - git log --graph --all 로 전체 분기/병합 히스토리 시각화 가능
  - 전환 후 개발자가 git checkout으로 stream 간 이동 가능
  - 별도 repo로 분리하면 merge 관계 표현이 불가능

구조:
  refs/heads/main         ← //ProjectSTAR/main
  refs/heads/dev          ← //ProjectSTAR/dev
  refs/heads/feature-x    ← //ProjectSTAR/feature-x
  refs/heads/release/1.0  ← //ProjectSTAR/release-1
```

## P4 Obliterate 처리

P4에서 `p4 obliterate`로 삭제된 파일/CL은 히스토리에서 완전히 제거된다.
동기화 시 다음 상황이 발생할 수 있다.

### 영향

```
1. CL 번호 불연속: obliterate된 CL은 p4 changes에서 조회 불가
   → CL 번호 건너뜀은 정상으로 처리 (에러 아님)

2. 파일 조회 실패: 특정 CL의 파일이 obliterate되어 p4 print 실패
   → 해당 파일 건너뛰고 경고 기록
   → commit에는 나머지 파일만 포함

3. Integration record 참조 실패: source CL이 obliterate됨
   → merge parent를 찾을 수 없음
   → 일반 commit으로 fallback, commit message에 원본 정보 기록
```

### 대응 전략

```
- p4 print 실패 시: 파일 건너뛰기 + sync_errors에 경고 기록
- source CL 미존재 시: 일반 commit으로 fallback + 로그 경고
- 사전 탐지: import 전 obliterate 이력 조사 (p4 obliterate -n)
```

## 구조적 한계 요약

| 한계 | 원인 | 영향 | 완화 방안 |
|------|------|------|----------|
| 부분 merge가 정확히 표현 안 됨 | Git merge는 commit 단위 | `git log branch1..branch2` 부정확 | commit message에 실제 integrate된 CL 목록 기록 |
| 파일별 revision 번호 없음 | Git에 개념 없음 | P4 revision 추적 불가 | commit message에 P4CL 태그 |
| Virtual stream 매핑 불가 | 물리적 실체 없음 | 해당 stream 동기화 불가 | 동기화 대상에서 제외 |
| Import path 무시 | Git에 대응 없음 | 일부 파일 누락 가능 | import된 파일은 원본 stream에서 동기화 |
| Stream path exclude | branch별 파일셋 차이 | 동기화 시 exclude 파일 포함 | .gitignore 또는 필터 스크립트로 근사 |
| Evil merge | P4 resolve 시 수동 수정 | git diff/rebase 비정상 | commit message에 경고, merge-only workflow 권장 |
| Obliterate된 CL/파일 | P4에서 완전 삭제 | 해당 CL/파일 동기화 불가 | 건너뛰기 + 경고 기록, commit message에 정보 보존 |
| Stream path remap | depot/workspace 경로 불일치 | Git 파일 경로 오류 가능 | scope 외, 사전 조사 후 필요 시 변환 로직 추가 |
