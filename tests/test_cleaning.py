from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from resume_screening.cleaning import ResumeQualityError, clean_resume


class ResumeCleaningTests(unittest.TestCase):
    def test_pdf_text_layer_is_converted_with_page_markers(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("pymupdf is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "resume.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text(
                (72, 72),
                "Go logistics order service delivery and production support. " * 5,
            )
            document.save(source)
            document.close()

            result = clean_resume(source, candidate_id="candidate-pdf")

        self.assertFalse(result.used_ocr)
        self.assertEqual(result.page_count, 1)
        self.assertIn("Go logistics order service", result.markdown)

    def test_image_only_pdf_uses_injected_ocr_fallback_once(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("pymupdf is not installed")
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "scanned.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(source)
            document.close()

            def fake_ocr(image: bytes) -> str:
                calls.append(image)
                return (
                    "通过 OCR 识别：负责 Go 物流订单服务的开发、测试、上线和故障排查。"
                    * 6
                )

            result = clean_resume(
                source,
                candidate_id="candidate-scan",
                ocr_image=fake_ocr,
            )

        self.assertTrue(result.used_ocr)
        self.assertEqual(len(calls), 1)
        self.assertIn("通过 OCR 识别", result.markdown)

    def test_opaque_platform_tokens_trigger_ocr_and_are_removed(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("pymupdf is not installed")
        token = "ac3bc4dd08a18ecb1Hx72N24GVNTw4m4WPudWOOnm_DTPxRg2Q~~"
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "tokenized.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((36, 72), (token + "\n") * 6, fontsize=6)
            document.save(source)
            document.close()

            def fake_ocr(image: bytes) -> str:
                calls.append(image)
                return "OCR正文：负责 Go 订单服务、Vue3 后台和物流履约链路交付。" * 6

            result = clean_resume(
                source,
                candidate_id="candidate-tokenized",
                ocr_image=fake_ocr,
            )

        self.assertTrue(result.used_ocr)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(token, result.model_text)
        self.assertIn("OCR正文", result.model_text)

    def test_text_resume_becomes_traceable_markdown_and_redacted_model_input(self):
        source_text = """张三
电话：13812345678  邮箱：candidate@example.com
项目经历
我使用 Go 开发跨境物流订单服务，负责接口、数据表和异常重试机制，完成生产上线。
我还重构了轨迹查询链路，将平均响应时间从 900ms 降低到 300ms，并负责上线后的监控与回滚方案。
"""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "resume.txt"
            source.write_text(source_text, encoding="utf-8")
            result = clean_resume(
                source, candidate_id="candidate-001", candidate_name="张三"
            )

        self.assertIn("candidate_id: candidate-001", result.markdown)
        self.assertIn("source_sha256:", result.markdown)
        self.assertIn("## 第 1 页", result.markdown)
        self.assertIn("13812345678", result.markdown)
        self.assertNotIn("13812345678", result.model_text)
        self.assertNotIn("candidate@example.com", result.model_text)
        self.assertNotIn("张三", result.model_text)
        self.assertIn("[已脱敏电话]", result.model_text)

    def test_low_quality_resume_stops_before_screening(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "broken.md"
            source.write_text("��", encoding="utf-8")
            with self.assertRaises(ResumeQualityError) as caught:
                clean_resume(source, candidate_id="candidate-broken")

        self.assertEqual(caught.exception.code, "U01_PARSE_QUALITY")

    def test_cleaning_is_deterministic_for_the_same_source(self):
        text = (
            "项目经历\n" + "负责 Go 物流订单系统的接口开发、测试、上线和故障排查。" * 8
        )
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "resume.md"
            source.write_text(text, encoding="utf-8")
            first = clean_resume(source, candidate_id="candidate-001")
            second = clean_resume(source, candidate_id="candidate-001")

        self.assertEqual(first.source_sha256, second.source_sha256)
        self.assertEqual(first.markdown, second.markdown)


if __name__ == "__main__":
    unittest.main()
