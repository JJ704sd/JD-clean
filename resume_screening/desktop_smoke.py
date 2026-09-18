"""Synthetic release checks, never use the developer's resumes or credentials."""

from __future__ import annotations

import json
import socket
import tempfile
import tkinter as tk
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from .desktop_store import ReviewStore, atomic_text

TEXT = (
    "Synthetic Candidate Resume. Bachelor of Computer Science. "
    "Built an order management service with Python and SQL. "
    "Designed database indexes, tested REST APIs and shipped to production. "
    "Measured latency and documented project responsibilities. "
)


def credential_smoke_test(report_path):
    from .desktop_model import credential_backend

    report = {"passed": False}
    backend = credential_backend()
    identity = "synthetic-" + uuid.uuid4().hex
    written = False
    try:
        backend.set_password("ResumeDeskSmoke", identity, "synthetic-not-a-real-key")
        written = True
        assert (
            backend.get_password("ResumeDeskSmoke", identity)
            == "synthetic-not-a-real-key"
        )
        report["passed"] = True
    except Exception as exc:  # noqa: BLE001 -- record failure type without credential arguments.
        report["error"] = type(exc).__name__
    finally:
        if written:
            backend.delete_password("ResumeDeskSmoke", identity)
    atomic_text(Path(report_path), json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


def fixtures(folder):
    import pymupdf

    folder.mkdir(parents=True, exist_ok=True)
    (folder / "示例 简历.txt").write_text(TEXT, encoding="utf-8")
    (folder / "示例.md").write_text(TEXT, encoding="utf-8")
    with zipfile.ZipFile(folder / "示例.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            + TEXT
            + "</w:t></w:r></w:p></w:body></w:document>",
        )
    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox(pymupdf.Rect(45, 45, 540, 750), TEXT, fontsize=16)
    page.insert_text(
        (45, 230),
        "项目经验：负责订单管理系统开发，优化数据库查询性能。",
        fontname="china-s",
        fontsize=14,
    )
    document.save(folder / "原生.pdf")
    scanned = pymupdf.open()
    scanned.new_page().insert_image(
        page.rect, stream=page.get_pixmap(dpi=150).tobytes("png")
    )
    scanned.save(folder / "扫描.pdf")
    scanned.close()
    document.close()
    return sorted(folder.iterdir())


def smoke_test(report_path):
    report_path = Path(report_path).resolve()
    report = {"checks": [], "passed": False}
    try:
        with tempfile.TemporaryDirectory(prefix="ResumeDesk-smoke-") as temporary:
            root = Path(temporary)
            sources = fixtures(root / "中文 inputs")
            # Block every socket connection: OCR must work with bundled models only.
            with (
                patch.object(
                    socket.socket,
                    "connect",
                    side_effect=AssertionError("network forbidden"),
                ),
                patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("network forbidden"),
                ),
            ):
                store = ReviewStore(root / "中文 data")
                for source in sources:
                    record = store.prepare(source, "senior-fullstack-engineer")
                    if record["status"] != "待人工审阅":
                        raise AssertionError(source.suffix + ": " + record["error"])
                    if source.suffix == ".pdf":
                        assert "项目" in record["content"], (
                            "Chinese PDF text was not recognized"
                        )
                    report["checks"].append(
                        source.name + ": offline preparation passed"
                    )
                store.save_review(
                    record["id"],
                    "Synthetic reviewer",
                    "信息不足，待人工追问",
                    [
                        {
                            "criterion": "合成审阅项",
                            "decision": "信息不足",
                            "note": "待确认",
                        }
                    ],
                )
                export = store.export(root / "exports")
                assert (export / "manual-review.csv").is_file()
                report["checks"].append("manual review and export passed")
                # Full UI construction catches missing Tcl/Tk and bundled policy resources.
                from .desktop import DesktopApp

                window = tk.Tk()
                window.withdraw()
                app = DesktopApp(window, root / "中文 data")
                unreviewed = next(
                    row for row in store.list_documents() if row["id"] != record["id"]
                )
                app.documents.selection_set(unreviewed["id"])
                window.update()
                assert app.current == unreviewed["id"], "selection did not load record"
                assert len(app.evidence) >= 3, (
                    f"policy matrix not loaded: {len(app.evidence)}"
                )
                window.destroy()
                report["checks"].append("Tk UI and policy resources passed")
        report["passed"] = True
    except Exception as exc:  # noqa: BLE001 -- smoke runner must write its report even on a packaging failure.
        report["error"] = f"{type(exc).__name__}: {exc}"
    atomic_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1
