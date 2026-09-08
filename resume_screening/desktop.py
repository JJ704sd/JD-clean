"""Chinese desktop entry point. All Tk calls stay on the main thread."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
from contextlib import closing
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .cleaning import SUPPORTED_SUFFIXES
from .desktop_model import (
    ModelConfig,
    configured_client,
    load_config,
    run_analysis,
    save_config,
)
from .desktop_store import (
    DECISIONS,
    ROLES,
    ReviewStore,
    atomic_text,
    data_root,
    resource_root,
)
from .watch import WatchScanner, is_ignored_watch_file

VERSION = "0.2.0-preview"
_BATCH_PAUSE_ERROR_CODES = frozenset(
    {"PROVIDER_AUTH_FAILED", "PROVIDER_RATE_LIMITED"}
)


class InstanceLock:
    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.stream = (root / "desktop.lock").open("a+b")
        try:
            self.stream.seek(0)
            if not self.stream.read(1):
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise ValueError("已有应用使用此数据目录，请返回已打开的窗口") from None

    def close(self):
        self.stream.close()


def open_path(path):
    path = str(Path(path).resolve())
    if os.name == "nt":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


class DesktopApp:
    def __init__(self, window, root):
        self.window = window
        self.store = ReviewStore(root)
        self.store.recover()
        self.events = queue.Queue()
        self.busy = False
        self.stop = threading.Event()
        self.exit_pending = False
        self.current = None
        self.current_criterion = None
        self.evidence = []
        self.dirty = False
        self.autosave_id = None
        self.window.title("简历工作台 · 本地整理与人工审阅")
        self.window.geometry(
            f"{min(1240, window.winfo_screenwidth() - 40)}x{min(760, window.winfo_screenheight() - 100)}+10+10"
        )
        self.window.minsize(980, 640)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(window)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f5f8")
        style.configure(
            "TLabel",
            background="#f3f5f8",
            font=("Microsoft YaHei UI" if os.name == "nt" else "Helvetica", 10),
        )
        style.configure("TButton", padding=(10, 6))
        style.configure("Treeview", rowheight=29)
        style.configure(
            "Title.TLabel",
            font=("Microsoft YaHei UI" if os.name == "nt" else "Helvetica", 20, "bold"),
        )
        outer = ttk.Frame(window, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="简历工作台", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer, text="无需 API 即可整理与审阅 · 材料保存在本机 · AI 分析按需启用"
        ).pack(anchor="w", pady=(4, 12))
        self.status = tk.StringVar(value="本地模式已就绪，无需配置 API。")
        ttk.Label(outer, textvariable=self.status, wraplength=1160).pack(
            side="bottom", anchor="w", pady=(8, 0)
        )
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True)
        self.tasks = ttk.Frame(self.tabs, padding=10)
        self.settings = ttk.Frame(self.tabs, padding=16)
        self.feishu = ttk.Frame(self.tabs, padding=16)
        self.history = ttk.Frame(self.tabs, padding=12)
        for page, title in (
            (self.tasks, "材料与审阅"),
            (self.settings, "模型与设置"),
            (self.history, "AI 历史"),
            (self.feishu, "飞书（可选）"),
        ):
            self.tabs.add(page, text=title)
        self.make_tasks()
        self.make_settings()
        self.make_history()
        self.make_feishu()
        self.refresh()
        self.poll_id = window.after(100, self.poll)
        window.bind("<Destroy>", self.on_destroy, add="+")

    def on_destroy(self, event):
        if event.widget is self.window:
            self.window.after_cancel(self.poll_id)
            if self.autosave_id is not None:
                self.window.after_cancel(self.autosave_id)

    def make_tasks(self):
        bar = ttk.Frame(self.tasks)
        bar.pack(fill="x")
        self.role = tk.StringVar(value="按文件名自动分流")
        ttk.Combobox(
            bar,
            textvariable=self.role,
            values=["按文件名自动分流", *ROLES.values()],
            state="readonly",
            width=23,
        ).pack(side="left", padx=(0, 8))
        for title, command in (
            ("导入文件", self.import_files),
            ("导入目录", self.import_folder),
            ("监听目录", self.watch_folder),
            ("停止接收", self.stop_work),
            ("导出人工审阅", self.export),
        ):
            ttk.Button(bar, text=title, command=command).pack(side="left", padx=3)
        self.documents = ttk.Treeview(
            self.tasks,
            columns=("name", "role", "status"),
            show="headings",
            height=3,
            selectmode="extended",
        )
        for key, title, width in (
            ("name", "材料", 510),
            ("role", "岗位", 230),
            ("status", "状态", 150),
        ):
            self.documents.heading(key, text=title)
            self.documents.column(key, width=width)
        self.documents.pack(fill="x", pady=10)
        self.documents.bind("<<TreeviewSelect>>", self.select_document)
        actions = ttk.Frame(self.tasks)
        actions.pack(fill="x")
        self.search = tk.StringVar()
        ttk.Entry(actions, textvariable=self.search, width=22).pack(side="left")
        ttk.Button(actions, text="查找原文线索", command=self.find_text).pack(
            side="left", padx=4
        )
        ttk.Button(actions, text="打开原文件副本", command=self.open_source).pack(
            side="left", padx=4
        )
        ttk.Button(actions, text="查看岗位规则", command=self.show_rules).pack(
            side="left", padx=4
        )
        ttk.Button(actions, text="对选中材料进行 AI 分析", command=self.analyze).pack(
            side="right"
        )
        pane = ttk.Panedwindow(self.tasks, orient="horizontal")
        pane.pack(fill="both", expand=True, pady=(8, 0))
        left, right = ttk.Frame(pane), ttk.Frame(pane)
        pane.add(left, weight=1)
        pane.add(right, weight=1)
        ttk.Label(left, text="本地提取文本（含页码，联系方式已脱敏）").pack(anchor="w")
        self.content = ScrolledText(
            left,
            wrap="word",
            width=46,
            height=10,
            font=("Microsoft YaHei UI" if os.name == "nt" else "Helvetica", 10),
        )
        self.content.pack(fill="both", expand=True, pady=5)
        self.content.configure(state="disabled")
        ttk.Label(right, text="人工审阅 · 检索命中不代表满足要求").pack(anchor="w")
        self.criteria = ttk.Treeview(
            right,
            columns=("criterion", "decision"),
            show="headings",
            height=3,
            selectmode="browse",
        )
        self.criteria.heading("criterion", text="岗位检查项")
        self.criteria.heading("decision", text="人工结论")
        self.criteria.column("criterion", width=310)
        self.criteria.column("decision", width=95)
        self.criteria.pack(fill="x", pady=5)
        self.criteria.bind("<<TreeviewSelect>>", self.select_criterion)
        self.decision = tk.StringVar(value="未审阅")
        self.decision.trace_add("write", lambda *_: self.mark_dirty())
        ttk.Combobox(
            right, textvariable=self.decision, values=DECISIONS, state="readonly"
        ).pack(anchor="w")
        ttk.Label(right, text="当前检查项的原文证据 / 人工备注 / 待追问事项").pack(
            anchor="w", pady=(6, 0)
        )
        self.note = ScrolledText(right, wrap="word", height=3)
        self.note.pack(fill="both", expand=True)
        self.note.bind("<KeyRelease>", lambda _: self.mark_dirty())
        footer = ttk.Frame(right)
        footer.pack(fill="x", pady=5)
        ttk.Label(footer, text="审阅者").pack(side="left")
        self.reviewer = tk.StringVar()
        ttk.Entry(footer, textvariable=self.reviewer, width=15).pack(
            side="left", padx=4
        )
        self.reviewer.trace_add("write", lambda *_: self.mark_dirty())
        ttk.Button(
            footer, text="保存审阅（含历史修订）", command=self.save_review
        ).pack(side="right")
        ttk.Label(right, text="整体人工意见（不生成 AI 分数）").pack(anchor="w")
        self.opinion = ttk.Entry(right)
        self.opinion.pack(fill="x")
        self.opinion.bind("<KeyRelease>", lambda _: self.mark_dirty())

    def make_settings(self):
        ttk.Label(
            self.settings,
            text="本地整理无需任何配置。只有点击测试或 AI 分析才调用模型。",
            wraplength=950,
        ).pack(anchor="w", pady=(0, 16))
        try:
            config = load_config(self.store.root)
        except (ValueError, OSError, TypeError):
            config = ModelConfig()
            self.status.set("模型配置无法读取，请重新填写；本地功能可用")
        self.provider = tk.StringVar(value=config.provider)
        self.base = tk.StringVar(value=config.base_url)
        self.model = tk.StringVar(value=config.model)
        self.key = tk.StringVar()
        form = ttk.Frame(self.settings)
        form.pack(anchor="w", fill="x")
        for index, (label, var) in enumerate(
            (
                ("协议", self.provider),
                ("HTTPS Base URL", self.base),
                ("模型名称", self.model),
                ("API Key", self.key),
            )
        ):
            ttk.Label(form, text=label).grid(
                row=index, column=0, sticky="w", pady=8, padx=(0, 16)
            )
            if index == 0:
                widget = ttk.Combobox(
                    form,
                    textvariable=var,
                    values=["openai-compatible", "minimax"],
                    state="readonly",
                    width=62,
                )
            else:
                widget = ttk.Entry(
                    form, textvariable=var, width=65, show="•" if index == 3 else ""
                )
            widget.grid(row=index, column=1, sticky="w")
        ttk.Label(
            self.settings,
            text="Base URL 通常以 /v1 结尾。Key 留空可使用已保存凭据；更换端点需重新填写。\n密钥保存到系统凭据库，不写入普通配置和导出。",
            wraplength=950,
        ).pack(anchor="w", pady=12)
        bar = ttk.Frame(self.settings)
        bar.pack(anchor="w")
        ttk.Button(bar, text="保存模型配置", command=self.save_model).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(
            bar, text="测试连接（可能产生少量费用）", command=self.test_model
        ).pack(side="left")
        ttk.Button(
            self.settings,
            text="继续本地整理，无需 API",
            command=lambda: self.tabs.select(self.tasks),
        ).pack(anchor="w", pady=20)
        ttk.Label(
            self.settings,
            text=f"版本：{VERSION}\n本机数据：{self.store.root}",
            wraplength=950,
        ).pack(anchor="w", pady=8)
        ttk.Button(
            self.settings,
            text="打开数据目录",
            command=lambda: open_path(self.store.root),
        ).pack(anchor="w")
        ttk.Button(self.settings, text="导出脱敏诊断", command=self.diagnostics).pack(
            anchor="w", pady=8
        )
        ttk.Button(self.settings, text="备份本机审阅数据", command=self.backup_data).pack(
            anchor="w", pady=3
        )
        ttk.Button(self.settings, text="从备份恢复审阅数据", command=self.restore_data).pack(
            anchor="w", pady=3
        )

    def make_history(self):
        ttk.Label(
            self.history,
            text="每次 AI 分析独立保存；模型格式失败、需复核与成功分别显示。重新分析会再次调用收费服务。",
        ).pack(anchor="w")
        self.runs = ttk.Treeview(
            self.history, columns=("date", "model", "status"), show="headings", height=4
        )
        for key, title in (("date", "时间"), ("model", "模型"), ("status", "状态")):
            self.runs.heading(key, text=title)
        self.runs.pack(fill="x", pady=10)
        self.runs.bind("<<TreeviewSelect>>", self.show_run)
        self.run_preview = ScrolledText(self.history, wrap="word", height=10)
        self.run_preview.pack(fill="both", expand=True, pady=(0, 8))
        self.run_preview.configure(state="disabled")
        ttk.Button(
            self.history, text="打开选中运行的结果和状态记录", command=self.open_run
        ).pack(anchor="w")
        ttk.Button(self.history, text="导出选中 AI 运行", command=self.export_run).pack(
            anchor="w", pady=4
        )

    def make_feishu(self):
        from .desktop_feishu import make_feishu_page

        make_feishu_page(self)

    def get_role(self):
        return next(
            (key for key, value in ROLES.items() if value == self.role.get()), None
        )

    def start_job(self, work):
        if self.busy:
            messagebox.showinfo("任务正在运行", "请等待当前任务结束，或点击停止接收。")
            return
        self.busy = True
        self.stop.clear()

        def run():
            try:
                result = work()
                self.events.put(("done", str(result or "处理完成")))
            except Exception as exc:  # noqa: BLE001 -- UI boundary must redact unexpected provider errors.
                # Known app errors are authored messages; never echo unexpected transport/parser text.
                value = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else f"{type(exc).__name__}：操作失败，请检查配置或材料"
                )
                self.events.put(("done", value))

        threading.Thread(target=run, name="resume-desk-worker", daemon=False).start()

    def poll(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "reset":
                self.reset_review()
                continue
            self.status.set(value)
            if kind == "progress":
                self.refresh()
            if kind == "done":
                self.busy = False
                self.refresh()
                if self.exit_pending:
                    self.window.destroy()
                    return
        self.poll_id = self.window.after(100, self.poll)

    def stop_work(self):
        self.stop.set()
        self.status.set("已停止接收新任务；当前任务完成后停止。")

    def reset_review(self):
        """Clear widgets after a data restore has replaced the local database."""
        self.current = None
        self.current_criterion = None
        self.evidence = []
        self.dirty = False
        for document_id in self.documents.selection():
            self.documents.selection_remove(document_id)
        self.criteria.delete(*self.criteria.get_children())
        self.content.configure(state="normal")
        self.content.delete("1.0", "end")
        self.content.configure(state="disabled")
        self.note.delete("1.0", "end")
        self.reviewer.set("")
        self.opinion.delete(0, "end")

    def import_files(self):
        paths = filedialog.askopenfilenames(
            filetypes=[("简历", "*.pdf *.docx *.txt *.md")]
        )
        if paths:
            self.prepare_files([Path(p) for p in paths])

    def import_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.prepare_files(
                [
                    p
                    for p in Path(folder).iterdir()
                    if p.is_file()
                    and not is_ignored_watch_file(p)
                    and p.suffix.lower() in SUPPORTED_SUFFIXES
                ]
            )

    def prepare_files(self, paths):
        role = self.get_role()
        if not paths:
            self.status.set("没有支持的简历文件")
            return
        if not messagebox.askokcancel(
            "开始本地整理",
            f"共 {len(paths)} 个文件\n岗位：{self.role.get()}\n仅在本机处理，不调用模型。岗位冲突将跳过。",
        ):
            return

        def work():
            failures = []
            processed = 0
            for index, path in enumerate(paths, 1):
                if self.stop.is_set():
                    break
                self.events.put(("progress", f"正在本地整理 {index}/{len(paths)}"))
                try:
                    record = self.store.prepare(path, role)
                    processed += 1
                    if record["status"] == "解析失败":
                        failures.append(f"{path.name}：{record['error']}")
                except ValueError as exc:
                    failures.append(f"{path.name}：{exc}")
                except OSError:
                    failures.append(
                        f"{path.name}：文件操作失败，请检查文件是否存在、访问权限和磁盘空间。"
                    )
            if failures:
                atomic_text(
                    self.store.root / "last-import-report.txt", "\n".join(failures)
                )
            return (
                f"已处理 {processed}/{len(paths)} 份，跳过或失败 {len(failures)} 份。详情见数据目录 last-import-report.txt。"
                if failures
                else f"本地整理结束：{processed}/{len(paths)} 份；请进行人工审阅。"
            )

        self.start_job(work)

    def watch_folder(self):
        if self.busy:
            return
        folder = filedialog.askdirectory()
        if not folder:
            return
        role = self.get_role()
        scanner = WatchScanner(
            folder, role=role, auto_route=role is None, accept_unlabeled=False
        )

        def work():
            count = 0
            while not self.stop.is_set():
                for item in scanner.scan():
                    if self.stop.is_set():
                        break
                    self.store.prepare(item.source_path, item.role)
                    count += 1
                self.events.put(
                    (
                        "progress",
                        f"监听中 · 已整理 {count} 份 · 跳过统计 {scanner.event_counts} · 无标签文件请手动导入",
                    )
                )
                self.stop.wait(5)
            return "目录监听已停止"

        self.start_job(work)

    def refresh(self):
        existing = set(self.documents.get_children())
        records = self.store.list_documents()
        active = {record["id"] for record in records}
        for document_id in existing - active:
            self.documents.delete(document_id)
        for record in records:
            values = (record["name"], ROLES[record["role"]], record["status"])
            if record["id"] in existing:
                self.documents.item(record["id"], values=values)
            else:
                self.documents.insert("", "end", iid=record["id"], values=values)
        self.runs.delete(*self.runs.get_children())
        with closing(self.store.connect()) as db:
            for row in db.execute("SELECT * FROM ai_runs ORDER BY created DESC"):
                config = json.loads(row["config"])
                labels = {
                    "succeeded": "AI 结果已生成（非最终）",
                    "manual_review": "需人工核对",
                    "retryable_failed": "失败，等待显式重试",
                }
                self.runs.insert(
                    "",
                    "end",
                    iid=row["id"],
                    values=(
                        row["created"],
                        config["model"],
                        labels.get(row["status"], row["status"]),
                    ),
                )

    def select_document(self, _=None):
        selection = self.documents.selection()
        if not selection or selection[0] == self.current:
            return
        if self.dirty:
            self.save_draft_now()
        if self.dirty and not messagebox.askyesno(
            "草稿已保存", "当前编辑已保存为草稿，是否切换材料？"
        ):
            if self.current:
                self.documents.selection_set(self.current)
            return
        self.current = selection[0]
        self.current_criterion = None
        record = self.store.get(self.current)
        self.content.configure(state="normal")
        self.content.delete("1.0", "end")
        readable = (
            record["content"].partition("\n---\n")[2].strip() or record["content"]
        )
        self.content.insert("1.0", readable or record["error"])
        self.content.configure(state="disabled")
        material = self.store.role_material(record["role"])
        items = re.findall(
            r"^\|\s*`?([A-Z][A-Z0-9-]+-\d+)`?\s*\|\s*([^|]+)\|", material, re.MULTILINE
        )
        review = self.store.latest_review(self.current)
        draft = self.store.get_draft(self.current)
        saved_state = draft or review
        restored_draft = False
        try:
            self.evidence = (
                json.loads(saved_state["evidence"])
                if saved_state
                else [
                    {
                        "criterion": f"{code} {title.strip()}",
                        "decision": "未审阅",
                        "note": "",
                    }
                    for code, title in items
                ]
            )
            restored_draft = bool(draft)
        except (TypeError, ValueError, json.JSONDecodeError):
            self.evidence = []
            saved_state = review
            self.status.set("自动保存草稿格式异常，已回退到最近一次正式审阅。")
        if not isinstance(self.evidence, list):
            self.evidence = []
        if not self.evidence:
            self.evidence = [
                {
                    "criterion": "岗位要求整体核对（见岗位规则）",
                    "decision": "未审阅",
                    "note": "",
                }
            ]
        self.criteria.delete(*self.criteria.get_children())
        for index, item in enumerate(self.evidence):
            self.criteria.insert(
                "", "end", iid=str(index), values=(item["criterion"], item["decision"])
            )
        self.reviewer.set(saved_state["reviewer"] if saved_state else "")
        self.opinion.delete(0, "end")
        self.opinion.insert(0, saved_state["opinion"] if saved_state else "")
        self.note.delete("1.0", "end")
        self.criteria.selection_set("0")
        self.dirty = False
        if self.autosave_id is not None:
            self.window.after_cancel(self.autosave_id)
            self.autosave_id = None
        if restored_draft:
            self.status.set("已恢复自动保存草稿；点击保存审阅后形成正式修订记录。")

    def mark_dirty(self):
        if self.current:
            self.dirty = True
            if self.autosave_id is not None:
                self.window.after_cancel(self.autosave_id)
            self.autosave_id = self.window.after(700, self.autosave_draft)

    def autosave_draft(self):
        self.autosave_id = None
        if self.current and self.dirty:
            self.save_draft_now()
            self.status.set("审阅草稿已自动保存；点击保存审阅后形成正式修订记录。")

    def save_draft_now(self):
        if self.autosave_id is not None:
            self.window.after_cancel(self.autosave_id)
            self.autosave_id = None
        if self.current and self.dirty:
            self.flush_criterion()
            self.store.save_draft(
                self.current, self.reviewer.get(), self.opinion.get(), self.evidence
            )

    def flush_criterion(self):
        if self.current_criterion is not None:
            item = self.evidence[self.current_criterion]
            item.update(
                decision=self.decision.get(), note=self.note.get("1.0", "end-1c")
            )
            self.criteria.item(
                str(self.current_criterion),
                values=(item["criterion"], item["decision"]),
            )

    def select_criterion(self, _=None):
        selected = self.criteria.selection()
        if not selected:
            return
        self.flush_criterion()
        dirty = self.dirty
        self.current_criterion = int(selected[0])
        item = self.evidence[self.current_criterion]
        self.decision.set(item["decision"])
        self.note.delete("1.0", "end")
        self.note.insert("1.0", item["note"])
        self.dirty = dirty

    def save_review(self):
        if not self.current:
            return
        self.flush_criterion()
        try:
            self.store.save_review(
                self.current, self.reviewer.get(), self.opinion.get(), self.evidence
            )
        except ValueError as exc:
            messagebox.showerror("无法保存", str(exc))
            return
        self.dirty = False
        self.refresh()
        self.status.set("人工审阅已保存，历史修订保留。")

    def find_text(self):
        self.content.tag_remove("match", "1.0", "end")
        query = self.search.get().strip()
        if not query:
            return
        start, count = "1.0", 0
        while True:
            position = self.content.search(query, start, stopindex="end", nocase=True)
            if not position:
                break
            end = f"{position}+{len(query)}c"
            self.content.tag_add("match", position, end)
            if count == 0:
                self.content.see(position)
            start, count = end, count + 1
        self.content.tag_configure("match", background="#ffe39b", foreground="#202020")
        self.status.set(f"找到 {count} 处线索。命中不代表符合，未命中也不代表不具备。")

    def open_source(self):
        if self.current:
            open_path(self.store.get(self.current)["snapshot"])

    def show_rules(self):
        if self.current:
            popup = tk.Toplevel(self.window)
            popup.title("岗位规则 · 当前版本原文")
            text = ScrolledText(popup, width=105, height=35, wrap="word")
            text.pack(fill="both", expand=True)
            text.insert(
                "1.0", self.store.role_material(self.store.get(self.current)["role"])
            )
            text.configure(state="disabled")

    def export(self):
        if self.busy:
            messagebox.showinfo("任务正在运行", "请停止接收并等待当前任务结束后导出。")
            return
        folder = filedialog.askdirectory(title="选择导出目录")
        if folder:
            self.start_job(lambda: f"已导出：{self.store.export(Path(folder))}")

    def model_config(self):
        return ModelConfig(
            self.provider.get(), self.base.get().strip(), self.model.get().strip()
        )

    def save_model(self):
        try:
            save_config(self.store.root, self.model_config(), self.key.get())
            self.key.set("")
            self.status.set("配置已保存，密钥存入系统凭据库。")
        except Exception as exc:  # noqa: BLE001 -- OS keychain errors must not leak credential arguments.
            messagebox.showerror(
                "配置未保存",
                str(exc)
                if isinstance(exc, ValueError)
                else "系统凭据库不可用，请检查系统权限",
            )

    def test_model(self):
        if self.busy or not messagebox.askokcancel(
            "连接测试", "将发送合成短文本，可能产生少量 API 费用。是否测试？"
        ):
            return
        config, key = self.model_config(), self.key.get()
        self.start_job(lambda: configured_client(config, key).test())

    def analyze(self):
        if self.busy:
            return
        selected = list(self.documents.selection())
        if not selected and self.current:
            selected = [self.current]
        if not selected:
            messagebox.showinfo("未选择材料", "请先选择一份或多份已整理成功的材料。")
            return
        config, key = self.model_config(), self.key.get()
        try:
            _ = config.endpoint
        except ValueError as exc:
            messagebox.showinfo("请先配置模型", str(exc))
            self.tabs.select(self.settings)
            return
        records = [self.store.get(document_id) for document_id in selected]
        ready = [
            record
            for record in records
            if record["status"] in ("待人工审阅", "人工审阅完成")
        ]
        if not ready:
            messagebox.showinfo(
                "没有可分析材料", "所选材料尚未整理成功，请先完成本地整理。"
            )
            return
        duplicates = [
            record
            for record in ready
            if self.store.find_ai_runs(
                record["id"],
                config_identity=config.identity,
                provider=config.provider,
                base_url=config.base_url,
                model=config.model,
            )
        ]
        skipped = len(records) - len(ready)
        duplicate_text = (
            f"其中 {len(duplicates)} 份已有相同端点和模型的历史运行，再次分析会再次计费。\n"
            if duplicates
            else ""
        )
        skipped_text = f"另有 {skipped} 份未整理成功，将跳过。\n" if skipped else ""
        if not messagebox.askokcancel(
            "开始 AI 分析",
            f"准备分析：{len(ready)} 份材料\n"
            f"模型：{config.model}\n端点：{config.endpoint}\n"
            f"{duplicate_text}"
            f"{skipped_text}"
            "将发送选中材料的脱敏文本并产生 API 费用。是否继续？",
        ):
            return

        def work():
            client = configured_client(config, key)
            counts = {}
            errors = []
            completed = 0
            paused_reason = None
            for index, record in enumerate(ready, 1):
                if self.stop.is_set():
                    break
                self.events.put(
                    ("progress", f"正在 AI 分析 {index}/{len(ready)}：{record['name']}")
                )
                try:
                    outcome = run_analysis(
                        self.store, record["id"], config, client
                    )
                    status, _folder = outcome
                    counts[status] = counts.get(status, 0) + 1
                    completed += 1
                    error_code = getattr(outcome, "error_code", None)
                    if error_code in _BATCH_PAUSE_ERROR_CODES:
                        paused_reason = error_code
                        errors.append(f"{record['name']}：{error_code}")
                        break
                except ValueError as exc:
                    errors.append(f"{record['name']}：{exc}")
                except Exception as exc:  # noqa: BLE001 -- isolate each paid run.
                    errors.append(
                        f"{record['name']}：{type(exc).__name__}，请在 AI 历史中核对状态"
                    )
            detail = "、".join(f"{status} {count} 份" for status, count in counts.items())
            if not detail:
                detail = "未完成"
            message = f"AI 批量分析结束：完成 {completed}/{len(ready)} 份（{detail}）。"
            if self.stop.is_set() and completed < len(ready):
                message += " 已停止接收后续材料。"
            if paused_reason:
                message += (
                    f" 供应商错误 {paused_reason}，批次已暂停；"
                    "修正配置或等待限流恢复后请显式重新发起。"
                )
            if errors:
                message += " 失败：" + "；".join(errors[:3])
                if len(errors) > 3:
                    message += f"；另有 {len(errors) - 3} 份失败"
            return message

        self.start_job(work)

    def open_run(self):
        selection = self.runs.selection()
        if selection:
            with closing(self.store.connect()) as db:
                row = db.execute(
                    "SELECT directory FROM ai_runs WHERE id=?", (selection[0],)
                ).fetchone()
            if row:
                open_path(row[0])

    def selected_run(self):
        selected = self.runs.selection()
        if not selected:
            return None
        with closing(self.store.connect()) as db:
            row = db.execute(
                "SELECT * FROM ai_runs WHERE id=?", (selected[0],)
            ).fetchone()
            return dict(row) if row else None

    def show_run(self, _=None):
        row = self.selected_run()
        if not row:
            return
        folder = Path(row["directory"])
        result = folder / "outputs" / row["document_id"] / "conclusion.md"
        text = f"运行状态：{row['status']}\n"
        if result.exists():
            text += result.read_text(encoding="utf-8")
        elif (folder / "queue.sqlite3").exists():
            with closing(sqlite3.connect(folder / "queue.sqlite3")) as db:
                task = db.execute(
                    "SELECT status,error_code,error_message FROM tasks ORDER BY task_id DESC LIMIT 1"
                ).fetchone()
            if task:
                text += f"任务状态：{task[0]}\n错误代码：{task[1] or ''}\n原因：{task[2] or ''}\n"
            text += "未生成合格的 AI 结论。可继续本地人工审阅；重新分析会再次调用 API。"
        self.run_preview.configure(state="normal")
        self.run_preview.delete("1.0", "end")
        self.run_preview.insert("1.0", text)
        self.run_preview.configure(state="disabled")

    def export_run(self):
        row = self.selected_run()
        if not row or self.busy:
            return
        selected = filedialog.askdirectory(title="选择 AI 结果导出目录")
        if selected:
            target = Path(selected) / ("ai-run-" + row["id"])
            if target.exists():
                messagebox.showinfo("已导出", "此目录已有该运行，请选择其他位置。")
                return
            self.start_job(lambda: shutil.copytree(row["directory"], target))

    def diagnostics(self):
        payload = {
            "version": VERSION,
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "resource_skills_available": (resource_root() / "skills").is_dir(),
            "busy": self.busy,
            "documents": len(self.store.list_documents()),
        }
        path = self.store.root / "diagnostics.json"
        atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
        self.status.set(f"诊断已保存：{path}（不含密钥与简历正文）")

    def backup_data(self):
        if self.busy:
            messagebox.showinfo("任务正在运行", "请等待当前任务结束后再备份。")
            return
        folder = filedialog.askdirectory(title="选择备份保存目录")
        if not folder:
            return
        self.start_job(
            lambda: f"审阅数据已备份：{self.store.backup(Path(folder))}（不含 API Key 和飞书凭据）"
        )

    def restore_data(self):
        if self.busy:
            messagebox.showinfo("任务正在运行", "请等待当前任务结束后再恢复。")
            return
        archive = filedialog.askopenfilename(
            title="选择 ResumeDesk 备份",
            filetypes=[("ResumeDesk 备份", "*.zip"), ("所有文件", "*.*")],
        )
        if not archive:
            return
        if not messagebox.askokcancel(
            "恢复审阅数据",
            "恢复前会自动备份当前数据，恢复会替换本机材料、审阅和 AI 历史。\n"
            "备份不包含 API Key 和飞书凭据。是否继续？",
        ):
            return

        def work():
            rollback = self.store.restore(Path(archive))
            self.events.put(("reset", ""))
            return f"数据恢复完成；如需撤销，可使用自动回滚备份：{rollback}"

        self.start_job(work)

    def close(self):
        if self.dirty:
            try:
                self.save_draft_now()
            except (OSError, ValueError) as exc:
                messagebox.showerror("草稿未保存", str(exc))
                return
            if not messagebox.askyesno(
                "草稿已保存", "当前审阅草稿已自动保存，是否退出应用？"
            ):
                return
        if self.busy:
            if messagebox.askokcancel(
                "任务正在运行",
                "停止接收新任务，并在当前任务结束后退出？已发送模型请求不代表远端取消。",
            ):
                self.exit_pending = True
                self.stop_work()
        else:
            self.window.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--smoke-test",
        type=Path,
        help="write isolated synthetic offline test report and exit",
    )
    parser.add_argument("--credential-smoke-test", type=Path)
    args = parser.parse_args(argv)
    if args.credential_smoke_test:
        from .desktop_smoke import credential_smoke_test

        return credential_smoke_test(args.credential_smoke_test)
    if args.smoke_test:
        from .desktop_smoke import smoke_test

        return smoke_test(args.smoke_test)
    root = (args.data_dir or data_root()).resolve()
    window = tk.Tk()
    try:
        lock = InstanceLock(root)
    except ValueError as exc:
        window.withdraw()
        messagebox.showinfo("应用已打开", str(exc))
        window.destroy()
        return 1
    try:
        DesktopApp(window, root)
        window.mainloop()
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
