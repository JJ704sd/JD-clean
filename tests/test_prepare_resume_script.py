from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "skills"
    / "screen-senior-fullstack-resumes"
    / "scripts"
    / "prepare_resume.py"
)


class PrepareResumeScriptTests(unittest.TestCase):
    def test_mineru_requires_explicit_external_processing_acknowledgement(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "resume.pdf"
            source.write_bytes(b"not uploaded")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(source),
                    "--candidate-id",
                    "candidate-001",
                    "--parser",
                    "mineru-flash",
                    "--output",
                    str(Path(tmp) / "prepared.md"),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("--allow-external-processing", completed.stderr)

    def test_local_parser_writes_redacted_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "resume.txt"
            source.write_text(
                "张三负责 Go 物流订单服务开发、测试和上线，电话 13812345678。" * 6,
                encoding="utf-8",
            )
            destination = Path(tmp) / "prepared.md"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(source),
                    "--candidate-id",
                    "candidate-001",
                    "--candidate-name",
                    "张三",
                    "--output",
                    str(destination),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            markdown = destination.read_text(encoding="utf-8")

        self.assertEqual(completed.returncode, 0)
        self.assertIn("解析器=local", completed.stdout)
        self.assertNotIn("13812345678", markdown)
        self.assertNotIn("张三", markdown)


if __name__ == "__main__":
    unittest.main()
