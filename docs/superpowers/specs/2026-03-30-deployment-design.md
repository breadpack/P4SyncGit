# P4GitSync 배포 환경 설계

## 목표

OS 플랫폼별(Windows/Linux) 설치 후 서비스로 실행 가능한 배포 환경 구축.
대화형 CLI로 config 생성, 서비스 등록/관리, 동기화 상태 조회를 제공한다.

## 대상 플랫폼

| OS | 배포 형태 | 서비스 관리 |
|----|----------|------------|
| Windows | PyInstaller exe | NSSM |
| Linux | PyInstaller exe 또는 Docker | systemd |

## CLI 명령 구조

```
p4gitsync setup [--config PATH]                          # config.toml 대화형 생성/수정
p4gitsync run [--config PATH]                            # 포그라운드 실행 (기존)

p4gitsync service install [--config PATH] [--name NAME]  # 서비스 등록
p4gitsync service start [--name NAME]                    # 서비스 시작
p4gitsync service stop [--name NAME]                     # 서비스 중지
p4gitsync service uninstall [--name NAME]                # 서비스 제거

p4gitsync status [--name NAME]                           # 동기화 상태 조회

p4gitsync import [--config PATH]                         # 초기 히스토리 import (기존)
p4gitsync resync [--config PATH]                         # CL 범위 재동기화 (기존)
p4gitsync rebuild-state [--config PATH]                  # State DB 재구성 (기존)
p4gitsync tree [--config PATH]                           # Stream 트리 미리보기 (기존)
p4gitsync preview [--config PATH]                        # Import 미리보기 (기존)
```

---

## 1. `p4gitsync setup` — 대화형 config 생성/수정

### 신규 생성 모드

기존 config.toml이 없으면 단계별로 설정을 수집한다.

```
$ p4gitsync setup --config ./config.toml

[1/5] P4 서버 설정
  P4 서버 주소 (예: ssl:p4server:1666): p4server01:1666
  P4 계정: CODE
  P4 비밀번호: ****
  P4 Stream (예: //depot/main): //stream/devmini

  연결 테스트 중... OK (virtual stream 감지: parent=//stream/dev, excludes=6개)

[2/5] Git 저장소 설정
  Git repo 경로: d:/Projects/CS_CODE.git
  Bare repo? (Y/n): Y
  기본 브랜치 (main): main

[3/5] 동기화 설정
  동기화 방향 (p4_to_git / git_to_p4 / bidirectional): bidirectional
  폴링 간격 (초, 기본 30): 30

[4/5] LFS 설정
  LFS 활성화? (Y/n): Y
  P4 typemap에서 binary 확장자 자동 감지... 14개 발견
    .png .jpg .ogg .wav .dll .so .exe .psd .xlsx .bytes .spine .pptx .docx .bin
  추가할 확장자 (쉼표 구분, 없으면 Enter):
  제외할 확장자 (쉼표 구분, 없으면 Enter): .prefab

[5/5] 설정 저장
  ./config.toml 저장 완료
```

### 수정 모드

기존 config.toml이 있으면 메뉴로 섹션 선택 후 변경.

```
$ p4gitsync setup --config ./config.toml

기존 설정 파일 감지: ./config.toml
변경할 섹션을 선택하세요:
  1. P4 서버 설정
  2. Git 저장소 설정
  3. 동기화 방향/정책
  4. LFS 설정
  5. API/알림 설정
  0. 완료 (저장)
>
```

### 구현 세부

- `p4gitsync/src/p4gitsync/cli/setup_wizard.py` — 대화형 wizard
- P4 연결 테스트: `P4Config.create_client()` → `connect()` → `resolve_virtual_stream()`
- LFS 확장자 감지: `p4 typemap -o`에서 binary 타입 추출 + 최근 CL에서 binary file_type 확장자 수집
- config 직렬화: `tomli_w` 또는 수동 TOML 생성 (tomllib은 읽기 전용)
- 비밀번호: 입력 시 `getpass.getpass()` 사용, TOML에 평문 저장 (내부 도구)

---

## 2. `p4gitsync service` — 서비스 관리

### 서비스 이름 규칙

- 기본: `p4gitsync`
- 다중 인스턴스: `--name`으로 지정 (예: `p4gitsync-devmini`, `p4gitsync-main`)

### 서비스 레지스트리

서비스 등록 정보를 `~/.p4gitsync/services.json`에 저장:

```json
{
  "p4gitsync-devmini": {
    "config": "d:\\p4gitsync\\config-devmini.toml",
    "name": "p4gitsync-devmini",
    "installed_at": "2026-03-30T10:00:00",
    "platform": "windows"
  }
}
```

### Windows (NSSM)

#### `service install`

1. NSSM이 없으면 자동 다운로드 (GitHub releases → `~/.p4gitsync/nssm.exe`)
2. `nssm install <name> <exe_path> --config <config_path> run`
3. `nssm set <name> AppStdout <log_dir>\output.log`
4. `nssm set <name> AppStderr <log_dir>\error.log`
5. `nssm set <name> AppRotateFiles 1`
6. `nssm set <name> AppRotateSeconds 86400`
7. `nssm set <name> Start SERVICE_AUTO_START`
8. `nssm set <name> AppRestartDelay 10000`
9. services.json에 등록

#### `service start/stop`

```
nssm start <name>
nssm stop <name>
```

#### `service uninstall`

```
nssm stop <name>
nssm remove <name> confirm
```

### Linux (systemd)

#### `service install`

1. systemd unit 파일 생성: `/etc/systemd/system/<name>.service`
2. `systemctl daemon-reload`
3. `systemctl enable <name>`
4. services.json에 등록

Unit 파일 템플릿:

```ini
[Unit]
Description=P4GitSync - {name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=p4sync
Group=p4sync
ExecStart={exe_path} --config {config_path} run
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier={name}

[Install]
WantedBy=multi-user.target
```

#### `service start/stop`

```
systemctl start <name>
systemctl stop <name>
```

#### `service uninstall`

```
systemctl stop <name>
systemctl disable <name>
rm /etc/systemd/system/<name>.service
systemctl daemon-reload
```

### 구현 세부

- `p4gitsync/src/p4gitsync/cli/service_manager.py` — 플랫폼별 서비스 관리
- `ServiceManager` 추상 클래스 + `WindowsServiceManager(NSSM)`, `LinuxServiceManager(systemd)` 구현체
- 플랫폼 감지: `sys.platform`
- 관리자 권한 필요 시 안내 메시지 (Windows: "관리자 권한으로 실행하세요", Linux: "sudo 필요")

---

## 3. `p4gitsync status` — 동기화 상태 조회

### 전체 조회

```
$ p4gitsync status

등록된 동기화 서비스:
┌──────────────────────┬──────────┬───────────────────┬────────────┬──────────┐
│ 이름                 │ 상태     │ Stream            │ Last CL    │ 가동시간 │
├──────────────────────┼──────────┼───────────────────┼────────────┼──────────┤
│ p4gitsync-devmini    │ ● 실행중 │ //stream/devmini  │ 334650     │ 2일 3시간│
│ p4gitsync-main       │ ○ 중지   │ //stream/main     │ 280100     │ -        │
└──────────────────────┴──────────┴───────────────────┴────────────┴──────────┘
```

### 개별 상세 조회

```
$ p4gitsync status --name p4gitsync-devmini

서비스: p4gitsync-devmini
  상태:        ● 실행중 (PID 6668)
  Config:      d:\p4gitsync\config-devmini.toml
  Stream:      //stream/devmini (virtual → //stream/dev)
  방향:        bidirectional
  Git repo:    d:\Projects\CS_CODE_kwonsanggoo.git
  Last CL:     334650
  총 Commit:   864
  LFS:         활성화 (16 확장자)
  가동시간:    2일 3시간 15분
  API:         http://localhost:8081
```

### 데이터 소스

- 서비스 상태: NSSM/systemd 조회
- PID/가동시간: 플랫폼별 프로세스 정보
- Stream/방향/LFS: config.toml 파싱
- Last CL/총 Commit: state.db 조회
- Git repo 크기: `git count-objects -vH` (선택)

### 구현 세부

- `p4gitsync/src/p4gitsync/cli/status_reporter.py`
- `services.json`에서 등록된 서비스 목록 조회
- 각 서비스의 config.toml → state.db 경로 → 상태 조회
- 테이블 출력: 간단한 문자열 포맷 (외부 의존성 없음)

---

## 4. 파일 구조

### 신규 파일

```
p4gitsync/src/p4gitsync/cli/
├── __init__.py
├── setup_wizard.py       # setup 대화형 wizard
├── service_manager.py    # 서비스 관리 (추상 + Windows/Linux 구현)
└── status_reporter.py    # status 조회/출력

deploy/
├── p4gitsync.service.template  # systemd unit 템플릿
```

### 수정 파일

```
p4gitsync/src/p4gitsync/__main__.py  # setup, service, status 서브커맨드 추가
p4gitsync/pyproject.toml             # tomli_w 의존성 추가 (TOML 쓰기)
```

---

## 5. 서비스 레지스트리 경로

| OS | 경로 |
|----|------|
| Windows | `%LOCALAPPDATA%\p4gitsync\services.json` |
| Linux | `~/.p4gitsync/services.json` |

---

## 6. 의존성

| 패키지 | 용도 | 필수 여부 |
|--------|------|----------|
| tomli_w | TOML 쓰기 (setup) | 필수 |
| getpass (stdlib) | 비밀번호 입력 | 내장 |
| NSSM | Windows 서비스 관리 | Windows에서 자동 다운로드 |

---

## 7. 에러 처리

| 상황 | 동작 |
|------|------|
| P4 연결 실패 (setup) | 에러 표시 후 재입력 요청 |
| 관리자 권한 없음 (service install) | 안내 메시지 후 종료 |
| NSSM 다운로드 실패 | 수동 설치 경로 안내 |
| config 미존재 (service install) | "먼저 p4gitsync setup 실행" 안내 |
| 이미 등록된 서비스명 | 덮어쓸지 확인 |
| state.db 접근 불가 (status) | "서비스가 초기화되지 않음" 표시 |

---

## 8. 범위 외 (YAGNI)

- Kubernetes/Helm 지원 — 현재 불필요
- macOS launchd — 대상 OS 아님
- GUI 설치 마법사 — CLI로 충분
- 자동 업데이트 — 수동 배포
- TLS/nginx 리버스 프록시 — 내부 도구
