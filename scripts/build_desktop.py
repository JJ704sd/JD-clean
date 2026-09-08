"""Build the current OS/architecture only, with explicitly bundled OCR resources."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="developer-only, artifact marked unverified",
    )
    args = parser.parse_args()
    if sys.platform not in ("win32", "darwin"):
        raise SystemExit("Build on Windows or macOS using native Python 3.12")
    if sys.platform == "darwin" and int(platform.mac_ver()[0].split(".")[0]) < 14:
        raise SystemExit("The locked native dependencies require macOS 14 or newer")
    # Build-time initialization fetches upstream checksum-verified models when absent.
    # End users receive these files; no model download is allowed at runtime.
    from rapidocr import RapidOCR

    RapidOCR()
    import rapidocr

    models = Path(rapidocr.__file__).parent / "models"
    if len(list(models.glob("*.onnx"))) < 3:
        raise SystemExit(
            "OCR models missing; refusing to create an incomplete offline package"
        )
    build = ROOT / "build/desktop"
    build.mkdir(parents=True, exist_ok=True)
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    notices = build / "THIRD-PARTY-NOTICES"
    notices.mkdir(exist_ok=True)
    versions = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        versions[name] = dist.version
        lines = [
            f"{name} {dist.version}",
            dist.metadata.get("License-Expression", ""),
            dist.metadata.get("License", ""),
            dist.metadata.get("Home-page", ""),
        ]
        for item in dist.files or []:
            if "license" in str(item).lower() or "copying" in str(item).lower():
                path = Path(dist.locate_file(item))
                if path.is_file() and path.stat().st_size < 500_000:
                    lines.append(path.read_text(encoding="utf-8", errors="replace"))
        (notices / (name.replace("/", "-") + ".txt")).write_text(
            "\n\n".join(lines), encoding="utf-8"
        )
    manifest = {
        "platform": sys.platform,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "dependencies": versions,
        "signed": False,
        "models": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in models.glob("*.onnx")
        },
    }
    (build / "build-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--windowed",
        "--onedir",
        "--name",
        "ResumeDesk",
        "--paths",
        str(ROOT),
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(build / "pyinstaller"),
        "--specpath",
        str(build),
        "--add-data",
        f"{ROOT / 'skills'}:skills",
        "--add-data",
        f"{notices}:THIRD-PARTY-NOTICES",
        "--add-data",
        f"{build / 'build-manifest.json'}:.",
        "--add-data",
        f"{ROOT / 'docs/desktop-user-guide.md'}:.",
        "--collect-all",
        "rapidocr",
        "--collect-all",
        "onnxruntime",
        "--collect-all",
        "pymupdf",
        "--hidden-import",
        "keyring.backends.Windows"
        if sys.platform == "win32"
        else "keyring.backends.macOS",
        "--exclude-module",
        "pytest",
    ]
    if sys.platform == "darwin":
        command += [
            "--osx-bundle-identifier",
            "local.resumedesk.desktop",
            "--target-arch",
            platform.machine(),
        ]
    subprocess.run(
        [*command, str(ROOT / "scripts/desktop_entry.py")], check=True, cwd=ROOT
    )
    app = (
        ROOT / "dist" / ("ResumeDesk" if sys.platform == "win32" else "ResumeDesk.app")
    )
    executable = app / (
        "ResumeDesk.exe" if sys.platform == "win32" else "Contents/MacOS/ResumeDesk"
    )
    report = build / "packaged-smoke.json"
    if not args.skip_smoke:
        result = subprocess.run(
            [str(executable), "--smoke-test", str(report)],
            cwd=build,
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if (
            result.returncode
            or not report.exists()
            or not json.loads(report.read_text(encoding="utf-8")).get("passed")
        ):
            raise SystemExit(f"Packaged smoke test failed. Inspect {report}")
    label = "windows-x64" if sys.platform == "win32" else "macos-" + platform.machine()
    archive_name = release / ("ResumeDesk-0.2.0-preview-" + label)
    if sys.platform == "darwin":
        # ditto preserves .app symlinks and metadata; generic ZIP writers do not.
        archive = Path(str(archive_name) + ".zip")
        subprocess.run(
            [
                "ditto",
                "-c",
                "-k",
                "--sequesterRsrc",
                "--keepParent",
                str(app),
                str(archive),
            ],
            check=True,
        )
    else:
        archive = Path(
            shutil.make_archive(
                str(archive_name), "zip", root_dir=app.parent, base_dir=app.name
            )
        )
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".sha256").write_text(
        f"{checksum}  {archive.name}\n", encoding="utf-8"
    )
    print(
        f"Built {archive}; unsigned preview; smoke {'NOT RUN' if args.skip_smoke else 'passed'}"
    )


if __name__ == "__main__":
    main()
