"""Command-line interface for the resumable screening worker."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path

from .cleaning import SUPPORTED_SUFFIXES
from .contracts import validate_record
from .metadata import infer_candidate_name, infer_role
from .minimax import MiniMaxClient
from .pipeline import ScreeningPipeline
from .queue import (
    ACTIVE_CONTRACTS,
    STALE_CONTRACT_VERSION,
    TaskSpec,
    TaskStore,
    WorkerAlreadyRunningError,
    WorkerLeaseError,
    sanitize_diagnostic,
)
from .versions import ROLE_VERSIONS
from .watch import WatchCandidate, WatchScanner, is_ignored_watch_file

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _candidate_id(path: Path, source_sha256: str | None = None) -> str:
    digest = source_sha256 or hashlib.sha256(path.read_bytes()).hexdigest()
    return f"candidate-{digest[:12]}"


def _local_today() -> date:
    return date.today()


def _modified_on(path: Path) -> date:
    return datetime.fromtimestamp(path.stat().st_mtime).date()


def _supported_input_file(path: Path) -> bool:
    return (
        path.is_file()
        and not is_ignored_watch_file(path)
        and path.suffix.casefold() in SUPPORTED_SUFFIXES
    )


def _input_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for value in paths:
        path = Path(value).resolve()
        if path.is_dir():
            files.extend(
                item
                for item in sorted(path.iterdir(), key=lambda item: item.name.casefold())
                if _supported_input_file(item)
            )
        elif _supported_input_file(path):
            files.append(path)
        else:
            raise ValueError(f"不支持或不存在的输入：{path}")
    unique: dict[Path, None] = {}
    for path in files:
        unique[path] = None
    return list(unique)


def _validate_explicit_role(path: Path, role: str) -> None:
    filename_role = infer_role(path)
    if filename_role is not None and filename_role != role:
        raise ValueError(f"文件名岗位与指定岗位冲突（{role}）")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="resume-screening")
    parser.add_argument("--database", default="var/screening-v8.sqlite3")
    parser.add_argument("--output", default="outputs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue", help="登记简历任务")
    enqueue.add_argument("paths", nargs="+")
    enqueue_routing = enqueue.add_mutually_exclusive_group(required=True)
    enqueue_routing.add_argument("--role", choices=sorted(ROLE_VERSIONS))
    enqueue_routing.add_argument(
        "--auto-route",
        action="store_true",
        help="仅按明确的受支持文件名前缀自动识别岗位；未知文件跳过",
    )
    enqueue.add_argument("--candidate-id")
    enqueue.add_argument("--candidate-name")
    enqueue.add_argument(
        "--today",
        action="store_true",
        help="仅登记本机时区下最后修改日期为今天的文件",
    )

    worker = subparsers.add_parser("worker", help="运行筛选任务")
    mode = worker.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="处理可用任务后退出")
    mode.add_argument("--watch", action="store_true", help="持续监听队列")
    worker.add_argument(
        "--max-tasks",
        type=int,
        help="本次最多处理的不同任务数；适合先运行小批次验证",
    )
    worker.add_argument("--poll-seconds", type=float, default=5.0)
    worker.add_argument("--input", help="watch 模式下持续扫描此简历目录")
    worker_routing = worker.add_mutually_exclusive_group()
    worker_routing.add_argument(
        "--role", choices=sorted(ROLE_VERSIONS), help="--input 对应的固定岗位"
    )
    worker_routing.add_argument(
        "--auto-route",
        action="store_true",
        help="watch 模式按明确文件名前缀自动分流三个支持岗位",
    )
    worker.add_argument(
        "--accept-unlabeled",
        action="store_true",
        help="固定岗位 watch 接收没有岗位前缀的文件（默认跳过）",
    )
    worker.add_argument(
        "--lease-seconds",
        type=int,
        default=300,
        help="数据库 worker 租约时长；异常退出后超过此时长视为 stale",
    )
    worker.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=15.0,
        help="worker 心跳周期",
    )
    worker.add_argument(
        "--provider-pause-seconds",
        type=int,
        default=60,
        help="供应商鉴权/全局限流后的 watch 暂停时长",
    )

    subparsers.add_parser("status", help="显示任务状态")

    health = subparsers.add_parser("health", help="显示 worker、队列和错误健康状态")
    health.add_argument(
        "--processing-threshold-seconds",
        type=int,
        default=900,
        help="processing 任务超过此时长即标记为超阈值",
    )

    retry = subparsers.add_parser("retry-failed", help="显式重置未完成的可重试任务")
    retry.add_argument("--task-id", type=int)

    export = subparsers.add_parser("export", help="导出成功结果")
    export.add_argument("--directory", default="exports")

    validate = subparsers.add_parser("validate", help="校验 screening.json")
    validate.add_argument("path")

    calibrate = subparsers.add_parser(
        "calibrate", aliases=["calibration"], help="导入人工原因并生成校准报告"
    )
    calibration_actions = calibrate.add_subparsers(
        dest="calibration_action", required=True
    )
    import_reviews = calibration_actions.add_parser(
        "import", help="从 CSV 导入人工结论和固定原因类别"
    )
    import_reviews.add_argument("csv_path")
    report = calibration_actions.add_parser("report", help="输出校准汇总 JSON")
    report.add_argument("--output")
    report.add_argument("--min-samples", type=int, default=10)
    return parser


def _enqueue(args: argparse.Namespace, store: TaskStore) -> int:
    files = _input_files(args.paths)
    if args.today:
        today = _local_today()
        files = [path for path in files if _modified_on(path) == today]
    if args.candidate_id and len(files) != 1:
        raise ValueError("--candidate-id 只能用于单份简历")
    if args.candidate_name and len(files) != 1:
        raise ValueError("--candidate-name 只能用于单份简历")
    ids: set[int] = set()
    skipped = 0
    task_roles: dict[int, str] = {}
    for path in files:
        if args.role:
            _validate_explicit_role(path, args.role)
        role = args.role or infer_role(path)
        if role is None:
            skipped += 1
            store.record_watch_event("UNKNOWN_FILE_SKIPPED")
            continue
        source_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        jd_version, rubric_version = ROLE_VERSIONS[role]
        record = store.enqueue(
            TaskSpec(
                source_path=path,
                source_sha256=source_sha256,
                candidate_id=args.candidate_id or _candidate_id(path, source_sha256),
                candidate_name=args.candidate_name or infer_candidate_name(path),
                role=role,
                jd_version=jd_version,
                rubric_version=rubric_version,
            )
        )
        ids.add(record.task_id)
        task_roles[record.task_id] = role
    if args.role:
        _, rubric_version = ROLE_VERSIONS[args.role]
        print(f"登记 {len(ids)} 份简历；当前规则版本：{rubric_version}")
    else:
        role_counts: dict[str, int] = {}
        for role in task_roles.values():
            role_counts[role] = role_counts.get(role, 0) + 1
        distribution = "，".join(
            f"{role}={count}" for role, count in sorted(role_counts.items())
        )
        print(f"登记 {len(ids)} 份简历；岗位分布：{distribution or '无'}")
    if skipped:
        print(f"跳过 {skipped} 个无法识别岗位的文件（仅记录 UNKNOWN_FILE_SKIPPED）")
    return 0


def _validate_worker_args(args: argparse.Namespace) -> bool:
    watch = bool(args.watch)
    if args.input and not watch:
        raise ValueError("--input 只能与 --watch 一起使用")
    if args.input and not (args.role or args.auto_route):
        raise ValueError("--input 必须同时指定 --role 或 --auto-route")
    if (args.role or args.auto_route) and not args.input:
        raise ValueError("worker 的岗位路由只能与 --input 一起使用")
    if args.accept_unlabeled and not args.role:
        raise ValueError("--accept-unlabeled 只能与固定岗位 --role 一起使用")
    if args.max_tasks is not None and args.max_tasks < 1:
        raise ValueError("--max-tasks 必须是正整数")
    if args.lease_seconds < 5:
        raise ValueError("--lease-seconds 必须至少为 5 秒")
    if args.heartbeat_seconds <= 0:
        raise ValueError("--heartbeat-seconds 必须为正数")
    if args.provider_pause_seconds < 1:
        raise ValueError("--provider-pause-seconds 必须为正整数")
    return watch


def _print_worker_startup(store: TaskStore) -> None:
    print(
        "活动合同："
        + json.dumps(
            store.health_snapshot(
                active_contracts=ACTIVE_CONTRACTS, model_configured=True
            )["active_contract"],
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    print(
        "队列版本分布："
        + json.dumps(store.contract_distribution(), ensure_ascii=False, sort_keys=True)
    )


def _enqueue_watch_candidates(
    store: TaskStore, candidates: list[WatchCandidate]
) -> None:
    for candidate in candidates:
        try:
            jd_version, rubric_version = ROLE_VERSIONS[candidate.role]
            store.enqueue(
                TaskSpec(
                    source_path=candidate.source_path,
                    source_sha256=candidate.source_sha256,
                    candidate_id=candidate.candidate_id,
                    candidate_name=candidate.candidate_name,
                    role=candidate.role,
                    jd_version=jd_version,
                    rubric_version=rubric_version,
                )
            )
        except (OSError, ValueError):
            # A file can be renamed or replaced between scan and insert.  The
            # next scan will observe its new signature; this poll is isolated.
            store.record_watch_event("WATCH_ENQUEUE_ERROR")


class _WorkerHeartbeat:
    """Renew the database lease while a model call or scan is in progress."""

    def __init__(self, store: TaskStore, lease_id: str, interval: float):
        self.store = store
        self.lease_id = lease_id
        self.interval = interval
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="resume-screening-heartbeat",
            daemon=True,
        )

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.store.heartbeat(self.lease_id)
            except Exception:
                # The main loop will stop at its next safe boundary.  Do not
                # echo a database/provider diagnostic from the heartbeat thread.
                self._lost.set()
                return

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.interval + 1.0))


def _worker(args: argparse.Namespace, store: TaskStore, output: Path) -> int:
    watch = _validate_worker_args(args)
    if not os.environ.get("MINIMAX_API_KEY"):
        print(
            "错误：未设置 MINIMAX_API_KEY；队列状态未改变，health 会显示 NOT_CONFIGURED。",
            file=sys.stderr,
        )
        return 2

    _print_worker_startup(store)
    lease_id: str | None = None
    heartbeat: _WorkerHeartbeat | None = None
    try:
        lease_id = store.acquire_worker(
            active_contracts=ACTIVE_CONTRACTS,
            model_configured=True,
            lease_seconds=args.lease_seconds,
        )
        stale_count = store.mark_stale_contracts(ACTIVE_CONTRACTS, lease_id=lease_id)
        if stale_count:
            print(f"已汇总 {stale_count} 个旧合同未完成任务为 {STALE_CONTRACT_VERSION}")
        interrupted_count = store.requeue_stale(lease_id=lease_id)
        if interrupted_count:
            print(f"已将 {interrupted_count} 个中断中的任务转入人工复核")
        pipeline = ScreeningPipeline(
            store=store,
            client=MiniMaxClient(),
            output_root=output,
            project_root=PROJECT_ROOT,
            lease_id=lease_id,
        )
        scanner = None
        if args.input:
            scanner = WatchScanner(
                args.input,
                role=args.role,
                auto_route=bool(args.auto_route),
                accept_unlabeled=bool(args.accept_unlabeled),
                on_event=store.record_watch_event,
            )

        processed_task_ids: set[int] = set()
        pause_until = 0.0
        heartbeat_interval = max(
            0.5, min(args.heartbeat_seconds, 60.0, args.lease_seconds / 3)
        )
        heartbeat = _WorkerHeartbeat(store, lease_id, heartbeat_interval)
        heartbeat.start()
        while True:
            if heartbeat.lost:
                raise WorkerLeaseError("heartbeat renewal failed")
            now = time.monotonic()

            if pause_until > now:
                if scanner is not None:
                    # Provider throttling pauses model calls, not discovery:
                    # stable files continue to be retained in the queue.
                    _enqueue_watch_candidates(store, scanner.scan())
                time.sleep(min(max(0.1, args.poll_seconds), pause_until - now))
                continue
            if pause_until:
                store.clear_worker_pause(lease_id)
                pause_until = 0.0

            if scanner is not None:
                _enqueue_watch_candidates(store, scanner.scan())

            task = pipeline.process_next()
            if task is not None:
                processed_task_ids.add(task.task_id)
                print(
                    "task="
                    f"{task.task_id} candidate={sanitize_diagnostic(task.candidate_id)} "
                    f"status={task.status}"
                )
                store.heartbeat(lease_id, success=task.status == "succeeded")
                if heartbeat.lost:
                    raise WorkerLeaseError("heartbeat renewal failed")

                global_pause = {
                    "PROVIDER_AUTH_FAILED",
                    "PROVIDER_RATE_LIMITED",
                    "MODEL_CALL_REJECTED",
                }
                if task.error_code in global_pause:
                    if watch:
                        store.set_worker_pause(
                            lease_id,
                            reason=task.error_code,
                            pause_seconds=args.provider_pause_seconds,
                        )
                        pause_until = time.monotonic() + args.provider_pause_seconds
                        print(
                            f"模型处理已暂停：{task.error_code}；watch 保留已发现队列任务。",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"错误：{task.error_code}；当前批次已暂停，请先检查供应商状态。",
                            file=sys.stderr,
                        )
                        return 2
                elif (
                    task.error_code == "MODEL_RETRYABLE" and not watch
                ):
                    print(
                        "错误：MODEL_RETRYABLE；请使用 retry-failed 后重试。",
                        file=sys.stderr,
                    )
                    return 2

                if (
                    args.max_tasks is not None
                    and len(processed_task_ids) >= args.max_tasks
                    and task.status != "retryable_failed"
                ):
                    return 0
                continue
            if not watch:
                return 0
            time.sleep(max(0.1, min(args.poll_seconds, 60.0)))
    except KeyboardInterrupt:
        print("worker 已停止")
        return 0
    except WorkerAlreadyRunningError as exc:
        print(f"错误：{sanitize_diagnostic(exc)}", file=sys.stderr)
        return 2
    except WorkerLeaseError as exc:
        print(
            f"错误：worker 租约失效，已停止：{sanitize_diagnostic(exc)}",
            file=sys.stderr,
        )
        return 2
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        if lease_id is not None:
            store.release_worker(lease_id)


def _status(store: TaskStore) -> int:
    counts = store.status_counts()
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 0


def _health(args: argparse.Namespace, store: TaskStore) -> int:
    snapshot = store.health_snapshot(
        active_contracts=ACTIVE_CONTRACTS,
        processing_threshold_seconds=args.processing_threshold_seconds,
    )
    print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    return 0


def _export(args: argparse.Namespace, store: TaskStore) -> int:
    destination = Path(args.directory).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    rows = []
    review_rows = []
    envelopes = []
    for task, envelope in store.successful_results():
        envelope = copy.deepcopy(envelope)
        scorecard = envelope["scorecard"]
        record = envelope["screening_record"]
        candidate_name = task.candidate_name or infer_candidate_name(task.source_path)
        if candidate_name and not record.get("candidate_name"):
            record["candidate_name"] = candidate_name
        recommendation = record.get(
            "model_recommendation", record.get("recommendation")
        )
        rows.append(
            {
                "task_id": task.task_id,
                "candidate_id": task.candidate_id,
                "candidate_name": candidate_name or "",
                "role": task.role,
                "rubric_version": task.rubric_version,
                "score": scorecard["score"],
                "grade": scorecard["grade"],
                "recommendation": recommendation,
            }
        )
        review_rows.append(
            {
                "task_id": task.task_id,
                "candidate_id": task.candidate_id,
                "candidate_name": candidate_name or "",
                "role": task.role,
                "grade": scorecard["grade"],
                "recommendation": recommendation,
                "required_review": "level_1+level_2"
                if recommendation == "second_review"
                else "level_1",
                "error_code": "",
            }
        )
        envelopes.append(envelope)
    for task in store.manual_review_tasks():
        candidate_name = task.candidate_name or infer_candidate_name(task.source_path)
        review_rows.append(
            {
                "task_id": task.task_id,
                "candidate_id": task.candidate_id,
                "candidate_name": candidate_name or "",
                "role": task.role,
                "grade": "",
                "recommendation": "manual_review",
                "required_review": "source_or_model_output_review",
                "error_code": task.error_code or "",
            }
        )
    (destination / "summary.json").write_text(
        json.dumps(envelopes, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (destination / "summary.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "task_id",
                "candidate_id",
                "candidate_name",
                "role",
                "rubric_version",
                "score",
                "grade",
                "recommendation",
            ),
        )
        writer.writeheader()
        writer.writerows(rows)
    with (destination / "review_queue.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "task_id",
                "candidate_id",
                "candidate_name",
                "role",
                "grade",
                "recommendation",
                "required_review",
                "error_code",
            ),
        )
        writer.writeheader()
        writer.writerows(review_rows)
    print(f"导出 {len(rows)} 条成功记录到 {destination}")
    return 0


def _validate(args: argparse.Namespace) -> int:
    envelope = json.loads(Path(args.path).read_text(encoding="utf-8"))
    record = envelope.get("screening_record", envelope)
    errors = validate_record(PROJECT_ROOT, record.get("role"), record)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("结构化筛选记录有效")
    return 0


def _calibrate(args: argparse.Namespace, store: TaskStore) -> int:
    if args.calibration_action == "import":
        count = store.import_human_reviews(args.csv_path)
        print(f"导入 {count} 条人工结果（重复行自动忽略）")
        return 0
    report = store.calibration_report(minimum_sample_size=args.min_samples)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")
        print(f"校准报告已写入 {destination}")
    else:
        print(payload, end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_console_encoding()
    parser = _parser()
    args = parser.parse_args(argv)
    store = TaskStore(args.database)
    try:
        if args.command == "enqueue":
            return _enqueue(args, store)
        if args.command == "worker":
            return _worker(args, store, Path(args.output).resolve())
        if args.command == "status":
            return _status(store)
        if args.command == "health":
            return _health(args, store)
        if args.command == "retry-failed":
            count = store.retry_failed(args.task_id)
            print(f"重新入队 {count} 个未完成任务")
            return 0
        if args.command == "export":
            return _export(args, store)
        if args.command == "validate":
            return _validate(args)
        if args.command in {"calibrate", "calibration"}:
            return _calibrate(args, store)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"错误：{sanitize_diagnostic(exc)}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2
