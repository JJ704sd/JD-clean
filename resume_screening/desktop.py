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
    clear_saved_credential,
    configured_client,
    list_models,
    load_config,
    run_analysis,
    save_config,
    saved_credential_available,
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
from .versions import APP_VERSION, AI_OUTPUT_CONTRACT_VERSION

VERSION = APP_VERSION
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
        self.selected_documents: set[str] = set()
        self.active_import_batch: str | None = None
        self.active_ai_batch: str | None = None
        self.onboarding_dismissed = False
        self.search_matches: list[str] = []
        self.search_index = -1
        self.busy_widgets: list[tk.Widget] = []
        self.progress_value = tk.IntVar(value=0)
        self.progress_maximum = tk.IntVar(value=1)
        self.progress_text = tk.StringVar(value="")
        self.current = None
        self.current_criterion = None
        self.evidence = []
        self.dirty = False
        self.autosave_id = None
        self.window.title("简历工作台 · 本地整理与人工审阅")
        self.window.geometry(
            f"{min(1240, window.winfo_screenwidth() - 40)}x{min(760, window.winfo_screenheight() - 100)}+10+10"
        )
        self.window.minsize(900, 560)
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
        progress_bar = ttk.Progressbar(
            outer,
            mode="determinate",
            variable=self.progress_value,
            maximum=1,
        )
        self.progress_bar = progress_bar
        progress_bar.pack(side="bottom", fill="x", pady=(4, 0))
        ttk.Label(outer, textvariable=self.progress_text).pack(
            side="bottom", anchor="w", pady=(2, 0)
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
        self.window.bind_all("<Control-o>", lambda _event: self.import_files())
        self.window.bind_all("<Control-f>", lambda _event: self.focus_search())
        self.window.bind_all("<Control-s>", lambda _event: self.save_review())
        self.window.bind_all("<Control-e>", lambda _event: self.export())
        self.window.bind_all("<Control-Tab>", self.next_tab)
        self.poll_id = window.after(100, self.poll)
        window.bind("<Destroy>", self.on_destroy, add="+")

    def on_destroy(self, event):
        if event.widget is self.window:
            self.window.after_cancel(self.poll_id)
            if self.autosave_id is not None:
                self.window.after_cancel(self.autosave_id)

    def make_tasks(self):
        self.onboarding = ttk.LabelFrame(self.tasks, text="开始使用（可关闭）", padding=8)
        ttk.Label(
            self.onboarding,
            text=(
                "1 导入简历 → 2 本地解析/OCR → 3 可选批量 AI → "
                "4 人工确认 → 5 导出。支持 PDF、扫描 PDF、DOCX、TXT、MD；无需 API 也可完成整理和人工审阅。"
            ),
            wraplength=1120,
        ).pack(side="left", fill="x", expand=True)
        onboarding_import = ttk.Button(
            self.onboarding, text="导入文件", command=self.import_files
        )
        onboarding_import.pack(side="left", padx=4)
        self.busy_widgets.append(onboarding_import)
        ttk.Button(self.onboarding, text="关闭引导", command=self.dismiss_onboarding).pack(
            side="left", padx=4
        )
        self.onboarding.pack(fill="x", pady=(0, 6))
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
            ("删除选中", self.delete_selected),
            ("导出人工审阅", self.export),
            ("导出完整审计", self.export_audit),
        ):
            button = ttk.Button(bar, text=title, command=command)
            button.pack(side="left", padx=3)
            self.busy_widgets.append(button)
            if title == "停止接收":
                self.stop_button = button
        self.recursive = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="目录递归", variable=self.recursive).pack(
            side="right", padx=4
        )
        document_scroll = ttk.Frame(self.tasks)
        document_scroll.pack(fill="x", pady=10)
        self.documents = ttk.Treeview(
            document_scroll,
            columns=(
                "name",
                "role",
                "status",
                "ai_status",
                "ai_band",
                "ai_recommendation",
                "risk",
                "human_status",
                "updated",
            ),
            show="headings",
            height=3,
            selectmode="extended",
        )
        for key, title, width in (
            ("name", "材料", 260),
            ("role", "岗位", 150),
            ("status", "本地状态", 105),
            ("ai_status", "AI 状态", 95),
            ("ai_band", "AI 档位", 75),
            ("ai_recommendation", "AI 建议", 110),
            ("risk", "主要风险", 180),
            ("human_status", "人工状态", 90),
            ("updated", "更新时间", 145),
        ):
            self.documents.heading(key, text=title)
            self.documents.column(key, width=width)
        self.documents.pack(side="top", fill="x")
        document_xscroll = ttk.Scrollbar(
            document_scroll, orient="horizontal", command=self.documents.xview
        )
        document_xscroll.pack(side="bottom", fill="x")
        self.documents.configure(xscrollcommand=document_xscroll.set)
        self.documents.bind("<<TreeviewSelect>>", self.select_document)
        self.documents.bind("<Control-a>", lambda _: self.select_all_visible())
        self.documents.bind("<Escape>", lambda _: self.clear_selection())
        self.selection_status = tk.StringVar(value="已选 0 份")
        ttk.Label(self.tasks, textvariable=self.selection_status).pack(anchor="w")
        filters = ttk.Frame(self.tasks)
        filters.pack(fill="x", pady=(0, 4))
        ttk.Label(filters, text="筛选岗位").pack(side="left")
        self.filter_role = tk.StringVar(value="全部岗位")
        ttk.Combobox(
            filters,
            textvariable=self.filter_role,
            values=["全部岗位", *ROLES.values()],
            state="readonly",
            width=18,
        ).pack(side="left", padx=(5, 12))
        ttk.Label(filters, text="筛选状态").pack(side="left")
        self.filter_status = tk.StringVar(value="全部状态")
        ttk.Combobox(
            filters,
            textvariable=self.filter_status,
            values=[
                "全部状态",
                "整理中",
                "待人工审阅",
                "人工审阅完成",
                "编码待确认",
                "解析失败",
            ],
            state="readonly",
            width=16,
        ).pack(side="left", padx=5)
        ttk.Label(filters, text="筛选 AI").pack(side="left", padx=(10, 0))
        self.filter_ai = tk.StringVar(value="全部 AI")
        ttk.Combobox(
            filters,
            textvariable=self.filter_ai,
            values=["全部 AI", "未运行", "处理中", "成功", "需人工核对", "失败"],
            state="readonly",
            width=12,
        ).pack(side="left", padx=5)
        ttk.Label(filters, text="排序").pack(side="left", padx=(10, 0))
        self.sort_by = tk.StringVar(value="最近更新")
        ttk.Combobox(
            filters,
            textvariable=self.sort_by,
            values=["最近更新", "AI 优先级", "主要风险"],
            state="readonly",
            width=12,
        ).pack(side="left", padx=5)
        for variable in (self.filter_role, self.filter_status, self.filter_ai, self.sort_by):
            variable.trace_add("write", lambda *_: self.refresh())
        ttk.Button(
            filters,
            text="清除筛选",
            command=lambda: (
                self.filter_role.set("全部岗位"),
                self.filter_status.set("全部状态"),
                self.filter_ai.set("全部 AI"),
                self.sort_by.set("最近更新"),
            ),
        ).pack(side="left", padx=5)
        ttk.Button(filters, text="全选当前", command=self.select_all_visible).pack(
            side="right", padx=3
        )
        ttk.Button(filters, text="清空选择", command=self.clear_selection).pack(
            side="right", padx=3
        )
        actions = ttk.Frame(self.tasks)
        actions.pack(fill="x")
        self.search = tk.StringVar()
        search_entry = ttk.Entry(actions, textvariable=self.search, width=22)
        self.search_entry = search_entry
        search_entry.pack(side="left")
        search_entry.bind("<Return>", lambda _: self.find_text())
        self.busy_widgets.append(search_entry)
        ttk.Button(actions, text="查找原文线索", command=self.find_text).pack(
            side="left", padx=4
        )
        ttk.Button(actions, text="上一处", command=lambda: self.move_search(-1)).pack(
            side="left", padx=2
        )
        ttk.Button(actions, text="下一处", command=lambda: self.move_search(1)).pack(
            side="left", padx=2
        )
        ttk.Button(actions, text="清除高亮", command=self.clear_search).pack(
            side="left", padx=2
        )
        ttk.Button(actions, text="打开原文件副本", command=self.open_source).pack(
            side="left", padx=4
        )
        ttk.Button(actions, text="查看岗位规则", command=self.show_rules).pack(
            side="left", padx=4
        )
        ttk.Button(actions, text="导入报告", command=self.open_import_report).pack(
            side="left", padx=4
        )
        ttk.Button(actions, text="修订历史", command=self.show_revision_history).pack(
            side="left", padx=4
        )
        analyze_button = ttk.Button(
            actions, text="对选中材料进行 AI 分析", command=self.analyze
        )
        analyze_button.pack(side="right")
        self.busy_widgets.append(analyze_button)
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

    def focus_search(self):
        self.tabs.select(self.tasks)
        self.search_entry.focus_set()
        return "break"

    def next_tab(self, _event=None):
        current = self.tabs.index(self.tabs.select())
        self.tabs.select((current + 1) % self.tabs.index("end"))
        return "break"

    def dismiss_onboarding(self):
        self.onboarding_dismissed = True
        self.onboarding.pack_forget()

    def _update_selection_status(self):
        visible = set(self.documents.get_children())
        visible_selected = len(visible & self.selected_documents)
        hidden_selected = len(self.selected_documents - visible)
        suffix = f"（另有 {hidden_selected} 份因筛选不可见）" if hidden_selected else ""
        self.selection_status.set(
            f"已选 {len(self.selected_documents)} 份 · 当前可见已选 {visible_selected} 份{suffix}"
        )

    def select_all_visible(self):
        self.selected_documents.update(self.documents.get_children())
        if self.documents.get_children():
            self.documents.selection_set(self.documents.get_children())
        self._update_selection_status()

    def clear_selection(self):
        self.selected_documents.clear()
        self.documents.selection_remove(self.documents.selection())
        self._update_selection_status()

    def _selected_ids(self) -> list[str]:
        visible = set(self.documents.get_children())
        selected = [item for item in self.documents.get_children() if item in self.selected_documents]
        selected.extend(item for item in self.selected_documents - visible)
        return selected

    def delete_selected(self):
        if self.busy:
            messagebox.showinfo("任务正在运行", "请等待当前任务结束后再删除材料。")
            return
        selected = self._selected_ids()
        if not selected and self.current:
            selected = [self.current]
        if not selected:
            messagebox.showinfo("未选择材料", "请先选择需要放入回收区的材料。")
            return
        if not messagebox.askokcancel(
            "移入回收区",
            f"将 {len(selected)} 份材料移入本机回收区，默认 7 天后永久清除。\n"
            "只影响本机应用管理的数据，不会删除飞书或模型供应商侧数据。是否继续？",
        ):
            return
        for document_id in selected:
            self.store.delete_document(document_id)
        self.selected_documents.difference_update(selected)
        self.current = None
        self.refresh()
        self.status.set(f"已移入回收区 {len(selected)} 份；可在设置页恢复或永久清除。")

    def show_revision_history(self):
        if not self.current:
            messagebox.showinfo("未选择材料", "请先选择一份材料。")
            return
        popup = tk.Toplevel(self.window)
        popup.title("人工修订时间线（只读）")
        popup.geometry("760x430")
        tree = ttk.Treeview(
            popup,
            columns=("revision", "created", "reviewer", "opinion"),
            show="headings",
        )
        for key, title, width in (
            ("revision", "版本", 70),
            ("created", "时间", 180),
            ("reviewer", "审阅者（自填）", 150),
            ("opinion", "人工意见", 330),
        ):
            tree.heading(key, text=title)
            tree.column(key, width=width)
        tree.pack(fill="both", expand=True, padx=8, pady=8)
        revisions = self.store.list_revisions(self.current)
        for row in revisions:
            tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(row.get("revision_number") or row["id"], row["created"], row["reviewer"], row["opinion"]),
            )
        preview = ScrolledText(popup, height=8, wrap="word")
        preview.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        preview.configure(state="disabled")

        def show(_=None):
            selected = tree.selection()
            if not selected:
                return
            row = self.store.get_revision(int(selected[0]))
            value = (
                f"修订 {row.get('revision_number') or row['id']} · {row['created']} · {row['reviewer']}\n"
                f"人工意见：{row['opinion']}\n\n"
                + json.dumps(json.loads(row["evidence"]), ensure_ascii=False, indent=2)
            )
            preview.configure(state="normal")
            preview.delete("1.0", "end")
            preview.insert("1.0", value)
            preview.configure(state="disabled")

        tree.bind("<<TreeviewSelect>>", show)
        ttk.Label(
            popup,
            text="历史版本只读查看，不会覆盖当前草稿或当前人工结论。",
        ).pack(anchor="w", padx=8, pady=(0, 8))

    def open_import_report(self):
        path = self.store.root / "last-import-report.txt"
        if not path.is_file():
            reports = (self.store.root / "import-reports").glob("import-*.txt")
            path = max(reports, key=lambda report: report.stat().st_mtime, default=path)
        if not path.is_file():
            messagebox.showinfo("暂无导入报告", "完成一次导入后，这里会显示逐文件报告。")
            return
        open_path(path)

    def export_audit(self):
        if self.busy:
            messagebox.showinfo("任务正在运行", "请等待当前任务结束后再导出审计记录。")
            return
        folder = filedialog.askdirectory(title="选择完整审计导出目录")
        if folder:
            selected = self._selected_ids()
            self.start_job(
                lambda: f"完整审计记录已导出：{self.store.export_audit(Path(folder), selected or None)}"
            )

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
        self.model_options = [config.model] if config.model else []
        self.model_status = tk.StringVar(
            value=""
        )
        self.key_status = tk.StringVar()
        for variable in (self.provider, self.base, self.model):
            variable.trace_add("write", lambda *_: self.refresh_key_status())
        form = ttk.Frame(self.settings)
        form.pack(anchor="w", fill="x")
        ttk.Label(form, text="服务协议").grid(row=0, column=0, sticky="w", pady=8, padx=(0, 16))
        provider_widget = ttk.Combobox(
            form,
            textvariable=self.provider,
            values=["openai-compatible", "minimax"],
            state="readonly",
            width=35,
        )
        provider_widget.grid(row=0, column=1, sticky="w")
        self.busy_widgets.append(provider_widget)
        ttk.Label(form, text="模型名称").grid(row=1, column=0, sticky="w", pady=8, padx=(0, 16))
        holder = ttk.Frame(form)
        self.model_widget = ttk.Combobox(
            holder,
            textvariable=self.model,
            values=self.model_options,
            state="normal",
            width=30,
        )
        self.model_widget.pack(side="left")
        discover = ttk.Button(holder, text="获取模型列表", command=self.discover_models)
        discover.pack(side="left", padx=(8, 0))
        holder.grid(row=1, column=1, sticky="w")
        self.busy_widgets.extend((self.model_widget, discover))
        ttk.Label(form, text="API Key").grid(row=2, column=0, sticky="w", pady=8, padx=(0, 16))
        key_widget = ttk.Entry(form, textvariable=self.key, width=40, show="•")
        key_widget.grid(row=2, column=1, sticky="w")
        self.busy_widgets.append(key_widget)
        advanced = ttk.LabelFrame(self.settings, text="高级设置（端点）", padding=8)
        advanced.pack(anchor="w", fill="x", pady=(8, 0))
        ttk.Label(advanced, text="HTTPS Base URL").grid(row=0, column=0, sticky="w", padx=(0, 16))
        base_widget = ttk.Entry(advanced, textvariable=self.base, width=58)
        base_widget.grid(row=0, column=1, sticky="w")
        self.busy_widgets.append(base_widget)
        ttk.Label(
            advanced,
            text="必须是 HTTPS；不含凭据、查询参数或片段。跨域重定向不会携带 Authorization。",
            wraplength=800,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(self.settings, textvariable=self.model_status).pack(
            anchor="w", pady=(2, 0)
        )
        ttk.Label(self.settings, textvariable=self.key_status).pack(
            anchor="w", pady=(2, 0)
        )
        ttk.Label(
            self.settings,
            text="Key 留空可使用已保存凭据；首次测试或 AI 分析时输入的 Key 会保存到系统凭据库。模型列表获取失败时仍可手动填写模型名。普通配置、备份、导出和诊断不包含明文密钥。",
            wraplength=950,
        ).pack(anchor="w", pady=12)
        bar = ttk.Frame(self.settings)
        bar.pack(anchor="w")
        save_button = ttk.Button(bar, text="保存模型配置", command=self.save_model)
        save_button.pack(
            side="left", padx=(0, 8)
        )
        test_button = ttk.Button(
            bar, text="测试连接（可能产生少量费用）", command=self.test_model
        )
        test_button.pack(side="left")
        self.busy_widgets.extend((save_button, test_button))
        ttk.Label(self.settings, text="模型状态", font=("Microsoft YaHei UI" if os.name == "nt" else "Helvetica", 10, "bold")).pack(anchor="w", pady=(10, 0))
        self.model_state_text = tk.StringVar(value="未配置")
        ttk.Label(self.settings, textvariable=self.model_state_text).pack(anchor="w")
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
        ttk.Button(self.settings, text="打开回收区", command=self.open_trash).pack(
            anchor="w", pady=3
        )
        ttk.Button(self.settings, text="清除当前端点凭据", command=self.clear_credential).pack(
            anchor="w", pady=3
        )
        ttk.Button(self.settings, text="清空全部本地数据", command=self.clear_all_data).pack(
            anchor="w", pady=3
        )
        self.refresh_key_status()
        self.refresh_model_state()

    def make_history(self):
        ttk.Label(
            self.history,
            text="每次 AI 分析属于独立批次；原始响应与规范化结果只读保存。AI 建议只影响工作优先级，不是最终招聘决定。",
        ).pack(anchor="w")
        ttk.Label(self.history, text="AI 批次").pack(anchor="w", pady=(8, 0))
        self.batches = ttk.Treeview(
            self.history,
            columns=("batch_date", "batch_model", "batch_status", "batch_progress"),
            show="headings",
            height=3,
        )
        for key, title, width in (
            ("batch_date", "创建时间", 180),
            ("batch_model", "模型", 220),
            ("batch_status", "状态", 130),
            ("batch_progress", "进度", 100),
        ):
            self.batches.heading(key, text=title)
            self.batches.column(key, width=width)
        self.batches.pack(fill="x", pady=(2, 8))
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
        self._set_busy_state(True)

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

    def _set_busy_state(self, busy: bool) -> None:
        for widget in self.busy_widgets:
            widget.configure(
                state="normal"
                if widget is self.stop_button or not busy
                else "disabled"
            )
        self.progress_bar.configure(maximum=max(1, self.progress_maximum.get()))

    def poll(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "reset":
                self.reset_review()
                continue
            if kind == "models":
                self.apply_discovered_models(value)
                continue
            if kind == "progress_info":
                self.progress_value.set(value.get("current", 0))
                self.progress_maximum.set(max(1, value.get("total", 1)))
                self.progress_bar.configure(maximum=max(1, value.get("total", 1)))
                self.progress_text.set(value.get("text", ""))
                self.status.set(value.get("text", ""))
                continue
            if kind == "model_state":
                self.refresh_model_state()
                self.status.set(value)
                continue
            self.status.set(value)
            if kind == "progress":
                self.refresh()
            if kind == "done":
                self.busy = False
                self._set_busy_state(False)
                self.progress_text.set("")
                self.refresh()
                if self.exit_pending:
                    self.window.destroy()
                    return
        self.poll_id = self.window.after(100, self.poll)

    def stop_work(self):
        self.stop.set()
        if self.active_ai_batch:
            self.store.request_ai_batch_stop(self.active_ai_batch)
        self.status.set("已停止接收新任务；当前任务完成后停止。")

    def reset_review(self):
        """Clear widgets after a data restore has replaced the local database."""
        self.current = None
        self.current_criterion = None
        self.evidence = []
        if self.autosave_id is not None:
            self.window.after_cancel(self.autosave_id)
            self.autosave_id = None
        self.dirty = False
        self.selected_documents.clear()
        for document_id in self.documents.selection():
            self.documents.selection_remove(document_id)
        self.criteria.delete(*self.criteria.get_children())
        self.content.configure(state="normal")
        self.content.delete("1.0", "end")
        self.content.configure(state="disabled")
        self.note.delete("1.0", "end")
        self.reviewer.set("")
        self.opinion.delete(0, "end")
        self._update_selection_status()

    def import_files(self):
        paths = filedialog.askopenfilenames(
            filetypes=[("简历", "*.pdf *.docx *.txt *.md")]
        )
        if paths:
            self.prepare_files([Path(p) for p in paths])

    def import_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.prepare_files([Path(folder)], recursive=self.recursive.get())

    def prepare_files(self, paths, *, recursive: bool = False):
        role = self.get_role()
        if not paths:
            self.status.set("没有支持的简历文件")
            return
        try:
            preview = self.store.preview_import(paths, role, recursive=recursive)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法预览导入", str(exc))
            return
        counts = preview["counts"]
        if not counts["total"]:
            self.status.set("没有找到可预览的文件")
            return
        if not messagebox.askokcancel(
            "导入预览",
            f"文件总数：{counts['total']}\n"
            f"预计处理：{counts['to_process']}（其中编码待确认 {counts['encoding_pending']}）\n"
            f"重复：{counts['duplicates']} · 岗位冲突：{counts['role_conflicts']} · "
            f"未知岗位：{counts['unknown_role']} · 其他跳过：{counts['skipped']}\n"
            f"岗位：{self.role.get()} · 递归：{'是' if recursive else '否'}\n"
            "仅在本机处理，不调用模型；导入后会生成逐文件报告。是否继续？",
        ):
            return
        batch_id = self.store.create_import_batch(
            source=str(paths[0]) if paths else "",
            recursive=recursive,
            planned_count=counts["to_process"],
        )
        self.active_import_batch = batch_id

        def work():
            failures = []
            processed = 0
            items = preview["items"]
            total = len(items)
            for index, item in enumerate(items, 1):
                if self.stop.is_set():
                    for remaining in items[index - 1 :]:
                        reason_code = (
                            remaining["reason_code"]
                            if not remaining["will_process"]
                            else "OK"
                        )
                        status = "重复" if reason_code == "DUPLICATE" else "跳过"
                        self.store.record_import_item(
                            batch_id,
                            path=remaining["path"],
                            role=remaining.get("role"),
                            document_id=remaining.get("document_id"),
                            status=status,
                            reason_code=reason_code,
                            message=(
                                remaining.get("message", "")
                                if not remaining["will_process"]
                                else "用户停止，未开始处理"
                            ),
                        )
                    break
                self.events.put(
                    (
                        "progress_info",
                        {"current": index - 1, "total": total, "text": f"正在本地整理 {index}/{total}：{item['name']}"},
                    )
                )
                if not item["will_process"]:
                    status = "重复" if item["reason_code"] == "DUPLICATE" else "跳过"
                    self.store.record_import_item(
                        batch_id,
                        path=item["path"],
                        role=item.get("role"),
                        document_id=item.get("document_id"),
                        status=status,
                        reason_code=item["reason_code"],
                        message=item.get("message", ""),
                    )
                    if status != "重复":
                        failures.append(f"{item['name']}：{item.get('message', '')}")
                    continue
                path = Path(item["path"])
                try:
                    record = self.store.prepare(path, item.get("role"), batch_id=batch_id)
                    if record["status"] == "编码待确认":
                        processed += 1
                        self.store.record_import_item(
                            batch_id,
                            path=path,
                            role=item.get("role"),
                            document_id=record["id"],
                            status="待确认",
                            reason_code="ENCODING_PENDING",
                            message=record["error"],
                        )
                    elif record["status"] == "解析失败":
                        self.store.record_import_item(
                            batch_id,
                            path=path,
                            role=item.get("role"),
                            document_id=record["id"],
                            status="失败",
                            reason_code="PARSE_ERROR",
                            message=record["error"],
                        )
                        failures.append(f"{path.name}：{record['error']}")
                    else:
                        processed += 1
                        self.store.record_import_item(
                            batch_id,
                            path=path,
                            role=item.get("role"),
                            document_id=record["id"],
                            status="成功",
                            reason_code="OK",
                            message="本地解析完成",
                        )
                except ValueError as exc:
                    self.store.record_import_item(
                        batch_id,
                        path=path,
                        role=item.get("role"),
                        document_id=item.get("document_id"),
                        status="失败",
                        reason_code="PARSE_ERROR",
                        message=str(exc),
                    )
                    failures.append(f"{path.name}：{exc}")
                except OSError:
                    self.store.record_import_item(
                        batch_id,
                        path=path,
                        role=item.get("role"),
                        document_id=item.get("document_id"),
                        status="失败",
                        reason_code="READ_ERROR",
                        message="文件操作失败，请检查权限和磁盘空间",
                    )
                    failures.append(
                        f"{path.name}：文件操作失败，请检查文件是否存在、访问权限和磁盘空间。"
                    )
            stopped = self.stop.is_set()
            report = self.store.finish_import_batch(
                batch_id, status="已停止" if stopped else "完成"
            )
            self.active_import_batch = None
            report_counts = report["counts"]
            skipped_or_failed = report_counts["failed"] + report_counts["skipped"]
            message = f"已处理 {processed}/{total} 份，跳过或失败 {skipped_or_failed} 份。"
            if stopped:
                message += " 已停止后续导入。"
            if failures or report_counts["failed"] or report_counts["skipped"]:
                message += " 详情见可打开的逐文件导入报告。"
            else:
                message += " 请进行人工审阅。"
            return message

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
        known_ids = {record["id"] for record in records}
        self.selected_documents.intersection_update(known_ids)
        role_filter = self.filter_role.get()
        status_filter = self.filter_status.get()
        role_code = next(
            (key for key, value in ROLES.items() if value == role_filter), None
        )
        if role_code:
            records = [record for record in records if record["role"] == role_code]
        if status_filter != "全部状态":
            records = [
                record for record in records if record["status"] == status_filter
            ]
        ai_filter = self.filter_ai.get()
        if ai_filter != "全部 AI":
            records = [record for record in records if record["ai_status"] == ai_filter]
        if self.sort_by.get() == "AI 优先级":
            order = {"需人工核对": 0, "失败": 1, "处理中": 2, "成功": 3, "未运行": 4}
            records.sort(key=lambda row: (order.get(row["ai_status"], 9), row["created"]))
        elif self.sort_by.get() == "主要风险":
            records.sort(key=lambda row: (not bool(row["ai_primary_risk"]), row["name"].casefold()))
        active = {record["id"] for record in records}
        for document_id in existing - active:
            self.documents.delete(document_id)
        for position, record in enumerate(records):
            values = (
                record["name"],
                ROLES[record["role"]],
                record["status"],
                record["ai_status"],
                record["ai_band"] or "",
                record["ai_recommendation"] or "",
                record["ai_primary_risk"] or "",
                record["human_status"],
                record["updated"],
            )
            if record["id"] in existing:
                self.documents.item(record["id"], values=values)
            else:
                self.documents.insert("", "end", iid=record["id"], values=values)
            self.documents.move(record["id"], "", position)
        visible_selected = [item for item in self.documents.get_children() if item in self.selected_documents]
        if visible_selected:
            self.documents.selection_set(visible_selected)
        self._update_selection_status()
        if records or self.store.list_documents():
            self.onboarding.pack_forget()
        elif not self.onboarding_dismissed:
            self.onboarding.pack(fill="x", pady=(0, 6), before=self.documents)
        self.runs.delete(*self.runs.get_children())
        self.batches.delete(*self.batches.get_children())
        with closing(self.store.connect()) as db:
            for batch in self.store.list_ai_batches():
                batch_id = batch["id"]
                item_count = db.execute(
                    "SELECT COUNT(*) FROM ai_batch_items WHERE batch_id=?", (batch_id,)
                ).fetchone()[0]
                self.batches.insert(
                    "",
                    "end",
                    iid=batch_id,
                    values=(
                        batch["created"],
                        batch["model"],
                        batch["status"],
                        f"{batch['completed_count']}/{item_count}",
                    ),
                )
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
        visible = set(self.documents.get_children())
        self.selected_documents.update(selection)
        self.selected_documents.difference_update(visible - set(selection))
        self._update_selection_status()
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
        self.search_matches = []
        self.search_index = -1
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
        except (TypeError, ValueError):
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
        if self.autosave_id is not None:
            self.window.after_cancel(self.autosave_id)
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
        if not dirty and self.autosave_id is not None:
            self.window.after_cancel(self.autosave_id)
            self.autosave_id = None

    def save_review(self):
        if not self.current:
            return
        self.flush_criterion()
        try:
            latest_ai = self.store.latest_ai_result(self.current)
            self.store.save_review(
                self.current,
                self.reviewer.get(),
                self.opinion.get(),
                self.evidence,
                source_ai_run_id=latest_ai.get("run_id") if latest_ai else None,
            )
        except ValueError as exc:
            messagebox.showerror("无法保存", str(exc))
            return
        if self.autosave_id is not None:
            self.window.after_cancel(self.autosave_id)
            self.autosave_id = None
        self.dirty = False
        self.refresh()
        self.status.set("人工审阅已保存，历史修订保留。")

    def find_text(self):
        self.content.tag_remove("match", "1.0", "end")
        self.search_matches = []
        self.search_index = -1
        query = self.search.get().strip()
        if not query:
            self.status.set("请输入要查找的原文线索。")
            return
        start, count = "1.0", 0
        while True:
            position = self.content.search(query, start, stopindex="end", nocase=True)
            if not position:
                break
            end = f"{position}+{len(query)}c"
            self.content.tag_add("match", position, end)
            self.search_matches.append(position)
            start, count = end, count + 1
        self.content.tag_configure("match", background="#ffe39b", foreground="#202020")
        if self.search_matches:
            self.search_index = 0
            self.content.see(self.search_matches[0])
            self.status.set(f"找到 {count} 处线索（1/{count}）。命中不代表符合，未命中也不代表不具备。")
        else:
            self.status.set("未找到线索。未命中不代表不具备，请继续人工核对。")

    def move_search(self, direction: int):
        if not self.search_matches:
            self.find_text()
            return
        self.search_index = (self.search_index + direction) % len(self.search_matches)
        self.content.see(self.search_matches[self.search_index])
        self.status.set(
            f"当前线索 {self.search_index + 1}/{len(self.search_matches)}。命中不代表符合。"
        )

    def clear_search(self):
        self.content.tag_remove("match", "1.0", "end")
        self.search_matches = []
        self.search_index = -1
        self.status.set("已清除原文高亮。")

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

    def refresh_key_status(self, config=None):
        try:
            config = config or self.model_config()
            _ = config.models_endpoint
            if saved_credential_available(config):
                self.key_status.set("API Key 状态：已安全保存（界面不会回显密钥；输入新 Key 可替换）")
            else:
                self.key_status.set("API Key 状态：未保存；点击保存、测试或 AI 分析后会写入系统凭据库")
        except (ValueError, OSError, TypeError):
            self.key_status.set("API Key 状态：等待有效的 HTTPS Base URL 和模型配置")

    def refresh_model_state(self):
        try:
            config = self.model_config()
            role = self.store.get(self.current)["role"] if self.current else ""
            value = self.store.model_state(config, role=role)
            state = value["state"]
            self.model_state_text.set(
                f"{state}。连接可用不等于质量验证；AI 建议不自动推进或淘汰候选人。"
            )
        except (OSError, ValueError, TypeError, sqlite3.DatabaseError):
            self.model_state_text.set("未配置。可继续本地整理，无需 API。")

    def persist_entered_key(self, config):
        """Remember a newly entered key before a paid operation starts."""

        key = self.key.get().strip()
        if not key:
            return
        save_config(self.store.root, config, key)
        self.key.set("")
        self.refresh_key_status(config)

    def apply_discovered_models(self, models):
        self.model_options = list(models)
        self.model_widget.configure(values=self.model_options)
        if not self.model.get().strip():
            self.model.set(self.model_options[0])
        if self.key.get().strip():
            try:
                self.persist_entered_key(self.model_config())
            except (OSError, TypeError, ValueError):
                # Keep the entered key visible so the user can save it explicitly.
                pass
        self.model_status.set(f"已获取 {len(self.model_options)} 个可用模型；可下拉选择，也可手动填写。")

    def discover_models(self):
        if self.busy:
            return
        try:
            config = self.model_config()
            _ = config.models_endpoint
        except ValueError as exc:
            messagebox.showinfo("无法获取模型", str(exc))
            return
        key = self.key.get().strip()
        if config.model.strip():
            try:
                self.persist_entered_key(config)
            except Exception as exc:  # noqa: BLE001 -- OS keychain errors must not leak credential arguments.
                messagebox.showerror(
                    "API Key 未保存",
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "系统凭据库不可用，请检查系统权限",
                )
                return

        def work():
            models = list_models(config, key)
            self.events.put(("models", models))
            return f"已获取 {len(models)} 个可用模型。"

        self.start_job(work)

    def save_model(self):
        try:
            config = self.model_config()
            save_config(self.store.root, config, self.key.get())
            self.key.set("")
            self.refresh_key_status(config)
            self.refresh_model_state()
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
        config, key = self.model_config(), self.key.get().strip()
        try:
            self.persist_entered_key(config)
            client = configured_client(config, key)
        except Exception as exc:  # noqa: BLE001 -- keep provider credentials out of UI errors.
            messagebox.showerror(
                "无法测试模型",
                str(exc)
                if isinstance(exc, ValueError)
                else "模型配置或系统凭据不可用，请检查设置",
            )
            return
        def work():
            result = client.test()
            self.store.mark_model_connection(
                config,
                role=self.store.get(self.current)["role"] if self.current else "",
            )
            self.events.put(("model_state", "连接可用"))
            return result

        self.start_job(work)

    def clear_credential(self):
        try:
            config = self.model_config()
            _ = config.models_endpoint
        except ValueError as exc:
            messagebox.showinfo("无法清除凭据", str(exc))
            return
        if not messagebox.askokcancel(
            "清除系统凭据",
            "只清除当前服务与 Base URL 对应的本机凭据，不删除材料、审阅或其他端点凭据。是否继续？",
        ):
            return
        try:
            clear_saved_credential(config)
        except Exception as exc:  # noqa: BLE001 -- keyring backend errors are shown without credential data.
            messagebox.showerror(
                "凭据未清除",
                str(exc) if isinstance(exc, ValueError) else "系统凭据库不可用，请检查系统权限",
            )
            return
        self.key_status.set("API Key 状态：未保存；已清除当前端点的系统凭据。")
        self.model_state_text.set("未配置。清除凭据后后续模型请求不会继续使用旧 Key。")
        self.status.set("当前端点凭据已从系统凭据库清除。")

    def clear_all_data(self):
        if self.busy:
            messagebox.showinfo("任务正在运行", "请等待当前任务结束后再清空本地数据。")
            return
        if not messagebox.askokcancel(
            "清空全部本地数据",
            "这会清除材料、提取文本、草稿、人工修订、AI 历史、导入报告和回收区。"
            "模型配置与系统凭据不会随之清除。是否继续？",
        ):
            return
        if not messagebox.askyesno(
            "再次确认清空",
            "本机应用管理的数据将无法从回收区恢复；飞书和供应商侧数据不受影响。确定清空？",
        ):
            return
        result = self.store.clear_all_data()
        self.reset_review()
        self.refresh()
        self.status.set(
            f"已清空本地数据：材料 {result['documents']} 份、修订 {result['revisions']} 条、AI 运行 {result['ai_runs']} 条；系统凭据仍保留。"
        )

    def open_trash(self):
        popup = tk.Toplevel(self.window)
        popup.title("本机回收区（默认 7 天后清除）")
        popup.geometry("780x360")
        tree = ttk.Treeview(
            popup,
            columns=("name", "deleted", "purge", "status"),
            show="headings",
            selectmode="extended",
        )
        for key, title, width in (
            ("name", "材料", 300),
            ("deleted", "删除时间", 175),
            ("purge", "计划清除", 175),
            ("status", "原状态", 100),
        ):
            tree.heading(key, text=title)
            tree.column(key, width=width)
        tree.pack(fill="both", expand=True, padx=8, pady=8)

        def reload_trash():
            tree.delete(*tree.get_children())
            for row in self.store.list_trash():
                tree.insert(
                    "",
                    "end",
                    iid=row["id"],
                    values=(row["name"], row["trash_deleted_at"], row["trash_purge_at"], row["original_status"]),
                )

        def restore():
            for document_id in tree.selection():
                self.store.restore_document(document_id)
            reload_trash()
            self.refresh()

        def purge():
            if not tree.selection() or not messagebox.askyesno(
                "永久清除", "选中材料及其本地原始副本、审阅和 AI 关联将永久清除，是否继续？"
            ):
                return
            for document_id in tree.selection():
                self.store.purge_document(document_id)
            reload_trash()
            self.refresh()

        buttons = ttk.Frame(popup)
        buttons.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(buttons, text="恢复", command=restore).pack(side="left", padx=3)
        ttk.Button(buttons, text="立即永久清除", command=purge).pack(side="left", padx=3)
        ttk.Label(buttons, text="仅作用于本机；不删除飞书或供应商侧数据。").pack(side="right")
        reload_trash()

    def analyze(self):
        if self.busy:
            return
        selected = list(
            dict.fromkeys(list(self.documents.selection()) + self._selected_ids())
        )
        if not selected and self.current:
            selected = [self.current]
        if not selected:
            messagebox.showinfo("未选择材料", "请先选择一份或多份已整理成功的材料。")
            return
        config, key = self.model_config(), self.key.get().strip()
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
        try:
            self.persist_entered_key(config)
            client = configured_client(config, key)
        except Exception as exc:  # noqa: BLE001 -- keep provider credentials out of UI errors.
            messagebox.showerror(
                "无法开始 AI 分析",
                str(exc)
                if isinstance(exc, ValueError)
                else "模型配置或系统凭据不可用，请检查设置",
            )
            return

        batch_snapshot = {
            "provider": config.provider,
            "base_url": config.base_url.strip().rstrip("/"),
            "endpoint_identity": config.endpoint_identity,
            "model": config.model,
            "config_identity": config.identity,
            "output_contract_version": AI_OUTPUT_CONTRACT_VERSION,
            "roles": sorted({record["role"] for record in ready}),
        }
        try:
            batch_id = self.store.create_ai_batch(
                [record["id"] for record in ready],
                config_snapshot=batch_snapshot,
                repeat_billing_confirmed=bool(duplicates),
            )
            self.store.mark_ai_batch_started(batch_id)
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            messagebox.showerror("无法创建 AI 批次", str(exc))
            return
        self.active_ai_batch = batch_id

        def work():
            counts = {}
            errors = []
            completed = 0
            paused_reason = None

            def progress_message() -> str:
                labels = {
                    "succeeded": "成功",
                    "manual_review": "需人工核对",
                    "retryable_failed": "失败",
                    "failed": "失败",
                }
                detail = "、".join(
                    f"{labels.get(status, status)} {count} 份"
                    for status, count in counts.items()
                )
                return f"已完成 AI 分析 {completed}/{len(ready)}" + (
                    f"（{detail}）" if detail else ""
                )

            try:
                for index, record in enumerate(ready, 1):
                    if self.stop.is_set():
                        break
                    self.events.put(
                        (
                            "progress_info",
                            {
                                "current": completed,
                                "total": len(ready),
                                "text": f"正在 AI 分析 {index}/{len(ready)}：{record['name']}",
                            },
                        )
                    )
                    try:
                        outcome = run_analysis(
                            self.store,
                            record["id"],
                            config,
                            client,
                            batch_id=batch_id,
                        )
                        status, _folder = outcome
                        counts[status] = counts.get(status, 0) + 1
                        completed += 1
                        error_code = outcome.error_code
                        run_id = outcome.run_id
                        item_status = {
                            "succeeded": "成功",
                            "manual_review": "需人工核对",
                            "retryable_failed": "失败",
                        }.get(status, "需人工核对")
                        self.store.update_ai_batch_item(
                            batch_id,
                            record["id"],
                            status=item_status,
                            run_id=run_id,
                            error=error_code or "",
                        )
                        self.events.put(
                            (
                                "progress_info",
                                {
                                    "current": completed,
                                    "total": len(ready),
                                    "text": progress_message(),
                                },
                            )
                        )
                        if error_code in _BATCH_PAUSE_ERROR_CODES:
                            paused_reason = error_code
                            errors.append(f"{record['name']}：{error_code}")
                            break
                    except ValueError as exc:
                        completed += 1
                        counts["failed"] = counts.get("failed", 0) + 1
                        self.store.update_ai_batch_item(
                            batch_id,
                            record["id"],
                            status="失败",
                            error=str(exc),
                        )
                        errors.append(f"{record['name']}：{exc}")
                    except Exception as exc:  # noqa: BLE001 -- isolate each paid run.
                        completed += 1
                        counts["failed"] = counts.get("failed", 0) + 1
                        self.store.update_ai_batch_item(
                            batch_id,
                            record["id"],
                            status="失败",
                            error=f"{type(exc).__name__}：操作失败",
                        )
                        errors.append(
                            f"{record['name']}：{type(exc).__name__}，请在 AI 历史中核对状态"
                        )
                labels = {
                    "succeeded": "成功",
                    "manual_review": "需人工核对",
                    "retryable_failed": "失败",
                    "failed": "失败",
                }
                detail = "、".join(
                    f"{labels.get(status, status)} {count} 份"
                    for status, count in counts.items()
                )
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
            finally:
                try:
                    self.store.finish_ai_batch(
                        batch_id,
                        stopped=self.stop.is_set(),
                        paused=bool(paused_reason),
                        note=paused_reason or "",
                    )
                finally:
                    self.active_ai_batch = None

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
