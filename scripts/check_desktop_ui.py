"""Capture only this application's synthetic UI for visual QA."""

import sys
import tempfile
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import ImageGrab

from resume_screening.desktop import DesktopApp
from resume_screening.desktop_smoke import TEXT
from resume_screening.desktop_store import ReviewStore


def main():
    output = ROOT / "build/desktop"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = root / "合成示例简历.txt"
        source.write_text(TEXT * 3, encoding="utf-8")
        store = ReviewStore(root / "data")
        record = store.prepare(source, "senior-fullstack-engineer")
        window = tk.Tk()
        app = DesktopApp(window, root / "data")
        app.documents.selection_set(record["id"])
        window.update()

        def capture():
            window.update()
            box = (
                window.winfo_rootx(),
                window.winfo_rooty(),
                window.winfo_rootx() + window.winfo_width(),
                window.winfo_rooty() + window.winfo_height(),
            )
            ImageGrab.grab(bbox=box).save(output / "desktop-ui.png")
            window.destroy()

        window.after(1200, capture)
        window.mainloop()


if __name__ == "__main__":
    main()
