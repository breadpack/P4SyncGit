# P4GitSync 동기화 메커니즘

## 동기화 모드

P4GitSync는 두 가지 동기화 모드를 지원합니다.

### 1. 단일 Stream 모드

하나의 P4 Stream을 하나의 Git branch에 동기화합니다.

```
P4 Stream (//depot/main) → Git branch (main)
```

- `stream_registry` 테이블에 등록된 stream이 1개일 때 자동 선택
- `SyncOrchestrator._start_single_stream()`이 실행

### 2. 다중 Stream 모드

여러 P4 Stream을 각각 별도의 Git branch로 동기화합니다.

```
P4 Stream (//depot/main)    → Git branch (main)
P4 Stream (//depot/develop) → Git branch (develop)
P4 Stream (//depot/feature) → Git branch (feature)
```

- `stream_registry` 테이블에 2개 이상의 stream 등록 시 자동 전환
- `MultiStreamHandler`가 처리

---

## 이벤트 기반 동기화 (Redis)

### 아키텍처

```
P4 submit → P4 Trigger (bash) → POST /api/trigger → Redis Stream
                                                          ↓
                                                   EventConsumer
                                                   (별도 스레드)
                                                          ↓
                                               SyncOrchestrator
                                              ._on_redis_changelist()
```

### Redis Stream 구조

- **Stream 키**: `p4sync:events` (설정 가능)
- **Consumer Group**: `p4sync-workers`
- **메시지 필드**: `changelist`, `user`, `stream`

### EventConsumer 동작

1. **XREADGROUP**: 새 메시지를 블로킹으로 읽기 (block_ms 단위)
2. **메시지 처리**: `on_changelist` 콜백 호출
3. **XACK**: 처리 완료 확인
4. **Stale 메시지 회수**: `pending_claim_timeout_hours` 초과된 pending 메시지를 XCLAIM으로 재할당
5. **Heartbeat 감시**: `heartbeat_timeout_minutes` 초과 시 fallback poll 실행

### 자동 복구

- Redis 연결 끊김 → 5초 대기 후 재연결 시도
- heartbeat 초과 → ChangelistPoller로 자동 전환

---

## 폴링 기반 동기화 (Fallback)

Redis가 비활성화되거나 연결이 끊긴 경우의 폴백 메커니즘입니다.

### ChangelistPoller

```python
def poll(stream, batch_size=50) -> list[int]:
    last_cl = state_store.get_last_synced_cl(stream)
    return p4_client.get_changes_after(stream, last_cl)[:batch_size]
```

- `polling_interval_seconds` 간격으로 P4 서버에 직접 조회
- 마지막 동기화 CL 이후의 신규 CL 목록 반환
- `batch_size`로 한 번에 처리할 최대 수 제한

---

## 파일 추출 방식

P4에서 파일 내용을 가져오는 방식은 `p4 print` 기반으로, 로컬 워크스페이스 sync 없이 메모리에서 직접 처리합니다.

### 배치 모드 (파일 2개 이상)

```python
# 단일 p4 print 호출로 여러 파일 일괄 추출
file_specs = ["//depot/path/file1.cpp#3", "//depot/path/file2.h#5"]
batch_results = p4_client.print_files_batch(file_specs)
# → {"//depot/path/file1.cpp": b"...", "//depot/path/file2.h": b"..."}
```

### 개별 모드 (파일 1개)

```python
content = p4_client.print_file_to_bytes(depot_path, revision)
```

### 실패 처리

- obliterate된 파일은 경고 로깅 후 건너뜀
- batch 실패 시 개별 추출로 자동 fallback

---

## Commit 생성 과정

### CommitBuilder.build_commit()

```
P4ChangeInfo (changelist 정보)
    ↓
1. 파일 추출 (_extract_file_changes)
   ├─ add/edit 파일: p4 print → bytes
   ├─ delete 파일: 삭제 목록
   └─ LFS 대상: 포인터로 변환
    ↓
2. Merge 분석 (_analyze_merge)
   ├─ integration 파일 존재? → MergeAnalyzer 호출
   └─ source stream + source CL 결정
    ↓
3. Commit 메타데이터 생성
   ├─ author: P4 user → Git author (user_mappings 테이블)
   ├─ timestamp: P4 changelist timestamp
   └─ message: 원본 description + [P4CL: NNN]
    ↓
4. Git commit 생성
   ├─ merge 없음 → GitOperator.create_commit()
   └─ merge 있음 → GitOperator.create_merge_commit()
    ↓
5. StateStore 기록
   ├─ cl_commit_map: CL ↔ SHA 매핑
   └─ sync_state: 마지막 동기화 CL 업데이트
```

### Commit Message 형식

```
원본 P4 changelist 설명

[P4CL: 12345]
[Integration: //depot/develop -> //depot/main]
[Source CL: 12340]
[Integrated files: 3]
```

---

## Merge 재현

### P4 Integration → Git Merge

P4의 Stream 간 integration(copy, merge, branch 등)을 Git merge commit으로 재현합니다.

### MergeAnalyzer 분석 흐름

1. changelist의 파일 action에서 integration 파일 필터링
2. `p4 filelog` 조회 → integration record 추출
3. source stream과 source CL 결정 (가장 많은 파일의 source가 primary)

### MergeInfo

```python
@dataclass
class MergeInfo:
    has_integration: bool
    primary_source_stream: str    # 통합 파일이 가장 많은 source
    source_changelist: int        # source의 최대 revision
    records: list[IntegrationRecord]
```

### Merge Commit 생성 조건

1. `MergeInfo.has_integration == True`
2. source stream의 source CL에 대응하는 Git commit SHA가 StateStore에 존재
3. 두 조건 모두 충족 시 → `create_merge_commit(parents=[target_sha, source_sha])`
4. 실패 시 → 일반 commit으로 fallback (integration 정보는 메시지에 기록)

---

## 다중 Stream 동기화

### MultiStreamHandler 동작 흐름

```
1. EventCollector.collect()
   ├─ 모든 등록된 stream에서 미동기화 CL 수집
   └─ 전역 정렬: (CL, priority)
       ├─ BranchCreateEvent (priority 0) — 먼저 실행
       └─ ChangelistEvent (priority 1) — 이후 실행

2. 이벤트 처리
   ├─ BranchCreateEvent:
   │   └─ parent stream의 분기점 commit에서 새 Git branch 생성
   └─ ChangelistEvent:
       └─ stream별 CommitBuilder로 commit 생성

3. Push
   ├─ push_after_every_commit=true: 매 commit마다 push
   └─ push_after_every_commit=false: 모든 이벤트 처리 후 일괄 push
```

### Stream 자동 감지

`StreamWatcher`가 P4 depot에서 새로운 stream을 자동으로 감지합니다.

- `auto_discover=true` 시 활성화
- `StreamPolicy.should_include(stream, stream_type)`로 필터링
- 신규 stream 감지 시 Slack INFO 알림

### Branch 생성 규칙

- Stream 이름에서 Git branch 이름 자동 생성
  - `//depot/main` → `main`
  - `//depot/develop` → `develop`
  - `//depot/feature/JIRA-123` → `feature/JIRA-123`
- parent stream의 `branch_point_cl` 위치에서 분기

---

## Push 전략

### 즉시 Push (`push_after_every_commit=true`)

- 매 commit 생성 후 즉시 `git push origin {branch}`
- 실시간성이 중요한 경우
- push 실패 시 StateStore에 `failed` 기록 → 재시작 시 재시도

### 일괄 Push (`push_after_every_commit=false`, 기본)

- 두 조건 중 하나 충족 시 push:
  1. 미push commit 수 ≥ `push_batch_size` (기본 10)
  2. 마지막 push 이후 `push_interval_seconds` (기본 60초) 경과
- 성능 우선 (네트워크 비용 절감)

### Remote 미설정 시

`remote_url`이 빈 문자열이면 push가 완전히 생략됩니다 (no-op). bare repository 로컬 전용 모드에서 사용합니다.

---

## 초기 히스토리 Import

### InitialImporter

`git fast-import`를 활용한 대량 히스토리 일괄 변환입니다.

```
P4 전체 CL 목록 조회
    ↓
배치 단위로 처리 (batch_size)
    ↓
각 CL에 대해:
├─ P4에서 파일 추출 (print_file_to_bytes)
├─ LFS 포인터 변환 (활성화 시)
├─ CommitMetadata 생성
├─ FastImporter.add_commit() → stdin에 기록
└─ 체크포인트 저장 (checkpoint_interval 마다)
    ↓
FastImporter.finish() → git fast-import 프로세스 종료
    ↓
_post_import() → 최종 SHA 매핑 + git gc
```

### fast-import 명령어 형식

```
commit refs/heads/{branch}
mark :{mark_number}
author {name} <{email}> {timestamp} +0000
committer {name} <{email}> {timestamp} +0000
data {message_length}
{message}
M 100644 inline {file_path}
data {content_length}
{binary_content}
D {deleted_file_path}
checkpoint
```

### 재개 지원

- `resume_on_restart=true` (기본): StateStore에서 마지막 체크포인트 CL 조회 후 이어서 진행
- 대규모 히스토리 (수만~수십만 CL) import 시 네트워크 끊김에 대비

### 서버 부하 보호

- `check_server_load()`: P4 서버 부하 확인
- 과부하 시 60초 대기 후 재시도
