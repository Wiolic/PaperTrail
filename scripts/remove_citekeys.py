#!/usr/bin/env python3
"""从库里彻底移除一批指定的 citekey: 删 papers/(含_SI)、notes/、notes-readable/、
extracted-text/、library.bib 条目、INDEX.md 行; 对应 state.json 记录改成
status=excluded_by_user(不是删除, 保留痕迹, 避免以后重跑同源目录时被当成新文件重新处理)。

用法: python scripts/remove_citekeys.py --file <citekey列表文件,每行一个> [--apply]
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_ingest as b


def remove_one(citekey: str):
    removed = []
    for p in b.PAPERS.glob(f"{citekey}*"):
        p.unlink()
        removed.append(str(p))
    for folder, ext in ((b.NOTES, ".md"), (b.NOTES_READABLE, ".md"), (b.EXTRACTED_TEXT, ".txt")):
        f = folder / f"{citekey}{ext}"
        if f.exists():
            f.unlink()
            removed.append(str(f))

    if b.BIB.exists():
        text = b.BIB.read_text(encoding="utf-8")
        pattern = re.compile(rf"\n?@article\{{{re.escape(citekey)},.*?\n\}}\n", re.S)
        new_text, n = pattern.subn("", text)
        if n:
            b.BIB.write_text(new_text, encoding="utf-8", newline="\n")
            removed.append(f"bib条目 x{n}")

    if b.INDEX.exists():
        text = b.INDEX.read_text(encoding="utf-8")
        new_text, n = re.subn(rf"^\|\s*{re.escape(citekey)}\s*\|.*\n", "", text, flags=re.M)
        if n:
            b.INDEX.write_text(new_text, encoding="utf-8", newline="\n")
            removed.append(f"INDEX行 x{n}")

    return removed


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="citekey 列表文件, 每行一个")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    citekeys = [l.strip() for l in Path(args.file).read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"共 {len(citekeys)} 个 citekey 待移除" + ("" if args.apply else " (预览模式, 不落盘)"))

    state = b.load_state()

    for ck in citekeys:
        print(f"- {ck}")
        if args.apply:
            removed = remove_one(ck)
            for path, info in state.items():
                if info.get("citekey") == ck:
                    info["status"] = "excluded_by_user"
            if not removed:
                print("  (未找到对应文件, 可能已被移除)")

    if args.apply:
        b.save_state(state)
        b.sync_index_count()
        print("\n已移除并同步 INDEX 计数。建议接着跑 build_keyword_index.sh + check_library.sh")


if __name__ == "__main__":
    main()
