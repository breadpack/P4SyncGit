# P4GitSync 운영 가이드

## 모니터링

### 상태 확인

```bash
# 서비스 상태
curl http://localhost:8080/api/health | jq .

# 동기화 현황
curl http://localhost:8080/api/status | jq .

# 컷오버 준비 상태
curl http://localhost:8080/api/cutover-readiness | jq .

# 미해결 에러
curl http://localhost:8080/api/errors | jq .
```

### 주요 로그 패턴

| 패턴 | 의미 |
|------|------|
| `CL \d+ -> commit` | 동기화 성공 |
| `Circuit breaker OPEN` | 무결성 실패, 동기화 중단 |
| `P4 연결 끊김` | P4 서버 연결 문제 |
| `push 실패` | Git push 실패 |
| `무결성 검증 실패` | P4/Git 파일 불일치 |
| `침묵 장애` | 동기화 중단 감지 |
| `Redis 연결 끊김` | Redis 연결 문제, fallback poll 전환 |

### 로그 설정

```toml
[logging]
level = "INFO"     # DEBUG로 변경하면 상세 로그 출력
format = "json"    # 또는 "text"
file = "/var/log/p4gitsync/sync.log"
```

---

## Slack 알림

### 심각도별 채널 분리

| 레벨 | 조건 | 채널 | 대응 |
|------|------|------|------|
| ERROR | CL 3회 연속 실패, 연결 실패, 무결성 실패, OOM/디스크 풀 | alerts | 즉시 대응 |
| WARN | 동기화 5분 지연, 큐 100건 초과, 디스크 부족, 침묵 장애 | warnings | 주의 관찰 |
| INFO | 컷오버 상태 변경, 신규 stream 감지, 일일 리포트 | info | 정보 확인 |

### 알림 분류 기준 (AlertClassifier)

**즉시 대응 (ERROR)**:
- 키워드 매칭: `ENOSPC`, `MemoryError`, `OOM`, `Connection` 관련
- `integrity_failure=True`
- `consecutive_failures >= 3`

**주의 관찰 (WARN)**:
- `sync_delay_minutes > 5`
- `pending_queue_size > 100`
- `disk_usage_percent > 85`

### 침묵 장애 감지

P4에 최근 활동이 있는데 동기화가 중단된 상태를 감지합니다.

- **감지 조건**: P4에 신규 CL 존재 + 마지막 동기화로부터 `silence_threshold_minutes` (기본 30분) 경과
- **알림**: WARN 레벨로 한 번만 알림 (동기화 재개 전까지 재알림 안 함)

### 일일 리포트

매일 `daily_report_hour` (기본 09:00)에 INFO 채널로 발송됩니다.

포함 정보:
- 처리된 CL 수
- 실패한 CL 수
- 동기화된 stream 목록
- 평균 동기화 시간

---

## 무결성 검증

### IntegrityChecker

P4와 Git의 파일 내용을 SHA256 해시로 비교합니다.

**검증 스케줄**:

| 주기 | 범위 | 용도 |
|------|------|------|
| 매일 | 100개 파일 샘플 | 빠른 이상 감지 |
| 매주 | 전체 파일 | 정밀 검증 |
| 매월 | 10~30% 랜덤 샘플 | 추가 검증 |

**검증 방식**:
1. `git ls-tree -r HEAD`로 Git 파일 목록 조회
2. 각 파일의 Git 내용과 P4 `#head` 내용의 SHA256 비교
3. 불일치 파일 로깅 및 결과 반환

### Circuit Breaker

무결성 검증 실패 시 자동으로 동기화를 중단합니다.

**상태 전이**:
```
CLOSED (정상) ──무결성 실패──> OPEN (동기화 중단)
OPEN ──────────검증 성공──> CLOSED (자동 복구)
OPEN ──────────수동 리셋──> CLOSED
```

**OPEN 상태**:
- `allow_sync()` → `False` 반환 → 폴링 루프에서 동기화 건너뜀
- Slack ERROR 알림 + 불일치 파일 상세
- 다음 주기 검증 성공 시 자동 CLOSED 복구

**수동 리셋**: Circuit Breaker가 열린 상태에서 원인 조치 후 서비스 재시작

---

## 장애 복구

### 실패한 CL 재시도

**API를 통한 재시도**:
```bash
curl -X POST http://localhost:8080/api/retry/12345
```

**CLI를 통한 재동기화**:
```bash
# 단일 CL
p4gitsync resync --from 12345 --to 12345

# 범위 재동기화
p4gitsync resync --from 12000 --to 12100
```

### State DB 손상 복구

Git 커밋 로그에서 State DB를 재구성합니다.

```bash
p4gitsync rebuild-state
```

- commit message의 `[P4CL: NNN]` 메타데이터를 파싱
- 모든 CL ↔ commit SHA 매핑 복구
- integration 정보도 함께 복구

### Git 리포지토리 손상 복구

```bash
# remote에서 다시 clone
p4gitsync reinit-git --remote git@github.com:org/repo.git
```

- 기존 repo를 `{path}.backup.{timestamp}`로 백업
- remote에서 새로 clone
- 실패 시 backup에서 자동 복원

### 정합성 불일치 해결

서비스 시작 시 Git HEAD와 StateStore의 일치 여부를 검증합니다.

불일치 시:
1. 에러 로그 + Slack 알림
2. 서비스 시작 중단
3. 수동 조치 필요:
   - `rebuild-state`로 State DB 재구성, 또는
   - `reinit-git`로 Git repo 재초기화

### 미완료 Push 재시도

서비스 시작 시 `git_push_status='pending'`인 항목을 자동으로 재시도합니다.

---

## State DB 관리

### 직접 접근

```bash
sqlite3 /path/to/state.db
```

### 주요 테이블

**sync_state** — Stream별 마지막 동기화 상태:
```sql
SELECT * FROM sync_state;
-- stream | last_cl | commit_sha | updated_at
```

**cl_commit_map** — CL ↔ commit SHA 매핑:
```sql
SELECT * FROM cl_commit_map WHERE stream = '//depot/main' ORDER BY changelist DESC LIMIT 10;
-- changelist | commit_sha | stream | branch | has_integration | git_push_status | created_at
```

**sync_errors** — 동기화 에러 기록:
```sql
SELECT * FROM sync_errors WHERE resolved = 0;
-- id | changelist | stream | error_msg | retry_count | resolved | created_at
```

**stream_registry** — 다중 stream 등록:
```sql
SELECT * FROM stream_registry;
-- stream | branch | parent_stream | branch_point_cl
```

### 사용자 매핑 관리

P4 사용자와 Git author 매핑:

```sql
-- 조회
SELECT * FROM user_mappings;

-- 추가/변경
INSERT OR REPLACE INTO user_mappings (p4_user, git_name, git_email)
VALUES ('jsmith', 'John Smith', 'john@company.com');

-- 일괄 추가
INSERT OR REPLACE INTO user_mappings VALUES
('alice', 'Alice Kim', 'alice@company.com'),
('bob', 'Bob Lee', 'bob@company.com');
```

매핑이 없는 P4 사용자는 `{p4_user}@{default_domain}` 형태로 자동 생성됩니다.

### Stream 수동 등록/제거

```sql
-- 추가
INSERT INTO stream_registry (stream, branch, parent_stream, branch_point_cl)
VALUES ('//depot/feature', 'feature', '//depot/main', 12345);

-- 제거
DELETE FROM stream_registry WHERE stream = '//depot/feature';
DELETE FROM sync_state WHERE stream = '//depot/feature';
```

### DB 백업

`DatabaseBackup` 컴포넌트가 자동으로 SQLite DB를 백업합니다. 수동 백업:

```bash
sqlite3 /path/to/state.db ".backup /path/to/backup.db"
```

---

## 컷오버 절차

### 사전 준비

1. **동기화 지연 확인**: `curl /api/cutover-readiness`로 `total_lag=0` 확인
2. **미해결 에러 처리**: `curl /api/errors`로 에러 목록 확인 및 해결
3. **무결성 검증 통과**: Circuit Breaker가 CLOSED 상태인지 확인

### Dry Run

```bash
p4gitsync cutover --dry-run
```

실제 변경 없이 모든 체크를 실행합니다. 결과를 확인하고 문제가 없으면 실제 실행합니다.

### 실행

```bash
# 1. P4 submit 차단 (P4 admin)
p4 configure set submit.disable=1

# 2. 컷오버 실행
p4gitsync cutover --execute

# 3. 결과 확인 후 P4 사용 중단
```

### 실패 시

- 실패한 phase에서 중단됨
- 에러 메시지에 원인 표시
- 원인 해결 후 `--execute` 재실행 가능
- P4 submit 차단 해제 필요 시: `p4 configure set submit.disable=0`

---

## systemd 서비스 등록

```ini
[Unit]
Description=P4GitSync Service
After=network.target redis.service

[Service]
Type=simple
User=p4sync
ExecStart=/opt/p4gitsync/venv/bin/python -m p4gitsync --config /app/config.toml run
Restart=on-failure
RestartSec=10
Environment=P4GITSYNC_LOGGING_LEVEL=INFO

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable p4gitsync
sudo systemctl start p4gitsync
sudo systemctl status p4gitsync
sudo journalctl -u p4gitsync -f
```
