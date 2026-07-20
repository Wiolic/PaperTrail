#!/usr/bin/env python3
"""
合并两个疑似重复收录的 citekey: 保留 winner, 把 loser 独有的更优字段(doi/keywords/si_files)
合并进 winner, 然后删除 loser 在 papers/ notes/ notes-readable/ extracted-text/ library.bib/
INDEX.md 五处的记录。

用法(先预览, 不加 --apply 不会写任何文件):
  python scripts/resolve_duplicate.py --winner <citekey_A> --loser <citekey_B>
  python scripts/resolve_duplicate.py --winner <citekey_A> --loser <citekey_B> --apply

判定"重复"不是本脚本的活(交给 build_topic_digest.py 的标题相似度查重, 或人工判断);
本脚本只负责"已经确认是重复, 该怎么二选一保留+合并"这一步, 减少手动改 5 处文件的重复劳动。

合并规则:
  - doi: winner 缺失(N/A/空)且 loser 有效时, 采用 loser 的 doi
  - keywords: winner 为空 [] 且 loser 非空时, 采用 loser 的 keywords
  - si_files: 两边取并集
  - 其余字段(title/authors/tags/体系/正文等)一律保留 winner 的, 不做合并(避免正文混杂)
  - library.bib: 用合并后的 winner 字段重写该条目; 删除 loser 条目
  - INDEX.md: 删除 loser 那一行(winner 行不变, 除非其 SI 标记因合并变化才更新)
  - papers/<loser>.pdf(以及 _SI 附件如果有)、notes/<loser>.md、notes-readable/<loser>.md、
    extracted-text/<loser>.txt 一并删除
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


def read_field(text: str, field: str) -> str:
    m = re.search(rf"^{re.escape(field)}:[ \t]*(.*)$", text, re.MULTILINE)  # [ \t]* 不用 \s*, 避免吃掉换行符跨行误匹配
    return m.group(1).strip() if m else ""


def is_missing_doi(doi: str) -> bool:
    return doi.strip().strip('"').upper() in {"", "N/A", "NA", "UNKNOWN", "NONE", "NULL"}


def is_empty_list_field(val: str) -> bool:
    return val.strip() in {"[]", ""}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--winner", required=True)
    ap.add_argument("--loser", required=True)
    ap.add_argument("--apply", action="store_true", help="不加此项只打印计划, 不实际改动")
    args = ap.parse_args()

    winner_note = NOTES / f"{args.winner}.md"
    loser_note = NOTES / f"{args.loser}.md"
    if not winner_note.exists() or not loser_note.exists():
        sys.exit(f"找不到笔记: winner={winner_note.exists()} loser={loser_note.exists()}")

    w_text = winner_note.read_text(encoding="utf-8")
    l_text = loser_note.read_text(encoding="utf-8")

    w_doi, l_doi = read_field(w_text, "doi"), read_field(l_text, "doi")
    w_kw, l_kw = read_field(w_text, "keywords"), read_field(l_text, "keywords")
    w_si, l_si = read_field(w_text, "si_files"), read_field(l_text, "si_files")

    plan = []
    new_text = w_text

    if is_missing_doi(w_doi) and not is_missing_doi(l_doi):
        plan.append(f"doi: {w_doi!r} -> {l_doi!r} (采用 loser 的有效 DOI)")
        new_text = re.sub(r"^doi:.*$", f"doi: {l_doi}", new_text, count=1, flags=re.MULTILINE)

    if is_empty_list_field(w_kw) and not is_empty_list_field(l_kw):
        plan.append(f"keywords: [] -> {l_kw} (采用 loser 的 keywords)")
        new_text = re.sub(r"^keywords:.*$", f"keywords: {l_kw}", new_text, count=1, flags=re.MULTILINE)

    if w_si != l_si and not is_empty_list_field(l_si):
        w_items = [] if is_empty_list_field(w_si) else [s.strip() for s in w_si.strip("[]").split(",")]
        l_items = [] if is_empty_list_field(l_si) else [s.strip() for s in l_si.strip("[]").split(",")]
        merged = sorted(set(w_items) | set(l_items))
        if merged != w_items:
            merged_str = "[" + ", ".join(merged) + "]"
            plan.append(f"si_files: {w_si} -> {merged_str} (并集)")
            new_text = re.sub(r"^si_files:.*$", f"si_files: {merged_str}", new_text, count=1, flags=re.MULTILINE)

    files_to_delete = [
        NOTES / f"{args.loser}.md",
        READABLE / f"{args.loser}.md",
        EXTRACTED / f"{args.loser}.txt",
        PAPERS / f"{args.loser}.pdf",
    ]
    files_to_delete += list(PAPERS.glob(f"{args.loser}_SI.*"))

    print(f"=== 合并计划: 保留 {args.winner}, 删除 {args.loser} ===")
    if plan:
        for p in plan:
            print(f"  字段合并: {p}")
    else:
        print("  字段合并: 无(winner 字段已是较优版本)")
    print("  将删除的文件:")
    for f in files_to_delete:
        print(f"    - {f.relative_to(ROOT)}" + ("" if f.exists() else "  [不存在,跳过]"))
    print(f"  将从 library.bib 删除 @article{{{args.loser}, ...}} 条目")
    print(f"  将从 INDEX.md 删除 {args.loser} 那一行")

    if not args.apply:
        print("\n(预览模式, 未写入任何改动。确认无误后加 --apply 执行)")
        return

    if new_text != w_text:
        winner_note.write_text(new_text, encoding="utf-8")

    for f in files_to_delete:
        if f.exists():
            f.unlink()

    bib_text = BIB.read_text(encoding="utf-8")
    bib_text = re.sub(
        rf"@article\{{{re.escape(args.loser)},.*?\n\}}\n\n?",
        "",
        bib_text,
        count=1,
        flags=re.DOTALL,
    )
    BIB.write_text(bib_text, encoding="utf-8")

    index_lines = INDEX.read_text(encoding="utf-8").split("\n")
    index_lines = [ln for ln in index_lines if not ln.startswith(f"| {args.loser} |")]
    INDEX.write_text("\n".join(index_lines), encoding="utf-8")

    print(f"\n完成: {args.loser} 已合并入 {args.winner} 并从库中移除。"
          f"记得跑一次 render_readable_notes.py 确认 winner 的 notes-readable 同步(若字段有改动)。")


if __name__ == "__main__":
    main()
