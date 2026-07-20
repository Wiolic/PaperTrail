#!/usr/bin/env python3
"""
全库标题相似度查重(不依赖 tags/关键词筛选, 扫描 notes/ 全部笔记), 供 check_library.sh 体检时自动调用。

背景: library.bib 的 DOI 查重(check_library.sh 里已有)只能抓住"两条都填了 DOI 且 DOI 相同"的重复;
但实际踩过的坑是 DOI 缺失(N/A)或 DOI 提取本身出错时, 同一篇论文换个 citekey 措辞就能骗过 DOI 查重
混进库两次(见 AGENTS.md"批量入库策略"一节的历史教训, 以及 2026-07-18 一次体检性任务里连续发现3组)。
这类重复此前只在做主题综述、按标签抽笔记时"恰好"被 build_topic_digest.py 的查重顺手带出来,
不做主题综述就发现不了——本脚本把同样的查重逻辑独立出来, 覆盖全库, 接入常规体检。

用法:
  python scripts/find_duplicate_titles.py                 # 阈值默认 0.90
  python scripts/find_duplicate_titles.py --threshold 0.85

退出码: 发现重复时返回 1(供 check_library.sh 判断 problems), 无重复返回 0。
"""

import argparse
import difflib
import re
import sys
from pathlib import Path

NOTES_DIR = Path(__file__).resolve().parent.parent / "notes"


def read_title(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^title:[ \t]*(.*)$", text, re.MULTILINE)  # [ \t]* 不用 \s*, 避免吃掉换行符跨行误匹配
    return m.group(1).strip() if m else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--threshold", type=float, default=0.90)
    args = ap.parse_args()

    entries = []
    for f in sorted(NOTES_DIR.glob("*.md")):
        title = read_title(f)
        if title:
            entries.append((f.stem, title))

    dupes = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            ck_a, title_a = entries[i]
            ck_b, title_b = entries[j]
            ratio = difflib.SequenceMatcher(None, title_a.lower(), title_b.lower()).ratio()
            if ratio >= args.threshold:
                dupes.append((ck_a, ck_b, ratio))

    if not dupes:
        print(f"OK: 全库 {len(entries)} 篇笔记标题查重(阈值{args.threshold})未发现重复")
        return 0

    print(f"[!] 全库 {len(entries)} 篇笔记标题查重发现 {len(dupes)} 组疑似重复(阈值{args.threshold}):")
    for a, b, ratio in dupes:
        print(f"    - {a}  <->  {b}   相似度={ratio:.3f}")
    print("    确认是真重复后用 scripts/resolve_duplicate.py --winner <保留> --loser <删除> 处理"
          "(先不加 --apply 预览)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
