"""Run the directly launchable Windows one-file executable in isolation."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXECUTABLE = ROOT / "release/ResumeDesk-0.2.0-preview-windows-x64-onefile.exe"


def main():
    if not EXECUTABLE.is_file():
        raise SystemExit(f"Missing one-file executable: {EXECUTABLE}")
    report_dir = ROOT / "build/desktop"
    report_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="isolated-onefile-", dir=ROOT / "build"
    ) as folder:
        working_directory = Path(folder).resolve()
        environment = os.environ.copy()
        for key in (
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "MINIMAX_API_KEY",
            "MINIMAX_API_BASE",
            "MINIMAX_API_ENDPOINT",
        ):
            environment.pop(key, None)
        environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
        for option, name in (
            ("--smoke-test", "onefile-isolated-smoke.json"),
            ("--credential-smoke-test", "onefile-isolated-credential-smoke.json"),
        ):
            report = report_dir / name
            subprocess.run(
                [str(EXECUTABLE), option, str(report)],
                cwd=working_directory,
                env=environment,
                check=True,
                timeout=300,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if not json.loads(report.read_text(encoding="utf-8"))["passed"]:
                raise SystemExit(f"One-file smoke failed: {report}")
    print("One-file executable, offline workflow and system credentials passed")


if __name__ == "__main__":
    main()
