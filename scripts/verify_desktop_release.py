"""Run the ZIP's executable from a fresh directory with no Python on PATH."""

import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    archive_path = ROOT / "release/ResumeDesk-0.2.0-preview-windows-x64.zip"
    report_dir = ROOT / "build/desktop"
    with tempfile.TemporaryDirectory(
        prefix="isolated-release-", dir=ROOT / "build"
    ) as folder:
        destination = Path(folder).resolve()
        assert destination.is_relative_to((ROOT / "build").resolve())
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                if (
                    not (destination / member.filename)
                    .resolve()
                    .is_relative_to(destination)
                ):
                    raise ValueError("Archive entry escapes extraction directory")
            archive.extractall(destination)
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
        executable = destination / "ResumeDesk/ResumeDesk.exe"
        for option, name in (
            ("--smoke-test", "isolated-release-smoke.json"),
            ("--credential-smoke-test", "isolated-credential-smoke.json"),
        ):
            report = report_dir / name
            subprocess.run(
                [str(executable), option, str(report)],
                cwd=destination,
                env=environment,
                check=True,
                timeout=240,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert json.loads(report.read_text(encoding="utf-8"))["passed"]
    print(
        "ZIP extraction, isolated executable, offline workflow and system credentials passed"
    )


if __name__ == "__main__":
    main()
