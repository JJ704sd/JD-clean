"""Local resume normalization, OCR fallback, quality checks, and PII minimization."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

PARSER_VERSION = "resume-cleaner-2026-09-01-v2"
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}
DOCUMENT_PARSERS = {"local", "mineru-flash"}
MINERU_FLASH_MAX_BYTES = 10 * 1024 * 1024

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[\w-]+", re.IGNORECASE | re.UNICODE
)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?86[\s-]?)?1[3-9](?:[\s-]?\d){9}(?!\d)"
)
LANDLINE_RE = re.compile(r"(?<!\d)0\d{2,3}[\s-]?\d{7,8}(?!\d)")
IDENTITY_RE = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?!\d)"
)
ADDRESS_LINE_RE = re.compile(
    r"(?im)^(?:现居住?地|家庭住址|详细地址|通讯地址)\s*[:：].*$"
)
# PDF text extraction may concatenate this platform marker with adjacent
# words. The marker has a 17-character hex prefix and a ``~~`` terminator;
# anchoring on that prefix avoids consuming a legitimate word such as
# ``Boot`` when the marker is concatenated with it.
OPAQUE_PLATFORM_TOKEN_RE = re.compile(
    r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{17}[A-Za-z0-9_-]{19,}~~"
)


class ResumeQualityError(ValueError):
    code = "U01_PARSE_QUALITY"


@dataclass(frozen=True)
class CleanedResume:
    source_path: Path
    source_sha256: str
    candidate_id: str
    markdown: str
    model_text: str
    used_ocr: bool
    page_count: int
    parser_version: str = PARSER_VERSION
    extraction_engine: str = "local"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_docx(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            raw_xml = archive.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise ResumeQualityError(f"DOCX 无法读取：{exc}") from exc
    root = ElementTree.fromstring(raw_xml)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t"))
        if text.strip():
            paragraphs.append(text.strip())
    return ["\n".join(paragraphs)]


def _rapid_ocr_image(image_bytes: bytes) -> str:
    try:
        from rapidocr import RapidOCR  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ResumeQualityError("扫描型 PDF 需要安装 OCR 可选依赖") from exc
    result = RapidOCR()(image_bytes)
    if hasattr(result, "txts"):
        return "\n".join(result.txts or [])
    if isinstance(result, tuple) and result and isinstance(result[0], list):
        lines = []
        for item in result[0]:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                lines.append(str(item[1]))
        return "\n".join(lines)
    return ""


def _strip_opaque_platform_tokens(text: str) -> str:
    return OPAQUE_PLATFORM_TOKEN_RE.sub("", text)


def _compact_length(text: str) -> int:
    return len(re.sub(r"\s+", "", _strip_opaque_platform_tokens(text)))


def _read_pdf(
    path: Path,
    *,
    ocr: bool,
    ocr_image: Callable[[bytes], str] | None,
) -> tuple[list[str], bool]:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("PDF 清洗需要安装 pymupdf") from exc
    try:
        document = pymupdf.open(path)
    except Exception as exc:
        raise ResumeQualityError(f"PDF 无法读取：{exc}") from exc
    pages: list[str] = []
    used_ocr = False
    engine = ocr_image or _rapid_ocr_image
    try:
        if document.needs_pass:
            raise ResumeQualityError("PDF 已加密，无法提取文本")
        for page in document:
            text = _strip_opaque_platform_tokens(
                page.get_text("text", sort=True)
            ).strip()
            if ocr and _compact_length(text) < 20:
                pixmap = page.get_pixmap(dpi=200, colorspace=pymupdf.csRGB, alpha=False)
                ocr_text = _strip_opaque_platform_tokens(
                    engine(pixmap.tobytes("png"))
                ).strip()
                if _compact_length(ocr_text) > _compact_length(text):
                    text = ocr_text
                    used_ocr = True
            pages.append(text)
    except ResumeQualityError:
        raise
    except Exception as exc:
        raise ResumeQualityError(f"PDF 无法读取：{exc}") from exc
    finally:
        if not document.is_closed:
            document.close()
    return pages, used_ocr


def _pdf_page_count(path: Path) -> int:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("PDF 清洗需要安装 pymupdf") from exc
    try:
        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise ResumeQualityError("PDF 已加密，无法提取文本")
            return document.page_count
    except ResumeQualityError:
        raise
    except Exception as exc:
        raise ResumeQualityError(f"PDF 无法读取：{exc}") from exc


def _mineru_executable() -> str:
    candidates = (
        ("mineru-open-api.cmd", "mineru-open-api.exe", "mineru-open-api")
        if os.name == "nt"
        else ("mineru-open-api",)
    )
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable:
            return executable
    raise ResumeQualityError("未安装 mineru-open-api，无法使用 MinerU 解析")


def _run_mineru_flash(path: Path) -> str:
    if path.stat().st_size > MINERU_FLASH_MAX_BYTES:
        raise ResumeQualityError("MinerU flash-extract 仅支持不超过 10 MB 的文件")
    if path.suffix.lower() == ".pdf" and _pdf_page_count(path) > 20:
        raise ResumeQualityError("MinerU flash-extract 仅支持不超过 20 页的 PDF")

    with tempfile.TemporaryDirectory(prefix="resume-mineru-") as temporary:
        destination = Path(temporary) / "extracted.md"
        command = [
            _mineru_executable(),
            "flash-extract",
            str(path),
            "--language",
            "ch",
            "--ocr",
            "--timeout",
            "300",
            "--output",
            str(destination),
        ]
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=330,
                check=False,
                creationflags=creation_flags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ResumeQualityError("MinerU 文档解析未完成") from exc
        if completed.returncode != 0:
            raise ResumeQualityError(
                f"MinerU 文档解析失败（退出码 {completed.returncode}）"
            )
        try:
            text = destination.read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            raise ResumeQualityError("MinerU 未生成可读取的 Markdown") from exc
    if not text:
        raise ResumeQualityError("MinerU 未提取到有效文本")
    return text


def _quality_check(pages: Iterable[str]) -> None:
    values = list(pages)
    combined = "\n".join(values)
    compact = re.sub(r"\s+", "", combined)
    if len(compact) < 80:
        raise ResumeQualityError("有效文本少于 80 个字符")
    replacement_ratio = combined.count("�") / max(len(combined), 1)
    if replacement_ratio > 0.02:
        raise ResumeQualityError("文本包含过多无法解码字符")
    meaningful = sum(char.isalnum() or "\u4e00" <= char <= "\u9fff" for char in compact)
    if meaningful / max(len(compact), 1) < 0.35:
        raise ResumeQualityError("文本有效字符比例过低")
    nonempty_pages = sum(bool(re.sub(r"\s+", "", page)) for page in values)
    if nonempty_pages / max(len(values), 1) < 0.5:
        raise ResumeQualityError("超过一半页面没有提取到有效文本")


def redact_for_model(text: str, *, candidate_name: str | None = None) -> str:
    redacted = unicodedata.normalize("NFKC", text)
    redacted = EMAIL_RE.sub("[已脱敏邮箱]", redacted)
    redacted = PHONE_RE.sub("[已脱敏电话]", redacted)
    redacted = LANDLINE_RE.sub("[已脱敏电话]", redacted)
    redacted = IDENTITY_RE.sub("[已脱敏证件号]", redacted)
    redacted = ADDRESS_LINE_RE.sub("[已脱敏地址]", redacted)
    if candidate_name and candidate_name.strip():
        redacted = redacted.replace(
            unicodedata.normalize("NFKC", candidate_name.strip()), "[候选人]"
        )
    return redacted


def clean_resume(
    source: str | Path,
    *,
    candidate_id: str,
    candidate_name: str | None = None,
    ocr: bool = True,
    ocr_image: Callable[[bytes], str] | None = None,
    document_parser: str = "local",
    mineru_extract: Callable[[Path], str] | None = None,
) -> CleanedResume:
    path = Path(source).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"unsupported resume format: {suffix}")
    if document_parser not in DOCUMENT_PARSERS:
        raise ValueError(f"unsupported document parser: {document_parser}")
    if document_parser == "mineru-flash" and suffix not in {".pdf", ".docx"}:
        raise ValueError("MinerU 解析仅用于 PDF 或 DOCX 简历")
    used_ocr = False
    extraction_engine = "local"
    mineru_body = False
    if document_parser == "mineru-flash":
        extractor = mineru_extract or _run_mineru_flash
        pages = [_strip_opaque_platform_tokens(extractor(path))]
        page_count = _pdf_page_count(path) if suffix == ".pdf" else 1
        used_ocr = suffix == ".pdf"
        extraction_engine = "mineru-flash"
        mineru_body = True
    elif suffix == ".pdf":
        pages, used_ocr = _read_pdf(path, ocr=ocr, ocr_image=ocr_image)
        page_count = len(pages)
    elif suffix == ".docx":
        pages = _read_docx(path)
        page_count = len(pages)
    else:
        try:
            pages = [path.read_text(encoding="utf-8-sig")]
        except UnicodeDecodeError as exc:
            raise ResumeQualityError("文本文件不是有效 UTF-8") from exc
        page_count = len(pages)
    pages = [_strip_opaque_platform_tokens(page) for page in pages]
    _quality_check(pages)

    source_sha256 = _sha256(path)
    generated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    body_parts: list[str] = []
    for index, page in enumerate(pages, start=1):
        heading = "## MinerU 提取结果" if mineru_body else f"## 第 {index} 页"
        body_parts.extend((heading, "", page.strip(), ""))
    body = "\n".join(body_parts).rstrip() + "\n"
    redacted_body = redact_for_model(body, candidate_name=candidate_name)
    markdown = (
        "---\n"
        f"candidate_id: {candidate_id}\n"
        f"source_sha256: {source_sha256}\n"
        f"parser_version: {PARSER_VERSION}\n"
        f"extraction_engine: {extraction_engine}\n"
        f"generated_at: {generated_at}\n"
        f"used_ocr: {str(used_ocr).lower()}\n"
        f"page_count: {page_count}\n"
        "---\n\n" + redacted_body
    )
    return CleanedResume(
        source_path=path,
        source_sha256=source_sha256,
        candidate_id=candidate_id,
        markdown=markdown,
        model_text=redacted_body,
        used_ocr=used_ocr,
        page_count=page_count,
        extraction_engine=extraction_engine,
    )
