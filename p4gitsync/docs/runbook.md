# P4GitSync 운영 매뉴얼 (Runbook)

## 1. 서비스 시작/중지/재시작

### 시작
```bash
python -m p4gitsync --config config.toml run
```

### 백그라운드 실행 (systemd)
```ini
# /etc/systemd/system/p4gitsync.service
[Unit]
Description=P4GitSync Service
After=network.target redis.service

[Service]
Type=simple
User=p4sync
WorkingDirectory=/opt/p4gitsync
ExecStart=/opt/p4gitsync/venv/bin/python -m p4gitsync --config config.toml run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl start p4gitsync
sudo systemctl stop p4gitsync
sudo systemctl restart p4gitsync
sudo systemctl status p4gitsync
```

### 중지
- `SIGINT` (Ctrl+C) 또는 `SIGTERM`으로 graceful shutdown
- 현재 처리 중인 CL 완료 후 종료

---

## 2. 로그 확인

### 로그 설정 (config.toml)
```toml
[logging]
level = "INFO"       # DEBUG, INFO, WARNING, ERROR
format = "json"      # "json" 또는 "text"
file = "/var/log/p4gitsync/sync.log"
```

### 실시간 로그 모니터링
```bash
tail -f /var/log/p4gitsync/sync.log | jq .
```

### 주요 로그 패턴
| 패턴 | 의미 |
|------|------|
| `CL \d+ -> commit` | CL 동기화 성공 |
| `Circuit breaker OPEN` | 무결성 실패로 동기화 중단 |
| `P4 연결 끊김` | P4 서버 연결 문제 |
| `push 실패` | Git remote push 실패 |
| `무결성 검증 실패` | P4/Git 파일 불일치 감지 |

---

## 3. API 엔드포인트

API 서버가 활성화된 경우 (`api.enabled = true`):

| 엔드포인트 | 메서드 | 설명 |
|-----------|--------|------|
| `/api/health` | GET | 서비스 상태 |
| `/api/status` | GET | Stream별 동기화 상태 |
| `/api/errors` | GET | 미해결 에러 목록 |
| `/api/cutover-readiness` | GET | 컷오버 준비 상태 |
| `/api/trigger` | POST | 수동 동기화 트리거 |
| `/api/retry/{changelist}` | POST | 실패 CL 재시도 |

### 컷오버 준비 상태 확인
```bash
curl http://localhost:8080/api/cutover-readiness | jq .
```
응답 예시:
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

---

## 4. User Mapping 변경

### State DB에서 직접 변경
```bash
sqlite3 /path/to/state.db
```
```sql
-- 매핑 조회
SELECT * FROM user_mappings;

-- 매핑 추가/변경
INSERT OR REPLACE INTO user_mappings (p4_user, git_name, git_email)
VALUES ('jsmith', 'John Smith', 'john.smith@company.com');
```

### 초기 매핑 자동 생성
`import` 명령 실행 시 P4 사용자 목록에서 자동 매핑이 생성됩니다.

---

## 5. Stream 수동 추가/제거

### Stream 추가
```sql
-- State DB에 stream 등록
INSERT INTO stream_registry (stream, branch, parent_stream, branch_point_cl)
VALUES ('//depot/feature', 'feature', '//depot/main', 12345);
```
서비스 재시작 후 자동으로 해당 stream 동기화를 시작합니다.

### Stream 제거
```sql
-- stream 등록 해제
DELETE FROM stream_registry WHERE stream = '//depot/feature';

-- 관련 동기화 상태 정리 (선택)
DELETE FROM sync_state WHERE stream = '//depot/feature';
DELETE FROM cl_commit_map WHERE stream = '//depot/feature';
```

### 자동 감지 설정 (config.toml)
```toml
[stream_policy]
auto_discover = true
include_patterns = ["//depot/*"]
exclude_types = ["virtual"]
task_stream_policy = "ignore"
```

---

## 6. 실패 CL 재시도

### API 사용
```bash
curl -X POST http://localhost:8080/api/retry/12345
```

### 수동 재시도 (CLI)
```bash
python -m p4gitsync --config config.toml resync --from 12345 --to 12345
```

### 범위 재동기화
```bash
python -m p4gitsync --config config.toml resync --from 12300 --to 12400
```

### 에러 해결 표시 (DB)
```sql
UPDATE sync_errors SET resolved = 1
WHERE changelist = 12345 AND stream = '//depot/main';
```

---

## 7. State DB 백업/복원

### 자동 백업
`DatabaseBackup`이 주기적으로 `{db_path}.backup` 파일을 생성합니다.

### 수동 백업
```bash
sqlite3 /path/to/state.db ".backup /path/to/backup.db"
```

### 복원
```bash
# 1. 서비스 중지
sudo systemctl stop p4gitsync

# 2. 현재 DB 이동
mv /path/to/state.db /path/to/state.db.old

# 3. 백업에서 복원
cp /path/to/backup.db /path/to/state.db

# 4. 서비스 시작
sudo systemctl start p4gitsync
```

### Git에서 State DB 재구성
State DB가 완전히 손실된 경우:
```bash
python -m p4gitsync --config config.toml rebuild-state
```

### Git 리포지토리 재초기화
Git 리포지토리가 손상된 경우:
```bash
python -m p4gitsync --config config.toml reinit-git --remote git@github.com:org/repo.git
```

---

## 8. 컷오버 절차

### 사전 확인 (Dry Run)
```bash
python -m p4gitsync cutover --config config.toml --dry-run
```

### 컷오버 실행
```bash
# 1. P4 admin에서 submit 차단 설정
# 2. 컷오버 실행
python -m p4gitsync cutover --config config.toml --execute
```

### 컷오버 실행 단계
1. **Freeze Check**: P4 submit 차단 확인
2. **Final Sync**: 잔여 CL 동기화, total_lag=0 확인
3. **Integrity Verify**: 전체 파일 무결성 검증
4. **Final Push**: 모든 branch 최종 push
5. **Switch Source**: Git을 공식 소스로 지정

### Rollback 전략
기본 전략은 **Forward Fix**입니다.
- 컷오버 실패 시 P4 submit 차단을 해제하고 동기화를 재개합니다
- Git에 이미 push된 내용은 revert commit으로 수정합니다

---

## 9. 긴급 대응 의사결정 트리

```
문제 발생
  |
  +-- 동기화 중단?
  |     |
  |     +-- Circuit Breaker OPEN?
  |     |     -> 무결성 검증 실패. 아래 "무결성 실패" 참조
  |     |
  |     +-- P4 연결 실패?
  |     |     -> 자동 재연결 3회 시도. 실패 시 서비스 재시작
  |     |
  |     +-- Git push 실패?
  |           -> 네트워크/인증 확인. 미완료 push는 재시작 시 자동 재시도
  |
  +-- 특정 CL 반복 실패?
  |     |
  |     +-- retry_count >= 3?
  |     |     -> Slack ERROR 알림. 수동 확인 필요
  |     |
  |     +-- 파일 내용 추출 실패?
  |     |     -> P4 obliterate 여부 확인. 건너뛸 수 없으면 에러 해결 표시 후 진행
  |     |
  |     +-- merge 분석 실패?
  |           -> 일반 commit으로 자동 fallback. 로그 확인
  |
  +-- 무결성 실패?
  |     |
  |     +-- 소수 파일 불일치?
  |     |     -> LFS 포인터 변환 문제 확인. 해당 파일 수동 비교
  |     |
  |     +-- 대규모 불일치?
  |           -> resync 또는 reinit-git으로 복구
  |
  +-- 성능 저하?
        |
        +-- P4 서버 과부하?
        |     -> server_load_threshold 조정. 자동 throttling 확인
        |
        +-- Git GC 필요?
              -> git_gc_interval 설정 확인 (기본: 5000 commits)
```

---

## 10. Redis 이벤트 시스템

### Redis 설정 (config.toml)
```toml
[redis]
enabled = true
url = "redis://localhost:6379/0"
stream_key = "p4sync:events"
group_name = "p4sync-workers"
consumer_name = "worker-1"
```

### Redis Stream 모니터링
```bash
# Stream 정보
redis-cli XINFO STREAM p4sync:events

# Consumer Group 상태
redis-cli XINFO GROUPS p4sync:events

# Pending 메시지 확인
redis-cli XPENDING p4sync:events p4sync-workers
```

---

## 11. Slack 알림 설정

### 채널별 Webhook 설정 (config.toml)
```toml
[slack]
alerts_webhook_url = "https://hooks.slack.com/services/..."    # ERROR
warnings_webhook_url = "https://hooks.slack.com/services/..."  # WARN
info_webhook_url = "https://hooks.slack.com/services/..."      # INFO
silence_threshold_minutes = 30
daily_report_hour = 9
```

### 알림 레벨
| 채널 | 레벨 | 내용 |
|------|------|------|
| alerts | ERROR | 동기화 실패, 무결성 실패, 연결 끊김 |
| warnings | WARN | 동기화 지연, 큐 과부하, 디스크 임계값, 침묵 장애 |
| info | INFO | 컷오버 상태, 신규 stream, 일일 리포트 |

---

## 12. LFS 설정

### LFS 설정 (config.toml)
```toml
[lfs]
enabled = true
extensions = [".uasset", ".umap", ".fbx", ".png", ".jpg"]
lockable_extensions = [".uasset", ".umap"]
server_type = "builtin"       # "builtin" (GitHub/GitLab) 또는 "self-hosted"
server_url = ""               # self-hosted 시 LFS 서버 URL
size_threshold_bytes = 102400  # 100KB
```

- `lockable_extensions`: P4 `+l` (exclusive lock)에 해당하는 파일. `.gitattributes`에 `lockable` 속성 추가
- `server_type = "self-hosted"` 시 `.lfsconfig` 파일이 자동 생성됨
- `.gitattributes`는 첫 번째 commit에 자동 포함됨
