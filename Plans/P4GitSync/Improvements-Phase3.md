# Phase 3 개선 작업 — 운영 안정화

현재 완성도: ~10% → 목표: 100%

## 개요

최소한의 trigger/health API와 Slack 에러 알림만 존재한다. Redis 이벤트 전환, 모니터링, 장애 복구, 성능 최적화, 컷오버 준비 등 운영 수준의 기능을 구현해야 한다.

**전제 조건**: Phase 2 개선 작업(Improvements-Phase2.md) 완료

---

## 1. P4 Trigger + Redis 이벤트 전환

### 현상

- `api_server.py`에 `/api/trigger` 엔드포인트만 존재
- Redis는 `pyproject.toml` 의존성에만 선언, 코드에서 미사용
- trigger 수신 시 실제 동기화를 트리거하지 않음

### 구현 내용

**Webhook → Redis Stream 발행:**
```python
# api_server.py
@app.post("/api/trigger")
async def receive_trigger(payload: TriggerPayload):
    await redis.xadd("p4sync:events", {
        "changelist": str(payload.changelist),
        "user": payload.user,
    })
    return {"status": "accepted"}
```

**EventConsumer — Redis Stream 소비:**
```python
class EventConsumer:
    async def consume(self):
        while True:
            messages = await redis.xreadgroup(
                group_name, consumer_name, {"p4sync:events": ">"},
                count=10, block=5000,
            )
            for msg in messages:
                await self._orchestrator.process_event(msg)
                await redis.xack("p4sync:events", group_name, msg.id)
```

**이중화 설계:**
- Primary: Trigger → Webhook → Redis → Worker (즉시 반응)
- Fallback: 폴링 (간격 1분으로 확대, 누락 CL catch-up)
- 중복 처리 방지: `state_store.get_commit_sha(cl)` 존재 시 건너뛰기

**Trigger Heartbeat:**
- 마지막 trigger 이벤트 수신 시각 기록
- 30분 이상 이벤트 없으면 P4 최근 submit 교차 확인
- trigger 장애 판정 시 폴링 간격 자동 축소

**Redis Stream 메시지 관리:**
- XADD 시 `MAXLEN ~10000` 상한 설정
- 24시간 이상 pending 메시지 자동 claim 또는 알림

### 신규 파일

- `services/event_consumer.py`

### 수정 파일

- `api/api_server.py` — Redis 발행 연동
- `services/sync_orchestrator.py` — EventConsumer 통합
- `config/sync_config.py` — Redis 설정 추가

---

## 2. 모니터링 HTTP API 확장

### 현상

`/api/health`만 존재. 동기화 상태, 에러 현황, 메트릭 조회 불가.

### 구현 내용

```
GET /api/status
  → stream별 동기화 상태, lag, 디스크/Git/Redis/성능/프로세스 지표

GET /api/cutover-readiness
  → 컷오버 가능 여부 및 blockers

GET /api/errors
  → 최근 에러 목록 (미해결 우선)

POST /api/retry/{changelist}
  → 실패한 CL 수동 재시도
```

**Redis 상태 모니터링:**
```python
async def get_redis_metrics(r, stream_key, group_name) -> dict:
    # memory_used_mb, stream_length, consumer_lag, pending_messages
```

### 수정 파일

- `api/api_server.py` — 엔드포인트 추가, orchestrator/state_store 참조 주입

### 신규 파일

- `api/redis_monitor.py` (Redis 사용 시)

---

## 3. Slack 알림 체계 구축

### 현상

`SlackNotifier`에 `send_error`/`send_info`만 존재. 다양한 알림 시나리오 미구현.

### 구현 내용

**채널 분리:**
- `#p4gitsync-alerts` — [ERROR] 즉시 대응
- `#p4gitsync-warnings` — [WARN] 주의 관찰
- `#p4gitsync-info` — [INFO], [READY/NOT READY], 일일 리포트

**알림 조건:**
- [ERROR] CL 처리 3회 연속 실패, P4/Git 연결 실패, 무결성 검증 실패, 디스크 풀/OOM
- [WARN] 동기화 지연 5분 초과, 미처리 큐 100건 초과, 디스크 임계값, 성능 저하 감지, 침묵 장애
- [READY/NOT READY] 컷오버 상태 변경 시
- [INFO] 신규 stream 감지, 일일 통계

**알림 피로 방지:**
- 동일 조건에 대해 24시간 재발송 금지
- 24시간 경과 후 미해결 시 "지속 중" 접두사와 함께 재알림

**침묵 장애 감지:**
```python
class SilenceDetector:
    def check(self, p4_has_recent_activity: bool) -> bool: ...
```

**디스크 풀/OOM 즉시 알림:**
```python
def classify_error(error: Exception) -> str:
    if isinstance(error, OSError) and error.errno == 28:  # ENOSPC
        return "immediate"
    if isinstance(error, MemoryError):
        return "immediate"
    return "standard"
```

**일일 리포트:** 매일 09:00 발송 (처리 건수, 에러, 성능, 디스크, Redis)

### 신규 파일

- `notifications/alert_classifier.py`
- `notifications/silence_detector.py`
- `notifications/daily_report.py`

### 수정 파일

- `notifications/notifier.py` — 채널 분리, 피로 방지 로직
- `services/sync_orchestrator.py` — 알림 트리거 포인트 추가

---

## 4. 장애 복구 자동화

### 현상

시작 시 정합성 검증 + 미완료 push 재시도는 존재. 자동 롤백, 상태 복원 도구 등 미구현.

### 구현 내용

**시나리오별 복구:**

| 시나리오 | 자동 복구 | 수동 도구 |
|---------|----------|----------|
| Worker 크래시 | systemd 자동 재시작 + State DB 재개 | — |
| P4 서버 중단 | exponential backoff (5s→5m) + Slack | — |
| Git remote 불가 | 로컬 commit 계속 + push 대기 큐 | — |
| State DB 손상 | — | `rebuild-state --from-git` |
| Git repo 손상 | — | `reinit-git --remote <url>` |
| State/Git 불일치 | 시작 시 자동 교차 검증 | — |

**Push 상태 기록:** 이미 `cl_commit_map.git_push_status` 존재 (pending/pushed/failed)

**State DB 백업:**
- 주기: 1일 1회
- 방법: `sqlite3.Connection.backup()`
- 보존: 30일, 이전 자동 삭제

**sync_errors 정리:**
- resolved=1 레코드: 90일 후 자동 삭제

**CLI 복구 명령어:**
```bash
python -m p4gitsync rebuild-state --from-git
python -m p4gitsync resync --from 12000 --to 12100 --stream //ProjectSTAR/main
python -m p4gitsync reinit-git --remote git@github.com:org/repo.git
```

### 신규 파일

- `services/recovery.py` — State DB 재구성, Git 재초기화, 부분 재동기화
- `services/db_backup.py` — SQLite 자동 백업

### 수정 파일

- `__main__.py` — CLI 서브커맨드 추가 (rebuild-state, resync, reinit-git)
- `state/state_store.py` — sync_errors 정리 로직

---

## 5. 성능 최적화

### 현상

incremental tree 빌드, fast-import, 서버 부하 체크, 배치 filelog는 존재. 추가 최적화 필요.

### 구현 내용

**파일 추출 최적화:**
- `p4 -x batch_file print`: 다중 파일 단일 프로세스 추출
- 적응형 전환: 변경 파일 수 임계값 초과 시 `p4 sync`로 전환

**병렬 처리:**
- integration 없는 독립 CL은 stream별 병렬 처리
- integration 있는 CL은 의존 완료 후 직렬 처리

**파이프라인 분리:**
- 단계 1: P4 추출 (I/O bound) → 임시 디렉토리
- 단계 2: Git 기록 (CPU/I/O bound) → commit 생성
- `asyncio.Queue`로 단계 간 전달

**Git Push 배치:**
- N개 commit 또는 T초마다 batch push
- 설정: `push_batch_size=10`, `push_interval_seconds=60`

**Git Repository 관리:**
- 시간 기반 `git gc`: 주 1회 (일요일 02:00)
- loose object 수 10000 초과 시 즉시 gc
- `git repack -a -d` 주 1회

**데이터 증가 대응:**
- `cl_commit_map` retention: pushed 후 1년 경과 → 아카이브 테이블로 이동
- Redis `maxmemory 256mb` + `allkeys-lru` 정책

**`_rebuild_tree` 최적화 (`pygit2_git_operator.py:152-237`):**
- 현재 매 레벨마다 전체 path_blobs 순회 (O(depth * N))
- prefix별 사전 분류로 O(N) 수준으로 개선

### 수정 파일

- `services/sync_orchestrator.py` — 병렬 처리, 파이프라인
- `services/commit_builder.py` — 배치 파일 추출
- `git/pygit2_git_operator.py` — _rebuild_tree 최적화
- `state/state_store.py` — retention 정책

---

## 6. 상시 전환 준비 (Always Ready)

### 구현 내용

**Readiness Dashboard:**
```
GET /api/cutover-readiness → {
    ready: true|false,
    blockers: [],
    streams: [...],
    total_lag: 0,
    unresolved_errors: 0,
    integrity_check: { status, sampled_cls, mismatches }
}
```

**ready = true 조건 (모두 충족):**
1. total_lag == 0
2. unresolved_errors == 0
3. integrity_check.status == "passed"
4. 모든 stream의 last_sync_time이 5분 이내

**주기적 무결성 검증:**
- 일일: 샘플 검증 (N개 파일 해시 비교)
- 주간: 전체 stream 최신 CL 전수 비교
- 월간: 랜덤 과거 CL 포함 광범위 검증

**Circuit Breaker:**
```python
class IntegrityCircuitBreaker:
    def on_integrity_failure(self) -> None:
        self.state = CircuitState.OPEN
        # 동기화 자동 중단 + Slack [ERROR]

    def allow_sync(self) -> bool:
        return self.state != CircuitState.OPEN
```

**Git Remote Branch Protection:**
- 동기화 대상 branch에 protection 규칙 적용
- 서비스 전용 deploy key만 push 허용
- force push, branch 삭제 차단

### 신규 파일

- `services/integrity_checker.py`
- `services/circuit_breaker.py`

### 수정 파일

- `api/api_server.py` — `/api/cutover-readiness` 엔드포인트
- `services/sync_orchestrator.py` — circuit breaker 통합

---

## 7. P4→Git 컷오버 실행

### 구현 내용

**컷오버 CLI 명령:**
```bash
python -m p4gitsync cutover --dry-run   # 리허설
python -m p4gitsync cutover --execute    # 실행
```

**Phase A: Freeze & Final Sync (최대 2시간):**
1. P4 submit 차단 확인
2. 잔여 CL 처리 완료 대기
3. total_lag=0 확인
4. 최종 무결성 검증 (전수)
5. 최종 push

**Phase B: 전환:**
6. Git remote를 공식 소스로 지정
7. 동기화 서비스 종료

**Rollback 전략:** Forward Fix 기본, P4 롤백은 최후 수단

### 신규 파일

- `services/cutover.py`

### 수정 파일

- `__main__.py` — cutover 서브커맨드

---

## 8. 운영 매뉴얼 (Runbook)

### 구현 내용

문서 작성 (코드 아님):
- 서비스 시작/중지/재시작 절차
- 로그 확인 및 로그 레벨 변경
- user_mapping 추가/변경
- Stream 수동 추가/제거
- 실패 CL 수동 재시도
- State DB 백업/복원
- 긴급 대응 의사결정 트리
- Bus Factor 2 이상 유지

### 신규 파일

- `docs/runbook.md`

---

## 9. Git LFS 지원 (선택)

### 현상

`lfs_config.py`에 확장자 목록과 gitattributes 생성만 존재. 실제 LFS 트래킹/푸시 통합 없음.

### 구현 내용 (LFS 도입 결정 시에만)

- `.gitattributes` 자동 생성 (첫 commit에 포함)
- LFS 대상 파일 감지 시 LFS 포인터 파일 생성
- P4 `+l` 타입 → LFS Lock 권장 파일 분류
- LFS 서버 옵션 선택 (GitHub/GitLab 내장 vs self-hosted)

### 수정 파일

- `services/commit_builder.py` — LFS 포인터 생성 분기
- `services/initial_importer.py` — 첫 commit에 .gitattributes 포함
- `config/lfs_config.py` — LFS 서버 설정 확장

---

## 10. Phase 3 테스트

### 필수 테스트

- EventConsumer — Redis 메시지 소비 및 처리
- 중복 CL 처리 방지
- 침묵 장애 감지 (P4 활동 교차 확인 포함)
- 알림 피로 방지 (24시간 재발송 금지)
- Circuit Breaker 상태 전이
- 무결성 검증 — match/mismatch 시나리오
- Readiness 판정 로직
- State DB 백업/복원
- 컷오버 dry-run

---

## 작업 우선순위 요약

| 순위 | 항목 | 의존성 |
|------|------|--------|
| 1 | Redis 이벤트 전환 | Phase 2 완료 |
| 2 | 모니터링 API 확장 | #1 |
| 3 | Slack 알림 체계 | #2 |
| 4 | 장애 복구 자동화 | #1 |
| 5 | 성능 최적화 | #1 |
| 6 | 상시 전환 준비 | #2, #3 |
| 7 | 컷오버 실행 | #6 |
| 8 | 운영 매뉴얼 | #1~#7 |
| 9 | Git LFS 지원 | Phase 1 LFS 결정 |
| 10 | Phase 3 테스트 | #1~#9 |

## 완료 기준

- [ ] P4 submit 후 5초 이내 Git commit 생성
- [ ] Trigger 장애 시 폴링 자동 보완
- [ ] stream별 동기화 상태 API 조회 가능
- [ ] 에러 알림 심각도별 채널 분리
- [ ] 일일 리포트 발송
- [ ] Worker 재시작 후 정상 재개
- [ ] State DB 자동 백업 동작
- [ ] `/api/cutover-readiness` 동작
- [ ] 무결성 검증 주기적 실행
- [ ] Circuit breaker 동작
- [ ] dry-run 컷오버 1회 이상 실시
- [ ] 운영 매뉴얼 초안 작성
