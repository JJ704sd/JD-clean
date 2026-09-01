"""Local resume normalization, OCR fallback, quality checks, and PII minimization."""

from __future__ import annotations

import hashlib
import re
import unicodedata
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

PARSER_VERSION = "resume-cleaner-2026-09-01-v2"
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}

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
OPAQUE_PLATFORM_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40,}~~(?![A-Za-z0-9_-])"
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
) -> CleanedResume:
    path = Path(source).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError(f"unsupported resume format: {suffix}")
    used_ocr = False
    if suffix == ".pdf":
        pages, used_ocr = _read_pdf(path, ocr=ocr, ocr_image=ocr_image)
    elif suffix == ".docx":
        pages = _read_docx(path)
    else:
        try:
            pages = [path.read_text(encoding="utf-8-sig")]
        except UnicodeDecodeError as exc:
            raise ResumeQualityError("文本文件不是有效 UTF-8") from exc
    pages = [_strip_opaque_platform_tokens(page) for page in pages]
    _quality_check(pages)

    source_sha256 = _sha256(path)
    generated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    body_parts: list[str] = []
    for index, page in enumerate(pages, start=1):
        body_parts.extend((f"## 第 {index} 页", "", page.strip(), ""))
    body = "\n".join(body_parts).rstrip() + "\n"
    redacted_body = redact_for_model(body, candidate_name=candidate_name)
    markdown = (
        "---\n"
        f"candidate_id: {candidate_id}\n"
        f"source_sha256: {source_sha256}\n"
        f"parser_version: {PARSER_VERSION}\n"
        f"generated_at: {generated_at}\n"
        f"used_ocr: {str(used_ocr).lower()}\n"
        f"page_count: {len(pages)}\n"
        "---\n\n" + redacted_body
    )
    return CleanedResume(
        source_path=path,
        source_sha256=source_sha256,
        candidate_id=candidate_id,
        markdown=markdown,
        model_text=redacted_body,
        used_ocr=used_ocr,
        page_count=len(pages),
    )
