#!/usr/bin/env python3
"""
"扩充"/"查新"工作流的扫描状态记录 —— 2026-07-20 加。

背景: "扩充"(搜某领域近N年top journals全量文献)和"查新"(搜库上次更新以来该领域的新文章)
本质是同一套搜索流程的两种时间范围, 区别只在于"从哪天开始搜"。这个脚本负责记录每个
"领域"(field, 自己起名, 如 "PEM催化剂")上次扫描到哪天、覆盖了哪些期刊/关键词, 让"查新"
可以只搜"上次扫描日期"之后的新文章, 而不是每次都从头全量重扫。

用法:
  python scripts/scan_state.py record --field "PEM催化剂" --date 2026-07-20 \\
      --journals "Science,Nature,Nature Materials,..." --note "覆盖Ir/Ru/非贵金属三类"
  python scripts/scan_state.py show --field "PEM催化剂"
  python scripts/scan_state.py list

不做什么: 不负责真正执行网络搜索(那是 Claude 用 WebSearch/WebFetch 做的事, 搜索策略见
AGENTS.md"扩充/查新工作流"一节), 这个脚本只负责记账, 让下一次"查新"知道从哪天开始搜。
"""

import argparse
import json
from datetime import date
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent / ".scan_state.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_record = sub.add_parser("record", help="记录一次扫描(扩充或查新完成后调用)")
    p_record.add_argument("--field", required=True, help="领域名, 自己起, 如 'PEM催化剂'")
    p_record.add_argument("--date", default=date.today().isoformat(), help="扫描覆盖到的日期, 默认今天")
    p_record.add_argument("--journals", default="", help="逗号分隔, 本次覆盖的期刊列表")
    p_record.add_argument("--engine", default="websearch",
                          help="本次扫描使用的引擎: openalex / semantic_scholar / websearch / both")
    p_record.add_argument("--note", default="", help="备注, 如覆盖了哪些子类/关键词策略")

    p_show = sub.add_parser("show", help="查看某个领域上次扫描到哪天")
    p_show.add_argument("--field", required=True)

    sub.add_parser("list", help="列出所有已记录的领域")

    args = ap.parse_args()
    state = load_state()

    if args.cmd == "record":
        entry = state.setdefault(args.field, {"history": []})
        entry["last_scanned"] = args.date
        entry["history"].append({
            "date": args.date,
            "journals": [j.strip() for j in args.journals.split(",") if j.strip()],
            "engine": args.engine,
            "note": args.note,
        })
        save_state(state)
        print(f"已记录: 领域「{args.field}」扫描到 {args.date}")

    elif args.cmd == "show":
        entry = state.get(args.field)
        if not entry:
            print(f"领域「{args.field}」还没有扫描记录, 这是第一次, 按'扩充'(近5年全量)处理")
            return
        print(f"领域「{args.field}」上次扫描到: {entry['last_scanned']}")
        print(f"历史记录 {len(entry['history'])} 次:")
        for h in entry["history"]:
            print(f"  - {h['date']}: 引擎={h.get('engine','websearch')}, 期刊={h['journals']}, 备注={h['note']}")

    elif args.cmd == "list":
        if not state:
            print("还没有任何领域的扫描记录")
            return
        for field, entry in state.items():
            print(f"「{field}」: 上次扫描到 {entry['last_scanned']} (共 {len(entry['history'])} 次记录)")


if __name__ == "__main__":
    main()
