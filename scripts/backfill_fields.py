#!/usr/bin/env python3
"""一次性回填: 给已入库(在 batch_ingest.py 加 表征方法/authors_full/extracted-text
缓存 这几个功能之前生成)的笔记补齐:
  1. extracted-text/<citekey>.txt: 重新读一次 papers/<citekey>.pdf 缓存抽取文字(最后一次要读这些PDF)
  2. notes/<citekey>.md frontmatter 里插入 表征方法 和 authors_full 两个字段
  3. library.bib 对应条目的 author 字段换成 authors_full(以 " and " 连接), 不再用 "et al." 截断
  4. 同步重生成 notes-readable/

用法:
    python scripts/backfill_fields.py            # 只打印预览, 不落盘
    python scripts/backfill_fields.py --apply     # 真正写入
    python scripts/backfill_fields.py --apply --limit 5   # 小规模验证
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_ingest as b

from openai import OpenAI

BACKFILL_PROMPT = """给你一篇论文的标题和正文摘录(摘要/引言/方法等), 提取两个字段。只输出一个 JSON 对象:
{
  "authors_full": ["完整作者名单, 按原文出现顺序全部列出, 每人一个数组元素, 格式\\"名 姓\\"; 找不到完整名单就尽量列出能确定的, 不要只写一个人"],
  "表征方法": ["标准化表征技术数组。纯缩写(DEMS/XPS/TEM/XRD/NMR/SEM/AFM/EELS等)直接大写书写; 带'原位/操作/非原位'
     前缀的技术, 前缀和技术名之间用空格(写 'in situ Raman'/'operando XRD'/'ex situ XPS', 不要写 'insitu-Raman'/
     'InsituXRD'粘连或连字符形式); 只列文中明确点名的具体技术, 原文含糊就不编, 纯计算论文或未点名任何具体技术则填 []"]
}
只根据给定文字判断, 不要编造。"""


def already_has_field(note_text: str, field: str) -> bool:
    return re.search(rf"^{field}:", note_text, re.M) is not None


def get_backfill_fields(client, model, title, text) -> dict:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": BACKFILL_PROMPT},
            {"role": "user", "content": f"标题: {title}\n\n正文摘录:\n{text[:12000]}"},
        ],
        temperature=0, response_format={"type": "json_object"}, stream=False,
    )
    return json.loads(resp.choices[0].message.content)


def update_bib_author(citekey: str, authors_full: list):
    if not b.BIB.exists() or not authors_full:
        return
    text = b.BIB.read_text(encoding="utf-8")
    author_field = " and ".join(authors_full)
    pattern = re.compile(
        rf"(@article\{{{re.escape(citekey)},.*?author\s*=\s*\{{)[^}}]*(\}})", re.S
    )
    new_text, n = pattern.subn(lambda m: m.group(1) + author_field + m.group(2), text, count=1)
    if n:
        b.BIB.write_text(new_text, encoding="utf-8", newline="\n")


def insert_fields_in_note(note_text: str, authors_full: list, characterization: list) -> str:
    authors_full_str = "[" + ", ".join(f'"{a}"' for a in authors_full) + "]"
    char_str = "[" + ", ".join(characterization) + "]"

    if not already_has_field(note_text, "authors_full"):
        note_text = re.sub(
            r"^(authors:.*)$", rf"\1\nauthors_full: {authors_full_str}", note_text, count=1, flags=re.M
        )
    if not already_has_field(note_text, "表征方法"):
        note_text = re.sub(
            r"^(方法关键词:.*)$", rf"\1\n表征方法: {char_str}", note_text, count=1, flags=re.M
        )
    return note_text


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 篇(0=全部), 用于小规模验证")
    args = ap.parse_args()

    import os
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("缺少环境变量 DEEPSEEK_API_KEY")
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=30.0, max_retries=1)

    note_files = sorted(b.NOTES.glob("*.md"))
    todo = [
        f for f in note_files
        if not already_has_field(f.read_text(encoding="utf-8"), "authors_full")
        or not already_has_field(f.read_text(encoding="utf-8"), "表征方法")
    ]
    total_needed = len(todo)
    if args.limit:
        todo = todo[: args.limit]

    print(f"notes/ 共 {len(note_files)} 篇, 需要回填 {total_needed} 篇"
          + (f", 本次限定处理 {len(todo)} 篇" if args.limit else "")
          + ("" if args.apply else " (预览模式, 不落盘, 加 --apply 才真正写入)"), flush=True)

    for i, note_path in enumerate(todo, 1):
        citekey = note_path.stem
        print(f"[{i}/{len(todo)}] 处理中: {citekey} ...", flush=True)

        pdf_path = b.PAPERS / f"{citekey}.pdf"
        if not pdf_path.exists():
            print("  找不到对应 PDF, 跳过", flush=True)
            continue

        try:
            text = b.extract_pdf_text(pdf_path)
        except Exception as e:
            print(f"  读 PDF 出错, 跳过: {e}", flush=True)
            continue

        note_text = note_path.read_text(encoding="utf-8")
        title_m = re.search(r"^title:\s*(.*)$", note_text, re.M)
        title = title_m.group(1).strip() if title_m else citekey

        try:
            fields = get_backfill_fields(client, args.model, title, text)
        except Exception as e:
            print(f"  调用 DeepSeek 出错, 跳过: {e}", flush=True)
            continue

        authors_full = fields.get("authors_full") or []
        characterization = fields.get("表征方法") or []
        print(f"  authors_full: {authors_full}\n  表征方法: {characterization}", flush=True)

        if args.apply:
            b.EXTRACTED_TEXT.mkdir(exist_ok=True)
            (b.EXTRACTED_TEXT / f"{citekey}.txt").write_text(text, encoding="utf-8", newline="\n")

            new_note_text = insert_fields_in_note(note_text, authors_full, characterization)
            note_path.write_text(new_note_text, encoding="utf-8", newline="\n")
            (b.NOTES_READABLE / f"{citekey}.md").write_text(
                b.make_readable(new_note_text), encoding="utf-8", newline="\n")

            update_bib_author(citekey, authors_full)
            print("  已回填: extracted-text + notes + notes-readable + bib author", flush=True)

    if args.apply:
        print(f"\n已回填 {len(todo)} 篇。建议接着跑: bash scripts/check_library.sh")
    else:
        print(f"\n共 {len(todo)} 篇待回填。确认无误后加 --apply 重跑。")


if __name__ == "__main__":
    main()
