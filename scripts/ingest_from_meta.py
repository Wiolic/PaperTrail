#!/usr/bin/env python3
"""
日常入库流程(路线C)的"落盘"步骤脚本化 —— 2026-07-19 加, 目的是减少 Claude 的 token 消耗。

背景: 之前路线C是"ds.py pdf-meta 抽JSON → Claude 读完整JSON → Claude 手动把7节正文重新打进
Write 工具"——JSON 到笔记 Markdown 的转换本来就是机械的格式转换, 不需要 Claude 重新阅读/重打一遍
内容, 那部分token纯粹浪费在"抄写"上。改成这个脚本后, Claude 的活变成: ①跑 ds.py pdf-meta,
②只读 JSON 里的 title/doi 判断查重, ③判断 SI 配对, ④调这个脚本让它自己组装六处文件。真正需要
判断的部分(查重/DOI核实/SI配对)还是 Claude 做, 机械转录不用 Claude 做。

用法:
  python scripts/ingest_from_meta.py --meta meta.json --pdf inbox/xxx.pdf --citekey 2025-JACS-Xxx
  python scripts/ingest_from_meta.py --meta meta.json --pdf inbox/xxx.pdf   # 不给citekey则自动生成
  python scripts/ingest_from_meta.py --meta meta.json --pdf inbox/xxx.pdf --si inbox/xxx_si.pdf

做的事: 复制主PDF(+SI)到 papers/、追加 library.bib 条目、写 notes/+notes-readable/、
抽全文存 extracted-text/、追加 INDEX.md 一行。不做查重/DOI核实/SI配对判断——这些留给调用方
(Claude)在调用前用 grep/Crossref 自己确认好, 传进来的 citekey/si 就是最终决定。

不做的事(仍需 Claude 自己做的判断): 查重、DOI 可信度核实、SI 是否配对、inbox 源文件删不删。
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_ingest as b  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAPERS = ROOT / "papers"
EXTRACTED = ROOT / "extracted-text"
BIB = ROOT / "library.bib"


def existing_citekeys() -> set:
    return {f.stem for f in (ROOT / "notes").glob("*.md")}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--meta", required=True, help="ds.py pdf-meta 产出的 JSON 文件路径")
    ap.add_argument("--pdf", required=True, help="主 PDF 源文件路径(会被复制, 不会移动/删除源文件)")
    ap.add_argument("--citekey", help="不给则按 meta 里的 year/journal_abbr/condensed_title 自动生成")
    ap.add_argument("--si", nargs="*", default=[], help="SI 源文件路径(可多个), 会复制改名为 <citekey>_SI.<ext>")
    args = ap.parse_args()

    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    pdf_src = Path(args.pdf)
    if not pdf_src.exists():
        sys.exit(f"PDF 不存在: {pdf_src}")

    taken = existing_citekeys()
    citekey = args.citekey or b.make_citekey(meta["year"], meta.get("journal_abbr", ""), meta.get("condensed_title", ""), taken)
    if citekey in taken:
        sys.exit(f"citekey 已存在, 请先确认查重结果: {citekey}")

    PAPERS.mkdir(exist_ok=True)
    EXTRACTED.mkdir(exist_ok=True)

    shutil.copy2(pdf_src, PAPERS / f"{citekey}.pdf")

    si_dest_names = []
    for si_src in args.si:
        si_src = Path(si_src)
        if not si_src.exists():
            print(f"[警告] SI 不存在, 跳过: {si_src}", file=sys.stderr)
            continue
        dest_name = f"{citekey}_SI{si_src.suffix}"
        shutil.copy2(si_src, PAPERS / dest_name)
        si_dest_names.append(dest_name)

    with BIB.open("a", encoding="utf-8", newline="\n") as f:
        f.write("\n" + b.bib_entry(citekey, meta))

    note_text = b.note_content(citekey, meta, si_dest_names)
    b.write_note(citekey, note_text)

    full_text = b.extract_pdf_text(PAPERS / f"{citekey}.pdf")
    (EXTRACTED / f"{citekey}.txt").write_text(full_text, encoding="utf-8")

    b.append_index(b.index_line(citekey, meta, si_dest_names))

    print(f"[完成] {citekey}  si={si_dest_names or '无'}")


if __name__ == "__main__":
    main()
