"""Optional Feishu preflight/import UI; never touches the default deployment."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .desktop_store import atomic_text
from .feishu_monitor import CliResponse, FeishuResumeMonitor, LarkCLI, MonitorConfig


class DesktopLarkCLI(LarkCLI):
    def __init__(self, executable, directory):
        super().__init__(executable)
        self.directory = directory

    def run(self, args, *, timeout=90):
        if (
            os.name == "nt"
            and self.executable.lower().endswith((".cmd", ".bat"))
            and any(any(char in str(arg) for char in '&|<>%^\r\n"') for arg in args)
        ):
            return CliResponse(
                2,
                {"ok": False, "error": {"message": "unsafe batch argument"}},
                "unsafe batch argument",
            )
        try:
            result = subprocess.run(
                [self.executable, *args],
                cwd=self.directory,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            try:
                payload = json.loads(result.stdout)
            except ValueError:
                payload = {
                    "ok": False,
                    "error": {"message": "CLI returned non-JSON; check login"},
                }
            return CliResponse(
                result.returncode,
                payload,
                "" if result.returncode == 0 else "CLI failed; check login",
                result.stdout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return CliResponse(
                2,
                {"ok": False, "error": {"message": "CLI unavailable or timeout"}},
                "CLI unavailable or timeout",
            )


def run_cycle(root, values, *, apply=False):
    for key in (
        "base_token",
        "table_id",
        "view_id",
        "folder_token",
        "pdf_directory",
        "job_prefix",
    ):
        if not values.get(key, "").strip():
            raise ValueError(f"请填写 {key}")
    source = Path(values["pdf_directory"]).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("简历目录不存在")
    executable = values.get("cli_executable") or shutil.which(
        "lark-cli.cmd" if os.name == "nt" else "lark-cli"
    )
    if not executable or not Path(executable).is_file():
        raise ValueError(
            "未找到 lark-cli。请先由管理员安装并完成登录，再选择可执行文件；本地整理不受影响。"
        )
    identity = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[
        :24
    ]
    folder = root / "feishu" / identity
    folder.mkdir(parents=True, exist_ok=True)
    config = MonitorConfig(
        **{
            k: values[k]
            for k in (
                "base_token",
                "table_id",
                "view_id",
                "folder_token",
                "job_prefix",
                "row_key_column",
                "link_column",
                "status_column",
                "error_column",
                "processed_at_column",
                "source_hash_column",
            )
        },
        pdf_directory=source,
        output_directory=folder / "outputs",
        state_path=folder / "state.json",
        report_path=folder / "report.json",
        history_path=folder / "history.jsonl",
        records_path=folder / "records.json",
        lock_path=folder / "cycle.lock",
        cli_executable=executable,
        dry_run=not apply,
        screening_enabled=False,
        screening_database=folder / "unused-ai.sqlite3",
        screening_output_directory=folder / "unused-ai",
    )
    report = FeishuResumeMonitor(
        config, cli=DesktopLarkCLI(executable, folder)
    ).run_cycle()
    return f"飞书：{report.get('cycle_status')} · {report.get('summary')} · 报告：{folder / 'report.json'}"


def make_feishu_page(app):
    ttk.Label(
        app.feishu,
        text="可选集成：需另行安装并登录 lark-cli。先预检查，再显式导入；不会自动写入 AI 结论。",
        wraplength=1050,
    ).pack(anchor="w", pady=(0, 12))
    defaults = {
        "base_token": "",
        "table_id": "",
        "view_id": "",
        "folder_token": "",
        "pdf_directory": "",
        "cli_executable": "",
        "job_prefix": "全栈工程师_深圳 15-25K",
        "row_key_column": "姓名",
        "link_column": "简历文档链接",
        "status_column": "处理状态",
        "error_column": "错误信息",
        "processed_at_column": "处理时间",
        "source_hash_column": "源 PDF 哈希",
    }
    path = app.store.root / "feishu-config.json"
    if path.exists():
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
            defaults.update({k: str(v) for k, v in values.items() if k in defaults})
        except (ValueError, OSError, AttributeError):
            pass
    fields = {}
    form = ttk.Frame(app.feishu)
    form.pack(fill="x")
    for index, (key, value) in enumerate(defaults.items()):
        fields[key] = tk.StringVar(value=value)
        ttk.Label(form, text=key).grid(row=index, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=fields[key], width=65).grid(
            row=index, column=1, sticky="w", padx=12, pady=3
        )
        if key in ("pdf_directory", "cli_executable"):

            def choose(k=key):
                selected = (
                    filedialog.askdirectory()
                    if k == "pdf_directory"
                    else filedialog.askopenfilename()
                )
                if selected:
                    fields[k].set(selected)

            ttk.Button(form, text="选择", command=choose).grid(row=index, column=2)

    def execute(apply=False):
        if app.busy:
            return
        values = {key: var.get().strip() for key, var in fields.items()}
        if apply and not messagebox.askokcancel(
            "导入飞书",
            "将向已配置的飞书文件夹创建文档，并按预检查结果回写对应表格字段。是否执行本轮？",
        ):
            return
        atomic_text(path, json.dumps(values, ensure_ascii=False, indent=2))
        app.start_job(lambda: run_cycle(app.store.root, values, apply=apply))

    bar = ttk.Frame(app.feishu)
    bar.pack(anchor="w", pady=16)
    ttk.Button(
        bar, text="保存并预检查（不写入飞书）", command=lambda: execute(False)
    ).pack(side="left", padx=(0, 8))
    ttk.Button(bar, text="执行本轮文档同步", command=lambda: execute(True)).pack(
        side="left"
    )
    ttk.Label(
        app.feishu,
        text="授权过期、表结构变化或字段不匹配时停止对应写入。人工审阅结果请从本地导出。",
        wraplength=1050,
    ).pack(anchor="w")
