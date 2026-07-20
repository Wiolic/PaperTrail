#!/usr/bin/env python3
"""
把指定 citekey 列表导出成 EndNote 可以一次性批量导入的 .ris(默认)/.bib 文件 —— 2026-07-19 加,
目的是消掉"从 Claude 拿到推荐引用 -> 手动在 EndNote 里逐篇搜索/新建条目"这一步。

背景: 之前的流程是 Claude 给出 citekey 推荐 -> 用户在 EndNote 里手动搜索每篇论文(搜不到就手动建条目)
-> 再在 Word 里用 Cite While You Write 插入。手动搜索这一步纯粹是体力活, 而且容易漏(标题打错字、
EndNote库里那篇论文用的标题措辞和实际不完全一致等)。现在改成: Claude 推荐完 citekey 后直接调这个
脚本导出一个只含这几篇的 .ris 文件, 用户在 EndNote 里 File > Import 一次性导入(EndNote 自带按
DOI/标题查重, 已有的不会重复导入), 之后在 Word 里直接能搜到、直接插入, 不用再手动一篇篇找。

RIS 是默认格式(EndNote 内置 "Reference Manager (RIS)" 导入过滤器识别度高、不需要手动选BibTeX过滤器);
需要 .bib 也支持, 但优先推荐 RIS。

用法:
  python scripts/export_for_endnote.py --citekeys 2024-Science-IrVI-Oxide-MnO2-OER,2025-AngewChemIntEd-Enhancing-Acidic-OER-Ir-Oxidation-State --out exports/citations.ris
  python scripts/export_for_endnote.py --citekeys-file scripts/some_list.txt --out exports/citations.ris
  python scripts/export_for_endnote.py --citekeys <...> --format bib --out exports/citations.bib

不做的事: 不附带 PDF(RIS/BibTeX 都不适合可靠地跨软件传递本地文件路径)。papers/<citekey>.pdf
已经在本地, 需要的话导入 EndNote 后手动把 PDF 拖进对应条目即可, 这个动作一次几秒钟, 不值得为此
折腾脆弱的 EndNote XML 内部字段。
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIB = ROOT / "library.bib"


def parse_bib_entries() -> dict:
    """解析 library.bib, 返回 {citekey: {title, author, journal, year, doi}}。"""
    text = BIB.read_text(encoding="utf-8")
    entries = {}
    for m in re.finditer(r"@article\{([^,]+),(.*?)\n\}", text, re.DOTALL):
        citekey = m.group(1).strip()
        body = m.group(2)
        fields = {}
        for fm in re.finditer(r"(\w+)\s*=\s*\{([^}]*)\}", body):
            fields[fm.group(1).strip().lower()] = fm.group(2).strip()
        entries[citekey] = fields
    return entries


def to_ris(citekey: str, e: dict) -> str:
    authors = [a.strip() for a in e.get("author", "").split(" and ") if a.strip()]
    lines = ["TY  - JOUR"]
    for a in authors:
        # RIS 作者格式 "姓, 名"; bib 里是 "名 姓", 简单转换(最后一个词当姓, 其余当名)
        parts = a.split()
        if len(parts) >= 2:
            lines.append(f"AU  - {parts[-1]}, {' '.join(parts[:-1])}")
        else:
            lines.append(f"AU  - {a}")
    lines.append(f"TI  - {e.get('title', '')}")
    lines.append(f"JO  - {e.get('journal', '')}")
    lines.append(f"PY  - {e.get('year', '')}")
    doi = e.get("doi", "")
    if doi and doi.upper() not in {"N/A", "NA", "UNKNOWN"}:
        lines.append(f"DO  - {doi}")
        lines.append(f"UR  - https://doi.org/{doi}")
    lines.append(f"N1  - citekey: {citekey}")  # 备注里留个 citekey, 方便回查这份笔记库
    lines.append("ER  - ")
    return "\n".join(lines)


def to_bib(citekey: str, e: dict) -> str:
    return (
        f"@article{{{citekey},\n"
        f"  title   = {{{e.get('title', '')}}},\n"
        f"  author  = {{{e.get('author', '')}}},\n"
        f"  journal = {{{e.get('journal', '')}}},\n"
        f"  year    = {{{e.get('year', '')}}},\n"
        f"  doi     = {{{e.get('doi', '')}}}\n"
        f"}}"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--citekeys", default="", help="逗号分隔的 citekey 列表")
    ap.add_argument("--citekeys-file", default="", help="文本文件, 每行一个 citekey")
    ap.add_argument("--format", choices=["ris", "bib"], default="ris")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    citekeys = []
    if args.citekeys:
        citekeys += [c.strip() for c in args.citekeys.split(",") if c.strip()]
    if args.citekeys_file:
        citekeys += [ln.strip() for ln in Path(args.citekeys_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not citekeys:
        sys.exit("至少给 --citekeys 或 --citekeys-file 之一")

    entries = parse_bib_entries()
    missing = [c for c in citekeys if c not in entries]
    if missing:
        print(f"[警告] 以下 citekey 在 library.bib 里找不到, 已跳过: {', '.join(missing)}", file=sys.stderr)

    found = [c for c in citekeys if c in entries]
    if not found:
        sys.exit("一个有效 citekey 都没有, 没有可导出的内容")

    if args.format == "ris":
        content = "\n\n".join(to_ris(c, entries[c]) for c in found)
    else:
        content = "\n\n".join(to_bib(c, entries[c]) for c in found)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    print(f"已写入 {out_path}({args.format.upper()}格式, {len(found)} 条)。"
          f"在 EndNote 里 File > Import > File, Import Option 选"
          f"{'Reference Manager (RIS)' if args.format == 'ris' else 'BibTeX'}, "
          f"Duplicates 选 Discard Duplicates 即可一次性批量导入且自动去重。")


if __name__ == "__main__":
    main()
