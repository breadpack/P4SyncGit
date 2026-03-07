# Phase 1 개선 작업 — 기반 구축 완성

현재 완성도: ~75% → 목표: 100%

## 개요

Phase 1의 코어 동기화 파이프라인은 동작 가능한 수준이나, 미통합 컴포넌트, 코드 품질 이슈, 테스트 부재가 주요 갭이다. Phase 2 착수 전에 반드시 해소해야 한다.

---

## 1. [심각] async 루프 내 동기 블로킹 해소

### 현상

`sync_orchestrator.py:35-40`에서 asyncio 이벤트 루프 안에서 P4/Git 호출이 모두 동기(blocking)로 수행된다. `asyncio.sleep`만 비동기이고 실제 작업은 전부 블로킹이므로 async를 사용하는 의미가 없다.

### 영향

- API 서버(FastAPI/uvicorn)와 병행 실행 시 이벤트 루프 데드락 또는 응답 지연
- `initial_importer.py:116-125`의 `time.sleep()`도 동일 문제

### 해결 방안

두 가지 중 택일:

**방안 A**: 블로킹 호출을 `asyncio.to_thread()`로 래핑
```python
# 변경 전
change_info = self._p4_client.describe(cl)
# 변경 후
change_info = await asyncio.to_thread(self._p4_client.describe, cl)
```

**방안 B**: async를 걷어내고 순수 동기 루프로 전환 (API 서버는 별도 스레드)

### 대상 파일

- `services/sync_orchestrator.py` — P4/Git/StateStore 호출 전체
- `services/initial_importer.py` — `time.sleep()` → `asyncio.sleep()`

---

## 2. [심각] 파일 추출 로직 중복 제거 (DRY)

### 현상

`commit_builder.py:59-82`와 `initial_importer.py:91-114`에서 다음 로직이 거의 동일하게 중복:
- `_depot_to_git_path()` — depot 경로에서 stream prefix 제거
- 파일 액션 필터링 (`"delete"`, `"move/delete"` 등 매직 스트링 반복)

### 해결 방안

- 공통 유틸리티 모듈 생성 (예: `p4/path_utils.py` 또는 `services/file_extractor.py`)
- `depot_to_git_path(depot_path, stream_prefix)` 함수 추출
- 파일 액션 상수를 `p4/p4_file_action.py`에 `DELETE_ACTIONS` 등으로 정의

### 대상 파일

- `services/commit_builder.py`
- `services/initial_importer.py`
- `p4/p4_file_action.py` (액션 상수 추가)

---

## 3. [심각] AppConfig 타입 안전 활용

### 현상

`config/sync_config.py`에 `AppConfig`, `P4Config`, `GitConfig` 등 타입 안전한 dataclass가 정의되어 있으나, `SyncOrchestrator`와 `__main__.py`에서 raw dict로 설정에 접근한다.

### 영향

- 설정 키 오타 시 런타임에서만 발견 가능
- IDE 자동완성/리팩토링 지원 불가

### 해결 방안

```python
# __main__.py 변경
config_dict = tomllib.load(f)
app_config = AppConfig.from_dict(config_dict)
orchestrator = SyncOrchestrator(app_config, ...)
```

### 대상 파일

- `__main__.py` — `load_config()` 반환 타입을 `AppConfig`로 변경
- `services/sync_orchestrator.py` — 생성자 파라미터를 `config: AppConfig`로 변경
- `services/initial_importer.py` — 동일하게 적용

---

## 4. [중요] InitialImporter 실행 경로 추가

### 현상

`InitialImporter` 클래스가 구현되어 있으나 어디에서도 호출되지 않는다. CLI 서브커맨드나 orchestrator 통합이 없어 실행 불가.

### 해결 방안

`__main__.py`에 CLI 서브커맨드 추가:

```python
# python -m p4gitsync import --stream //ProjectSTAR/main
# python -m p4gitsync run  (기존 동기화 루프)
```

또는 `SyncOrchestrator.start()`에서 최초 실행 시 자동 import 판단:
```python
if last_synced_cl == 0:
    await self._run_initial_import()
```

### 대상 파일

- `__main__.py` — argparse 서브커맨드 추가
- `services/sync_orchestrator.py` — 초기 import 호출 (선택)

---

## 5. [중요] 로깅 설정 중복 제거

### 현상

- `__main__.py:51-58`에 `_JsonFormatter` 클래스 정의
- `config/logging_config.py:5-12`에 동일한 `JsonFormatter` 존재
- `logging_config.py`가 실제로 import되지 않음

### 해결 방안

- `__main__.py`의 `_JsonFormatter` 삭제
- `config/logging_config.py`의 `JsonFormatter`와 `setup_logging()` 사용
- `__main__.py`에서 `from p4gitsync.config.logging_config import setup_logging` 호출

### 대상 파일

- `__main__.py`
- `config/logging_config.py`

---

## 6. [중요] SyncOrchestrator DIP 준수

### 현상

`sync_orchestrator.py:65`에서 `Pygit2GitOperator` 구체 클래스에 직접 의존. `GitOperator` Protocol이 있음에도 타입 힌트가 구체 클래스.

### 해결 방안

```python
# 변경 전
self._git_operator: Pygit2GitOperator | None = None
# 변경 후
self._git_operator: GitOperator | None = None
```

설정 기반으로 구현체 선택:
```python
if config.git.backend == "cli":
    self._git_operator = GitCliOperator(...)
else:
    self._git_operator = Pygit2GitOperator(...)
```

### 대상 파일

- `services/sync_orchestrator.py`
- `config/sync_config.py` — `GitConfig`에 `backend` 필드 추가

---

## 7. [중요] 리소스 해제 보장

### 현상

- `SyncOrchestrator`에 context manager 패턴 미적용
- `stop()`이 호출되지 않으면 DB 연결, P4 연결이 해제되지 않음
- `_initialize_components()`에서 예외 발생 시 이미 생성된 리소스 누수

### 해결 방안

```python
class SyncOrchestrator:
    async def __aenter__(self):
        await self._initialize_components()
        return self

    async def __aexit__(self, *exc):
        await self.stop()
```

### 대상 파일

- `services/sync_orchestrator.py`

---

## 8. [중요] API 서버 통합

### 현상

- `api_server.py`의 FastAPI 앱이 orchestrator에 통합되지 않음
- uvicorn 실행 로직 없음
- `_last_trigger_time` 등 전역 변수 사용

### 해결 방안

- `__main__.py`에서 uvicorn을 별도 스레드/태스크로 실행
- `api_server.py`를 클래스로 리팩토링, orchestrator/state_store 참조 주입
- `/api/trigger` 수신 시 `asyncio.Event`로 즉시 동기화 트리거

### 대상 파일

- `__main__.py`
- `api/api_server.py`
- `services/sync_orchestrator.py`

---

## 9. [중요] P4 재연결 메커니즘

### 현상

`P4Client`에 연결 끊김 시 자동 재연결 로직이 없다. 장시간 폴링 시 P4 서버 재시작 등으로 연결이 끊어지면 복구 불가.

### 해결 방안

```python
def _ensure_connected(self):
    if not self._p4.connected():
        self._p4.connect()
```

또는 각 API 메서드에 데코레이터로 재연결 로직 적용.

### 대상 파일

- `p4/p4_client.py`

---

## 10. [중요] 예외 무시 패턴 수정

### 현상

- `sync_orchestrator.py:169-170` — `_mark_batch_pushed`에서 예외를 `pass`로 무시
- `initial_importer.py:123-125` — `_throttle_if_needed`에서 예외를 `pass`로 무시

### 해결 방안

최소한 `logger.warning()` 또는 `logger.exception()`으로 기록:
```python
except Exception:
    logger.exception("push 상태 갱신 실패")
```

### 대상 파일

- `services/sync_orchestrator.py`
- `services/initial_importer.py`

---

## 11. [권장] StateStore 트랜잭션 배치화

### 현상

`record_commit`, `update_push_status` 등 개별 호출마다 `self._conn.commit()` 실행. CL 단위 반복 호출 시 매번 디스크 flush 발생.

### 해결 방안

```python
# context manager 기반 트랜잭션
async with state_store.transaction():
    state_store.record_commit(cl, sha, stream)
    state_store.set_last_synced_cl(stream, cl)
```

### 대상 파일

- `state/state_store.py`

---

## 12. [권장] 사용자 매핑 캐싱

### 현상

`state_store.get_git_author()`가 폴링 루프에서 CL마다 호출되지만, 사용자 매핑은 자주 변경되지 않는다.

### 해결 방안

`functools.lru_cache` 또는 간단한 dict 캐시 적용.

### 대상 파일

- `state/state_store.py` 또는 `services/commit_builder.py`

---

## 13. [권장] LFS 파이프라인 연결

### 현상

`config/lfs_config.py`에 gitattributes 생성 로직이 있으나 동기화 흐름에 미연결.

### 해결 방안

- `CommitBuilder`에서 LFS 대상 확장자 파일 감지 시 LFS 포인터 파일 생성
- 초기 import 시 `.gitattributes`를 첫 commit에 포함

### 대상 파일

- `services/commit_builder.py`
- `services/initial_importer.py`
- `config/lfs_config.py`

---

## 14. 핵심 비즈니스 로직 테스트 추가

### 현상

서비스 레이어(비즈니스 로직) 전체가 미테스트 상태:
- `SyncOrchestrator` — 0개
- `CommitBuilder` — 0개
- `ChangelistPoller` — 0개
- `InitialImporter` — 0개
- `P4Client` — 0개

### 작업 내용

P4Client와 GitOperator를 mock하여 단위 테스트 추가:

**최우선:**
- `CommitBuilder.build_commit()` — 파일 add/edit/delete/move 변환, 경로 변환, 사용자 매핑
- `SyncOrchestrator._process_changelist()` — 처리 흐름 순서 검증
- `SyncOrchestrator._verify_on_startup()` — 정합성 검증 시나리오

**높은 우선순위:**
- `ChangelistPoller.poll()` — batch_size 절단, 마지막 CL 이후 조회
- `InitialImporter.run()` — 재개, checkpoint, throttle 동작
- `AppConfig.from_dict()` — 설정 파싱, 필수 키 누락 시 에러

**경계값/에러 테스트:**
- 빈 changelist, 특수문자 경로, DB 동시 접근
- `GitCliOperator.create_merge_commit` (현재 미테스트)
- `FastImporter.add_merge_commit` (현재 미테스트)

### 인프라 개선

- `_make_metadata` 헬퍼를 공통 `conftest.py`로 추출
- mock 인프라 구축 (P4Client, GitOperator stub)

---

## 작업 우선순위 요약

| 순위 | 항목 | 심각도 |
|------|------|--------|
| 1 | async 블로킹 해소 | 심각 |
| 2 | 파일 추출 로직 중복 제거 | 심각 |
| 3 | AppConfig 타입 안전 활용 | 심각 |
| 4 | InitialImporter 실행 경로 | 중요 |
| 5 | 로깅 설정 중복 제거 | 중요 |
| 6 | DIP 준수 (GitOperator Protocol) | 중요 |
| 7 | 리소스 해제 보장 | 중요 |
| 8 | API 서버 통합 | 중요 |
| 9 | P4 재연결 메커니즘 | 중요 |
| 10 | 예외 무시 패턴 수정 | 중요 |
| 11 | StateStore 트랜잭션 배치화 | 권장 |
| 12 | 사용자 매핑 캐싱 | 권장 |
| 13 | LFS 파이프라인 연결 | 권장 |
| 14 | 핵심 비즈니스 로직 테스트 추가 | 중요 |
