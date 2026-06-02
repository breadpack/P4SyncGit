import logging
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from p4gitsync.config.lfs_config import LfsConfig
from p4gitsync.config.sync_config import InitialImportConfig, P4Config
from p4gitsync.errors import ContentExtractionError
from p4gitsync.git.commit_metadata import CommitMetadata
from p4gitsync.git.fast_importer import FastImporter
from p4gitsync.git.file_mode import git_mode_from_p4_type
from p4gitsync.lfs.lfs_object_store import LfsObjectStore
from p4gitsync.lfs.lfs_routing import partition_for_lfs
from p4gitsync.p4.p4_change_info import P4ChangeInfo
from p4gitsync.p4.p4_client import P4Client
from p4gitsync.p4.p4_file_action import ADD_EDIT_ACTIONS, DELETE_ACTIONS, P4FileAction
from p4gitsync.p4.path_utils import depot_to_git_path
from p4gitsync.p4.virtual_stream_filter import VirtualStreamFilter
from p4gitsync.state.state_store import StateStore

logger = logging.getLogger("p4gitsync.initial_import")

_BATCH_PRINT_SIZE = 200    # 최적화 #2: 50 → 200
_PREFETCH_WORKERS = 4      # P4 서버 부하 고려 (8은 과다)
_DESCRIBE_BATCH_SIZE = 10  # 소묶음 → 대형 CL 편중 완화
_LARGE_CL_THRESHOLD = 500  # 이 이상이면 병렬 print 사용
_SENTINEL = None           # 큐 종료 신호


@dataclass
class _CLData:
    """CL 하나의 추출 결과."""
    cl: int
    info: P4ChangeInfo
    # (git_path, content, git_mode)
    normal_results: list[tuple[str, bytes, int]] = field(default_factory=list)
    lfs_results: list[tuple[str, bytes]] = field(default_factory=list)  # (git_path, pointer_bytes)
    deletes: list[str] = field(default_factory=list)
    file_count: int = 0
    skipped: bool = False  # 최적화 #3: virtual filter로 파일 0개
    # 대형 CL 메모리 스트리밍: content 를 모으지 않고 메인이 write 시점에 추출.
    streaming: bool = False
    normal_files: list[tuple[P4FileAction, str]] = field(default_factory=list)
    lfs_files: list[tuple[P4FileAction, str]] = field(default_factory=list)


class InitialImporter:
    """전체 히스토리 초기 import.

    최적화:
    1. 다중 prefetch 워커 — N개 P4 연결로 CL을 동시 추출
    2. batch print 크기 200 — API 호출 횟수 감소
    3. 빈 CL 스킵 — virtual filter 적용 후 파일 없으면 describe만으로 종료
    """

    def __init__(
        self,
        p4_client: P4Client,
        state_store: StateStore,
        repo_path: str,
        stream: str,
        config: InitialImportConfig | None = None,
        lfs_config: LfsConfig | None = None,
        lfs_store: LfsObjectStore | None = None,
        virtual_filter: VirtualStreamFilter | None = None,
        p4_config: P4Config | None = None,
    ) -> None:
        self._p4 = p4_client
        self._state = state_store
        self._repo_path = repo_path
        self._stream = stream
        self._virtual_filter = virtual_filter
        self._p4_config = p4_config
        if virtual_filter:
            self._poll_stream = virtual_filter.parent_stream
            self._stream_prefix_len = virtual_filter.parent_prefix_len
        else:
            self._poll_stream = stream
            self._stream_prefix_len = len(stream) + 1
        self._lfs = lfs_config
        self._lfs_store = lfs_store

        cfg = config or InitialImportConfig()
        self._checkpoint_interval = cfg.checkpoint_interval
        self._server_load_threshold = cfg.server_load_threshold
        self._throttle_wait_seconds = cfg.throttle_wait_seconds
        self._streaming_file_threshold = cfg.streaming_file_threshold
        self._streaming_bytes_threshold = cfg.streaming_bytes_threshold
        self._repack_interval = cfg.repack_interval_checkpoints
        self._checkpoint_count = 0
        self._main_extract_client: P4Client | None = None
        self._worker_stats: list[dict] = [
            {"cls": 0, "files": 0, "elapsed": 0.0} for _ in range(_PREFETCH_WORKERS)
        ]

    def run(self, branch: str) -> None:
        """전체 히스토리 import 실행."""
        last_cl = self._state.get_last_synced_cl(self._stream)

        # 전체 CL 을 한 번만 조회해 진행률과 남은 목록을 함께 계산(중복 호출 제거).
        all_full = self._p4.get_changes_after(self._poll_stream, 0)
        grand_total = len(all_full)
        if last_cl > 0:
            already_done = sum(1 for c in all_full if c <= last_cl)
            all_changes = [c for c in all_full if c > last_cl]
        else:
            already_done = 0
            all_changes = all_full
        del all_full

        if not all_changes:
            logger.info("import 대상 CL 없음 (stream=%s)", self._stream)
            return

        total = len(all_changes)
        logger.info(
            "초기 import: 전체 %d건 중 %d건 완료, 남은 %d건 처리 시작 (워커=%d)",
            grand_total, already_done, total,
            _PREFETCH_WORKERS,
        )

        # prefetch 워커용 P4 연결 + 결과 큐 (순서 보장)
        result_queue: queue.Queue[_CLData | None] = queue.Queue(
            maxsize=_PREFETCH_WORKERS * 2,
        )
        stop_event = threading.Event()

        prefetch_clients = self._create_prefetch_clients(_PREFETCH_WORKERS)
        # 대형 CL 스트리밍용 메인 전용 연결(워커 연결은 다음 묶음 추출로 점유 중이라 재사용 불가).
        self._main_extract_client = self._create_prefetch_clients(1)[0]
        # LFS 병렬 추출용 워커별 전용 연결 (재사용)
        self._lfs_clients: list[P4Client] = []
        if self._lfs_store and self._lfs and self._lfs.enabled:
            self._lfs_clients = self._create_prefetch_clients(_PREFETCH_WORKERS)

        prefetch_thread = threading.Thread(
            target=self._prefetch_loop,
            args=(all_changes, prefetch_clients, result_queue, stop_event),
            name="prefetch-dispatcher",
            daemon=True,
        )
        prefetch_thread.start()

        fast_importer = FastImporter(self._repo_path)
        fast_importer.start()
        import_start_time = time.monotonic()
        skipped = 0
        last_written_cl = 0
        actual_processed = 0
        error_cls: list[int] = []
        self._grand_total = grand_total
        self._already_done = already_done
        self._rate_samples: deque[tuple[float, int]] = deque(maxlen=6)
        for ws in self._worker_stats:
            ws["cls"] = ws["files"] = 0
            ws["elapsed"] = 0.0

        try:
            for i in range(total):
                # timeout 부여로 Ctrl+C(KeyboardInterrupt) 수신 가능
                cl_data = None
                while True:
                    try:
                        cl_data = result_queue.get(timeout=1.0)
                        break
                    except queue.Empty:
                        if stop_event.is_set():
                            break
                        continue
                if cl_data is None:
                    break
                if cl_data is _SENTINEL:
                    break

                cl = cl_data.cl
                next_i = i + 1

                # 최적화 #3: 빈 CL 스킵 (state에 기록하지 않음 — last_written_cl 기준으로만 기록)
                if cl_data.skipped:
                    skipped += 1
                    del cl_data
                    self._log_progress(next_i, total, skipped, import_start_time)
                    continue

                try:
                    self._write_cl_to_importer(cl_data, i, branch, fast_importer)
                    last_written_cl = cl
                    actual_processed = next_i
                except (OSError, ContentExtractionError) as e:
                    error_cls.append(cl)
                    logger.error(
                        "fast-import write 실패, import 중단 (CL %d): %s", cl, e,
                    )
                    logger.error(
                        "재시작하면 마지막 체크포인트(CL %d)부터 이어서 진행됩니다.",
                        last_written_cl,
                    )
                    break
                del cl_data

                # checkpoint — last_written_cl 기준으로만 state 기록
                if next_i % self._checkpoint_interval == 0 and last_written_cl > 0:
                    rc = fast_importer.finish()
                    if rc != 0:
                        logger.error("fast-import 체크포인트 실패 (returncode=%d), state 기록 스킵", rc)
                        break
                    head_sha = self._get_head_sha(branch)
                    self._state.set_last_synced_cl(self._stream, last_written_cl, head_sha)
                    self._state.record_commit(last_written_cl, head_sha, self._stream, branch)
                    # 워커별 통계 출력
                    for wid, ws in enumerate(self._worker_stats):
                        if ws["cls"] > 0:
                            rate = ws["cls"] / ws["elapsed"] if ws["elapsed"] > 0 else 0
                            logger.info(
                                "  워커%d: %d CL, %d파일, %.1f CL/s",
                                wid, ws["cls"], ws["files"], rate,
                            )
                    global_done = self._already_done + next_i
                    global_pct = global_done / self._grand_total * 100 if self._grand_total else 0
                    bar = self._progress_bar(global_pct)
                    logger.info(
                        "%s %.1f%% 체크포인트 저장 | HEAD=%s, CL %d",
                        bar, global_pct,
                        head_sha[:8] if head_sha else "N/A", cl,
                    )
                    # fast-import 프로세스가 없는 시점에 중간 repack/throttle 수행.
                    self._checkpoint_count += 1
                    if (
                        self._repack_interval > 0
                        and self._checkpoint_count % self._repack_interval == 0
                    ):
                        self._run_repack()
                    self._throttle_if_needed()
                    fast_importer = FastImporter(self._repo_path)
                    fast_importer.start()

                self._log_progress(next_i, total, skipped, import_start_time)

        finally:
            stop_event.set()
            final_rc = fast_importer.finish()
            for c in prefetch_clients:
                try:
                    c.disconnect()
                except Exception:
                    pass
            for c in self._lfs_clients:
                try:
                    c.disconnect()
                except Exception:
                    pass
            self._lfs_clients = []
            if self._main_extract_client is not None:
                try:
                    self._main_extract_client.disconnect()
                except Exception:
                    pass
                self._main_extract_client = None
            prefetch_thread.join(timeout=5)

        if last_written_cl > 0 and final_rc == 0:
            self._post_import(branch, last_written_cl)
        elif last_written_cl > 0 and final_rc != 0:
            logger.error("fast-import 최종 finish 실패 (returncode=%d), state 기록 스킵", final_rc)
        elapsed = time.monotonic() - import_start_time
        logger.info(
            "초기 import 완료: %d/%d CL 처리, %d 스킵, %d 에러, 소요 %s",
            actual_processed, total, skipped, len(error_cls),
            self._format_duration(elapsed),
        )
        if error_cls:
            logger.warning("에러 발생 CL 목록: %s", error_cls)

    # ── prefetch 파이프라인 ──────────────────────────────

    def _create_prefetch_clients(self, count: int) -> list[P4Client]:
        """prefetch 워커용 P4 연결 생성."""
        clients = []
        for idx in range(count):
            if self._p4_config:
                client = self._p4_config.create_client()
            else:
                client = P4Client(
                    port=self._p4._p4.port,
                    user=self._p4._p4.user,
                    workspace=self._p4._p4.client,
                )
            client.connect()
            clients.append(client)
        logger.info("prefetch P4 연결 %d개 생성", count)
        return clients

    def _prefetch_loop(
        self,
        all_changes: list[int],
        clients: list[P4Client],
        result_queue: queue.Queue,
        stop_event: threading.Event,
    ) -> None:
        """다중 워커로 CL 묶음을 병렬 추출, 순서 보장하여 큐에 넣는다."""
        num_workers = len(clients)

        worker_in: list[queue.Queue[list[int] | None]] = [
            queue.Queue() for _ in range(num_workers)
        ]
        worker_out: list[queue.Queue[list[_CLData] | None]] = [
            queue.Queue() for _ in range(num_workers)
        ]

        worker_stats = self._worker_stats

        lfs_clients = self._lfs_clients

        def worker_fn(worker_id: int) -> None:
            p4 = clients[worker_id]
            lfs_p4 = lfs_clients[worker_id] if worker_id < len(lfs_clients) else None
            stats = worker_stats[worker_id]
            while not stop_event.is_set():
                cl_batch = worker_in[worker_id].get()
                if cl_batch is None:
                    break
                try:
                    t0 = time.monotonic()
                    results = self._extract_cl_batch(cl_batch, p4, lfs_p4)
                    elapsed = time.monotonic() - t0
                    stats["cls"] += len(results)
                    stats["files"] += sum(d.file_count for d in results)
                    stats["elapsed"] += elapsed
                    worker_out[worker_id].put(results)
                except Exception:
                    logger.exception(
                        "CL 묶음 추출 실패 (worker %d, CLs=%s)", worker_id, cl_batch,
                    )
                    stop_event.set()
                    worker_out[worker_id].put(None)

        threads = []
        for wid in range(num_workers):
            t = threading.Thread(
                target=worker_fn, args=(wid,),
                name=f"p4-worker-{wid}", daemon=True,
            )
            t.start()
            threads.append(t)

        try:
            chunks = [
                all_changes[s:s + _DESCRIBE_BATCH_SIZE]
                for s in range(0, len(all_changes), _DESCRIBE_BATCH_SIZE)
            ]

            submit_idx = 0
            pending_order: list[int] = []
            for _ in range(min(num_workers, len(chunks))):
                wid = submit_idx % num_workers
                worker_in[wid].put(chunks[submit_idx])
                pending_order.append(wid)
                submit_idx += 1

            for wid in pending_order:
                if stop_event.is_set():
                    break
                batch_result = worker_out[wid].get()
                if batch_result is None:
                    break
                for cl_data in batch_result:
                    result_queue.put(cl_data)
                del batch_result

                if submit_idx < len(chunks) and not stop_event.is_set():
                    worker_in[wid].put(chunks[submit_idx])
                    pending_order.append(wid)
                    submit_idx += 1

        finally:
            for wq in worker_in:
                wq.put(None)
            for t in threads:
                t.join(timeout=5)

        result_queue.put(_SENTINEL)

    # ── CL 추출 ──────────────────────────────────────

    def _extract_cl_batch(
        self, cls: list[int], p4: P4Client, lfs_p4: P4Client | None = None,
    ) -> list[_CLData]:
        """CL 묶음을 일괄 describe 후 각각 batch print."""
        infos = p4.describe_batch(cls)
        results = []
        for info in infos:
            data = self._build_cl_data(info, p4, lfs_p4)
            results.append(data)
        return results

    def _build_cl_data(
        self, info: P4ChangeInfo, p4: P4Client, lfs_p4: P4Client | None = None,
    ) -> _CLData:
        """P4ChangeInfo로부터 파일을 batch print로 추출."""
        cl = info.changelist
        data = _CLData(cl=cl, info=info)

        add_edit_files: list[tuple[P4FileAction, str]] = []

        for fa in info.files:
            if self._virtual_filter and not self._virtual_filter.is_included(fa.depot_path):
                continue
            git_path = depot_to_git_path(fa.depot_path, self._poll_stream, self._stream_prefix_len)
            if git_path is None:
                continue
            if fa.action in DELETE_ACTIONS:
                data.deletes.append(git_path)
            elif fa.action in ADD_EDIT_ACTIONS:
                add_edit_files.append((fa, git_path))

        if not add_edit_files and not data.deletes:
            data.skipped = True
            return data

        if self._lfs_store and self._lfs:
            # 확장자 OR (binary 타입 AND 크기임계)로 LFS 라우팅. auto_detect_binary 가
            # 꺼져 있으면 확장자 전용(기존 동작). ambiguous 파일만 p4 sizes 선조회.
            lfs_files, normal_files = partition_for_lfs(add_edit_files, self._lfs, p4)
        else:
            normal_files = list(add_edit_files)
            lfs_files = []

        # 대형 CL 은 content 를 메모리에 모으지 않고 메인이 write 시점에 스트리밍 추출.
        if self._is_streaming_cl(normal_files):
            data.streaming = True
            data.normal_files = normal_files
            data.lfs_files = lfs_files
            data.file_count = len(add_edit_files) + len(data.deletes)
            return data

        total_normal = len(normal_files)
        total_lfs = len(lfs_files)

        # normal과 LFS를 동시 추출 (각각 다른 P4 연결 사용)
        if total_lfs > 0 and total_normal > 0 and lfs_p4:
            # LFS 스레드의 예외를 포착해 join 후 재전파(silent loss 방지).
            lfs_error: list[BaseException] = []

            def _lfs_target() -> None:
                try:
                    self._extract_lfs_files(lfs_files, lfs_p4, data, cl)
                except BaseException as e:  # noqa: BLE001 - 메인 스레드로 전달
                    lfs_error.append(e)

            lfs_thread = threading.Thread(
                target=_lfs_target,
                name=f"lfs-{cl}",
                daemon=True,
            )
            lfs_thread.start()

            # lfs_p4는 LFS 스레드가 점유 중이므로 normal 병렬 print에 넘기지 않는다
            # (동일 P4 연결 동시 사용 방지).
            try:
                if total_normal >= _LARGE_CL_THRESHOLD:
                    data.normal_results = self._parallel_print(normal_files, p4, cl)
                else:
                    self._sequential_print(normal_files, p4, data, cl)
            finally:
                lfs_thread.join()

            if lfs_error:
                raise lfs_error[0]
        else:
            if total_normal >= _LARGE_CL_THRESHOLD:
                data.normal_results = self._parallel_print(normal_files, p4, cl, lfs_p4)
            elif total_normal > 0:
                self._sequential_print(normal_files, p4, data, cl)

            if total_lfs > 0:
                self._extract_lfs_files(lfs_files, lfs_p4 or p4, data, cl)

        data.file_count = len(add_edit_files) + len(data.deletes)
        if data.file_count > 100:
            logger.info("CL %d 추출 완료: %d파일 (%d LFS)", cl, data.file_count, total_lfs)
        return data

    def _extract_lfs_files(
        self,
        lfs_files: list[tuple[P4FileAction, str]],
        p4: P4Client,
        data: _CLData,
        cl: int,
    ) -> None:
        """LFS 파일을 디스크로 직접 print(p4 print -o) → 청크 단위로 LFS store에 저장.

        대형 에셋을 메모리에 통째로 올리지 않도록 print_file_to_disk +
        store_from_file(4MB 청크) 경로를 사용한다. 추출 실패 시
        ContentExtractionError를 던져 CL을 실패시킨다(무결성 보장).
        """
        missing: list[str] = []
        for fa, git_path in lfs_files:
            try:
                tmp_path = p4.print_file_to_disk(
                    fa.depot_path, fa.revision, self._lfs_store.tmp_dir,
                )
            except Exception:
                missing.append(f"{fa.depot_path}#{fa.revision}")
                continue
            pointer = self._lfs_store.store_from_file(tmp_path)
            data.lfs_results.append((git_path, pointer.pointer_bytes))
        if missing:
            raise ContentExtractionError(cl, missing)

    def _sequential_print(
        self,
        normal_files: list[tuple[P4FileAction, str]],
        p4: P4Client,
        data: _CLData,
        cl: int,
    ) -> None:
        """단일 P4 연결로 순차 batch print."""
        total = len(normal_files)
        missing: list[str] = []
        for chunk_start in range(0, total, _BATCH_PRINT_SIZE):
            chunk = normal_files[chunk_start:chunk_start + _BATCH_PRINT_SIZE]
            file_specs = [f"{fa.depot_path}#{fa.revision}" for fa, _ in chunk]
            batch_results = p4.print_files_batch(file_specs)

            for fa, git_path in chunk:
                content = batch_results.get(fa.depot_path)
                if content is not None:
                    data.normal_results.append(
                        (git_path, content, git_mode_from_p4_type(fa.file_type))
                    )
                else:
                    missing.append(f"{fa.depot_path}#{fa.revision}")
            del batch_results
        if missing:
            raise ContentExtractionError(cl, missing)

    def _parallel_print(
        self,
        normal_files: list[tuple[P4FileAction, str]],
        own_p4: P4Client,
        cl: int,
        extra_p4: P4Client | None = None,
    ) -> list[tuple[str, bytes]]:
        """여러 P4 연결로 병렬 batch print. 대형 CL용."""
        # 기존 연결을 활용 (LFS 전용 연결이 유휴 상태일 때)
        all_clients = [own_p4]
        if extra_p4:
            all_clients.append(extra_p4)
        num_connections = len(all_clients)
        logger.info(
            "CL %d: 대형 CL (%d파일), %d개 연결로 병렬 print",
            cl, len(normal_files), num_connections,
        )

        # 파일을 batch 단위로 분할
        chunks: list[list[tuple[P4FileAction, str]]] = [
            normal_files[s:s + _BATCH_PRINT_SIZE]
            for s in range(0, len(normal_files), _BATCH_PRINT_SIZE)
        ]

        # 각 연결에 청크를 라운드로빈 배정하여 병렬 실행
        results_lock = threading.Lock()
        all_results: list[tuple[str, bytes, int]] = []
        all_missing: list[str] = []
        done_count = [0]

        def print_worker(p4: P4Client, my_chunks: list[list[tuple[P4FileAction, str]]]) -> None:
            for chunk in my_chunks:
                file_specs = [f"{fa.depot_path}#{fa.revision}" for fa, _ in chunk]
                batch_results = p4.print_files_batch(file_specs)
                partial = []
                partial_missing = []
                for fa, git_path in chunk:
                    content = batch_results.get(fa.depot_path)
                    if content is not None:
                        partial.append(
                            (git_path, content, git_mode_from_p4_type(fa.file_type))
                        )
                    else:
                        partial_missing.append(f"{fa.depot_path}#{fa.revision}")
                del batch_results
                with results_lock:
                    all_results.extend(partial)
                    all_missing.extend(partial_missing)
                    done_count[0] += len(chunk)
                    if done_count[0] % 1000 < _BATCH_PRINT_SIZE:
                        logger.info(
                            "  파일: %d/%d 추출 (CL %d)",
                            done_count[0], len(normal_files), cl,
                        )

        # 청크를 연결별로 분배
        per_client_chunks: list[list[list[tuple[P4FileAction, str]]]] = [
            [] for _ in range(num_connections)
        ]
        for idx, chunk in enumerate(chunks):
            per_client_chunks[idx % num_connections].append(chunk)

        # 병렬 실행
        threads = []
        for i, p4 in enumerate(all_clients):
            if per_client_chunks[i]:
                t = threading.Thread(
                    target=print_worker, args=(p4, per_client_chunks[i]),
                    name=f"print-{cl}-{i}", daemon=True,
                )
                t.start()
                threads.append(t)

        for t in threads:
            t.join()

        if all_missing:
            raise ContentExtractionError(cl, all_missing)

        return all_results

    # ── fast-import write ────────────────────────────

    def _write_cl_to_importer(
        self, cl_data: _CLData, index: int, branch: str, fast_importer: FastImporter,
    ) -> None:
        name, email = self._state.get_git_author(cl_data.info.user)
        metadata = CommitMetadata(
            author_name=name,
            author_email=email,
            author_timestamp=cl_data.info.timestamp,
            message=cl_data.info.description,
            p4_changelist=cl_data.cl,
        )
        fast_importer.begin_commit(branch, metadata)

        if index == 0 and self._lfs and self._lfs.enabled:
            attrs = self._lfs.generate_gitattributes().encode("utf-8")
            fast_importer.write_file(".gitattributes", attrs)
            lfsconfig = self._lfs.generate_lfsconfig()
            if lfsconfig is not None:
                fast_importer.write_file(".lfsconfig", lfsconfig.encode("utf-8"))

        for git_path in cl_data.deletes:
            fast_importer.write_delete(git_path)

        if cl_data.streaming:
            # 대형 CL: 메인 전용 연결로 chunk 단위 추출하며 즉시 write(메모리 가드).
            self._stream_normal_to_importer(cl_data, fast_importer)
            self._stream_lfs_to_importer(cl_data, fast_importer)
        else:
            for git_path, content, mode in cl_data.normal_results:
                fast_importer.write_file(git_path, content, mode)
            for git_path, pointer_bytes in cl_data.lfs_results:
                fast_importer.write_file(git_path, pointer_bytes)

        fast_importer.end_commit()

    def _is_streaming_cl(self, normal_files: list[tuple[P4FileAction, str]]) -> bool:
        """normal 파일 수 또는 (알려진) 누적 크기가 임계를 넘으면 스트리밍 대상."""
        if len(normal_files) >= self._streaming_file_threshold:
            return True
        known = sum(fa.size for fa, _ in normal_files if fa.size is not None)
        return known >= self._streaming_bytes_threshold

    def _stream_normal_to_importer(
        self, cl_data: _CLData, fast_importer: FastImporter,
    ) -> None:
        """normal 파일을 chunk 단위로 추출해 즉시 write. content 를 누적하지 않는다."""
        p4 = self._main_extract_client
        missing: list[str] = []
        nf = cl_data.normal_files
        for s in range(0, len(nf), _BATCH_PRINT_SIZE):
            chunk = nf[s:s + _BATCH_PRINT_SIZE]
            specs = [f"{fa.depot_path}#{fa.revision}" for fa, _ in chunk]
            batch = p4.print_files_batch(specs)
            for fa, git_path in chunk:
                content = batch.get(fa.depot_path)
                if content is None:
                    missing.append(f"{fa.depot_path}#{fa.revision}")
                    continue
                fast_importer.write_file(
                    git_path, content, git_mode_from_p4_type(fa.file_type),
                )
            del batch  # chunk 즉시 해제
        if missing:
            raise ContentExtractionError(cl_data.cl, missing)

    def _stream_lfs_to_importer(
        self, cl_data: _CLData, fast_importer: FastImporter,
    ) -> None:
        """LFS 파일을 디스크 스트리밍(4MB 청크)으로 추출해 포인터를 write."""
        if not cl_data.lfs_files:
            return
        p4 = self._main_extract_client
        missing: list[str] = []
        for fa, git_path in cl_data.lfs_files:
            try:
                tmp_path = p4.print_file_to_disk(
                    fa.depot_path, fa.revision, self._lfs_store.tmp_dir,
                )
            except Exception:
                missing.append(f"{fa.depot_path}#{fa.revision}")
                continue
            pointer = self._lfs_store.store_from_file(tmp_path)
            fast_importer.write_file(git_path, pointer.pointer_bytes)
        if missing:
            raise ContentExtractionError(cl_data.cl, missing)

    # ── 유틸 ─────────────────────────────────────────

    def _log_progress(
        self, done: int, total: int, skipped: int, start_time: float,
    ) -> None:
        if done % 100 == 0:
            eta = self._calc_eta(done, total)
            global_done = self._already_done + done
            global_pct = global_done / self._grand_total * 100 if self._grand_total else 0
            bar = self._progress_bar(global_pct)
            logger.info(
                "%s %.1f%% (%d/%d) | 이번 세션: %d CL, skip=%d | ETA=%s",
                bar, global_pct, global_done, self._grand_total,
                done, skipped, eta,
            )

    @staticmethod
    def _progress_bar(pct: float, width: int = 20) -> str:
        filled = int(width * pct / 100)
        return "[" + "#" * filled + "-" * (width - filled) + "]"

    def _calc_eta(self, done: int, total: int) -> str:
        """최근 구간 이동 평균 기반 ETA. 최근 5개 샘플 사용."""
        now = time.monotonic()
        self._rate_samples.append((now, done))

        if len(self._rate_samples) < 2:
            return "계산 중..."

        oldest_time, oldest_done = self._rate_samples[0]
        dt = now - oldest_time
        dcl = done - oldest_done
        if dt <= 0 or dcl <= 0:
            return "계산 중..."

        rate = dcl / dt
        remaining = (total - done) / rate
        hours, rem = divmod(int(remaining), 3600)
        minutes, secs = divmod(rem, 60)
        if hours > 0:
            return f"{hours}시간 {minutes}분 ({rate:.1f} CL/s)"
        return f"{minutes}분 {secs}초 ({rate:.1f} CL/s)"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        hours, rem = divmod(int(seconds), 3600)
        minutes, secs = divmod(rem, 60)
        if hours > 0:
            return f"{hours}시간 {minutes}분"
        return f"{minutes}분 {secs}초"

    def _get_head_sha(self, branch: str) -> str:
        result = subprocess.run(
            ["git", "rev-parse", f"refs/heads/{branch}"],
            cwd=self._repo_path,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _throttle_if_needed(self) -> None:
        try:
            if self._p4.check_server_load(self._server_load_threshold):
                logger.warning(
                    "P4 서버 과부하 감지. %d초 대기.", self._throttle_wait_seconds
                )
                time.sleep(self._throttle_wait_seconds)
        except Exception:
            logger.exception("서버 부하 확인 중 오류 발생")

    def _run_repack(self) -> None:
        """중간 repack 으로 packfile 파편화를 통합(디스크/시간 분산). 실패해도 import 계속."""
        logger.info("git repack -ad 중간 통합 실행 (checkpoint %d)...", self._checkpoint_count)
        result = subprocess.run(
            ["git", "repack", "-ad", "-q"],
            cwd=self._repo_path,
            capture_output=True,
        )
        if result.returncode == 0:
            logger.info("git repack 완료")
        else:
            logger.warning(
                "git repack 실패(무시): %s",
                result.stderr.decode(errors="replace")[-200:],
            )

    def _post_import(self, branch: str, last_cl: int) -> None:
        head_sha = self._get_head_sha(branch)
        if head_sha:
            self._state.set_last_synced_cl(self._stream, last_cl, head_sha)
            self._state.record_commit(last_cl, head_sha, self._stream, branch)
            logger.info("import 후 HEAD: %s (CL %d)", head_sha[:8], last_cl)

        subprocess.run(
            ["git", "gc"],
            cwd=self._repo_path,
            capture_output=True,
        )
        logger.info("import 후 git gc 완료")
