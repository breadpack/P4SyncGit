# Phase 3: 운영 안정화

## 목표

- 폴링 → 이벤트 기반(Trigger + Redis)으로 전환
- 모니터링/알림 체계 구축
- 장애 복구 자동화
- 성능 최적화
- 운영 매뉴얼(Runbook) 작성

## 기술 스택

- Worker: Python 3.12+ (systemd/supervisor)
- P4: p4python
- Git: pygit2 + git CLI fallback
- State DB: sqlite3
- HTTP API: FastAPI + uvicorn
- 알림: slack-sdk
- Redis: redis-py
- 로깅: Python 표준 logging (JSON 구조화)

## 전제 조건

- Phase 2 완료 (다중 stream + merge 동기화 동작 확인)

## Step 3-1: P4 Trigger + Redis 이벤트 전환

### 작업 내용

폴링 방식을 이벤트 방식으로 전환. 폴링은 fallback으로 유지.

1. P4 서버에 `change-commit` 트리거 등록 (스크립트는 [Ref-Architecture.md](Ref-Architecture.md) 참조)
2. Worker에 Webhook receiver 추가 (FastAPI endpoint)
3. Webhook → Redis Stream 발행
4. EventConsumer가 Redis Stream에서 소비

### 이중화 설계

```
Primary: Trigger → Webhook → Redis → Worker (즉시 반응)
Fallback: 폴링 (30초~1분 주기, 누락 CL catch-up)

폴링은 제거하지 않고 유지.
Trigger가 실패하거나 네트워크 문제로 webhook이 누락되면
폴링이 자동으로 보완.
```

```python
# event_consumer.py: Redis Stream에서 이벤트 수신 시 즉시 처리
# changelist_poller.py: 주기적으로 누락 CL 확인 (간격을 1분으로 늘림)
# 둘 다 SyncOrchestrator에 CL을 전달 → 중복 체크 후 처리
```

### 중복 처리 방지

```python
# sync_orchestrator.py
async def process_changelist(self, cl: int, stream: str) -> None:
    existing_sha = await self.state_store.get_commit_sha(cl)
    if existing_sha is not None:
        return  # 이미 처리됨 — 건너뜀
    # ... 동기화 처리
```

### P4 Trigger Heartbeat 메커니즘

Trigger 스크립트 자체의 장애(삭제, 권한 변경, P4 서버 재설정 등)를 감지하기 위해 heartbeat를 도입한다.

```
1. Trigger 스크립트가 주기적으로(1분마다) Redis에 heartbeat 메시지 전송
   - 키: p4gitsync:trigger:heartbeat
   - 값: 타임스탬프 (TTL 3분)

2. Worker가 heartbeat 부재를 감지
   - 3분 이상 heartbeat 없으면 [WARN] Slack 알림
   - 이 경우 폴링 간격을 30초로 자동 축소 (보완 강화)

3. heartbeat 복구 시
   - 폴링 간격을 원래 값(1분)으로 복원
   - [INFO] 복구 알림
```

### Redis Stream 메시지 관리

처리 완료된 메시지의 무한 축적을 방지:
  - XADD 시 MAXLEN ~10000 으로 상한 설정
  - 또는 처리 완료 후 XDEL로 개별 삭제
  - Consumer Group의 PEL(Pending Entries List) 주기적 정리
  - 24시간 이상 pending된 메시지는 자동 claim 또는 알림

### 완료 기준

- [ ] P4 submit 후 5초 이내 Git commit 생성
- [ ] Trigger 장애 시 폴링으로 자동 보완
- [ ] 중복 처리 없음
- [ ] Trigger heartbeat 부재 시 알림 발생 및 폴링 간격 자동 축소

## Step 3-2: 모니터링 HTTP API

### 작업 내용

Worker Service에 FastAPI + uvicorn 기반 HTTP endpoint 추가.

```
GET /api/health
  → 200 OK / 503 Service Unavailable

GET /api/status
  → {
      "streams": [
        {
          "p4_stream": "//ProjectSTAR/main",
          "git_branch": "main",
          "last_synced_cl": 12345,
          "p4_head_cl": 12347,
          "lag": 2,
          "last_sync_time": "2026-03-06T10:30:00Z",
          "error_count": 0
        }
      ],
      "total_processed": 15000,
      "total_lag": 2,
      "disk": {
        "git_repo_size_mb": 15360,
        "state_db_size_mb": 45,
        "workspaces_total_size_mb": 51200,
        "disk_free_space_mb": 204800
      },
      "git": {
        "loose_objects": 1234,
        "pack_files": 3,
        "total_commits": 150000
      },
      "redis": {
        "memory_used_mb": 128,
        "stream_length": 42,
        "consumer_lag": 0,
        "pending_messages": 0
      },
      "performance": {
        "avg_cl_processing_ms": 2300,
        "p99_cl_processing_ms": 8500,
        "avg_p4_command_ms": 450,
        "avg_git_operation_ms": 120
      },
      "process": {
        "memory_mb": 512,
        "uptime": "3d 14h 22m"
      }
    }

GET /api/cutover-readiness
  → 컷오버 가능 여부 및 blockers (Step 3-6 참조)

GET /api/errors
  → 최근 에러 목록

POST /api/retry/{changelist}
  → 실패한 CL 수동 재시도
```

### Redis 상태 모니터링

`/api/status` 응답에 Redis 메트릭을 포함하여 Redis 자체의 건강 상태를 관찰한다.

```python
# redis_monitor.py
import redis.asyncio as aioredis

async def get_redis_metrics(r: aioredis.Redis, stream_key: str, group_name: str) -> dict:
    info = await r.info("memory")
    stream_info = await r.xinfo_stream(stream_key)
    groups = await r.xinfo_groups(stream_key)

    consumer_lag = 0
    pending = 0
    for g in groups:
        if g["name"] == group_name:
            consumer_lag = g["lag"]
            pending = g["pel-count"]
            break

    return {
        "memory_used_mb": round(info["used_memory"] / 1024 / 1024, 1),
        "stream_length": stream_info["length"],
        "consumer_lag": consumer_lag,
        "pending_messages": pending,
    }
```

### 완료 기준

- [ ] health check endpoint 동작
- [ ] stream별 동기화 상태 조회 가능
- [ ] 디스크/Git/성능/프로세스 지표 조회 가능
- [ ] Redis 메모리, Stream 길이, consumer lag 지표 조회 가능
- [ ] 에러 목록 및 수동 재시도

## Step 3-3: Slack 알림

### 작업 내용

slack-sdk를 활용한 운영 알림. 심각도별 채널 분리를 권장한다.

```
채널 분리 권장:
  #p4gitsync-alerts   : [ERROR] 레벨 — 즉시 대응 필요
  #p4gitsync-warnings : [WARN] 레벨 — 주의 관찰 필요
  #p4gitsync-info     : [INFO], [READY], [NOT READY] — 정보성 알림 및 일일 리포트

알림 조건:
  [ERROR]     CL 처리 3회 연속 실패
  [ERROR]     P4 또는 Git 연결 실패
  [ERROR]     무결성 검증 실패 (파일 내용 불일치)
  [ERROR]     디스크 풀 또는 OOM 감지 (1회 실패 시 즉시 알림)
  [WARN]      동기화 지연 5분 초과
  [WARN]      미처리 CL 큐 100건 초과
  [WARN]      디스크 사용량 임계값 초과 (기본: 남은 공간 10GB 미만)
  [WARN]      Git loose object 수 임계값 초과 → git gc 권장
  [WARN]      CL당 평균 처리 시간이 이전 주 대비 2배 초과 (성능 저하 감지)
  [WARN]      Worker 프로세스 메모리 1GB 초과 (메모리 누수 의심)
  [WARN]      N분간 새 메시지 없음 — 침묵 장애 감지
  [READY]     컷오버 준비 완료 (NOT READY → READY 전환 시)
  [NOT READY] 컷오버 불가 상태 진입 (READY → NOT READY 전환 시)
  [INFO]      신규 stream 감지 및 branch 생성
  [INFO]      일일 동기화 통계 (09:00)

알림 피로 방지 정책:
  - 동일 조건(동일 에러 유형 + 동일 stream)에 대해 24시간 재발송 금지
  - 24시간 내 동일 알림이 반복 발생하면 첫 번째만 전송하고, 이후는 카운트만 기록
  - 24시간 경과 후에도 미해결 시 "지속 중" 접두사와 함께 재알림
```

### 침묵 장애 감지

일정 시간 동안 새로운 CL 처리 메시지가 없으면 서비스 자체가 멈춘 것으로 판단한다.

```python
# silence_detector.py
from dataclasses import dataclass
from datetime import datetime, timedelta

@dataclass
class SilenceDetectorConfig:
    silence_threshold_minutes: int = 30  # 설정 가능
    check_interval_seconds: int = 60

class SilenceDetector:
    def __init__(self, config: SilenceDetectorConfig):
        self.config = config
        self._last_activity: datetime | None = None
        self._alerted = False

    def record_activity(self) -> None:
        self._last_activity = datetime.utcnow()
        self._alerted = False

    def check(self, p4_has_recent_activity: bool = True) -> bool:
        """침묵 장애 여부 반환. True이면 알림 필요.

        P4 활동 교차 확인: P4 서버에 최근 submit이 없는 경우(야간, 주말 등)
        침묵은 정상 상태이므로 알림을 억제한다. p4_has_recent_activity가 False이면
        P4에도 활동이 없는 것이므로 침묵 장애로 판단하지 않는다.
        """
        if self._last_activity is None:
            return False
        threshold = timedelta(minutes=self.config.silence_threshold_minutes)
        is_silent = (datetime.utcnow() - self._last_activity) > threshold
        if is_silent and not self._alerted:
            if not p4_has_recent_activity:
                # P4에도 활동이 없으면 정상 침묵 — 알림하지 않음
                return False
            self._alerted = True
            return True
        return False
```

### 디스크 풀 / OOM 즉시 알림

디스크 풀과 OOM(Out Of Memory)은 데이터 손실 위험이 높으므로 3회 재시도를 기다리지 않고 1회 실패 시 즉시 [ERROR] 알림을 전송한다.

```python
# alert_classifier.py
def classify_error(error: Exception) -> str:
    """에러 유형에 따라 즉시 알림 여부를 결정."""
    if isinstance(error, OSError) and error.errno == 28:  # ENOSPC
        return "immediate"
    if isinstance(error, MemoryError):
        return "immediate"
    return "standard"  # 3회 연속 실패 후 알림
```

### 일일 리포트 형식

```
P4GitSync Daily Report (2026-03-06)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Processed: 47 changelists
  main:      12 commits (3 merges)
  dev:       28 commits (1 merge)
  release/1: 7 commits
Errors: 0
Avg latency: 2.3s
Performance:
  Avg CL processing: 2.3s (prev week: 2.1s)
  P4 commands: 450ms avg
  Git operations: 120ms avg
Disk:
  Git repo: 15.0 GB
  State DB: 45 MB
  Free space: 200 GB
Redis:
  Memory: 128 MB
  Stream length: 0
  Pending: 0
```

### 완료 기준

- [ ] 에러 알림 수신 확인 (심각도별 채널 분리)
- [ ] 일일 리포트 발송
- [ ] 디스크 풀/OOM 즉시 알림 동작
- [ ] 침묵 장애 감지 알림 동작

## Step 3-4: 장애 복구 자동화

### 시나리오별 복구

```
1. Worker 프로세스 크래시
   → 자동 재시작 (systemd/Docker restart policy)
   → State DB의 마지막 CL부터 재개
   → 정상 동작

2. P4 서버 일시 중단
   → 재시도 with exponential backoff (5s, 15s, 45s, 2m, 5m)
   → 5분 초과 시 Slack 알림
   → P4 복구 후 자동 catch-up

3. Git remote 접속 불가
   → commit은 로컬에 계속 생성
   → push만 대기 큐에 쌓음 (State DB에 push 상태 기록)
   → 복구 후 일괄 push

4. State DB 손상
   → Git log에서 마지막 P4CL 태그를 파싱하여 재구성
   → p4 changes와 git log를 비교하여 cl_commit_map 복원

5. Git repo 손상
   → remote에서 re-clone
   → State DB 기준으로 마지막 CL부터 재개

6. State DB와 Git 정합성 불일치
   (예: commit 생성 후 StateStore 기록 전 크래시)
   → 서비스 시작 시 자동 정합성 검증:
     a. 각 branch의 Git HEAD commit에서 P4CL 태그 추출
     b. StateStore의 last_synced_cl과 비교
     c. Git에 commit이 있으나 DB에 없으면: Git commit 정보로 DB 보완
     d. DB에 있으나 Git에 없으면: DB 레코드 삭제 후 재동기화
   → 정합성 복구 결과를 로그 + Slack 알림

7. 복합 장애: 로컬 미push 커밋 + Git repo 손상 동시 발생
   → State DB의 push 상태 기록을 확인하여 미push 커밋 식별
   → 미push 커밋이 있는 경우 re-clone 전에 로컬 commit을 백업
     a. 손상되지 않은 로컬 branch를 임시 bare repo로 복사
     b. remote에서 re-clone
     c. 백업한 commit을 cherry-pick 또는 rebase로 복원
     d. 복원 완료 후 push
   → 로컬 commit 백업 불가 시: State DB 기준으로 해당 CL 범위 재동기화
   → 전 과정을 Slack [ERROR] 알림으로 실시간 보고
```

### Push 상태 State DB 기록

Git remote 접속 불가 시 미push 커밋을 보호하기 위해, commit 생성과 push 완료를 State DB에 별도 기록한다.

```python
# state_store.py
from dataclasses import dataclass

@dataclass
class CommitRecord:
    changelist: int
    commit_sha: str
    stream: str
    git_push_status: str  # 'pending': 로컬에만 존재, 'pushed': remote에 push 완료, 'failed': push 실패

async def mark_committed(self, cl: int, sha: str, stream: str) -> None:
    """commit 생성 시 git_push_status='pending'으로 기록."""
    await self.db.execute(
        "INSERT INTO cl_commit_map (changelist, commit_sha, stream, git_push_status) "
        "VALUES (?, ?, ?, 'pending')",
        (cl, sha, stream),
    )

async def mark_pushed(self, shas: list[str]) -> None:
    """push 완료 시 git_push_status='pushed'로 갱신."""
    placeholders = ",".join("?" for _ in shas)
    await self.db.execute(
        f"UPDATE cl_commit_map SET git_push_status = 'pushed' WHERE commit_sha IN ({placeholders})",
        shas,
    )

async def get_unpushed_commits(self) -> list[CommitRecord]:
    """미push 커밋 목록 조회."""
    rows = await self.db.execute_fetchall(
        "SELECT changelist, commit_sha, stream, git_push_status "
        "FROM cl_commit_map WHERE git_push_status != 'pushed' ORDER BY changelist"
    )
    return [CommitRecord(*row) for row in rows]
```

### 복구 명령어

```bash
# State DB 재구성
python -m p4gitsync rebuild-state --from-git

# 특정 CL 범위 재동기화
python -m p4gitsync resync --from 12000 --to 12100 --stream //ProjectSTAR/main

# Git repo 재초기화
python -m p4gitsync reinit-git --remote git@github.com:org/repo.git
```

### State DB 백업

- 주기: 1일 1회 (설정 가능)
- 방법: SQLite Online Backup API (`sqlite3.Connection.backup()`)
- 보존: 최소 30일분 유지, 이전 자동 삭제
- 백업 경로: `{state_db_path}.backup.{date}`

```python
# db_backup.py
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

def backup_state_db(
    source_path: Path,
    backup_dir: Path,
    retention_days: int = 30,
) -> Path:
    """SQLite Online Backup API를 사용한 무중단 백업."""
    backup_name = f"{source_path.stem}.backup.{datetime.now():%Y%m%d}"
    backup_path = backup_dir / backup_name

    source = sqlite3.connect(source_path)
    dest = sqlite3.connect(backup_path)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    # 보존 기간 초과 백업 삭제
    cutoff = datetime.now() - timedelta(days=retention_days)
    for old in backup_dir.glob(f"{source_path.stem}.backup.*"):
        try:
            date_str = old.suffix.lstrip(".")
            file_date = datetime.strptime(date_str, "%Y%m%d")
            if file_date < cutoff:
                old.unlink()
        except ValueError:
            continue

    return backup_path
```

### sync_errors 정리

- resolved=1 레코드: 90일 후 자동 삭제
- resolved=0 레코드: 삭제하지 않음 (수동 해결 필요)

### 완료 기준

- [ ] Worker 재시작 후 정상 재개
- [ ] P4/Git 연결 복구 후 자동 catch-up
- [ ] State DB 재구성 명령 동작
- [ ] State DB와 Git 정합성 불일치 자동 복구
- [ ] State DB 자동 백업 동작 (30일 보존)
- [ ] 미push 커밋 보호 로직 동작 (push 상태 기록)
- [ ] 복합 장애 시나리오 복구 절차 검증

## Step 3-5: 성능 최적화

### 파일 추출 추가 최적화

Phase 1에서 p4 print를 기본 파일 추출 방식으로 채택하였다.
추가 최적화:
  - `p4 -x batch_file print`: 다중 파일을 단일 프로세스로 추출
  - 적응형 전환: 변경 파일 수가 임계값 초과 시 p4 sync로 전환
  - rename 파일은 p4 print + 경로 변경으로 처리

### 병렬 처리

```
Stream 간 독립 commit (merge가 아닌 경우):
  → stream별 Worker 스레드(또는 asyncio task) 병렬 처리

단, merge commit이 필요한 CL은 직렬 처리 (의존성 있음)

구현:
  1. pending CL 목록에서 integration 없는 CL을 먼저 분류
  2. stream별로 독립 CL을 병렬 처리
  3. integration 있는 CL은 의존 CL 처리 완료 후 직렬 처리
```

### 파이프라인 단계 분리

P4 추출과 Git 기록을 별도 단계로 분리하면 각 단계를 독립적으로 스케일링할 수 있다.

```
단계 1: P4 추출 (I/O bound — P4 서버 통신)
  → P4에서 파일 추출 + 메타데이터 수집
  → 결과를 로컬 임시 디렉토리에 저장

단계 2: Git 기록 (CPU/I/O bound — 로컬 디스크)
  → 임시 디렉토리의 파일을 Git working tree에 반영
  → commit 생성

장점:
  - P4 추출 중 Git 잠금 없음 → 다른 stream의 Git 기록과 병렬 가능
  - 단계별 실패 시 해당 단계만 재시도
  - 향후 P4 추출을 별도 프로세스/서버로 분리 가능 (확장성)

구현 방향:
  - asyncio.Queue로 단계 간 데이터 전달
  - 단계 1이 완료된 CL부터 단계 2가 순차 처리
```

### Git Push 배치

```
매 commit마다 push → 네트워크 오버헤드

최적화:
  - N개 commit 또는 T초마다 batch push
  - 설정: push_batch_size=10, push_interval_seconds=60
  - 둘 중 먼저 도달하는 조건에서 push
```

### Git Repository 관리

- **시간 기반 git gc**: 주 1회 정기 실행 (cron 또는 스케줄러로 예약, 기본: 일요일 02:00)
  - commit 수 기반(매 5000 commit)은 보조 트리거로 유지
- loose object 수 임계값(기본 10000) 초과 시 즉시 git gc
- `git repack -a -d` 주기적 실행 (1주 1회, git gc와 동일 스케줄)
- 성능 저하 감지 시 `git gc --aggressive` 수동 실행 옵션

### 데이터 증가 대응

#### cl_commit_map retention 정책

장기 운영 시 cl_commit_map 테이블의 무한 증가를 방지하기 위한 retention 정책:
- `git_push_status = 'pushed'` 완료 후 1년 경과한 레코드는 아카이브 테이블(`cl_commit_map_archive`)로 이동
- 아카이브 테이블은 조회 전용으로 유지하며, 필요 시 원본 테이블과 UNION으로 조회
- 아카이브 작업은 주 1회 비활성 시간에 실행

#### Redis maxmemory 설정

Redis 사용 시 메모리 무한 증가를 방지하기 위해 maxmemory 설정을 필수로 적용한다:
- `maxmemory 256mb` (환경에 따라 조정)
- `maxmemory-policy allkeys-lru` — Stream 데이터 특성상 오래된 메시지부터 제거
- Redis 메모리 사용량이 설정값의 80% 도달 시 [WARN] 알림 발송

### 완료 기준

- [ ] batch print 방식 적용 후 처리 속도 측정
- [ ] batch push 적용 후 네트워크 호출 감소 확인
- [ ] git gc 주 1회 자동 실행 동작 확인
- [ ] cl_commit_map retention 정책 적용 확인
- [ ] Redis maxmemory 설정 적용 확인 (Redis 사용 시)
- [ ] 파이프라인 단계 분리 적용 시 처리량 측정

## Step 3-6: 상시 전환 준비 (Always Ready)

### 목표

동기화 서비스가 지속적으로 실행되면서 Git을 항상 P4와 동일한 최신 상태로 유지한다.
팀이 결정하는 **임의의 시점**에 즉시 컷오버할 수 있어야 한다.

### Readiness Dashboard

상시 전환 가능 여부를 실시간으로 판단할 수 있는 지표를 노출한다.

```
GET /api/cutover-readiness
→ {
    "ready": true | false,
    "blockers": [],                // ready=false일 때 이유
    "streams": [
      {
        "p4_stream": "//ProjectSTAR/main",
        "git_branch": "main",
        "last_synced_cl": 12345,
        "p4_head_cl": 12345,         // P4 최신 CL
        "lag": 0,                    // 미동기화 CL 수
        "last_sync_time": "2026-03-06T10:30:00Z"
      }
    ],
    "total_lag": 0,                  // 전체 미동기화 CL 수
    "unresolved_errors": 0,
    "last_full_sync_check": "2026-03-06T10:30:00Z",
    "integrity_check": {
      "last_run": "2026-03-06T09:00:00Z",
      "status": "passed",
      "sampled_cls": 50,
      "mismatches": 0
    }
  }
```

### Readiness 판정 기준

```
ready = true 조건 (모두 충족해야 함):
  1. total_lag == 0 — 모든 stream이 최신 CL까지 동기화됨
  2. unresolved_errors == 0 — 미해결 에러 없음
  3. integrity_check.status == "passed" — 최근 무결성 검증 통과
  4. 모든 stream의 last_sync_time이 5분 이내

ready = false일 때 blockers 예시:
  - "Stream //ProjectSTAR/dev has 3 unsynced CLs"
  - "2 unresolved sync errors"
  - "Integrity check failed: CL 12300 content mismatch"
  - "Stream //ProjectSTAR/main last synced 12 minutes ago"
```

### 주기적 무결성 검증 (Integrity Check)

```
지속적 sync 중에 데이터 정합성을 보장하기 위해
주기적으로 P4와 Git의 내용을 비교 검증한다.

검증 주기:
  - 일일 샘플 검증: 1일 1회 (설정 가능)
  - 주간 전수 검증: 1주 1회 — 전체 stream의 최신 CL 전체 파일 비교
  - 월간 전수 검증: 1월 1회 — 랜덤 과거 CL을 포함한 광범위 검증

검증 방법:
  1. 각 stream의 최신 동기화 CL을 선택
  2. p4 print로 해당 CL 시점의 파일 N개 추출
  3. git show {commit_sha}:{path}로 동일 파일 조회
  4. 내용 해시 비교
  5. 불일치 시 sync_errors에 기록 + Slack 알림

추가 검증:
  - stream별 총 CL 수 vs Git commit 수 일치 확인
  - merge commit의 parent SHA가 State DB와 일치하는지 확인
  - branch 분기점이 State DB의 branch_point_sha와 일치하는지 확인
```

### 무결성 검증 실패 시 Circuit Breaker

무결성 검증 실패는 데이터 신뢰성 문제이므로, 추가 손상을 방지하기 위해 동기화를 자동 중단한다.

```python
# circuit_breaker.py
from dataclasses import dataclass, field
from enum import Enum

class CircuitState(Enum):
    CLOSED = "closed"      # 정상 동작
    OPEN = "open"          # 동기화 중단
    HALF_OPEN = "half_open"  # 시험적 재개

@dataclass
class IntegrityCircuitBreaker:
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    threshold: int = 1  # 무결성 실패 1회로 즉시 중단

    def on_integrity_failure(self) -> None:
        self.failure_count += 1
        if self.failure_count >= self.threshold:
            self.state = CircuitState.OPEN
            # Slack [ERROR] 알림: "무결성 검증 실패 — 동기화 자동 중단"
            # 수동 조사 후 reset 필요

    def reset(self) -> None:
        """수동 확인 후 호출하여 동기화 재개."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0

    def allow_sync(self) -> bool:
        return self.state != CircuitState.OPEN
```

### Git Remote Branch Protection

Git remote에 branch protection 규칙을 설정하여 동기화 서비스 외의 push를 차단한다.

```
필수 설정:
  - main, dev 등 동기화 대상 branch에 protection 규칙 적용
  - 동기화 서비스 전용 deploy key 또는 service account만 push 허용
  - force push 차단
  - branch 삭제 차단
  - 이를 통해 동기화 데이터의 무결성을 외부 변경으로부터 보호
```

### Slack 상태 알림

```
기존 알림에 추가:
  [READY]    컷오버 준비 완료 (lag=0, error=0, integrity=passed)
  [NOT READY] 컷오버 불가 — 사유: {blockers}

상태 변경 시에만 알림 (반복 알림 방지):
  NOT READY → READY: "P4GitSync: 컷오버 준비 완료"
  READY → NOT READY: "P4GitSync: 컷오버 불가 — {reason}"
```

### 완료 기준

- [ ] `/api/cutover-readiness` endpoint 동작
- [ ] 무결성 검증 주기적 실행 및 결과 기록 (일일/주간/월간)
- [ ] 무결성 실패 시 circuit breaker 동작 (동기화 자동 중단)
- [ ] Git remote branch protection 설정 완료
- [ ] ready/not-ready 상태 변경 시 Slack 알림
- [ ] 24시간 이상 ready=true 유지 확인

## Step 3-7: P4→Git 컷오버 실행

### 목표

Step 3-6의 readiness가 확인된 상태에서, 팀이 결정한 시점에 컷오버를 실행한다.
컷오버는 **주 초(월~화)에 실행**을 권장한다. 문제 발생 시 주중에 대응할 여유가 확보된다.

### 전제 조건

```
- /api/cutover-readiness → ready: true
- 팀 전원 Git workflow 교육 완료
- **CI/CD 파이프라인 Git 기반 전환 완료** (P4와 병행 테스트 통과)
  ※ **CI/CD 전환은 컷오버와 동시 착수해야 한다** — 컷오버 후에 CI/CD 전환을 시작하면
    빌드 파이프라인 부재로 팀 전체 개발이 중단될 위험이 있음.
    Phase 3 초기부터 CI/CD 전환을 병행하여 컷오버 시점에는 Git 기반 빌드가
    완전히 동작하는 상태여야 한다.
  ※ CI/CD 전환 범위: 빌드 스크립트, 아티팩트 경로, 환경 변수, 자동 배포 파이프라인 등
- Git remote에 branch protection 규칙 설정 완료
- dry-run 컷오버 1회 이상 실시 완료 (전체 절차를 리허설하여 소요 시간 측정 및 문제점 사전 발견)
- 컷오버 최대 기한 설정 (예: "3월 31일까지 미완료 시 다음 분기로 연기")
  → 무기한 지연을 방지하고, 팀의 의사결정을 촉진
```

### 컷오버 절차

```
Phase A: Freeze & Final Sync (최대 2시간 유지보수 윈도우 확보)
  1. P4 submit 차단 (p4 protect으로 write 권한 제거)
  2. 동기화 서비스가 잔여 CL 처리 완료까지 대기
  3. /api/cutover-readiness → total_lag=0 확인
  4. 최종 무결성 검증 실행 (전체 stream, 샘플 수 증가)
  5. 최종 push (모든 branch)

Phase B: 전환
  6. 팀 전체에 Git 전환 공지
  7. P4 서버를 read-only로 전환 (아카이브)
  8. 동기화 서비스 종료
  9. Git remote를 공식 소스로 지정

Phase C: 검증 (전환 후 1~2주)
  10. 팀 워크플로우 모니터링 (PR, merge, branch 생성)
  11. git blame, bisect 정상 동작 확인 (실무 사용)
  12. 문제 발생 시 P4 read-only에서 히스토리 참조 가능
```

### Rollback 계획

> **[Critical] 수동 역적용의 비현실성 인정 및 Forward Fix 전략 수립**
>
> 전환 후 수백~수천 건의 Git commit을 수동으로 P4에 역적용하는 것은 비현실적이다.
> 따라서 **Forward Fix(전진 수정)를 기본 전략**으로 채택하고,
> P4로의 완전 롤백은 극단적 케이스에서만 최후 수단으로 고려한다.

```
전환 후 문제 발생 시 기본 전략: Forward Fix (전진 수정)

기본 원칙:
  "문제 발견 → 원인 분석 → 수정 → Git에서 계속 진행"
  P4로 롤백하는 것이 아니라, Git 위에서 문제를 해결하고 전진한다.

Forward Fix 절차:
  1. 문제 발견 시 즉시 원인 분류
     a. 동기화 데이터 문제 (히스토리 누락, 파일 내용 불일치 등)
     b. Git workflow 문제 (팀 적응, CI/CD 호환성 등)
     c. 인프라 문제 (Git 서버 성능, LFS 용량 등)
  2. 원인별 대응
     a. 동기화 데이터 문제 → 부분 재동기화 또는 수동 보정 commit
     b. Git workflow 문제 → 추가 교육, workflow 가이드 보완
     c. 인프라 문제 → 서버 증설, 설정 최적화
  3. 수정 완료 후 Git에서 정상 운영 계속

Phase C-1: Forward Fix 기간 (전환 후 2~4주)
  1. 전환 직후 집중 모니터링 체계 가동
  2. 문제 발생 시 Forward Fix 절차에 따라 대응
  3. P4 서버는 read-only로 유지 (히스토리 참조용)
  4. 이 기간 동안 P4 서버 write-ready 상태도 병행 유지 (극단적 롤백 대비)

Phase C-2: 극단적 케이스의 P4 롤백 (최후 수단)
  Forward Fix가 불가능한 극단적 상황에서만 수동 역적용을 검토한다.
  극단적 케이스 예시:
    - Git 저장소의 복구 불가능한 데이터 손상
    - 보안 사고로 인한 Git 서버 전면 오염
    - 법적/규제 요구에 의한 즉시 원복 명령

  극단적 롤백 절차:
    1. Git push 차단 (branch protection으로 전체 write 차단)
    2. P4 서버 write 권한 복원
    3. P4의 마지막 상태에서 작업 재개
    4. Git에서 전환 후 발생한 변경을 P4에 수동 재적용
       (이 과정은 수일~수주 소요될 수 있으며, 변경량에 비례하여 비용 증가)
    5. 원인 분석 후 동기화 서비스 재가동, 재시도

P4 아카이브 정책:
  - 컷오버 후 최소 3개월간 P4 서버를 read-only로 유지 (히스토리 참조용)
  - 3개월 경과 후 cold storage 아카이브 옵션 검토:
    a. P4 서버 데이터를 tar/zip으로 아카이브 후 오프라인 스토리지에 보관
    b. P4 checkpoint + journal 파일만 별도 보관 (필요 시 서버 복원 가능)
    c. 클라우드 cold storage (예: AWS Glacier, Azure Cool Blob) 활용
  - 즉시 폐기하지 않음 — 규정 준수, 감사, 분쟁 해결 등을 위해 장기 보존 권장

리스크 최소화를 위한 사전 조치:
  - 전환 전 2~4주간 Shadow 모드 운영
    (실제 업무는 P4, Git은 읽기 전용으로 팀이 검증)
    ※ Shadow 모드 1주는 충분하지 않음 — 최소 2주 이상 운영하여
      스프린트 1회 주기 이상의 실무 패턴을 관찰해야 한다.
  - 전환 후 2~4주(Forward Fix 기간) 동안 P4 서버 write-ready 상태 유지
  - 전환 직후 수일간 집중 모니터링 체계 가동
  - 컷오버를 주 초(월~화)에 실행하여 주중 대응 시간 확보
```

### 완료 기준

- [ ] dry-run 컷오버 1회 이상 실시 (실제 컷오버 전에 전 과정을 리허설)
- [ ] Freeze → Final Sync → Push가 최대 2시간 유지보수 윈도우 내 완료
- [ ] 팀 전체 Git workflow 전환 확인
- [ ] P4 서버 read-only 전환 (최소 3개월 유지)
- [ ] 전환 후 1주간 블로커 이슈 없음

## Step 3-8: 운영 매뉴얼 (Runbook) 작성

### 작업 내용

운영 중 발생하는 일상적인 작업과 장애 대응 절차를 문서화.

### 필수 항목

```
1. 일상 운영
   - 서비스 시작/중지/재시작 절차
     - systemd: systemctl start/stop/restart p4gitsync
     - supervisor: supervisorctl start/stop/restart p4gitsync
   - 로그 확인 및 로그 레벨 변경
     - journalctl -u p4gitsync -f
     - 로그 레벨: LOG_LEVEL 환경 변수 또는 설정 파일
   - 일일 리포트 확인 및 이상 징후 판별

2. 데이터 관리
   - user_mapping 추가/변경 절차
   - Stream 수동 추가/제거 절차
   - State DB 백업 확인 및 수동 복원

3. 장애 대응
   - 실패 CL 수동 재시도 (/api/retry)
   - State DB 재구성 (python -m p4gitsync rebuild-state)
   - Git repo 재초기화 (python -m p4gitsync reinit-git)
   - 에러 유형별 원인-해결 매트릭스

4. 긴급 대응 의사결정 트리
   - 동기화 지연 > 5분 → 원인 분류 (P4/Git/네트워크)
   - 연속 실패 > 3회 → blocking 여부 확인 → 건너뛰기 또는 수동 개입
   - 정합성 검증 실패 → circuit breaker 작동 확인 → 영향 범위 확인 → 부분 재동기화

5. P4/Git 서버 점검 대응
   - P4 서버 점검 전/후 체크리스트
   - Git remote 점검 전/후 체크리스트

6. 조직/인력 관리
   - Bus Factor 2 이상 유지: 시스템 운영 가능 인력을 최소 2인 이상 확보
   - 주요 의사결정은 ADR(Architecture Decision Record)로 문서화
   - 담당자 변경 시 인수인계 체크리스트 활용
```

### 완료 기준

- [ ] 운영 매뉴얼 초안 작성
- [ ] 팀원 1인 이상이 매뉴얼만으로 기본 운영 가능 확인

## Step 3-9: Git LFS 지원 (선택)

LFS 도입 여부는 Phase 1 Step 1-0에서 결정한다. 이 Step은 LFS를 도입하기로 결정한 경우에만 해당한다.

바이너리 에셋까지 동기화할 경우.

### 작업 내용

```
.gitattributes 자동 생성:
  *.png filter=lfs diff=lfs merge=lfs -text
  *.fbx filter=lfs diff=lfs merge=lfs -text
  *.wav filter=lfs diff=lfs merge=lfs -text
  ...

확장자 목록은 설정 파일에서 관리.
```

### LFS File Locking

P4의 exclusive checkout (+l 타입)과 Git LFS Lock의 매핑:
  - P4 +l 타입 파일 → LFS tracked + locking 권장 파일로 분류
  - 전환 후 팀 워크플로우에 LFS Lock 사용 가이드 제공

### LFS 서버 옵션

- GitHub/GitLab 내장 LFS: 간편하지만 용량 제한 있음
- Self-hosted (Gitea, MinIO 기반): 용량 제한 없음, 인프라 관리 필요
- 바이너리 에셋 규모에 따라 선택

### 주의사항

- Git LFS 서버(GitHub/GitLab) 스토리지 용량 제한 확인 필요
- 대규모 게임 에셋은 수십~수백 GB → LFS 비용 고려
- 코드만 동기화하고 에셋은 제외하는 것이 현실적일 수 있음

### 완료 기준

- [ ] .gitattributes 자동 생성
- [ ] LFS 파일 정상 push 확인
