from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from resume_screening.cli import main
from resume_screening.queue import TaskStore


class WatchCliTests(unittest.TestCase):
    def test_auto_route_watch_accepts_mixed_downloads_and_keeps_unknown_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "Downloads"
            incoming.mkdir()
            for filename in (
                "【ai产品经理】候选人甲.md",
                "【全栈开发实习生】候选人乙.md",
                "【资深全栈】候选人丙.md",
                "电子发票.md",
            ):
                (incoming / filename).write_text("有效简历内容 " * 30, encoding="utf-8")

            class FakePipeline:
                def __init__(self, **_: object):
                    self.tasks = iter(
                        SimpleNamespace(
                            task_id=index,
                            candidate_id=f"candidate-{index}",
                            status="succeeded",
                            error_code=None,
                        )
                        for index in range(1, 4)
                    )

                def process_next(self):
                    return next(self.tasks)

            with (
                patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}, clear=True),
                patch("resume_screening.cli.ScreeningPipeline", FakePipeline),
                patch("resume_screening.cli.time.sleep"),
                redirect_stdout(io.StringIO()),
            ):
                code = main(
                    [
                        "--database",
                        str(root / "state.sqlite3"),
                        "worker",
                        "--watch",
                        "--input",
                        str(incoming),
                        "--auto-route",
                        "--max-tasks",
                        "3",
                    ]
                )

            self.assertEqual(code, 0)
            store = TaskStore(root / "state.sqlite3")
            tasks = []
            while (task := store.claim_next()) is not None:
                tasks.append(task)
            self.assertEqual(
                {task.role for task in tasks},
                {
                    "ai-product-manager",
                    "fullstack-development-intern",
                    "senior-fullstack-engineer",
                },
            )
            self.assertEqual(len(tasks), 3)


if __name__ == "__main__":
    unittest.main()

