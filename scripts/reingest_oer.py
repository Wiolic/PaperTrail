#!/usr/bin/env python3
"""OER 文献重入库: 用更新后的算法(方法关键词实验优先 + keywords 同义词归一化)重新处理已有 OER 论文。

流程:
  1. 扫描 notes/ 找到所有 tags 含 OER 的论文
  2. 从 extracted-text/ 读取已有文本(不重读 PDF)
  3. 调用 DeepSeek API 重新生成 metadata
  4. 保留不变字段(citekey/title/authors/DOI/year/journal/si_files/added)
  5. 更新可变字段(方法关键词/keywords/表征方法/类型/体系/7节正文)
  6. 写回 notes/ + notes-readable/

用法:
  python scripts/reingest_oer.py                    # 处理全部 OER 论文
  python scripts/reingest_oer.py --limit 5          # 只处理前 5 篇(测试)
  python scripts/reingest_oer.py --dry-run          # 只展示, 不写入
  python scripts/reingest_oer.py --resume           # 跳过已重处理的(看 .reingest_state.json)

依赖: openai (pip install openai)
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: 需要 openai. 运行: pip install openai", file=sys.stderr)
    sys.exit(1)

# 路径
ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "notes"
NOTES_READABLE = ROOT / "notes-readable"
EXTRACTED_TEXT = ROOT / "extracted-text"
STATE_FILE = ROOT / ".reingest_state.json"

# 从 batch_ingest 导入核心函数
sys.path.insert(0, str(ROOT / "scripts"))
from batch_ingest import (
    build_extract_system_prompt,
    call_deepseek,
    make_readable,
)

# ---- 状态管理 ----

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ---- 找到 OER 论文 ----

def find_oer_papers() -> list[str]:
    """返回所有 tags 含 OER 的 citekey 列表。"""
    oer = []
    for f in sorted(NOTES.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        fm_end = text.find("---", 3)
        if fm_end < 0:
            continue
        fm = text[:fm_end]
        tags_match = re.search(r"^tags:\s*\[([^\]]*)\]", fm, re.MULTILINE)
        if tags_match:
            tags = [t.strip().strip("'\"") for t in tags_match.group(1).split(",")]
            if "OER" in tags:
                oer.append(f.stem)
    return oer

# ---- 读取现有笔记的不可变字段 ----

def parse_existing_note(citekey: str) -> dict | None:
    """读取现有笔记, 提取需保留的字段。"""
    fp = NOTES / f"{citekey}.md"
    if not fp.exists():
        return None
    text = fp.read_text(encoding="utf-8")
    fm_end = text.find("---", 3)
    if fm_end < 0:
        return None
    fm = text[3:fm_end]

    preserved = {"citekey": citekey}
    for line in fm.split("\n"):
        line = line.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key in ("title", "authors", "year", "journal", "doi", "added",
                    "si_files", "rating", "related", "status"):
            preserved[key] = val
        elif key == "authors_full":
            preserved["authors_full_raw"] = val  # 原始 YAML 行, 后面直接嵌入

    return preserved

# ---- 重建笔记 ----

def rebuild_note(preserved: dict, meta: dict) -> str:
    """用保留字段 + 新 meta 重建笔记。"""
    citekey = preserved["citekey"]

    # 新字段
    tags = meta.get("tags") or []
    tags_str = "[" + ", ".join(tags) + "]"
    keywords = meta.get("keywords") or []
    keywords_str = "[" + ", ".join(keywords) + "]"
    characterization = meta.get("表征方法") or []
    char_str = "[" + ", ".join(characterization) + "]"

    # 保留 authors_full 原始行
    authors_full_raw = preserved.get("authors_full_raw", "[]")

    # si_files 保留原值
    si_raw = preserved.get("si_files", "[]")

    # status: 如果原来是 read 则保留 read, 否则设 skimmed
    status = preserved.get("status", "skimmed").strip("'\"")
    if status not in ("read", "skimmed", "unread"):
        status = "skimmed"

    # rating/related 保留原值
    rating = preserved.get("rating", "")
    related = preserved.get("related", "[]")

    frontmatter = f"""---
citekey: {citekey}
title: {preserved.get('title', meta.get('title', ''))}
authors: {preserved.get('authors', meta.get('authors_display', ''))}
authors_full: {authors_full_raw}
year: {preserved.get('year', meta.get('year', ''))}
journal: {preserved.get('journal', meta.get('journal', ''))}
doi: {preserved.get('doi', meta.get('doi', 'N/A'))}
tags: {tags_str}
keywords: {keywords_str}
类型: {meta.get('类型', '')}
方法关键词: {meta.get('方法关键词', '')}
表征方法: {char_str}
体系: {meta.get('体系', '')}
status: {status}
rating: {rating}
related: {related}
si_files: {si_raw}
added: {preserved.get('added', date.today().isoformat())}
---"""

    body = f"""

## 三句话总结
{meta.get('summary_3lines', '')}

## 研究问题与核心结论
{meta.get('problem_conclusion', '')}

## 方法要点
{meta.get('method_points', '')}

## 关键图表与数据
{meta.get('key_results', '')}

## 与我课题的关联
{meta.get('relevance', '')}

## 质疑与局限
{meta.get('caveats', '')}

## 值得追的参考文献
{meta.get('follow_up_refs', '')}
"""
    return frontmatter + body


# ---- 处理单篇 ----

def reingest_one(citekey: str, client: OpenAI, model: str, reasoning_effort: str,
                  dry_run: bool = False) -> tuple[str, str, dict | None]:
    """重入库一篇。返回 (citekey, status, meta_or_error)。"""
    # 读 extracted text
    et_file = EXTRACTED_TEXT / f"{citekey}.txt"
    if not et_file.exists():
        return citekey, "skip_no_text", None

    text = et_file.read_text(encoding="utf-8")
    if len(text.strip()) < 50:
        return citekey, "skip_empty_text", None

    # 读现有笔记保留字段
    preserved = parse_existing_note(citekey)
    if not preserved:
        return citekey, "skip_no_note", None

    # 调 DeepSeek
    try:
        meta = call_deepseek(client, model, text, reasoning_effort)
    except Exception as e:
        return citekey, "error", {"__error__": str(e)}

    if dry_run:
        print(f"\n[dry-run] {citekey}")
        print(f"  方法关键词: {meta.get('方法关键词', '')[:80]}")
        print(f"  keywords: {meta.get('keywords', [])[:5]}...")
        print(f"  类型: {meta.get('类型', '')}")
        return citekey, "dry_run", meta

    # 重建笔记
    new_content = rebuild_note(preserved, meta)

    # 写入
    (NOTES / f"{citekey}.md").write_text(new_content, encoding="utf-8", newline="\n")
    (NOTES_READABLE / f"{citekey}.md").write_text(
        make_readable(new_content), encoding="utf-8", newline="\n")

    return citekey, "done", meta


def main():
    parser = argparse.ArgumentParser(description="OER 文献重入库")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 篇 (0=全部)")
    parser.add_argument("--dry-run", action="store_true", help="只展示不写入")
    parser.add_argument("--resume", action="store_true", help="跳过已重处理的")
    parser.add_argument("--model", default="deepseek-chat", help="DeepSeek 模型名")
    parser.add_argument("--reasoning-effort", default="low", help="reasoning_effort")
    parser.add_argument("--workers", type=int, default=3, help="并发数")
    args = parser.parse_args()

    # 找 OER 论文
    all_oer = find_oer_papers()
    print(f"找到 {len(all_oer)} 篇 OER 论文")

    # resume: 跳过已处理的
    state = load_state() if args.resume else {}
    if args.resume:
        done_keys = {k for k, v in state.items() if v.get("status") == "done"}
        all_oer = [k for k in all_oer if k not in done_keys]
        print(f"resume 模式: 跳过 {len(done_keys)} 篇已处理, 剩余 {len(all_oer)} 篇")

    if args.limit > 0:
        all_oer = all_oer[:args.limit]
        print(f"limit={args.limit}, 处理 {len(all_oer)} 篇")

    if not all_oer:
        print("没有需要处理的论文")
        return

    # DeepSeek client
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("ERROR: 需要设置 DEEPSEEK_API_KEY 环境变量", file=sys.stderr)
        sys.exit(1)
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    # 并发处理
    _print_lock = threading.Lock()
    done_count = 0
    error_count = 0
    skip_count = 0
    total = len(all_oer)

    def _process(idx: int, ck: str) -> tuple[int, str, str, dict | None]:
        return idx, *reingest_one(ck, client, args.model, args.reasoning_effort, args.dry_run)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_process, i, ck): ck for i, ck in enumerate(all_oer)}
        for future in as_completed(futures):
            idx, ck, status, meta = future.result()
            with _print_lock:
                done_count_progress = sum(1 for f in futures if f.done())
                if status == "done":
                    done_count += 1
                    mk = (meta or {}).get("方法关键词", "")[:60]
                    print(f"  [{done_count_progress}/{total}] ✅ {ck}  方法关键词: {mk}")
                    state[ck] = {"status": "done"}
                elif status.startswith("skip"):
                    skip_count += 1
                    print(f"  [{done_count_progress}/{total}] ⏭ {ck} ({status})")
                elif status == "dry_run":
                    done_count += 1
                elif status == "error":
                    error_count += 1
                    err = (meta or {}).get("__error__", "unknown")
                    print(f"  [{done_count_progress}/{total}] ❌ {ck}: {err}")
                    state[ck] = {"status": "error", "error": err}

                # 每 10 篇保存状态
                if done_count_progress % 10 == 0 and not args.dry_run:
                    save_state(state)

    # 最终保存
    if not args.dry_run:
        save_state(state)

    print(f"\n{'='*60}")
    print(f"重入库完成: 成功 {done_count}, 跳过 {skip_count}, 失败 {error_count}, 共 {total}")
    if error_count > 0:
        print("失败的论文可重新运行(--resume)重试")


if __name__ == "__main__":
    main()
