#!/usr/bin/env python3
"""
DOI 回填——扫描全库 notes/, 对 doi 字段为 N/A 或缺失的论文, 用 Crossref 按标题反查补全。

踩过的坑(batch_ingest.py 里有详细记录):
- Crossref 返回 type=="component" 的是 Supporting Information, 不是论文本身, 要跳过
- 返回标题与查询标题相似度不够高(阈值 0.90)时不能采信, Crossref 模糊搜索返回不相关论文的先例
- 查不到就保留 N/A, 不要瞎猜

用法:
    python scripts/backfill_doi.py                    # 预览模式, 只看哪些能补, 不写盘
    python scripts/backfill_doi.py --apply            # 真正写入 notes/ + library.bib
    python scripts/backfill_doi.py --apply --delay 2  # 每次请求间隔 2 秒(Crossref polite)
"""
import argparse
import difflib
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Windows 控制台 UTF-8
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = ROOT / "notes"
BIB_PATH = ROOT / "library.bib"

MISSING_DOI_VALUES = {"n/a", "na", "unknown", "none", "null", "", "not available", "not found", "not provided"}
CROSSREF_MATCH_THRESHOLD = 0.90


def is_missing_doi(doi: str) -> bool:
    return (doi or "").strip().lower() in MISSING_DOI_VALUES


def lookup_doi_via_crossref(title: str, timeout: float = 10.0) -> str | None:
    """按标题查 Crossref, 返回 DOI 或 None。跳过 component(SI) 记录, 检查标题相似度。"""
    title = (title or "").strip()
    if not title:
        return None
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode({
        "query.bibliographic": title,
        "rows": 5,
    })
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "PaperTrail/1.0 (mailto:example@example.com)"
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("message", {}).get("items", [])
        norm_query = re.sub(r"\s+", " ", title).strip().lower()
        for item in items:
            if item.get("type") == "component":
                continue
            returned_title = (item.get("title") or [""])[0]
            ratio = difflib.SequenceMatcher(
                None, norm_query, re.sub(r"\s+", " ", returned_title).strip().lower()
            ).ratio()
            if ratio >= CROSSREF_MATCH_THRESHOLD:
                return item.get("DOI")
        return None
    except Exception:
        return None


def find_missing_doi_notes() -> list[dict]:
    """扫描 notes/, 返回 [{citekey, title, doi, path}, ...]。"""
    results = []
    if not NOTES_DIR.exists():
        return results
    for f in sorted(NOTES_DIR.glob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        if not m:
            continue
        fm = m.group(1)
        doi_m = re.search(r"^doi:\s*(.+)$", fm, re.MULTILINE)
        doi = doi_m.group(1).strip() if doi_m else ""
        if not is_missing_doi(doi):
            continue
        title_m = re.search(r"^title:\s*(.+)$", fm, re.MULTILINE)
        title = title_m.group(1).strip() if title_m else ""
        results.append({
            "citekey": f.stem,
            "title": title,
            "doi": doi,
            "path": f,
        })
    return results


def update_note_doi(note_path: Path, new_doi: str):
    """替换笔记 frontmatter 中的 doi 行。"""
    text = note_path.read_text(encoding="utf-8")
    new_text = re.sub(r"^doi:\s*.+$", f"doi: {new_doi}", text, count=1, flags=re.MULTILINE)
    note_path.write_text(new_text, encoding="utf-8", newline="\n")


def update_bib_doi(citekey: str, new_doi: str):
    """更新 library.bib 中对应条目的 doi 字段。"""
    if not BIB_PATH.exists():
        return
    text = BIB_PATH.read_text(encoding="utf-8")
    # 找 @article{citekey, ... } 块中的 doi 行
    pattern = rf"(@article\{{{citekey},.*?doi\s*=\s*\{{)[^}}]*(\}})"
    new_text = re.sub(pattern, rf"\g<1>{new_doi}\2", text, flags=re.DOTALL)
    if new_text != text:
        BIB_PATH.write_text(new_text, encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description="DOI 回填: 为 N/A 的论文查 Crossref 补全 DOI")
    parser.add_argument("--apply", action="store_true", help="真正写入文件(默认预览)")
    parser.add_argument("--delay", type=float, default=1.0, help="Crossref 请求间隔秒数 (默认: 1)")
    args = parser.parse_args()

    missing = find_missing_doi_notes()
    if not missing:
        print("全库 DOI 完整, 无需回填。")
        return

    print(f"发现 {len(missing)} 篇 DOI 缺失的论文, 开始逐篇查 Crossref...\n")

    found, not_found, errors = [], [], []
    for i, note in enumerate(missing):
        print(f"[{i+1}/{len(missing)}] {note['citekey']}")
        print(f"  标题: {note['title'][:80]}")
        try:
            doi = lookup_doi_via_crossref(note["title"])
            if doi:
                print(f"  ✓ 找到 DOI: {doi}")
                found.append((note, doi))
                if args.apply:
                    update_note_doi(note["path"], doi)
                    update_bib_doi(note["citekey"], doi)
                    print(f"  → 已写入 notes/ + library.bib")
            else:
                print(f"  ✗ Crossref 未找到匹配")
                not_found.append(note)
        except Exception as e:
            print(f"  ✗ 查询出错: {e}")
            errors.append((note, str(e)))
        if i < len(missing) - 1:
            time.sleep(args.delay)
        print()

    print(f"=== 汇总 ===")
    print(f"DOI 缺失: {len(missing)}")
    print(f"查到了:   {len(found)}")
    print(f"没查到:   {len(not_found)}")
    if errors:
        print(f"出错:     {len(errors)}")
    if not args.apply and found:
        print(f"\n预览模式, 以上 {len(found)} 条未写入。加 --apply 真正写入。")


if __name__ == "__main__":
    main()
