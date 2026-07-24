#!/usr/bin/env python3
"""
按条件(标签/期刊/关键词)筛选 notes/, 把对应的 PDF(papers/) 和易读笔记(notes-readable/)
一起复制到一个独立文件夹, 供整理成"某主题参考资料包"发给别人或本地翻阅用。

用法:
  # 用 "top journals"(见 scripts/top_journals.txt)+ tags 筛选:
  python scripts/export_referable_folder.py --top-journals --tags PEMWE --out PEMWE_referable

  # 任意期刊名单 + 标题正则:
  python scripts/export_referable_folder.py --journals "Science,Nature" --title-regex "iridium" --out ir_science_nature

只读 notes/papers/notes-readable, 只在目标文件夹里写文件(默认路径为库根目录下的同名文件夹),
不改动 notes/papers/notes-readable 本身, 可重复运行(会覆盖同名文件, 增量更新用)。
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "notes"
READABLE = ROOT / "notes-readable"
PAPERS = ROOT / "papers"
TOP_JOURNALS_FILE = Path(__file__).resolve().parent / "top_journals.txt"


def load_top_journals() -> set:
    if not TOP_JOURNALS_FILE.exists():
        sys.exit(f"找不到 {TOP_JOURNALS_FILE}")
    lines = TOP_JOURNALS_FILE.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")}


def parse_field(text: str, field: str) -> str:
    m = re.search(rf"^{re.escape(field)}:[ \t]*(.*)$", text, re.MULTILINE)  # [ \t]* 不用 \s*, 避免吃掉换行符跨行误匹配
    return m.group(1).strip() if m else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top-journals", action="store_true", help="期刊限定为 scripts/top_journals.txt 里的名单")
    ap.add_argument("--journals", default="", help="逗号分隔的期刊名单(与 --top-journals 二选一或都不填=不限期刊)")
    ap.add_argument("--tags", default="", help="逗号分隔标签, 任意命中即选中")
    ap.add_argument("--title-regex", default="", help="标题正则筛选(忽略大小写)")
    ap.add_argument("--out", required=True, help="输出文件夹(相对库根目录或绝对路径)")
    args = ap.parse_args()

    journal_set = None
    if args.top_journals:
        journal_set = load_top_journals()
    elif args.journals:
        journal_set = {j.strip() for j in args.journals.split(",") if j.strip()}

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    title_re = re.compile(args.title_regex, re.IGNORECASE) if args.title_regex else None

    if journal_set is None and not tags and not title_re:
        sys.exit("至少指定 --top-journals/--journals、--tags、--title-regex 之一, 否则会导出全库")

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    matched = []
    for f in sorted(NOTES.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        citekey = parse_field(text, "citekey")
        journal = parse_field(text, "journal")
        title = parse_field(text, "title")
        note_tags = [t.strip().strip("[]") for t in parse_field(text, "tags").strip("[]").split(",")]

        if journal_set is not None and journal not in journal_set:
            continue
        if tags and not any(t.lower() in {nt.lower() for nt in note_tags} for t in tags):
            continue
        if title_re and not title_re.search(title):
            continue

        matched.append((citekey, journal, title))

    missing = []
    for citekey, journal, title in matched:
        pdf_src = PAPERS / f"{citekey}.pdf"
        md_src = READABLE / f"{citekey}.md"
        if pdf_src.exists():
            shutil.copy2(pdf_src, out_dir / pdf_src.name)
        else:
            missing.append(f"{citekey}.pdf")
        if md_src.exists():
            shutil.copy2(md_src, out_dir / md_src.name)
        else:
            missing.append(f"{citekey}.md (notes-readable)")

    print(f"匹配 {len(matched)} 篇, 已复制到 {out_dir}")
    for citekey, journal, title in matched:
        print(f"  - {citekey}  [{journal}]")
    if missing:
        print(f"警告: {len(missing)} 个文件缺失, 未复制:")
        for m in missing:
            print(f"    - {m}")


if __name__ == "__main__":
    main()
