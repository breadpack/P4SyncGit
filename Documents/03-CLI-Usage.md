# P4GitSync CLI 사용법

## 엔트리포인트

```bash
p4gitsync [--config CONFIG_PATH] <command> [options]
```

- `--config`: 설정 파일 경로 (기본값: `config.toml`)
- 명령 미지정 시 `run`이 기본 실행됨

---

## 명령어 목록

| 명령 | 설명 |
|------|------|
| `run` | 동기화 루프 실행 (기본) |
| `import` | 초기 히스토리 전체 import |
| `resync` | 특정 CL 범위 재동기화 |
| `rebuild-state` | Git log에서 State DB 재구성 |
| `reinit-git` | Git 리포지토리 재초기화 |
| `cutover` | P4→Git 컷오버 실행 |

---

## `run` — 동기화 루프 실행

```bash
p4gitsync run
```

기본 명령. 서비스를 시작하고 지속적으로 P4 변경사항을 Git에 동기화합니다.

**동작 순서**:
1. 모든 컴포넌트 초기화 (P4Client, GitOperator, StateStore 등)
2. 정합성 검증 (Git HEAD와 StateStore 일치 확인)
3. API 서버 시작 (활성화 시)
4. Redis EventConsumer 시작 (활성화 시)
5. 단일/다중 stream 모드에 따라 폴링 루프 실행

**종료 방식**: `SIGINT` (Ctrl+C) 또는 `SIGTERM` → 현재 CL 처리 후 graceful shutdown

### Docker 환경

```bash
# 서비스 시작
docker compose up -d

# 로그 확인
docker compose logs -f p4gitsync

# 서비스 중지
docker compose down
```

---

## `import` — 초기 히스토리 Import

```bash
p4gitsync import [--stream STREAM_PATH]
```

P4 Stream의 전체 히스토리를 Git으로 일괄 변환합니다.

| 옵션 | 필수 | 설명 |
|------|------|------|
| `--stream` | 아니오 | P4 stream 경로 (미지정 시 config의 `p4.stream` 사용) |

**동작**:
- `git fast-import`를 사용한 고속 변환
- 체크포인트 단위로 진행 상태 저장
- 중단 시 마지막 체크포인트에서 자동 재개 (`resume_on_restart=true`)
- P4 서버 부하 감지 시 자동 throttle (60초 대기)

### 예시

```bash
# config의 p4.stream 사용
p4gitsync import

# 특정 stream 지정
p4gitsync import --stream //YourDepot/main

# Docker 환경
docker compose exec p4gitsync p4gitsync import --stream //YourDepot/main
```

### 관련 설정

```toml
[initial_import]
mode = "full_history"
start_changelist = 1        # 시작 CL (기본: 전체)
batch_size = 100             # 배치 크기
resume_on_restart = true     # 재개 지원
checkpoint_interval = 1000   # 체크포인트 주기
use_fast_import = true       # fast-import 사용
```

---

## `resync` — CL 범위 재동기화

```bash
p4gitsync resync --from FROM_CL --to TO_CL [--stream STREAM_PATH]
```

특정 Changelist 범위를 다시 동기화합니다. 손상된 commit 복구에 사용합니다.

| 옵션 | 필수 | 설명 |
|------|------|------|
| `--from` | 예 | 시작 CL 번호 |
| `--to` | 예 | 종료 CL 번호 |
| `--stream` | 아니오 | P4 stream 경로 |

### 예시

```bash
# CL 12000~12100 재동기화
p4gitsync resync --from 12000 --to 12100

# 특정 stream만
p4gitsync resync --from 12000 --to 12100 --stream //YourDepot/main

# 단일 CL 재동기화
p4gitsync resync --from 12345 --to 12345

# Docker 환경
docker compose exec p4gitsync p4gitsync resync --from 12000 --to 12100
```

---

## `rebuild-state` — State DB 재구성

```bash
p4gitsync rebuild-state
```

Git 커밋 로그에서 P4CL 메타데이터를 추출하여 State DB를 재구성합니다.

**동작**:
1. `git log --format=%H%n%B --reverse`로 모든 commit 조회
2. commit message에서 `[P4CL: NNN]` 패턴 추출
3. `[Integration: //source -> //target]` 정보 추출
4. StateStore에 매핑 복구

**용도**:
- State DB 손상 시
- 새 환경에서 기존 Git repo와 연결 시

### 예시

```bash
p4gitsync rebuild-state

# Docker 환경
docker compose exec p4gitsync p4gitsync rebuild-state
```

---

## `reinit-git` — Git 리포지토리 재초기화

```bash
p4gitsync reinit-git --remote REMOTE_URL
```

Git 리포지토리를 remote에서 다시 클론하여 재초기화합니다.

| 옵션 | 필수 | 설명 |
|------|------|------|
| `--remote` | 예 | Git remote URL |

**동작**:
1. 기존 repo를 `{repo_path}.backup.{timestamp}`로 이동
2. Remote에서 `git clone`
3. 실패 시 backup에서 복원

**용도**:
- Git 리포지토리 손상 시
- remote과 로컬 repo가 불일치할 때

### 예시

```bash
p4gitsync reinit-git --remote git@github.com:org/repo.git

# Docker 환경
docker compose exec p4gitsync p4gitsync reinit-git --remote git@github.com:org/repo.git
```

---

## `cutover` — P4→Git 컷오버

```bash
p4gitsync cutover --dry-run
p4gitsync cutover --execute
```

P4에서 Git으로의 전환(컷오버)을 실행합니다. `--dry-run`과 `--execute` 중 하나를 반드시 지정해야 합니다.

| 옵션 | 설명 |
|------|------|
| `--dry-run` | 시뮬레이션 (실제 변경 없음, 샘플 검증) |
| `--execute` | 실제 컷오버 실행 |

### 5단계 프로세스

| 단계 | Phase | 설명 |
|------|-------|------|
| 1 | FREEZE_CHECK | P4 submit 차단 확인 |
| 2 | FINAL_SYNC | 잔여 CL 동기화, total_lag=0 확인 |
| 3 | INTEGRITY_VERIFY | 전체 파일 무결성 검증 |
| 4 | FINAL_PUSH | 모든 branch 최종 push |
| 5 | SWITCH_SOURCE | Git을 공식 소스로 지정 |

### Dry Run vs Execute

| 항목 | Dry Run | Execute |
|------|---------|---------|
| P4 freeze 확인 | 경고만 | 필수 |
| 무결성 검증 | 샘플 50개 | 전체 파일 |
| 최종 push | 미실행 | 실행 |
| 소스 전환 | 미실행 | 실행 |

### 예시

```bash
# 시뮬레이션
p4gitsync cutover --dry-run

# 실제 실행 (P4 submit 차단 후)
p4gitsync cutover --execute

# Docker 환경
docker compose exec p4gitsync p4gitsync cutover --dry-run
docker compose exec p4gitsync p4gitsync cutover --execute
```

### 출력 예시

```
==================================================
결과: 컷오버 완료
Phase: switch_source
  - P4 freeze 확인됨
  - 잔여 CL 동기화 완료 (total_lag=0)
  - 무결성 검증 통과 (0 mismatches)
  - 모든 branch push 완료
  - Git을 공식 소스로 지정
==================================================
```
