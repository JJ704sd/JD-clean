"""PyInstaller entry, intentionally does not import the development .env."""

from resume_screening.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
