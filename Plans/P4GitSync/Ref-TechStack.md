# 기술 스택 선정

## 선정 기준

1. 최소 외부 의존성
2. P4/Git 공식 바인딩 활용
3. 유지보수 용이성
4. 팀 내 기술 스택 친숙도

## 컴포넌트별 기술 스택

### 1. Trigger (P4 Server 측)

**선정: Bash + curl**

| 후보 | 평가 |
|------|------|
| Bash + curl | 최소 오버헤드, P4 서버 프로세스 블로킹 없음 |
| Python | 런타임 의존성 추가, 트리거에는 과한 스택 |
| PowerShell | Windows 전용, P4 서버가 Linux면 불가 |

트리거는 메시지 큐에 changelist 번호만 발행하고 즉시 반환해야 함 (< 100ms).
복잡한 로직은 일절 넣지 않는다.

### 2. 메시지 큐

**선정: Redis Streams**

| 후보 | 장점 | 단점 | 선정 여부 |
|------|------|------|-----------|
| Redis Streams | 경량, 영속성, 소비자 그룹 지원 | 전용 MQ 대비 기능 제한 | **선정** |
| NATS JetStream | 경량, 고성능 | JetStream 설정 추가 필요 | 대안 |
| Kafka | 강력한 순서 보장 | 이 용도에 오버스펙 | 제외 |
| HTTP Webhook 직접 구현 | MQ 인프라 불필요 | 재시도/순서 보장 직접 구현 | 제외 |

선정 근거:
- Consumer Group으로 Worker 수평 확장 가능
- 메시지 영속성으로 Worker 재시작 시 유실 없음
- redis-py로 간결하게 연동 가능

> **Redis 선택사항 안내**: 단일 Worker 구성에서는 Redis가 과잉일 수 있다. 폴링 방식만으로도 충분한 경우 Redis 없이 운영 가능하며, 이 경우 P4 Trigger → Redis → Worker 경로 대신 폴링만으로 동기화한다. Redis는 다음 조건 중 하나 이상 충족 시 도입을 권장한다:
> - Worker 수평 확장이 필요한 경우 (Consumer Group 활용)
> - P4 Trigger 기반 즉시 반응이 필요한 경우 (준실시간 5초 이내)
> - 이벤트 순서 보장 및 재처리가 중요한 경우

### 3. Worker Service

**선정: Python 3.12+**

| 후보 | 장점 | 단점 | 선정 여부 |
|------|------|------|-----------|
| Python | p4python 공식 바인딩, 간결한 구현 | 타입 안전성 약함 | **선정** |
| .NET Worker Service | 타입 안전성, 컴파일 타임 검증 | P4/Git 연동에 보일러플레이트 많음 | 제외 |
| Go | 바이너리 배포 간편 | P4 공식 바인딩 없음 | 제외 |

선정 근거:
- p4python, pygit2 등 공식 바인딩으로 CLI 파싱 불필요
- sqlite3 내장, redis-py/slack-sdk 등 성숙한 라이브러리 생태계
- 스크립트 성격의 작업(파일 추출, 변환, 커밋)에 자연스러움
- 프로토타이핑과 디버깅이 빠름
- Phase 문서(Phase1~3) 전체가 Python 기반으로 작성되어 일관성 확보

.NET Worker Service 제외 사유:
- P4NET(P4API.NET)과 LibGit2Sharp 조합 시 보일러플레이트가 많고, 공식 바인딩 성숙도가 p4python 대비 낮음
- 이 프로젝트의 핵심 작업(파일 추출, 변환, commit 생성)이 스크립트 성격에 가까워 정적 타이핑의 이점이 제한적

> **팀 역량 불일치 리스크**: 팀 내 주력 기술 스택이 C#/.NET인 경우, Python 기반 시스템의 유지보수 및 트러블슈팅에 추가 학습 비용이 발생한다. 특히 asyncio, pygit2(libgit2 네이티브 바인딩) 등 Python 고급 기능에 대한 경험이 부족하면 운영 리스크로 이어질 수 있다. 팀 역량 평가 후 .NET Worker Service 재검토가 필요할 수 있으며, 이 경우 P4CommandLine(CLI 래퍼) + LibGit2Sharp 조합을 대안으로 고려한다.

프로세스 데몬화: systemd (Linux) 또는 supervisor

> **Windows 호환성 경고**: `asyncio`의 `loop.add_signal_handler()`는 Windows에서 `NotImplementedError`를 발생시킨다. Windows 환경에서는 `signal.signal()`을 사용하거나, Windows Service(NSSM 또는 `pywin32`의 `win32serviceutil`)로 등록해야 한다. 자세한 플랫폼 분기 코드는 Phase1-Foundation.md의 `__main__.py`를 참조한다.

### 4. P4 연동

**선정: p4python (공식 바인딩)**

| 후보 | 장점 | 단점 |
|------|------|------|
| p4python | 공식 바인딩, dict 반환, 파싱 불필요 | Perforce 서버 버전 호환성 확인 필요 |
| p4 CLI (Process) | 버전 무관, 디버깅 용이 | 출력 파싱 필요, 프로세스 생성 오버헤드 |

p4python 특징:
- `p4.run_describe()` 등 명령어가 Python dict/list로 직접 반환
- 텍스트 파싱 로직이 완전히 제거됨
- connection pooling으로 프로세스 생성 오버헤드 없음
- 멀티라인 description, 한글/유니코드 처리가 자연스러움

사용할 주요 API:
- `p4.run_changes()`: changelist 조회
- `p4.run_describe()`: changelist 상세 (integration 포함)
- `p4.run_filelog()`: 파일별 integration 히스토리
- `p4.run_print()`: 특정 리비전 파일 내용 추출
- `p4.run_sync()`: 워크스페이스 동기화
- `p4.run_streams()`: stream 목록/계층 조회

Fallback: p4 CLI + `-ztag` 옵션 (p4python 설치 불가 환경)

> **p4python Windows SSL 충돌 리스크**: p4python은 내장 OpenSSL과 시스템/Python의 OpenSSL이 충돌하여 SSL 연결 실패가 발생할 수 있다. 특히 Windows 환경에서 빈번하며, `ssl:` 프로토콜 사용 시 `P4Exception: SSL library init failure` 등의 오류가 나타날 수 있다. 이 경우 p4 CLI `-ztag` fallback으로 즉시 전환한다.

### 5. Git 연동

**선정: pygit2 + git CLI 혼용**

| 후보 | 장점 | 단점 |
|------|------|------|
| pygit2 | libgit2 바인딩, plumbing 가능, 고성능 | 빌드 의존성 (libgit2) |
| GitPython | 설치 간편 | git CLI 래퍼에 불과, 성능 제한 |
| git CLI (Process) | 완전한 기능, 안정적 | 파싱 필요 |

전략:
- IGitOperator 인터페이스(프로토콜)로 추상화
- 구현 1: Pygit2GitOperator (기본 구현)
  - 일반 작업 (add, commit): pygit2
  - Plumbing (commit-tree, write-tree): pygit2 ObjectDatabase
  - Merge commit: Repository.create_commit (parent 직접 지정)
  - **주의**: pygit2는 Windows에서 libgit2 네이티브 빌드 의존성이 있으며, 바이너리 휠이 제공되지 않는 환경에서는 빌드 실패 리스크가 존재한다
- 구현 2: GitCliOperator (동등한 기본 구현)
  - pygit2와 동등한 수준의 기본 구현으로 취급 (단순 fallback이 아님)
  - git commit-tree, git update-ref 등 plumbing 명령어 사용
  - pygit2 호환성 문제, 메모리 이슈, 또는 Windows 빌드 리스크 시 즉시 전환 가능
- 대규모 초기 import에서 메모리 이슈 발생 시 GitCliOperator 사용 권장
- **push는 처음부터 `git push` CLI 사용**: pygit2의 remote.push()는 인증/SSH 설정이 복잡하므로 push는 항상 `subprocess`를 통한 `git push` CLI로 수행한다

### 6. State DB

**선정: SQLite**

| 후보 | 장점 | 단점 |
|------|------|------|
| SQLite | 파일 기반, 배포 간편, Python 내장 (sqlite3) | 동시 쓰기 제한 |
| Redis | 이미 존재 | 영속성 관리, 조회 기능 제한 |
| PostgreSQL | 풍부한 기능, 동시성 | 이 용도에 오버스펙 |

저장 데이터:
- Stream ↔ Branch 매핑
- Changelist ↔ Git Commit SHA 매핑 (merge parent 조회에 필수)
- P4 User ↔ Git Author 매핑
- 동기화 상태 및 에러 로그

운영 설정:
- WAL 모드 (PRAGMA journal_mode=WAL): 읽기/쓰기 동시성 향상
- busy_timeout=5000ms: 멀티 스레드 환경에서 lock 대기
- 주기적 백업: SQLite Online Backup API, 1일 1회

확장성 한계:
- 동시 writer 1개 제한 (WAL 모드에서도)
- CL 30만 개 규모까지는 문제 없음 (PRIMARY KEY 인덱스)
- 그 이상 또는 다중 Worker 필요 시 PostgreSQL 마이그레이션 고려
- StateStore 프로토콜 추상화로 DB 엔진 교체 용이성 확보

### 7. 모니터링/알림

**선정: logging + slack-sdk**

- 로깅: Python 표준 logging 모듈 (구조화된 JSON 로그)
- 에러 알림: Slack 채널 (slack-sdk)
- 상태 대시보드: 간단한 HTTP endpoint (FastAPI 또는 Flask)
- 메트릭: 처리된 CL 수, 지연 시간, 실패 수

## 의존성 요약

```
Python Packages:
- p4python                              (P4 공식 바인딩)
- pygit2                                (Git 조작)
- redis                                 (Redis Streams 소비)
- slack-sdk                             (Slack 알림)
- fastapi / uvicorn                     (상태 대시보드, 선택)

Built-in:
- sqlite3                              (State DB)
- logging                              (로깅)
- subprocess                           (CLI fallback)

External:
- p4 CLI (PATH에 존재, p4python fallback용)
- git CLI (PATH에 존재, pygit2 fallback용)
- Redis Server
```

## 기술 부채 인식

현재 기술 선택에서 의도적으로 수용한 한계와 향후 전환 경로:

| 항목 | 현재 선택 | 한계 | 전환 트리거 (정량적 조건) | 전환 경로 |
|------|----------|------|--------------------------|----------|
| P4 연동 | p4python | 서버 버전 호환성 확인 필요, Windows SSL 충돌 리스크 | SSL 연결 실패 반복 또는 설치 불가 시 즉시 전환 | p4 CLI `-ztag` fallback |
| Git 연동 | pygit2 + Git CLI (동등한 기본 구현) | pygit2: libgit2 빌드 의존성, Windows 빌드 리스크 | pygit2 설치 실패 또는 메모리 누수(1GB 초과) 감지 시 전환 | IGitOperator(프로토콜) → GitCliOperator는 동등한 기본 구현. push는 처음부터 git CLI 사용 |
| State DB | SQLite | 단일 writer, 수평 확장 불가 | SQLITE_BUSY 에러 주 10회 초과 시 전환 검토 | StateStore(프로토콜) → PostgreSQL 전환 |
| Worker | 단일 인스턴스 | 수평 확장 불가 | CL 처리 지연 1시간 초과 시 전환 검토 | 파이프라인 분리: P4 Extractor(다수) + Git Writer(1대) 분리 설계를 사전 마련 |
| 타입 안전성 | 동적 타이핑 (Python) | 런타임 에러 가능성 | 타입 관련 런타임 에러 월 5회 초과 시 mypy strict 도입 | mypy strict + dataclass/TypedDict 활용 |
