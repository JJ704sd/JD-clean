"""Export only source and specifications, never personal data or runtime state."""

import hashlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    path = release / "ResumeDesk-0.2.0-preview-source.zip"
    files = [
        ROOT / name for name in ("README.md", "pyproject.toml", "uv.lock", ".gitignore")
    ]
    for name in ("resume_screening", "scripts", "tests", "skills", "docs"):
        files.extend(
            p
            for p in (ROOT / name).rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and p.suffix in (".py", ".md", ".json", ".yaml", ".ps1", ".command")
        )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(files):
            relative = file.relative_to(ROOT).as_posix()
            entry = zipfile.ZipInfo("ResumeDesk-source/" + relative)
            entry.create_system = 3
            entry.external_attr = (
                0o100755 if file.suffix == ".command" else 0o100644
            ) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, file.read_bytes())
    path.with_suffix(".sha256").write_text(
        hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name + "\n",
        encoding="utf-8",
    )
    print(path)


if __name__ == "__main__":
    main()
