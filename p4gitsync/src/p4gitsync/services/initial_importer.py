import logging
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from p4gitsync.config.lfs_config import LfsConfig
from p4gitsync.config.sync_config import InitialImportConfig, P4Config
from p4gitsync.git.commit_metadata import CommitMetadata
from p4gitsync.git.fast_importer import FastImporter
from p4gitsync.lfs.lfs_object_store import LfsObjectStore
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
    normal_results: list[tuple[str, bytes]] = field(default_factory=list)
    lfs_results: list[tuple[str, bytes]] = field(default_factory=list)  # (git_path, pointer_bytes)
    deletes: list[str] = field(default_factory=list)
    file_count: int = 0
    skipped: bool = False  # 최적화 #3: virtual filter로 파일 0개


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
        self._server_load_threshold = 50
        self._throttle_wait_seconds = 60
        self._worker_stats: list[dict] = [
            {"cls": 0, "files": 0, "elapsed": 0.0} for _ in range(_PREFETCH_WORKERS)
        ]

    def run(self, branch: str) -> None:
        """전체 히스토리 import 실행."""
        last_cl = self._state.get_last_synced_cl(self._stream)

        # 전체 CL 목록 (진행률 계산용)
        all_total_changes = self._p4.get_all_changes(self._poll_stream)
        grand_total = len(all_total_changes)
        already_done = 0
        if last_cl > 0 and all_total_changes:
            # last_cl 이하인 CL 수 = 이미 처리된 수
            already_done = sum(1 for c in all_total_changes if c <= last_cl)
        del all_total_changes

        all_changes = self._p4.get_changes_after(self._poll_stream, last_cl)

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
        consecutive_errors = 0
        max_consecutive_errors = 5
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
                    consecutive_errors = 0  # 성공 시 연속 에러 카운트 초기화
                except OSError as e:
                    consecutive_errors += 1
                    error_cls.append(cl)
                    logger.error(
                        "fast-import write 실패 (CL %d, 연속 %d/%d): %s",
                        cl, consecutive_errors, max_consecutive_errors, e,
                    )
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error(
                            "연속 %d건 에러 발생, import 중단. 에러 CL: %s",
                            max_consecutive_errors, error_cls[-max_consecutive_errors:],
                        )
                        break
                    # fast-import 재시작하여 다음 CL 시도
                    fast_importer.finish()
                    fast_importer = FastImporter(self._repo_path)
                    fast_importer.start()
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
                    eta = self._calc_eta(next_i, total)
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

        normal_files: list[tuple[P4FileAction, str]] = []
        lfs_files: list[tuple[P4FileAction, str]] = []
        for fa, git_path in add_edit_files:
            if self._lfs_store and self._lfs and self._lfs.is_lfs_target(git_path):
                lfs_files.append((fa, git_path))
            else:
                normal_files.append((fa, git_path))

        total_normal = len(normal_files)
        total_lfs = len(lfs_files)

        # normal과 LFS를 동시 추출 (각각 다른 P4 연결 사용)
        if total_lfs > 0 and total_normal > 0 and lfs_p4:
            lfs_thread = threading.Thread(
                target=self._extract_lfs_files,
                args=(lfs_files, lfs_p4, data, cl),
                name=f"lfs-{cl}",
                daemon=True,
            )
            lfs_thread.start()

            if total_normal >= _LARGE_CL_THRESHOLD:
                data.normal_results = self._parallel_print(normal_files, p4, cl, lfs_p4)
            else:
                self._sequential_print(normal_files, p4, data, cl)

            lfs_thread.join()
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
        """LFS 파일을 batch print → pointer 변환."""
        for chunk_start in range(0, len(lfs_files), _BATCH_PRINT_SIZE):
            chunk = lfs_files[chunk_start:chunk_start + _BATCH_PRINT_SIZE]
            file_specs = [f"{fa.depot_path}#{fa.revision}" for fa, _ in chunk]
            batch_results = p4.print_files_batch(file_specs)

            for fa, git_path in chunk:
                content = batch_results.get(fa.depot_path)
                if content is not None:
                    pointer = self._lfs_store.store_from_stream([content])
                    data.lfs_results.append((git_path, pointer.pointer_bytes))
                    del content
            del batch_results

    def _sequential_print(
        self,
        normal_files: list[tuple[P4FileAction, str]],
        p4: P4Client,
        data: _CLData,
        cl: int,
    ) -> None:
        """단일 P4 연결로 순차 batch print."""
        total = len(normal_files)
        for chunk_start in range(0, total, _BATCH_PRINT_SIZE):
            chunk = normal_files[chunk_start:chunk_start + _BATCH_PRINT_SIZE]
            file_specs = [f"{fa.depot_path}#{fa.revision}" for fa, _ in chunk]
            batch_results = p4.print_files_batch(file_specs)

            for fa, git_path in chunk:
                content = batch_results.get(fa.depot_path)
                if content is not None:
                    data.normal_results.append((git_path, content))
            del batch_results

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
        all_results: list[tuple[str, bytes]] = []
        done_count = [0]

        def print_worker(p4: P4Client, my_chunks: list[list[tuple[P4FileAction, str]]]) -> None:
            for chunk in my_chunks:
                file_specs = [f"{fa.depot_path}#{fa.revision}" for fa, _ in chunk]
                batch_results = p4.print_files_batch(file_specs)
                partial = []
                for fa, git_path in chunk:
                    content = batch_results.get(fa.depot_path)
                    if content is not None:
                        partial.append((git_path, content))
                del batch_results
                with results_lock:
                    all_results.extend(partial)
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
        for git_path, content in cl_data.normal_results:
            fast_importer.write_file(git_path, content)
        for git_path, pointer_bytes in cl_data.lfs_results:
            fast_importer.write_file(git_path, pointer_bytes)

        fast_importer.end_commit()

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
