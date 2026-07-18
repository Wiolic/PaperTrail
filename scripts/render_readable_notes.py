#!/usr/bin/env python3
"""从 notes/(唯一真源,不分行) 重新生成 notes-readable/(正文按60字折行,供人眼阅读)。

notes-readable/ 是纯生成物,不要手工编辑——改动应该在 notes/ 里做,然后重跑本脚本同步。
用法: python scripts/render_readable_notes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_ingest as b


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    b.NOTES_READABLE.mkdir(exist_ok=True)
    n = 0
    for note_path in sorted(b.NOTES.glob("*.md")):
        text = note_path.read_text(encoding="utf-8")
        readable = b.make_readable(text)
        (b.NOTES_READABLE / note_path.name).write_text(readable, encoding="utf-8", newline="\n")
        n += 1
    print(f"已同步 {n} 篇笔记到 notes-readable/")


if __name__ == "__main__":
    main()
