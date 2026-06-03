# P4GitSync CLI 사용법

## 엔트리포인트

```bash
p4gitsync [--config CONFIG_PATH] <command> [options]
```

- `--config`: 설정 파일 경로 (기본값: `config.toml`)
- 명령 미지정 시 `run`이 기본 실행됨

---

## 명령어 목록

### 실행/서비스

| 명령 | 설명 |
|------|------|
| `run` | 동기화 루프 실행 (기본) |
| `setup` | 대화형 config.toml 생성/수정 마법사 |
| `service install/start/stop/uninstall` | OS 서비스 등록·관리 |
| `status` | 동기화 상태 조회 |

### 마이그레이션·히스토리

| 명령 | 설명 |
|------|------|
| `preflight` | 마이그레이션 사전 점검 (용량·전략·case충돌·비LFS대용량) |
| `import` | 초기 히스토리 전체 import |
| `resync` | 특정 CL 범위 재동기화 |
| `rebuild-state` | Git log에서 State DB 재구성 |
| `reinit-git` | Git 리포지토리 재초기화 |
| `cutover` | P4→Git 컷오버 실행 |
| `lfs-push` | LFS 객체 배치 업로드(재개 가능) — TiB급 마이그레이션용 |

### 탐색·미리보기

| 명령 | 설명 |
|------|------|
| `tree` | P4 Stream 계층 트리 미리보기 |
| `preview` | import branch/merge 타임라인 문서 생성 |

### 거버넌스·프로비저닝

| 명령 | 설명 |
|------|------|
| `provision` | 팀 권장 설정 파일 생성 (bootstrap/훅/gitconfig) |
| `provision-gitlab` | GitLab API로 거버넌스 직접 적용 |
| `provision-github` | GitHub Rulesets API로 거버넌스 직접 적용 |
| `tune-repo` | 현재 repo에 pack/gc 튜닝 적용 |
| `bundle` | repo 번들(.bundle) 생성 — Bundle URI 초기 clone 부하 분산용 |

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
- LFS 활성 시: LFS 포인터/객체 생성 및 임시파일 정리
- import 시작 시 `[pack_tuning]` 설정을 repo에 자동 주입 (repack/gc 효율화)

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
2. commit message에서 git trailer 표준 `P4CL: NNN` 패턴 추출 (구형 `[P4CL: NNN]` 대괄호 포맷도 하위호환 파싱)
3. `Integration: //source -> //target` trailer 정보 추출 (대괄호 감싸기 선택적)
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
| 3 | INTEGRITY_VERIFY | `[cutover].verify_mode` 전략(기본 `smart`)으로 무결성 검증. `verify_workers` 병렬. LFS 파일은 로컬 LFS object MD5 ↔ P4 fstat digest 교차검증 |
| 4 | FINAL_PUSH | 모든 branch 최종 push |
| 5 | SWITCH_SOURCE | Git을 공식 소스로 지정 |

`verify_mode` 전략:

| 전략 | 동작 |
|------|------|
| `smart` (기본) | `verify_large_threshold_bytes`(기본 5MiB) 이상 파일 전수 검증 + 나머지에서 `verify_sample_count`개 샘플 |
| `full` | 전체 파일 전수 검증 |
| `sample` | 무작위 `verify_sample_count`개 샘플만 검증 |

관련 설정: `[cutover]` 섹션 — [§ 컷오버 설정](02-Configuration.md#컷오버-설정-cutover)

### Dry Run vs Execute

| 항목 | Dry Run | Execute |
|------|---------|---------|
| P4 freeze 확인 | 경고만 | 경고 후 계속 |
| 무결성 검증 | 샘플 50개 고정 | `verify_mode` 전략 적용(기본 `smart`) |
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

---

## `preflight` — 마이그레이션 사전 점검

```bash
p4gitsync preflight [--stream STREAM] [--top-dirs ...] [--large-threshold-mb N] [-o REPORT]
```

마이그레이션 전에 실행하여 용량 사이징, history 전략 권고, case-collision 탐지, 비-LFS 대용량 탐지, 로컬 디스크 여유를 점검합니다. blocker 발견 시 `exit 1`로 종료합니다.

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--stream` | config `p4.stream` | 점검 대상 P4 stream 경로 |
| `--top-dirs` | (없음) | 용량을 분류해 볼 최상위 경로 목록 (공백 구분, 예: `//depot/main/CODE //depot/main/Art`) |
| `--large-threshold-mb` | `5.0` | 비-LFS 대용량 탐지 임계 (MiB) |
| `-o`, `--output` | (없음, 콘솔 출력) | 리포트 저장 경로 |

**점검 항목**:
- 용량 사이징: head 용량 + history 전체 용량 산출
- history 전략 권고: `full` / `truncate` / `hybrid`
- case-collision 탐지: P4 대소문자 무시 → Git 대소문자 구분 충돌 파일 목록
- 비-LFS 대용량 탐지: `--large-threshold-mb` 이상 파일 중 LFS 라우팅 대상 아닌 파일
- 로컬 디스크 여유 점검: history 용량 × 1.5 기준

### 예시

```bash
# 기본 점검
p4gitsync preflight

# 특정 stream + 경로별 분류 + 리포트 저장
p4gitsync preflight --stream //YourDepot/main \
  --top-dirs //YourDepot/main/Code //YourDepot/main/Content \
  --large-threshold-mb 10 \
  -o preflight-report.md

# Docker 환경
docker compose exec p4gitsync p4gitsync preflight --stream //YourDepot/main -o /data/report.md
```

---

## `lfs-push` — LFS 객체 배치 업로드

```bash
p4gitsync lfs-push [--remote REMOTE] [--batch-size N] [--continue-on-error] [--reset-progress] [--progress-file PATH]
```

TiB급 마이그레이션 후 LFS 객체를 remote에 배치 단위로 업로드합니다. 완료 OID를 progress.json에 원자적으로 기록하므로 중단 후 재실행 시 완료된 OID를 건너뜁니다.

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--remote` | `origin` | 대상 Git remote |
| `--batch-size` | `200` | 배치당 OID 수 |
| `--continue-on-error` | false | 배치 실패 시 중단하지 않고 계속 (실패 OID는 다음 실행에서 재시도) |
| `--reset-progress` | false | 진행 상태 초기화 후 처음부터 재시작 |
| `--progress-file` | (자동 결정) | 진행 상태 파일 경로 |

### 예시

```bash
# 기본 업로드
p4gitsync lfs-push

# 배치 크기 조절 + 에러 시 계속
p4gitsync lfs-push --batch-size 100 --continue-on-error

# 진행 상태 초기화 후 재시작
p4gitsync lfs-push --reset-progress

# Docker 환경
docker compose exec p4gitsync p4gitsync lfs-push --batch-size 200
```

---

## `tree` — Stream 계층 트리 미리보기

```bash
p4gitsync tree [--depot DEPOT] [--include-deleted] [--include-virtual]
```

P4 depot의 Stream 계층 구조를 트리 형태로 출력합니다. import 전 branch 구조를 파악하는 데 사용합니다.

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--depot` | config `p4.stream`에서 추출 | P4 depot 경로 |
| `--include-deleted` | false | 삭제된 stream 포함 |
| `--include-virtual` | false | virtual stream 포함 |

### 예시

```bash
p4gitsync tree --depot //YourDepot
p4gitsync tree --depot //YourDepot --include-deleted
```

---

## `preview` — import 미리보기

```bash
p4gitsync preview [--depot DEPOT] [-o OUTPUT] [--no-merge-scan] [--merge-scan-limit N]
```

import 시 재현될 branch/merge 타임라인을 Markdown 문서로 생성합니다. 실제 import 전에 결과를 예측하는 데 사용합니다.

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--depot` | config `p4.stream`에서 추출 | P4 depot 경로 |
| `-o`, `--output` | `import-preview.md` | 출력 파일 경로 |
| `--no-merge-scan` | false | merge 스캔 생략 (branch 구조만, 빠른 미리보기) |
| `--merge-scan-limit` | `0` (전체) | stream당 merge 스캔 CL 수 제한 (0=전체) |

### 예시

```bash
p4gitsync preview --depot //YourDepot -o preview.md
p4gitsync preview --depot //YourDepot --no-merge-scan
p4gitsync preview --depot //YourDepot --merge-scan-limit 1000
```

---

## `provision` — 팀 권장 설정 파일 생성

```bash
p4gitsync provision [-o OUTPUT_DIR] [--max-file-size-mb N]
```

마이그레이션된 repo를 팀이 사용하기 위한 권장 설정 파일 묶음을 생성합니다.

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `-o`, `--output` | `provision` | 생성물 출력 디렉터리 |
| `--max-file-size-mb` | `5.0` | 비-LFS 대용량 차단 임계 (MiB) |

**생성 파일**:

| 파일 | 설명 |
|------|------|
| `bootstrap-clone.sh` / `.ps1` | 역할별 partial clone + sparse-checkout + LFS 부분 fetch + scalar 스크립트. `BUNDLE_URI` 환경변수로 번들 clone 지원 |
| `pre-receive` | 비-LFS 대용량 파일 push 차단 훅 |
| `recommended.gitconfig` | 클라이언트 권장 git 설정 |
| `recommended-repo.gitconfig` | bare/서버 repo 권장 pack 튜닝 설정 |
| `GITLAB-SETUP.md` | GitLab 거버넌스 체크리스트 |
| `.gitattributes` | LFS 활성 시 LFS 라우팅 규칙 |

### 예시

```bash
p4gitsync provision -o ./team-setup --max-file-size-mb 10

# Docker 환경
docker compose exec p4gitsync p4gitsync provision -o /data/provision
```

---

## `provision-gitlab` — GitLab 거버넌스 적용

```bash
p4gitsync provision-gitlab --gitlab-url URL --project PROJECT [--token TOKEN] [옵션...]
```

GitLab API를 호출하여 거버넌스 설정을 직접 적용합니다. 토큰 미지정 시 환경변수 `GITLAB_TOKEN` 또는 `P4GITSYNC_GITLAB_TOKEN`을 사용합니다.

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--gitlab-url` | env `P4GITSYNC_GITLAB_URL` | GitLab base URL |
| `--project` | env `P4GITSYNC_GITLAB_PROJECT` | 프로젝트 경로 또는 ID |
| `--token` | env `GITLAB_TOKEN` | GitLab 접근 토큰 |
| `--max-file-size-mb` | `5.0` | push rule 대용량 차단 임계 (MiB) |
| `--protect` | `["main"]` | 보호할 브랜치 목록 (공백 구분) |
| `--merge-train` | false | merge train 활성화 |
| `--no-lfs` | false | LFS 비활성화 (기본: LFS 활성) |
| `--dry-run` | false | 실제 API 호출 없이 적용 계획만 출력 |

**적용 항목**: push rule (`max_file_size`) + 프로젝트 설정 (LFS·merge train·파이프라인) + protected branch (force-push·직접 push 금지)

### 예시

```bash
p4gitsync provision-gitlab \
  --gitlab-url https://gitlab.company.com \
  --project mygroup/myrepo \
  --protect main develop \
  --merge-train \
  --dry-run

# 실제 적용
p4gitsync provision-gitlab \
  --gitlab-url https://gitlab.company.com \
  --project mygroup/myrepo \
  --protect main
```

---

## `provision-github` — GitHub 거버넌스 적용

```bash
p4gitsync provision-github --repo OWNER/REPO [--token TOKEN] [옵션...]
```

GitHub Rulesets API를 호출하여 거버넌스 설정을 직접 적용합니다. 설정은 멱등 upsert로 동작하며 기존 ruleset 조회 시 `per_page=100`을 사용합니다.

> **주의**: push ruleset (대용량 파일 차단)은 조직 소유 repository에서만 적용됩니다. GitHub의 LFS는 자동 지원되므로 별도 API 토글이 없습니다.

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--repo` | env `P4GITSYNC_GITHUB_REPO` | `owner/repo` 형식 |
| `--token` | env `GITHUB_TOKEN` | GitHub 접근 토큰 |
| `--api-url` | `https://api.github.com` | GitHub API base URL (GHES는 `https://HOST/api/v3`, env `P4GITSYNC_GITHUB_API_URL`) |
| `--max-file-size-mb` | `5.0` | push ruleset 대용량 차단 임계 (MB, 1~100) |
| `--protect` | `["main"]` | 보호할 브랜치 목록 (공백 구분) |
| `--merge-method` | `merge` | 허용 merge 방식 (`merge` / `squash` / `rebase`) |
| `--merge-queue` | false | merge queue 활성화 |
| `--no-require-pr` | false | PR 경유 강제 해제 (기본: PR 강제) |
| `--required-approvals` | `1` | PR 필수 승인 수 |
| `--status-check` | `[]` | 필수 status check context 목록 (공백 구분) |
| `--enforcement` | `active` | ruleset 적용 모드 (`active` / `evaluate` / `disabled`) |
| `--dry-run` | false | 실제 API 호출 없이 적용 계획만 출력 |

**적용 항목**: push ruleset (대용량 파일 차단) + branch ruleset (non-fast-forward 금지, deletion 금지, PR 필수, status check, merge queue) + repo settings (merge 방식)

### 예시

```bash
# 미리보기
p4gitsync provision-github \
  --repo myorg/myrepo \
  --protect main develop \
  --merge-method squash \
  --required-approvals 2 \
  --dry-run

# GHES + merge queue 적용
p4gitsync provision-github \
  --repo myorg/myrepo \
  --api-url https://github.company.com/api/v3 \
  --merge-queue \
  --enforcement active
```

---

## `tune-repo` — pack/gc 튜닝 적용

```bash
p4gitsync tune-repo [--dry-run]
```

`[pack_tuning]` 설정을 현재 repo의 git config에 직접 주입합니다. import 시에는 자동으로 주입되며, 기존 repo에 수동으로 적용할 때 사용합니다.

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--dry-run` | false | 적용할 설정만 출력 (실제 변경 없음) |

관련 설정: [§ Pack 튜닝 설정](02-Configuration.md#pack-튜닝-설정-pack_tuning)

### 예시

```bash
# 적용할 설정 미리보기
p4gitsync tune-repo --dry-run

# 실제 적용
p4gitsync tune-repo
```

---

## `bundle` — repo 번들 생성

```bash
p4gitsync bundle [-o OUTPUT] [--all]
```

`git bundle create`를 사용하여 repo 번들 파일을 생성합니다. 100GB+ repo의 초기 clone 부하를 Bundle URI 방식으로 오프로드할 때 사용합니다. 배포 후 `git clone --bundle-uri=<URL>` 또는 `provision`이 생성한 bootstrap 스크립트의 `BUNDLE_URI` 환경변수로 활용합니다.

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `-o`, `--output` | `repo.bundle` | 번들 출력 파일 경로 |
| `--all` | false | 모든 ref 포함 (기본: `default_branch`만) |

### 예시

```bash
# 기본 브랜치만 번들
p4gitsync bundle -o /data/repo.bundle

# 모든 ref 포함
p4gitsync bundle --all -o /data/repo-all.bundle

# 번들 배포 후 clone
git clone --bundle-uri=https://cdn.company.com/repo.bundle git@github.com:org/repo.git
```

---

## `setup` — 대화형 설정 마법사

```bash
p4gitsync setup [--config CONFIG_PATH]
```

대화형으로 `config.toml`을 생성하거나 수정합니다. config 파일 없이도 실행 가능합니다.

---

## `service` — 서비스 관리

```bash
p4gitsync service install [--name NAME]
p4gitsync service start   [--name NAME]
p4gitsync service stop    [--name NAME]
p4gitsync service uninstall [--name NAME]
```

OS 서비스(Windows: NSSM, Linux: systemd)로 등록·관리합니다.

| 서브명령 | 설명 |
|---------|------|
| `install` | 서비스 등록 |
| `start` | 서비스 시작 |
| `stop` | 서비스 중지 |
| `uninstall` | 서비스 제거 |

`--name`: 서비스 이름 (기본: `p4gitsync`)

---

## `status` — 동기화 상태 조회

```bash
p4gitsync status [--name NAME]
```

등록된 서비스의 동기화 상태를 조회합니다. `--name`으로 특정 서비스만 조회할 수 있습니다.
