#!/usr/bin/env python3
"""
把 citekey 里某个期刊简称统一改写成另一个(如 ACIE -> AngewChemIntEd), 涉及全部五处:
papers/<citekey>.pdf(含 _SI.*)、notes/<citekey>.md、notes-readable/<citekey>.md、
extracted-text/<citekey>.txt、library.bib 的 @article{<citekey>, 、INDEX.md 的表格行、
以及笔记正文/frontmatter 里出现的旧 citekey 字符串(如 related 字段引用了旧 citekey)。

背景: citekey 格式是 <年份>-<期刊简称>-<提炼标题>, 同一期刊如果历史上用过不同缩写
(比如 Angewandte Chemie International Edition 同时存在 ACIE 和 AngewChemIntEd 两种缩写)
会导致同一期刊的论文在库里"看起来"分成两类, 检索/统计时容易漏。本脚本批量做这种改名。

用法(先预览):
  python scripts/rename_journal_abbr.py --from ACIE --to AngewChemIntEd
  python scripts/rename_journal_abbr.py --from ACIE --to AngewChemIntEd --apply

只匹配 citekey 中 "<年份>-<简称>-" 这个位置的简称片段(用年份+连字符定位, 避免误改
标题部分恰好包含同样字符串的情况)。
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "notes"
READABLE = ROOT / "notes-readable"
PAPERS = ROOT / "papers"
EXTRACTED = ROOT / "extracted-text"
BIB = ROOT / "library.bib"
INDEX = ROOT / "INDEX.md"

CITEKEY_ABBR_RE_TEMPLATE = r"^(\d{{4}}-){0}-"


def find_matching_citekeys(old_abbr: str) -> list[str]:
    pattern = re.compile(CITEKEY_ABBR_RE_TEMPLATE.format(re.escape(old_abbr)))
    return sorted(f.stem for f in NOTES.glob("*.md") if pattern.match(f.stem))


def new_citekey(old_ck: str, old_abbr: str, new_abbr: str) -> str:
    return re.sub(CITEKEY_ABBR_RE_TEMPLATE.format(re.escape(old_abbr)),
                   lambda m: m.group(1) + new_abbr + "-", old_ck, count=1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="old_abbr", required=True)
    ap.add_argument("--to", dest="new_abbr", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    citekeys = find_matching_citekeys(args.old_abbr)
    if not citekeys:
        sys.exit(f"没有找到期刊简称为 {args.old_abbr} 的 citekey")

    print(f"=== 将 {len(citekeys)} 个 citekey 的期刊简称 {args.old_abbr} -> {args.new_abbr} ===")
    renames = [(ck, new_citekey(ck, args.old_abbr, args.new_abbr)) for ck in citekeys]
    for old, new in renames:
        print(f"  {old}  ->  {new}")

    if not args.apply:
        print("\n(预览模式, 未写入任何改动。确认无误后加 --apply 执行)")
        return

    bib_text = BIB.read_text(encoding="utf-8")
    index_text = INDEX.read_text(encoding="utf-8")

    for old_ck, new_ck in renames:
        # 1) 重命名四类文件
        for base_dir, ext in [(NOTES, ".md"), (READABLE, ".md"), (EXTRACTED, ".txt")]:
            src = base_dir / f"{old_ck}{ext}"
            if src.exists():
                src.rename(base_dir / f"{new_ck}{ext}")
        pdf_src = PAPERS / f"{old_ck}.pdf"
        if pdf_src.exists():
            pdf_src.rename(PAPERS / f"{new_ck}.pdf")
        for si in PAPERS.glob(f"{old_ck}_SI.*"):
            si.rename(PAPERS / si.name.replace(old_ck, new_ck, 1))

        # 2) library.bib: 条目名 + 笔记里可能引用旧 citekey 的地方(如 related 字段)一起替换
        bib_text = re.sub(rf"@article\{{{re.escape(old_ck)},", f"@article{{{new_ck},", bib_text)

        # 3) INDEX.md 表格行开头的 citekey
        index_text = re.sub(rf"(?m)^\| {re.escape(old_ck)} \|", f"| {new_ck} |", index_text)

        # 4) 全库 notes/ 里如果有 [[old_ck]] 或 related 数组引用旧 citekey, 一并替换
        for f in NOTES.glob("*.md"):
            text = f.read_text(encoding="utf-8")
            if old_ck in text:
                f.write_text(text.replace(old_ck, new_ck), encoding="utf-8")

    BIB.write_text(bib_text, encoding="utf-8")
    INDEX.write_text(index_text, encoding="utf-8")

    print(f"\n完成: {len(renames)} 个 citekey 已从 {args.old_abbr} 改名为 {args.new_abbr}。"
          f"记得跑 render_readable_notes.py 重新同步 notes-readable(笔记正文里的旧citekey引用已一并替换,"
          f"但 notes-readable 是重命名而非重新渲染, 内容里的旧引用不会自动更新, 建议直接重跑一次全量同步)。")


if __name__ == "__main__":
    main()
