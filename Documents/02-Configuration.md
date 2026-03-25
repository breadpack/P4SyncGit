# P4GitSync 설정 레퍼런스

## 설정 방식

설정은 두 가지 방식으로 제공됩니다. **환경변수가 config.toml보다 우선합니다.**

1. **config.toml 파일** — TOML 형식 설정 파일
2. **환경변수** — `P4GITSYNC_{SECTION}_{KEY}` 형식

### 환경변수 규칙

```
P4GITSYNC_{SECTION}_{KEY}=값
```

- SECTION: 대문자 (P4, GIT, STATE, SYNC, INITIAL_IMPORT, LOGGING, API, REDIS, SLACK, LFS, STREAM_POLICY)
- KEY: 대문자, 언더스코어 구분
- 값 자동 변환: `true`/`false` → bool, 정수 → int, 소수 → float

예시:
```bash
P4GITSYNC_P4_PORT=ssl:p4server:1666        # [p4] port
P4GITSYNC_SYNC_BATCH_SIZE=100              # [sync] batch_size
P4GITSYNC_INITIAL_IMPORT_BATCH_SIZE=200    # [initial_import] batch_size
```

---

## P4 설정 (`[p4]`)

| 키 | 환경변수 | 타입 | 기본값 | 설명 |
|----|---------|------|--------|------|
| port | `P4GITSYNC_P4_PORT` | str | `""` | P4 서버 주소 (예: `ssl:p4server:1666`) |
| user | `P4GITSYNC_P4_USER` | str | `""` | P4 서비스 계정 |
| workspace | `P4GITSYNC_P4_WORKSPACE` | str | `""` | P4 워크스페이스 이름 |
| stream | `P4GITSYNC_P4_STREAM` | str | `""` | 동기화 대상 기본 stream (예: `//YourDepot/main`) |
| filelog_batch_size | `P4GITSYNC_P4_FILELOG_BATCH_SIZE` | int | `200` | filelog 조회 배치 크기 |

---

## Git 설정 (`[git]`)

| 키 | 환경변수 | 타입 | 기본값 | 설명 |
|----|---------|------|--------|------|
| repo_path | `P4GITSYNC_GIT_REPO_PATH` | str | `""` | 로컬 Git 리포지토리 경로 |
| remote_url | `P4GITSYNC_GIT_REMOTE_URL` | str | `""` | Git remote URL (비워두면 push 안 함) |
| default_branch | `P4GITSYNC_GIT_DEFAULT_BRANCH` | str | `"main"` | Git 기본 브랜치명 |
| backend | `P4GITSYNC_GIT_BACKEND` | str | `"pygit2"` | Git 조작 백엔드 (`"pygit2"` 또는 `"cli"`) |
| bare | `P4GITSYNC_GIT_BARE` | bool | `false` | bare repository로 초기화 여부 |

### Bare Repository 모드

`bare=true` 설정 시:
- 새 리포지토리를 `git init --bare`로 초기화
- working tree 없이 Git 객체만 관리
- `remote_url`을 비워두면 로컬 전용으로 사용 가능

```toml
[git]
repo_path = "/data/my-repo.git"
bare = true
remote_url = ""
```

---

## State 설정 (`[state]`)

| 키 | 환경변수 | 타입 | 기본값 | 설명 |
|----|---------|------|--------|------|
| db_path | `P4GITSYNC_STATE_DB_PATH` | str | `""` | SQLite State DB 경로 |

---

## 동기화 설정 (`[sync]`)

| 키 | 환경변수 | 타입 | 기본값 | 설명 |
|----|---------|------|--------|------|
| polling_interval_seconds | `P4GITSYNC_SYNC_POLLING_INTERVAL_SECONDS` | int | `30` | 폴링 주기 (초) |
| batch_size | `P4GITSYNC_SYNC_BATCH_SIZE` | int | `50` | 한 번에 처리할 최대 CL 수 |
| push_after_every_commit | `P4GITSYNC_SYNC_PUSH_AFTER_EVERY_COMMIT` | bool | `false` | 매 commit마다 push (`false`이면 일괄 push) |
| file_extraction_mode | `P4GITSYNC_SYNC_FILE_EXTRACTION_MODE` | str | `"print"` | 파일 추출 방식 |
| print_to_sync_threshold | `P4GITSYNC_SYNC_PRINT_TO_SYNC_THRESHOLD` | int | `50` | batch print 전환 임계값 |
| git_gc_interval | `P4GITSYNC_SYNC_GIT_GC_INTERVAL` | int | `5000` | git gc 실행 주기 (commit 수) |
| error_retry_threshold | `P4GITSYNC_SYNC_ERROR_RETRY_THRESHOLD` | int | `3` | ERROR 알림 발송 재시도 횟수 |
| push_batch_size | `P4GITSYNC_SYNC_PUSH_BATCH_SIZE` | int | `10` | 일괄 push 단위 (commit 수) |
| push_interval_seconds | `P4GITSYNC_SYNC_PUSH_INTERVAL_SECONDS` | int | `60` | 일괄 push 최대 대기 시간 (초) |

### Push 전략

- **`push_after_every_commit=true`**: 매 commit 즉시 push. 실시간성 우선.
- **`push_after_every_commit=false`** (기본): `push_batch_size` 도달 또는 `push_interval_seconds` 경과 시 일괄 push. 성능 우선.

---

## 초기 Import 설정 (`[initial_import]`)

| 키 | 환경변수 | 타입 | 기본값 | 설명 |
|----|---------|------|--------|------|
| mode | `P4GITSYNC_INITIAL_IMPORT_MODE` | str | `"full_history"` | import 모드 |
| start_changelist | `P4GITSYNC_INITIAL_IMPORT_START_CHANGELIST` | int | `1` | 시작 CL 번호 |
| batch_size | `P4GITSYNC_INITIAL_IMPORT_BATCH_SIZE` | int | `100` | 배치 크기 |
| resume_on_restart | `P4GITSYNC_INITIAL_IMPORT_RESUME_ON_RESTART` | bool | `true` | 재시작 시 마지막 체크포인트에서 재개 |
| checkpoint_interval | `P4GITSYNC_INITIAL_IMPORT_CHECKPOINT_INTERVAL` | int | `1000` | 체크포인트 저장 주기 (CL 수) |
| use_fast_import | `P4GITSYNC_INITIAL_IMPORT_USE_FAST_IMPORT` | bool | `true` | git fast-import 사용 |
| replica_port | `P4GITSYNC_INITIAL_IMPORT_REPLICA_PORT` | str | `""` | P4 replica 서버 포트 (대규모 import 시 부하 분산) |

---

## 로깅 설정 (`[logging]`)

| 키 | 환경변수 | 타입 | 기본값 | 설명 |
|----|---------|------|--------|------|
| level | `P4GITSYNC_LOGGING_LEVEL` | str | `"INFO"` | 로그 레벨 (DEBUG, INFO, WARNING, ERROR) |
| format | `P4GITSYNC_LOGGING_FORMAT` | str | `"json"` | 로그 포맷 (`"json"` 또는 `"text"`) |
| file | `P4GITSYNC_LOGGING_FILE` | str | `""` | 로그 파일 경로 (미설정 시 stderr만) |

---

## API 설정 (`[api]`)

| 키 | 환경변수 | 타입 | 기본값 | 설명 |
|----|---------|------|--------|------|
| enabled | `P4GITSYNC_API_ENABLED` | bool | `false` | HTTP API 서버 활성화 |
| host | `P4GITSYNC_API_HOST` | str | `"127.0.0.1"` | 바인드 주소 |
| port | `P4GITSYNC_API_PORT` | int | `8080` | 포트 번호 |
| trigger_secret | `P4GITSYNC_API_TRIGGER_SECRET` | str | `""` | API 트리거 인증 시크릿 (X-Trigger-Secret 헤더) |

---

## Redis 설정 (`[redis]`)

| 키 | 환경변수 | 타입 | 기본값 | 설명 |
|----|---------|------|--------|------|
| enabled | `P4GITSYNC_REDIS_ENABLED` | bool | `false` | Redis 이벤트 시스템 사용 |
| url | `P4GITSYNC_REDIS_URL` | str | `"redis://localhost:6379/0"` | Redis 연결 URL |
| stream_key | `P4GITSYNC_REDIS_STREAM_KEY` | str | `"p4sync:events"` | Redis Stream 키 |
| group_name | `P4GITSYNC_REDIS_GROUP_NAME` | str | `"p4sync-workers"` | Consumer group 이름 |
| consumer_name | `P4GITSYNC_REDIS_CONSUMER_NAME` | str | `"worker-1"` | Consumer 이름 |
| max_stream_length | `P4GITSYNC_REDIS_MAX_STREAM_LENGTH` | int | `10000` | Stream 최대 길이 |
| block_ms | `P4GITSYNC_REDIS_BLOCK_MS` | int | `5000` | XREADGROUP 블로킹 시간 (ms) |
| batch_size | `P4GITSYNC_REDIS_BATCH_SIZE` | int | `10` | 한 번에 처리할 메시지 수 |
| heartbeat_timeout_minutes | `P4GITSYNC_REDIS_HEARTBEAT_TIMEOUT_MINUTES` | int | `30` | heartbeat 초과 시 fallback poll |
| pending_claim_timeout_hours | `P4GITSYNC_REDIS_PENDING_CLAIM_TIMEOUT_HOURS` | int | `24` | stale pending 메시지 claim 타임아웃 |

---

## Slack 알림 설정 (`[slack]`)

| 키 | 환경변수 | 타입 | 기본값 | 설명 |
|----|---------|------|--------|------|
| webhook_url | `P4GITSYNC_SLACK_WEBHOOK_URL` | str | `""` | 기본 Webhook URL |
| channel | `P4GITSYNC_SLACK_CHANNEL` | str | `"#p4sync-alerts"` | 기본 채널 |
| alerts_webhook_url | `P4GITSYNC_SLACK_ALERTS_WEBHOOK_URL` | str | `""` | ERROR 전용 Webhook |
| warnings_webhook_url | `P4GITSYNC_SLACK_WARNINGS_WEBHOOK_URL` | str | `""` | WARN 전용 Webhook |
| info_webhook_url | `P4GITSYNC_SLACK_INFO_WEBHOOK_URL` | str | `""` | INFO 전용 Webhook |
| alerts_channel | `P4GITSYNC_SLACK_ALERTS_CHANNEL` | str | `"#p4gitsync-alerts"` | ERROR 채널 |
| warnings_channel | `P4GITSYNC_SLACK_WARNINGS_CHANNEL` | str | `"#p4gitsync-warnings"` | WARN 채널 |
| info_channel | `P4GITSYNC_SLACK_INFO_CHANNEL` | str | `"#p4gitsync-info"` | INFO 채널 |
| silence_threshold_minutes | `P4GITSYNC_SLACK_SILENCE_THRESHOLD_MINUTES` | int | `30` | 침묵 장애 감지 임계값 (분) |
| daily_report_hour | `P4GITSYNC_SLACK_DAILY_REPORT_HOUR` | int | `9` | 일일 리포트 시간 (0-23) |

---

## LFS 설정 (`[lfs]`)

| 키 | 환경변수 | 타입 | 기본값 | 설명 |
|----|---------|------|--------|------|
| enabled | `P4GITSYNC_LFS_ENABLED` | bool | `false` | Git LFS 활성화 |
| extensions | — | list[str] | 아래 참조 | LFS 대상 확장자 목록 |
| size_threshold_bytes | `P4GITSYNC_LFS_SIZE_THRESHOLD_BYTES` | int | `102400` | 크기 임계값 (100KB) |
| lockable_extensions | — | list[str] | 아래 참조 | P4 exclusive lock 대상 확장자 |
| server_type | `P4GITSYNC_LFS_SERVER_TYPE` | str | `"builtin"` | LFS 서버 타입 (`"builtin"` / `"self-hosted"`) |
| server_url | `P4GITSYNC_LFS_SERVER_URL` | str | `""` | self-hosted LFS 서버 URL |

### 기본 LFS 확장자

```
.uasset, .umap, .fbx, .png, .jpg, .jpeg, .tga, .psd,
.wav, .mp3, .ogg, .mp4, .bin, .dll, .so, .exe
```

### 기본 Lockable 확장자

```
.uasset, .umap
```

### LFS 동작

- 첫 commit에서 `.gitattributes`와 `.lfsconfig` 자동 생성
- LFS 대상 파일은 실제 내용 대신 LFS 포인터로 저장
- 포인터 형식: `version https://git-lfs.github.com/spec/v1\noid sha256:{hash}\nsize {bytes}`

---

## Stream 정책 설정 (`[stream_policy]`)

| 키 | 환경변수 | 타입 | 기본값 | 설명 |
|----|---------|------|--------|------|
| auto_discover | `P4GITSYNC_STREAM_POLICY_AUTO_DISCOVER` | bool | `true` | 새 stream 자동 감지 |
| include_patterns | — | list[str] | `[]` | 포함 stream 패턴 (fnmatch, 예: `//YourDepot/*`) |
| exclude_types | — | list[str] | `[]` | 제외 stream 타입 (예: `virtual`) |
| exclude_streams | — | list[str] | `[]` | 제외 stream 목록 |
| task_stream_policy | `P4GITSYNC_STREAM_POLICY_TASK_STREAM_POLICY` | str | `"ignore"` | task stream 처리 (`"ignore"` / `"include"`) |

---

## 전체 config.toml 예시

```toml
[p4]
port = "ssl:p4server:1666"
user = "p4sync-service"
workspace = "p4sync-main"
stream = "//YourDepot/main"

[git]
repo_path = "/data/git-repo"
remote_url = "git@github.com:org/repo.git"
default_branch = "main"

[state]
db_path = "/data/state.db"

[sync]
polling_interval_seconds = 30
batch_size = 50

[initial_import]
use_fast_import = true
resume_on_restart = true

[logging]
level = "INFO"
format = "json"

[api]
enabled = true
host = "0.0.0.0"
port = 8080

[redis]
enabled = false
url = "redis://localhost:6379/0"

[slack]
webhook_url = ""

[lfs]
enabled = false

[stream_policy]
auto_discover = true
include_patterns = ["//YourDepot/*"]
exclude_types = ["virtual"]
task_stream_policy = "ignore"
```
