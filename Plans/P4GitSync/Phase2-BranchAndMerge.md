# Phase 2: 다중 Stream — Branch/Merge 매핑

## 목표

- **단일 Git repository** 안에서 모든 P4 Stream을 branch로 동기화
- **Stream 분기점을 정확히 탐지**하여 올바른 commit에서 Git branch 생성
- Stream 간 integration을 Git merge commit으로 재현
- Stream 생성/삭제를 Git branch 생성/보존으로 반영
- 전환 후 `git log --graph --all`로 전체 분기/병합 히스토리가 자연스럽게 보여야 함

## 핵심 원칙

```
1. 모든 Stream은 하나의 Git repository 안에서 branch로 표현
   - 별도 repo 분리 금지 (merge 관계 표현 불가)
2. Stream 부모-자식 관계 = Git branch 분기점
   - "동기화 시점의 HEAD"가 아니라 "stream이 실제로 생성된 시점의 parent commit"에서 분기
3. 전역 CL 정렬 필수
   - Stream 생성 이벤트도 CL 타임라인에 정확히 삽입
```

## 기술 스택

| 영역 | 라이브러리 |
|------|-----------|
| P4 연동 | `p4python` (`run_changes`, `run_describe`, `run_filelog`, `run_streams` 등) |
| Git 연동 | `pygit2` (`Repository`, `create_commit`, `TreeBuilder` 등) + git CLI fallback |
| State DB | `sqlite3` |
| 타입 체계 | `dataclass`, `TypedDict`, `Protocol` |
| 비동기 | `asyncio` (`async def` / `await`) |

> **참고:** 본 문서의 p4 CLI bash 예시는 동작 원리 설명 용도이며, 실제 구현은 모두 `p4python` API를 사용한다.

## 전제 조건

- Phase 1 완료 (단일 stream 동기화 동작 확인)

## Step 2-1: 다중 Stream 동기화

### 작업 내용

설정에 여러 stream 매핑을 추가하고, 전역 CL 순서로 처리.

```toml
[[streams]]
p4_stream = "//ProjectSTAR/main"
git_branch = "main"

[[streams]]
p4_stream = "//ProjectSTAR/dev"
git_branch = "dev"

[[streams]]
p4_stream = "//ProjectSTAR/release-1"
git_branch = "release/1.0"
```

### 초기 히스토리 Import 시 제약사항

초기 히스토리 import에서 전역 CL 정렬이 정확하려면,
**모든 대상 stream을 사전 등록한 후** import를 시작해야 한다.

이유:
  - import 중 CL 5000을 처리하고 있을 때, CL 3000에서 생성된 stream을
    뒤늦게 발견하면 이미 지나간 분기점을 처리할 수 없다.
  - AutoDiscover는 incremental 동기화 모드에서만 활성화해야 한다.

절차:
  1. `p4python`의 `run_streams`로 전체 stream 목록 수집
  2. StreamPolicy 필터 적용 (include/exclude)
  3. 대상 stream 전체를 stream_mapping에 사전 등록
  4. 전체 히스토리 import 시작
  5. import 완료 후 AutoDiscover 활성화

### 전역 CL 정렬 (Stream 생성 이벤트 포함)

```
변경 전 (Phase 1): stream별 독립 폴링
변경 후 (Phase 2): 모든 stream의 CL + stream 생성 이벤트를 시간순 병합 처리

이유:
  1. merge commit의 parent를 올바르게 연결하려면
     source branch의 commit이 먼저 존재해야 함
  2. branch 분기점이 정확하려면
     stream 생성 이벤트가 올바른 위치에 삽입되어야 함
```

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, auto


class EventPriority(IntEnum):
    """동일 CL 번호 내 이벤트 정렬 우선순위.
    BranchCreate가 Changelist보다 먼저 처리되어야 한다.
    """
    BRANCH_CREATE = 0
    CHANGELIST = 1


@dataclass
class SyncEvent:
    cl: int
    stream: str

    @property
    def priority(self) -> int:
        raise NotImplementedError


@dataclass
class BranchCreateEvent(SyncEvent):
    parent_stream: str | None

    @property
    def priority(self) -> int:
        return EventPriority.BRANCH_CREATE


@dataclass
class ChangelistEvent(SyncEvent):

    @property
    def priority(self) -> int:
        return EventPriority.CHANGELIST


async def collect_and_process_events(
    config: SyncConfig,
    state_store: StateStore,
    p4_client: P4Client,
) -> None:
    """모든 stream에서 미동기화 CL 수집 후 전역 CL 순서로 처리."""

    pending: list[SyncEvent] = []

    for mapping in config.streams:
        last_cl = await state_store.get_last_synced_cl(mapping.p4_stream)

        if last_cl == 0:
            # 아직 동기화 안 된 stream -> 분기점 이벤트 삽입
            first_cl = await p4_client.get_first_changelist(mapping.p4_stream)
            pending.append(BranchCreateEvent(
                cl=first_cl - 1,  # first_cl 직전에 branch 생성
                stream=mapping.p4_stream,
                parent_stream=mapping.parent_stream,
            ))

        new_cls = await p4_client.get_changes_after(mapping.p4_stream, last_cl)
        pending.extend(
            ChangelistEvent(cl=cl, stream=mapping.p4_stream)
            for cl in new_cls
        )

    # CL 번호순 정렬 (= 시간순)
    # 동일 CL 번호일 경우 BranchCreateEvent가 ChangelistEvent보다 먼저 처리
    pending.sort(key=lambda evt: (evt.cl, evt.priority))

    # 순차 처리
    for evt in pending:
        if isinstance(evt, BranchCreateEvent):
            await create_branch_from_parent(evt)
        elif isinstance(evt, ChangelistEvent):
            await process_changelist(evt.cl, evt.stream)
```

> **정렬 안정성:** `pending.sort(key=lambda evt: (evt.cl, evt.priority))`로 1차 CL 번호, 2차 이벤트 타입 우선순위를 적용한다. `BranchCreateEvent`(priority=0)가 동일 CL의 `ChangelistEvent`(priority=1)보다 항상 먼저 처리되므로, branch가 생성된 뒤에 해당 CL의 commit이 진행된다.

### P4 Workspace 관리

Stream별 전용 workspace 필요.

```python
from __future__ import annotations

from typing import Protocol


class WorkspaceManager(Protocol):
    """Stream별 P4 workspace 관리."""

    async def get_or_create_workspace(self, stream: str) -> str:
        """stream에 대응하는 workspace를 조회하거나 생성한다.

        Returns:
            workspace 이름 (예: "p4gitsync-dev-abc123")
        """
        ...

    async def sync_workspace(self, workspace: str, changelist: int) -> None:
        """workspace를 특정 CL로 sync한다.

        내부적으로 p4python의 run_sync를 사용한다.
        """
        ...
```

### 완료 기준

- [ ] 3개 이상 stream이 각각 Git branch로 동기화
- [ ] CL이 전역 순서대로 commit됨

## Step 2-2: MergeAnalyzer 구현

### 작업 내용

Changelist의 integration 레코드를 분석하여 merge 관계를 파악.

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class IntegrationRecord:
    source_depot_path: str    # //dev/src/foo.py
    target_depot_path: str    # //main/src/foo.py
    source_revision: int
    source_stream: str        # //ProjectSTAR/dev (파싱)


@dataclass
class MergeInfo:
    has_integration: bool
    primary_source_stream: str | None = None      # 주 source stream
    source_changelist: int | None = None           # source의 최대 CL
    records: list[IntegrationRecord] = field(default_factory=list)  # 상세 레코드


class MergeAnalyzer(Protocol):
    """CL의 integration 정보를 분석하여 source stream(들)을 반환한다."""

    async def analyze(self, change_info: P4ChangeInfo) -> MergeInfo:
        """p4python의 run_describe / run_filelog 결과를 받아 분석한다."""
        ...
```

### Integration 정보 추출

실제 구현은 `p4python` API(`run_describe`, `run_filelog`, `run_integrated`)를 사용한다.
아래 bash 예시는 동작 원리를 설명하기 위한 참고 자료이다.

```bash
# 참고: p4 CLI 동작 원리 (실제 구현은 p4python API 사용)

# 방법 1: p4 describe 출력에서 파싱
p4 describe -s 12345
# ... //main/src/foo.py#5 integrate
# 이 정보만으로는 source를 알 수 없음

# 방법 2: p4 filelog로 상세 조회 (필수)
p4 filelog -m1 //main/src/foo.py#5
# //main/src/foo.py
#   #5 change 12345 integrate on 2026/03/06
#     ... copy from //dev/src/foo.py#12

# 방법 2-1: p4python 배치 호출 (integration 파일이 많은 CL에서 권장)
# run_filelog에 여러 파일 경로를 한번에 전달하여 조회
# 개별 호출 대비 네트워크 왕복을 절감하여 성능 개선

# 방법 3: p4 integrated (더 직접적)
p4 integrated //main/src/foo.py#5
# //main/src/foo.py#5 - copy from //dev/src/foo.py#12
```

```python
# p4python API를 사용한 integration 정보 추출 예시

async def extract_integration_records(
    p4: P4,
    change_info: P4ChangeInfo,
) -> list[IntegrationRecord]:
    """CL의 integrate action 파일에 대해 p4python으로 filelog 조회."""

    integrate_files = [
        f for f in change_info.files
        if f.action in ("integrate", "branch", "copy", "merge")
    ]

    if not integrate_files:
        return []

    # run_filelog에 여러 파일을 한번에 전달 (배치 조회)
    file_paths = [f.depot_path for f in integrate_files]
    filelog_results = p4.run_filelog("-m1", *file_paths)

    records: list[IntegrationRecord] = []
    for result in filelog_results:
        for revision in result.revisions:
            for integ in revision.integrations:
                source_stream = extract_stream_from_path(integ.file)
                records.append(IntegrationRecord(
                    source_depot_path=integ.file,
                    target_depot_path=result.depotFile,
                    source_revision=integ.erev,
                    source_stream=source_stream,
                ))

    return records
```

### Source Stream 결정 알고리즘

```
1. CL의 모든 integrate action 파일에 대해 p4python의 run_filelog 실행
   (여러 파일 경로를 한번에 전달하여 배치 조회)
2. source depot path에서 stream 경로 추출
   //dev/src/foo.py -> //ProjectSTAR/dev
3. source stream별로 파일 수 집계
4. 가장 많은 파일의 source = primary_source_stream
5. source CL 중 최대값 = source_changelist
```

### Obliterate된 Source CL 처리

```
Source CL 조회 실패 시 (obliterate 등):
  1. 해당 integration record를 건너뛰고 로그 경고
  2. 나머지 integration record로 source stream 결정
  3. 전체 integration record가 실패하면 일반 commit으로 fallback
```

### 완료 기준

- [ ] integration이 없는 CL -> `has_integration = False`
- [ ] 단일 source 통합 -> source stream/CL 정상 식별
- [ ] 다중 source 통합 -> primary source 결정

## Step 2-3: Merge Commit 생성

### 작업 내용

MergeAnalyzer 결과를 기반으로 Git merge commit 생성.

```python
async def process_changelists(
    sorted_changelists: list[int],
    p4_client: P4Client,
    merge_analyzer: MergeAnalyzer,
    state_store: StateStore,
    git_operator: GitOperator,
    branch: str,
    work_dir: Path,
) -> None:
    """SyncOrchestrator의 CL 처리 루프 (merge commit 생성 포함)."""

    for cl in sorted_changelists:
        change_info = await p4_client.describe(cl)
        merge_info = await merge_analyzer.analyze(change_info)

        if merge_info.has_integration:
            # source branch의 대응 commit SHA 조회
            source_sha = await state_store.get_commit_sha(
                merge_info.source_changelist
            )

            if source_sha is None:
                # source CL이 아직 동기화 안 됨
                # -> 순서 오류 또는 동기화 대상 외 stream
                # -> 일반 commit으로 fallback, 로그 경고
                logger.warning(
                    "Source CL %d not synced yet, falling back to normal commit",
                    merge_info.source_changelist,
                )
                commit_sha = await git_operator.create_commit(
                    branch, change_info, work_dir
                )
            else:
                target_head = await git_operator.get_branch_head(branch)
                commit_sha = await git_operator.create_merge_commit(
                    branch=branch,
                    parents=[target_head, source_sha],
                    metadata=change_info,
                    work_dir=work_dir,
                )
        else:
            commit_sha = await git_operator.create_commit(
                branch, change_info, work_dir
            )

        await state_store.record_commit(cl, commit_sha)
```

### Commit Message에 Integration 정보 기록

```
Merge from //ProjectSTAR/dev to //ProjectSTAR/main

Integrated changelists: 12300, 12305, 12310
Source stream: //ProjectSTAR/dev
Target stream: //ProjectSTAR/main

P4CL: 12345
```

### Evil Merge 감지

P4의 integration은 cherry-pick, partial merge, resolve 과정에서
Git의 3-way merge 결과와 다른 tree를 만들 수 있다.
이를 "evil merge"라 하며, 감지 시 commit message에 경고를 기록한다.

감지 방법:
  1. merge commit 생성 후, 두 parent에 대해 git merge-base를 계산
  2. 3-way merge 결과와 실제 tree를 비교 (선택적, 성능 비용 있음)
  3. 차이가 있으면 commit message에 "Note: This merge contains
     additional changes beyond the merge (evil merge)" 추가

이 한계는 전환 후 팀에 교육 필요:
  - git rebase 사용 자제, merge-only workflow 권장
  - GitHub/GitLab PR diff가 비직관적일 수 있음

### 완료 기준

- [ ] `git log --graph --oneline`에서 merge 관계 시각적 확인
- [ ] merge commit이 올바른 2개 parent를 가짐
- [ ] source SHA가 없을 때 graceful fallback

## Step 2-4: Stream 생성/삭제 감지 및 정확한 분기점 매핑

### 작업 내용

StreamWatcher — 주기적으로 stream 목록을 조회하여 변경 감지.

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class P4StreamInfo:
    stream: str           # //ProjectSTAR/dev
    type: str             # development, release, mainline
    parent: str | None    # //ProjectSTAR/main (mainline은 None)
    first_changelist: int  # 해당 stream 최초 CL


@dataclass
class StreamChanges:
    created: list[P4StreamInfo] = field(default_factory=list)
    deleted: list[P4StreamInfo] = field(default_factory=list)


class StreamWatcher(Protocol):
    """P4 stream 목록 변경 감지."""

    async def get_streams(self) -> list[P4StreamInfo]:
        """현재 P4 stream 목록을 조회한다.

        내부적으로 p4python의 run_streams를 사용한다.
        """
        ...

    async def detect_changes(self) -> StreamChanges:
        """이전 조회 대비 신규/삭제된 stream을 감지한다."""
        ...
```

아래 bash 예시는 동작 원리를 설명하기 위한 참고 자료이다.
실제 구현은 `p4python` API(`run_streams`, `run_stream`, `run_changes`)를 사용한다.

```bash
# 참고: p4 CLI 동작 원리 (실제 구현은 p4python API 사용)

# stream 목록 및 부모 관계 조회
p4 streams //ProjectSTAR/...
# Stream //ProjectSTAR/main mainline none 'Main branch'
# Stream //ProjectSTAR/dev development //ProjectSTAR/main 'Dev branch'

# stream 상세 정보 (부모 확인)
p4 stream -o //ProjectSTAR/dev
# Parent: //ProjectSTAR/main

# stream 최초 CL 확인
p4 changes -s submitted -r //ProjectSTAR/dev/...
# (oldest first, 첫 줄이 최초 CL)
```

```python
# p4python API를 사용한 stream 정보 조회 예시

async def get_stream_info(p4: P4, stream_path: str) -> P4StreamInfo:
    """p4python으로 stream 상세 정보를 조회한다."""

    # stream 부모/타입 조회
    stream_spec = p4.run_stream("-o", stream_path)[0]
    parent = stream_spec.get("Parent")
    stream_type = stream_spec.get("Type", "development")

    # mainline은 parent가 "none"
    if parent == "none":
        parent = None

    # 최초 CL 조회 (oldest first)
    changes = p4.run_changes("-s", "submitted", "-r", f"{stream_path}/...")
    first_cl = int(changes[0]["change"]) if changes else 0

    return P4StreamInfo(
        stream=stream_path,
        type=stream_type,
        parent=parent,
        first_changelist=first_cl,
    )
```

### Stream 생성 시 처리 — 정확한 분기점

```
핵심: "현재 parent HEAD"가 아니라 "stream이 실제로 분기된 시점의 parent commit"에서 branch 생성

알고리즘:
1. p4python의 run_stream으로 Parent 확인
2. p4python의 run_changes로 최초 CL (first_cl) 확인
3. parent stream에서 first_cl 직전까지 동기화된 commit SHA 조회
   -> parent_sha = state_store.get_last_commit_before(parent_stream, first_cl)
4. parent_sha에서 Git branch 생성
5. stream_mapping 테이블에 등록 (parent_stream, first_cl 기록)
6. P4 workspace 생성
7. first_cl부터 동기화 시작
```

```python
async def create_branch_from_parent(
    evt: BranchCreateEvent,
    state_store: StateStore,
    git_operator: GitOperator,
    logger: logging.Logger,
) -> None:
    """parent stream의 분기 시점 commit에서 Git branch를 생성한다."""

    # parent stream에서 분기 직전 commit 조회
    parent_sha = await state_store.get_last_commit_before(
        evt.parent_stream, evt.cl
    )

    if parent_sha is None:
        # parent stream이 아직 동기화 안 됨 -> 순서 오류
        raise RuntimeError(
            f"Parent stream {evt.parent_stream} has no commits "
            f"before CL {evt.cl}"
        )

    # Git branch 생성 (분기점에서)
    git_branch = stream_to_git_branch(evt.stream)
    await git_operator.create_branch(git_branch, parent_sha)

    # stream_mapping 등록
    await state_store.register_stream(StreamMapping(
        p4_stream=evt.stream,
        git_branch=git_branch,
        parent_stream=evt.parent_stream,
        first_changelist=evt.cl,
        last_synced_cl=0,
    ))

    logger.info(
        "Branch '%s' created at %s (parent: %s, before CL %d)",
        git_branch, parent_sha[:8], evt.parent_stream, evt.cl,
    )
```

### Mainline Stream 처리 (분기 없이 시작)

```
Mainline은 parent가 없으므로 분기점이 없다.
  -> 최초 CL을 initial commit으로 생성 (orphan branch)
  -> 이것이 repository의 root commit이 됨
```

### Stream 삭제 시 처리

```
1. stream_mapping.is_active = 0 으로 변경
2. Git branch는 삭제하지 않음 (히스토리 보존)
3. 폴링 대상에서 제외
4. 선택적: Git tag로 마킹 ("archived/stream-name")
```

### Workspace 관리 전략

Stream 수가 많아질 때를 대비한 Lazy Workspace 관리:
  - 활성 동기화 대상 stream에 대해서만 workspace 유지
  - p4 print 방식 사용 시 workspace 자체가 불필요할 수 있음
  - 비활성 stream(`is_active=0`)의 workspace는 일정 기간 후 정리

### 완료 기준

- [ ] 신규 stream -> **분기 시점의 parent commit**에서 Git branch 생성
- [ ] mainline -> orphan branch (initial commit)로 시작
- [ ] stream 삭제 -> 동기화 중단, branch 보존
- [ ] `git log --graph --all --oneline`에서 분기점이 정확히 표현됨

## Step 2-5: Stream 필터링 정책

### 설정

```toml
[stream_policy]
auto_discover = true
include_patterns = ["//ProjectSTAR/*"]
exclude_types = ["virtual"]
exclude_streams = ["//ProjectSTAR/sandbox-*"]
task_stream_policy = "ignore"
```

```python
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StreamPolicy:
    """Stream 필터링 정책 설정."""

    auto_discover: bool = True
    include_patterns: list[str] = field(default_factory=list)
    exclude_types: list[str] = field(default_factory=list)
    exclude_streams: list[str] = field(default_factory=list)
    task_stream_policy: str = "ignore"  # "ignore" | "include" | "archive"
```

### 동작

```
auto_discover = True:
  StreamWatcher가 신규 stream 감지 시 자동 등록
  exclude_types / exclude_streams에 매칭되면 제외

auto_discover = False:
  streams 설정에 명시된 stream만 동기화
```

### 완료 기준

- [ ] include/exclude 패턴 정상 동작
- [ ] virtual stream 자동 제외
- [ ] task stream 정책 적용
