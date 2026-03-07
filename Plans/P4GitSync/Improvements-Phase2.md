# Phase 2 개선 작업 — 다중 Stream, Branch/Merge 구현

현재 완성도: ~20% → 목표: 100%

## 개요

Phase 2의 데이터 모델(stream_registry, merge commit API, get_last_commit_before)은 준비되어 있으나, 핵심 비즈니스 로직이 전무하다. MergeAnalyzer, 다중 stream orchestration, 전역 CL 정렬, Stream 감지/필터링을 구현해야 한다.

**전제 조건**: Phase 1 개선 작업(Improvements-Phase1.md) 완료

---

## 1. 다중 Stream Orchestration 구현

### 현상

`SyncOrchestrator`가 단일 stream만 처리한다. `stream_registry` 테이블과 `StreamMapping` 모델은 존재하지만 다중 stream 폴링/정렬 로직이 없다.

### 구현 내용

**전역 CL 정렬 기반 이벤트 수집 및 처리:**

```python
class SyncEvent:
    cl: int
    stream: str
    priority: int  # BranchCreate=0, Changelist=1

class BranchCreateEvent(SyncEvent): ...
class ChangelistEvent(SyncEvent): ...
```

- 모든 등록된 stream에서 미동기화 CL 수집
- 아직 동기화되지 않은 stream의 분기점 이벤트 삽입
- `(cl, priority)` 기준 전역 정렬 후 순차 처리
- BranchCreateEvent가 동일 CL의 ChangelistEvent보다 먼저 처리

### 초기 히스토리 Import 제약

- 모든 대상 stream을 사전 등록한 후 import 시작
- AutoDiscover는 incremental 동기화 모드에서만 활성화

### 신규 파일

- `services/sync_event.py` — SyncEvent, BranchCreateEvent, ChangelistEvent
- `services/event_collector.py` — 전역 이벤트 수집 및 정렬

### 수정 파일

- `services/sync_orchestrator.py` — 다중 stream 루프로 확장

---

## 2. MergeAnalyzer 구현

### 현상

P4 integration record 분석 클래스가 존재하지 않는다. `p4_client.run_filelog()`는 이미 배치 조회를 지원한다.

### 구현 내용

계획서 Step 2-2에 정의된 대로:

```python
@dataclass
class IntegrationRecord:
    source_depot_path: str
    target_depot_path: str
    source_revision: int
    source_stream: str

@dataclass
class MergeInfo:
    has_integration: bool
    primary_source_stream: str | None = None
    source_changelist: int | None = None
    records: list[IntegrationRecord] = field(default_factory=list)

class MergeAnalyzer(Protocol):
    def analyze(self, change_info: P4ChangeInfo) -> MergeInfo: ...
```

**Source Stream 결정 알고리즘:**
1. CL의 integrate/branch/copy/merge action 파일에 대해 `run_filelog` 배치 조회
2. source depot path에서 stream 경로 추출
3. source stream별 파일 수 집계 → 최다 = primary_source_stream
4. source CL 중 최대값 = source_changelist

**Obliterate 대응:**
- source CL 조회 실패 시 해당 record 건너뛰기 + 로그 경고
- 전체 실패 시 일반 commit으로 fallback

### 신규 파일

- `p4/merge_analyzer.py` — IntegrationRecord, MergeInfo, MergeAnalyzer 구현

### 수정 파일

- `p4/p4_client.py` — `run_integrated()` 메서드 추가 (필요 시)

---

## 3. Merge Commit 생성 로직 연결

### 현상

`GitOperator.create_merge_commit()`, `Pygit2GitOperator.create_merge_commit()`, `GitCliOperator.create_merge_commit()`, `FastImporter.add_merge_commit()` 모두 구현됨. 그러나 호출하는 비즈니스 로직이 없다.

### 구현 내용

`SyncOrchestrator._process_changelist()` 내에서:

```python
merge_info = self._merge_analyzer.analyze(change_info)

if merge_info.has_integration:
    source_sha = self._state_store.get_commit_sha(
        merge_info.source_changelist
    )
    if source_sha:
        target_head = self._git_operator.get_head_sha(branch)
        commit_sha = self._git_operator.create_merge_commit(
            branch, [target_head, source_sha], metadata, work_dir
        )
    else:
        # source 미동기화 → 일반 commit fallback + 경고
        commit_sha = self._git_operator.create_commit(...)
else:
    commit_sha = self._git_operator.create_commit(...)
```

**Commit Message에 Integration 정보 기록:**
```
Merge from //ProjectSTAR/dev to //ProjectSTAR/main

Integrated changelists: 12300, 12305, 12310
Source stream: //ProjectSTAR/dev
Target stream: //ProjectSTAR/main

P4CL: 12345
```

**Evil Merge 감지** (선택적):
- merge commit 생성 후 3-way merge 결과와 실제 tree 비교
- 차이 시 commit message에 경고 추가

### 수정 파일

- `services/sync_orchestrator.py`
- `services/commit_builder.py` — merge 메시지 포맷팅
- `git/commit_metadata.py` — merge용 메시지 포맷 추가

---

## 4. Stream 생성/삭제 감지 (StreamWatcher)

### 현상

`state_store.register_stream`, `get_stream_mapping`, `get_last_commit_before` 메서드와 `p4_client.get_streams`는 존재하지만, 감지 루프 및 자동 분기점 매핑 로직이 없다.

### 구현 내용

**StreamWatcher — 주기적 stream 목록 변경 감지:**

```python
@dataclass
class P4StreamInfo:
    stream: str           # //ProjectSTAR/dev
    type: str             # development, release, mainline
    parent: str | None
    first_changelist: int

@dataclass
class StreamChanges:
    created: list[P4StreamInfo]
    deleted: list[P4StreamInfo]

class StreamWatcher:
    async def detect_changes(self) -> StreamChanges: ...
```

**Stream 생성 시 — 정확한 분기점:**
1. `run_stream`으로 Parent 확인
2. `run_changes`로 최초 CL 확인
3. `state_store.get_last_commit_before(parent_stream, first_cl)` → parent_sha
4. `git_operator.create_branch(git_branch, parent_sha)`
5. `state_store.register_stream(...)` 등록

**Mainline Stream:**
- parent 없음 → orphan branch (initial commit)

**Stream 삭제 시:**
- `stream_registry.is_active = 0` 변경
- Git branch 삭제하지 않음 (히스토리 보존)
- 폴링 대상에서 제외

### GitOperator 확장

```python
class GitOperator(Protocol):
    # 추가 메서드
    def create_branch(self, branch: str, start_sha: str) -> None: ...
```

### 신규 파일

- `services/stream_watcher.py` — P4StreamInfo, StreamChanges, StreamWatcher

### 수정 파일

- `git/git_operator.py` — `create_branch` 메서드 추가
- `git/pygit2_git_operator.py` — 구현
- `git/git_cli_operator.py` — 구현
- `services/sync_orchestrator.py` — StreamWatcher 통합

---

## 5. Stream 필터링 정책

### 현상

포함/제외 패턴 기반 필터링이 없다.

### 구현 내용

설정:
```toml
[stream_policy]
auto_discover = true
include_patterns = ["//ProjectSTAR/*"]
exclude_types = ["virtual"]
exclude_streams = ["//ProjectSTAR/sandbox-*"]
task_stream_policy = "ignore"
```

```python
@dataclass
class StreamPolicy:
    auto_discover: bool = True
    include_patterns: list[str] = field(default_factory=list)
    exclude_types: list[str] = field(default_factory=list)
    exclude_streams: list[str] = field(default_factory=list)
    task_stream_policy: str = "ignore"

    def should_include(self, stream_info: P4StreamInfo) -> bool: ...
```

- `auto_discover = True`: StreamWatcher가 신규 stream 감지 시 자동 등록 (exclude 매칭 제외)
- `auto_discover = False`: 설정에 명시된 stream만 동기화
- `fnmatch` 패턴 매칭으로 include/exclude 적용

### 수정 파일

- `config/sync_config.py` — `StreamPolicy` dataclass 추가
- `services/stream_watcher.py` — 필터링 적용

---

## 6. P4 Workspace 관리

### 현상

Stream별 전용 workspace 관리 로직이 없다.

### 구현 내용

```python
class WorkspaceManager:
    async def get_or_create_workspace(self, stream: str) -> str: ...
    async def sync_workspace(self, workspace: str, changelist: int) -> None: ...
```

- 활성 동기화 대상 stream에 대해서만 workspace 유지
- p4 print 방식 사용 시 workspace 불필요할 수 있음
- 비활성 stream의 workspace는 일정 기간 후 정리

### 신규 파일

- `p4/workspace_manager.py`

---

## 7. Phase 2 테스트

### 필수 테스트

- `MergeAnalyzer.analyze()` — integration 없는 CL, 단일 source, 다중 source, obliterate 대응
- `StreamWatcher.detect_changes()` — 신규/삭제 감지
- `StreamPolicy.should_include()` — include/exclude 패턴 매칭
- `create_branch_from_parent()` — 분기점 정확성
- merge commit parent 연결 정확성
- 전역 CL 정렬 순서 검증

---

## 작업 우선순위 요약

| 순위 | 항목 | 의존성 |
|------|------|--------|
| 1 | 다중 Stream Orchestration | Phase 1 완료 |
| 2 | MergeAnalyzer | 없음 (독립 구현 가능) |
| 3 | Merge Commit 생성 연결 | #1 + #2 |
| 4 | Stream 생성/삭제 감지 | #1 |
| 5 | Stream 필터링 정책 | #4 |
| 6 | Workspace 관리 | #1 |
| 7 | Phase 2 테스트 | #1~#6 |

## 완료 기준

- [ ] 3개 이상 stream이 각각 Git branch로 동기화
- [ ] CL이 전역 순서대로 commit됨
- [ ] integration 있는 CL이 merge commit으로 생성됨 (올바른 2개 parent)
- [ ] 신규 stream이 분기 시점의 parent commit에서 branch 생성됨
- [ ] mainline이 orphan branch로 시작
- [ ] stream 삭제 시 동기화 중단, branch 보존
- [ ] `git log --graph --all --oneline`에서 분기/병합 히스토리 정확 표현
- [ ] include/exclude 패턴 정상 동작
- [ ] virtual stream 자동 제외
