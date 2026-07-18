#!/usr/bin/env python3
"""对账 scripts/.ingest_state.json: 有些源文件在磁盘上其实已经处理过(papers/notes/bib都有对应
条目), 但因为早期运行被中途 TaskStop/中断, state.json 没记录到, 导致重跑时被当成"新文件"重新
调用一次 DeepSeek, 结果又因为 DOI 相同被判重复跳过——白花一次 API 调用。

本脚本用纯本地正则从 PDF 文字里抓 DOI(不调用 DeepSeek, 零成本), 和 library.bib 里已有的 DOI
比对, 匹配上就直接把这个源文件在 state.json 里标记为 skipped_duplicate, 下次 batch_ingest.py
就会跳过它, 不再浪费 API 调用。

用法: python scripts/reconcile_state.py --source <目录> [--apply]
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_ingest as b

DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>()]+", re.I)


def extract_doi_locally(pdf_path: Path) -> str | None:
    try:
        text = b.extract_pdf_text(pdf_path, max_pages=3, max_chars=8000)
    except Exception:
        return None
    m = DOI_RE.search(text)
    if not m:
        return None
    doi = m.group(0).rstrip(".,;:")  # 常见标点粘连清理
    return doi.lower()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    source = Path(args.source)
    main_to_si, _, _ = b.group_and_pair(source)
    all_mains = sorted(main_to_si.keys())

    state = b.load_state()
    dois = b.existing_dois()  # 已在 bib 里的 DOI(小写), 不含 N/A 等占位值

    # DOI -> citekey 映射, 供reconcile结果里显示匹配到了谁
    doi_to_citekey = {}
    if b.BIB.exists():
        text = b.BIB.read_text(encoding="utf-8")
        for m in re.finditer(r"@article\{([^,]+),.*?doi\s*=\s*\{([^}]*)\}", text, re.S):
            ck, doi = m.group(1), m.group(2).strip().lower()
            if not b.is_missing_doi(doi):
                doi_to_citekey[doi] = ck

    todo = [p for p in all_mains if str(p) not in state]
    print(f"待检查(state里还没记录的源文件): {len(todo)} 个", flush=True)

    reconciled = 0
    for i, pdf_path in enumerate(todo, 1):
        doi = extract_doi_locally(pdf_path)
        if doi and doi in dois:
            ck = doi_to_citekey.get(doi, "?")
            print(f"[{i}/{len(todo)}] 对账匹配: {pdf_path.name} (doi={doi}) -> 已有 {ck}, 标记跳过", flush=True)
            if args.apply:
                state[str(pdf_path)] = {"status": "skipped_duplicate", "citekey": ck, "reconciled": True}
            reconciled += 1

    if args.apply:
        b.save_state(state)

    print(f"\n共 {len(todo)} 个待检查, 对账匹配到 {reconciled} 个已存在的重复(本地正则比对, 未调用DeepSeek)。"
          + ("已写入 state.json。" if args.apply else "预览模式未写入, 加 --apply 落盘。"))
    print(f"剩余约 {len(todo) - reconciled} 个源文件下次 batch_ingest.py 跑的时候会真正调用 DeepSeek 处理。")


if __name__ == "__main__":
    main()
