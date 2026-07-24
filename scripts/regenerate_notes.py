#!/usr/bin/env python3
"""
用全文重新生成笔记正文/表征方法/关键词 —— 修复"只读了前几页导致质疑与局限/参考文献长期是
占位话"这个历史问题的批处理脚本。

背景(2026-07-19): 在此之前 `ds.py pdf-meta` 默认只读 PDF 前 6000 字符(约1-2页), 导致
DeepSeek 写"质疑与局限""值得追的参考文献"这类依赖论文讨论章节/参考文献列表(在文档靠后位置)
的字段时, 系统性地只能写"原文摘录部分未见..."这种占位话——不是模型偷懒, 是它压根没看到那部分
内容。规则已改(`extract_pdf_text`/`ds.py --max-chars` 默认改成全文, 见 AGENTS.md), 全库
`extracted-text/` 也已用全文重新生成过。本脚本负责下一步: 用这份全文缓存重新调用 DeepSeek,
重写受影响的笔记字段。

只由 DeepSeek 读全文做重写, Claude(我)不读论文全文进自己的上下文——脚本只在终端打印
"[citekey] 完成"这种一行状态, 复核判断仍由 Claude 做, 但复核的对象是重写后的笔记, 不是原文。

覆盖哪些字段: keywords / 类型 / 方法关键词 / 表征方法 / 体系 / 正文七节(三句话总结...值得追的
参考文献)。**不覆盖** citekey/title/authors/year/journal/doi/tags/status/rating/related/
si_files/added ——这些要么是已核实过的元数据, 要么是人工判断字段, 不应被批量重写覆盖。

用法:
  python scripts/regenerate_notes.py --citekey 2025-JACS-Xxx          # 单篇, 强制重跑
  python scripts/regenerate_notes.py --limit 20                       # 批量, 断点续跑
  python scripts/regenerate_notes.py --until-done                     # 一直跑到全部处理完
  python scripts/regenerate_notes.py --limit 20 --include-read        # 默认跳过 status:read
                                                                        # (人工精读过的, 不轻易覆盖),
                                                                        # 加这个才连它们一起重写

断点续跑状态记在 scripts/.regenerate_state.json, 重跑自动跳过已处理的(除非 --citekey 单篇强制)。
跑完建议执行 python scripts/render_readable_notes.py 全量同步 notes-readable/。
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "notes"
EXTRACTED = ROOT / "extracted-text"
STATE_FILE = Path(__file__).resolve().parent / ".regenerate_state.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_ingest as b  # noqa: E402
import ds  # noqa: E402


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def read_field(text: str, field: str) -> str:
    m = re.search(rf"^{re.escape(field)}:[ \t]*(.*)$", text, re.MULTILINE)  # [ \t]* 不用 \s*, 避免吃掉换行符跨行误匹配
    return m.group(1).strip() if m else ""


def format_list_field(items) -> str:
    if not items:
        return "[]"
    return "[" + ", ".join(str(x) for x in items) + "]"


def rebuild_note(citekey: str, old_text: str, meta: dict) -> str:
    """保留 citekey/title/authors/authors_full/year/journal/doi/tags/status/rating/related/
    si_files/added, 用 meta 里的新内容替换 keywords/类型/方法关键词/表征方法/体系 和正文七节。"""
    def keep(field):
        return read_field(old_text, field)

    frontmatter = "\n".join([
        f"citekey: {citekey}",
        f"title: {keep('title')}",
        f"authors: {keep('authors')}",
        f"authors_full: {keep('authors_full')}",
        f"year: {keep('year')}",
        f"journal: {keep('journal')}",
        f"doi: {keep('doi')}",
        f"tags: {keep('tags')}",
        f"keywords: {format_list_field(meta.get('keywords'))}",
        f"类型: {meta.get('类型', keep('类型'))}",
        f"方法关键词: {meta.get('方法关键词', keep('方法关键词'))}",
        f"表征方法: {format_list_field(meta.get('表征方法'))}",
        f"体系: {meta.get('体系', keep('体系'))}",
        f"status: {keep('status')}",
        f"rating: {keep('rating')}",
        f"related: {keep('related')}",
        f"si_files: {keep('si_files')}",
        f"added: {keep('added')}",
    ])

    body = "\n\n".join([
        "## 三句话总结\n" + meta.get("summary_3lines", ""),
        "## 研究问题与核心结论\n" + meta.get("problem_conclusion", ""),
        "## 方法要点\n" + meta.get("method_points", ""),
        "## 关键图表与数据\n" + meta.get("key_results", ""),
        "## 与我课题的关联\n" + meta.get("relevance", ""),
        "## 质疑与局限\n" + meta.get("caveats", ""),
        "## 值得追的参考文献\n" + meta.get("further_reading", ""),
    ])

    return f"---\n{frontmatter}\n---\n\n{body}\n"


def process_one(client, citekey: str, model: str) -> bool:
    note_path = NOTES / f"{citekey}.md"
    text_path = EXTRACTED / f"{citekey}.txt"
    if not note_path.exists():
        print(f"[跳过] {citekey}: 找不到笔记")
        return False
    if not text_path.exists():
        print(f"[跳过] {citekey}: 找不到 extracted-text 缓存")
        return False

    old_text = note_path.read_text(encoding="utf-8")
    full_text = text_path.read_text(encoding="utf-8")

    result = ds.call(client, model, b.build_extract_system_prompt(), full_text, temperature=0, json_mode=True)
    meta = json.loads(result)

    new_text = rebuild_note(citekey, old_text, meta)
    note_path.write_text(new_text, encoding="utf-8")
    print(f"[完成] {citekey}")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--citekey", help="只处理单篇, 强制重跑(忽略 state)")
    ap.add_argument("--citekeys-file", help="文本文件, 每行一个 citekey, 只处理这些(仍走断点续跑 state,"
                                             "方便先处理某个主题子集, 比如只重写 PEMWE 相关笔记)")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--until-done", action="store_true")
    ap.add_argument("--include-read", action="store_true", help="默认跳过 status:read(人工精读过的), 加此项连它们一起重写")
    ap.add_argument("--model", default="deepseek-v4-pro",
                     help="笔记正文是给人读的精读内容, 默认用更强的 v4-pro(对应原 reasoner), "
                          "不是纯结构化抽取那种可以用便宜的 v4-flash 打发的活")
    args = ap.parse_args()

    client = ds.get_client()

    if args.citekey:
        process_one(client, args.citekey, args.model)
        return

    state = load_state()

    allowed = None
    if args.citekeys_file:
        allowed = {ln.strip() for ln in Path(args.citekeys_file).read_text(encoding="utf-8").splitlines() if ln.strip()}

    while True:
        candidates = []
        for f in sorted(NOTES.glob("*.md")):
            ck = f.stem
            if allowed is not None and ck not in allowed:
                continue
            if ck in state:
                continue
            if not args.include_read:
                status = read_field(f.read_text(encoding="utf-8"), "status")
                if status == "read":
                    continue
            candidates.append(ck)
        todo = candidates[: args.limit]
        if not todo:
            print(f"没有待处理的笔记(已处理 {len(state)} 篇)。")
            return

        print(f"本批处理 {len(todo)} 篇(已处理 {len(state)} 篇)。")
        for ck in todo:
            try:
                ok = process_one(client, ck, args.model)
                state[ck] = {"status": "done" if ok else "skipped"}
            except Exception as e:
                print(f"[出错] {ck}: {e}")
                state[ck] = {"status": "error", "error": str(e)}
            save_state(state)

        if not args.until_done:
            return


if __name__ == "__main__":
    main()
