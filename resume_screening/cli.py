"""Command-line interface for the resumable screening worker."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

from .cleaning import SUPPORTED_SUFFIXES
from .contracts import validate_record
from .metadata import infer_candidate_name, infer_role
from .minimax import MiniMaxClient
from .pipeline import ScreeningPipeline
from .queue import TaskSpec, TaskStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROLE_VERSIONS = {
    "ai-product-manager": ("ai-pm-2026-08-v2", "ai-pm-rubric-2026-08-18-v3"),
    "senior-fullstack-engineer": (
        "senior-fullstack-2026-08-14-v1",
        "senior-fullstack-2026-09-01-v8",
    ),
    "fullstack-development-intern": (
        "fullstack-intern-2026-08-14-v1",
        "fullstack-intern-2026-08-24-v4",
    ),
}


def _configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _candidate_id(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return f"candidate-{digest}"


def _local_today() -> date:
    return date.today()


def _modified_on(path: Path) -> date:
    return datetime.fromtimestamp(path.stat().st_mtime).date()


def _input_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for value in paths:
        path = Path(value).resolve()
        if path.is_dir():
            files.extend(
                item
                for item in sorted(path.iterdir())
                if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES
            )
        elif path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
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
        raise ValueError(
            f"文件名岗位 {filename_role} 与指定岗位冲突（{role}）：{path.name}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="resume-screening")
    parser.add_argument("--database", default="var/screening.sqlite3")
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
    mode.add_argument("--once", action="store_true", help="清空当前队列后退出")
    mode.add_argument("--watch", action="store_true", help="持续监听队列")
    worker.add_argument(
        "--max-tasks",
        type=int,
        help="本次最多处理的不同任务数；适合先运行小批次验证",
    )
    worker.add_argument("--poll-seconds", type=float, default=5.0)
    worker.add_argument("--input", help="watch 模式下持续扫描此简历目录")
    worker.add_argument(
        "--role", choices=sorted(ROLE_VERSIONS), help="--input 对应的固定岗位"
    )

    subparsers.add_parser("status", help="显示任务状态")

    retry = subparsers.add_parser("retry-failed", help="显式重置未完成的可重试任务")
    retry.add_argument("--task-id", type=int)

    export = subparsers.add_parser("export", help="导出成功结果")
    export.add_argument("--directory", default="exports")

    validate = subparsers.add_parser("validate", help="校验 screening.json")
    validate.add_argument("path")
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
    skipped: list[Path] = []
    task_roles: dict[int, str] = {}
    for path in files:
        if args.role:
            _validate_explicit_role(path, args.role)
        role = args.role or infer_role(path)
        if role is None:
            skipped.append(path)
            continue
        jd_version, rubric_version = ROLE_VERSIONS[role]
        record = store.enqueue(
            TaskSpec(
                source_path=path,
                candidate_id=args.candidate_id or _candidate_id(path),
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
        print(f"跳过 {len(skipped)} 个无法识别岗位的文件：")
        for path in skipped:
            print(f"- {path.name}")
    return 0


def _worker(args: argparse.Namespace, store: TaskStore, output: Path) -> int:
    if not os.environ.get("MINIMAX_API_KEY"):
        print("错误：未设置 MINIMAX_API_KEY；队列状态未改变。", file=sys.stderr)
        return 2
    store.requeue_stale()
    pipeline = ScreeningPipeline(
        store=store,
        client=MiniMaxClient(),
        output_root=output,
        project_root=PROJECT_ROOT,
    )
    watch = bool(args.watch)
    if args.input and not watch:
        raise ValueError("--input 只能与 --watch 一起使用")
    if args.input and not args.role:
        raise ValueError("--input 必须同时指定 --role")
    if args.role and not args.input:
        raise ValueError("worker 的 --role 只能与 --input 一起使用")
    if args.max_tasks is not None and args.max_tasks < 1:
        raise ValueError("--max-tasks 必须是正整数")
    watch_versions = ROLE_VERSIONS.get(args.role) if args.input else None
    processed_task_ids: set[int] = set()
    try:
        while True:
            if args.input and watch_versions:
                jd_version, rubric_version = watch_versions
                for path in _input_files([args.input]):
                    _validate_explicit_role(path, args.role)
                    store.enqueue(
                        TaskSpec(
                            source_path=path,
                            candidate_id=_candidate_id(path),
                            role=args.role,
                            jd_version=jd_version,
                            rubric_version=rubric_version,
                        )
                    )
            task = pipeline.process_next()
            if task is not None:
                processed_task_ids.add(task.task_id)
                print(
                    f"task={task.task_id} candidate={task.candidate_id} status={task.status}"
                )
                provider_batch_failure = task.error_code in {
                    "MODEL_CALL_REJECTED",
                    "MODEL_CALL_AMBIGUOUS",
                } or (
                    task.error_code == "MODEL_RETRYABLE"
                    and task.status == "manual_review"
                )
                if provider_batch_failure:
                    print(
                        f"错误：{task.error_code}；为避免批量重复失败，worker 已停止。",
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
                break
            time.sleep(max(0.5, min(args.poll_seconds, 60.0)))
    except KeyboardInterrupt:
        print("worker 已停止")
    return 0


def _status(store: TaskStore) -> int:
    counts = store.status_counts()
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
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
        if args.command == "retry-failed":
            count = store.retry_failed(args.task_id)
            print(f"重新入队 {count} 个未完成任务")
            return 0
        if args.command == "export":
            return _export(args, store)
        if args.command == "validate":
            return _validate(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2
