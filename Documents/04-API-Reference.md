# P4GitSync API 레퍼런스

## 개요

FastAPI 기반 HTTP API 서버. 동기화 상태 조회, 수동 트리거, 에러 관리 기능을 제공합니다.

### 활성화

```toml
[api]
enabled = true
host = "0.0.0.0"
port = 8080
trigger_secret = "your-secret"  # 선택
```

### 인증

`trigger_secret`이 설정된 경우, `/api/trigger`와 `/api/retry/{changelist}` 요청에 `X-Trigger-Secret` 헤더가 필요합니다.

```bash
curl -H "X-Trigger-Secret: your-secret" -X POST ...
```

잘못된 시크릿 시 `403 Forbidden` 반환.

---

## 엔드포인트

### GET /api/health

서비스 상태 확인.

**응답 (200)**:
```json
{
  "status": "ok",
  "last_trigger_time": "2024-03-25T14:30:45.123456"
}
```

- `last_trigger_time`: 마지막 트리거 시간 (없으면 `null`)

---

### GET /api/status

Stream별 동기화 상태, Redis 상태, 프로세스 정보 조회.

**응답 (200)**:
```json
{
  "streams": [
    {
      "stream": "//depot/main",
      "branch": "main",
      "last_synced_cl": 12345,
      "parent_stream": null
    },
    {
      "stream": "//depot/develop",
      "branch": "develop",
      "last_synced_cl": 12340,
      "parent_stream": "//depot/main"
    }
  ],
  "redis": {
    "stream_length": 10,
    "groups": 1,
    "pending_messages": 2,
    "memory_used_mb": 2.45
  },
  "process": {
    "pid": 1234,
    "trigger_count": 42,
    "last_trigger_time": "2024-03-25T14:30:45.123456",
    "uptime_info": "running"
  }
}
```

- `redis`: Redis 비활성화 시 `null`
- `streams`: 등록된 모든 stream의 동기화 현황

---

### GET /api/errors

미해결 동기화 에러 목록 조회.

**응답 (200)**:
```json
[
  {
    "changelist": 12345,
    "stream": "//depot/main",
    "error_msg": "파일 추출 실패: //depot/main/src/file.cpp#3",
    "retry_count": 3,
    "created_at": "2024-03-25T14:30:45"
  }
]
```

---

### GET /api/cutover-readiness

컷오버 준비 상태 및 차단 요인 조회.

**응답 (200)**:
```json
{
  "ready": true,
  "total_lag": 0,
  "unresolved_errors": 0,
  "integrity_passed": true,
  "last_sync_seconds_ago": 15.2,
  "blockers": []
}
```

**Blockers 예시** (준비되지 않은 경우):
```json
{
  "ready": false,
  "total_lag": 5,
  "unresolved_errors": 2,
  "integrity_passed": false,
  "last_sync_seconds_ago": 300.0,
  "blockers": [
    "동기화 지연: 5 CL 미처리",
    "미해결 에러 2건",
    "무결성 검증 실패"
  ]
}
```

---

### POST /api/trigger

P4 changelist 동기화를 수동으로 트리거합니다. P4 Trigger 스크립트에서 호출하는 용도입니다.

**요청**:
```json
{
  "changelist": 12345,
  "user": "john"
}
```

**응답 (202 Accepted)**:

Redis 활성화 시:
```json
{
  "status": "accepted",
  "changelist": 12345,
  "redis_msg_id": "1234567890-0"
}
```

Redis 비활성화 시:
```json
{
  "status": "accepted",
  "changelist": 12345
}
```

### P4 Trigger 스크립트 예시

```bash
#!/bin/bash
# P4 trigger로 등록: submit-commit 타입
CL=$1
USER=$2
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "X-Trigger-Secret: your-secret" \
  -d "{\"changelist\": $CL, \"user\": \"$USER\"}" \
  http://p4gitsync:8080/api/trigger
```

---

### POST /api/retry/{changelist}

실패한 CL의 동기화를 수동으로 재시도합니다.

**경로 파라미터**:
- `changelist`: 재시도할 CL 번호

**응답 (200)** — 성공:
```json
{
  "status": "retried",
  "changelist": 12345,
  "commit_sha": "abc123def456..."
}
```

**응답 (500)** — 실패:
```json
{
  "status": "failed",
  "changelist": 12345,
  "error": "파일 추출 실패"
}
```

---

## Redis 모니터링

API 서버는 Redis 상태도 모니터링합니다 (`api/redis_monitor.py`).

`/api/status` 응답의 `redis` 필드에서 확인 가능:

| 필드 | 설명 |
|------|------|
| stream_length | Redis Stream의 현재 메시지 수 |
| groups | Consumer group 수 |
| pending_messages | 아직 ACK되지 않은 메시지 수 |
| memory_used_mb | Redis 메모리 사용량 (MB) |
