#!/usr/bin/env python3
"""给配不上主文献的 SI 文件, 用 DeepSeek 识别出的标题去匹配 library.bib 里已入库的文献,
匹配上就正式绑定: 复制改名进 papers/<citekey>_SI.<ext>, 更新该笔记 notes/ 和 notes-readable/
的 si_files 字段, 更新 INDEX.md 对应行的 SI 列。

用法:
    python scripts/match_orphan_si.py --source <PDF源目录>           # 只打印匹配建议, 不落盘
    python scripts/match_orphan_si.py --source <PDF源目录> --apply   # 真正写入

注意: 库里还没入库的论文, 它的 SI 自然配不上, 这是预期行为, 不是 bug——建议在
batch_ingest.py 全部批次跑完后再执行 --apply, 匹配面才最大。
"""
import argparse
import difflib
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_ingest as b

from openai import OpenAI

IDENTIFY_PROMPT = """给你一段疑似"补充材料(Supporting Information)"文件的首页文字。
判断它是否明确提到了自己所属的主论文标题(常见形式如 "Supporting Information for: <标题>"、
"Supplementary Information for <标题>", 或首页顶部直接重复主标题)。只输出一个 JSON 对象, 不要解释文字:
{"is_si": true 或 false, "candidate_title": "识别到的主论文标题, 识别不到就空字符串", "confidence": "high/medium/low"}
识别不到就诚实说识别不到(candidate_title 留空), 不要编造标题。"""

AUTO_APPLY_THRESHOLD = 0.85  # 高于此值才自动绑定, 实测 0.55~0.85 之间可能是标题关键词凑巧重叠的假匹配(如两篇都是"Iridium Oxide...PEMWE"但并非同一篇)
REVIEW_THRESHOLD = 0.55      # 高于此值但够不到自动阈值, 报告给人工确认, 不自动写入


def load_bib_titles() -> dict:
    if not b.BIB.exists():
        return {}
    text = b.BIB.read_text(encoding="utf-8")
    entries = {}
    for m in re.finditer(r"@article\{([^,]+),.*?title\s*=\s*\{([^}]*)\}", text, re.S):
        entries[m.group(1)] = m.group(2)
    return entries


def best_match(candidate_title: str, bib_titles: dict):
    best_key, best_ratio = None, 0.0
    for ck, title in bib_titles.items():
        ratio = difflib.SequenceMatcher(None, candidate_title.lower(), title.lower()).ratio()
        if ratio > best_ratio:
            best_key, best_ratio = ck, ratio
    return best_key, best_ratio


def apply_binding(citekey: str, si_path: Path):
    note_path = b.NOTES / f"{citekey}.md"
    note_text = note_path.read_text(encoding="utf-8")
    m = re.search(r"si_files:\s*\[(.*?)\]", note_text)
    existing = [t.strip() for t in m.group(1).split(",") if t.strip()] if m and m.group(1).strip() else []
    dest_name = f"{citekey}_SI{si_path.suffix.lower()}"
    if dest_name in existing:
        print("    已绑定过, 跳过")
        return

    shutil.copy2(si_path, b.PAPERS / dest_name)
    new_list = existing + [dest_name]
    new_si_str = "[" + ", ".join(new_list) + "]"
    note_text2 = re.sub(r"si_files:\s*\[.*?\]", f"si_files: {new_si_str}", note_text, count=1)
    note_path.write_text(note_text2, encoding="utf-8", newline="\n")
    (b.NOTES_READABLE / f"{citekey}.md").write_text(
        b.make_readable(note_text2), encoding="utf-8", newline="\n")

    idx_text = b.INDEX.read_text(encoding="utf-8")
    label = "Word" if si_path.suffix.lower() in b.SI_EXTS else "PDF"
    pattern = re.compile(rf"(\|\s*{re.escape(citekey)}\s*\|.*\|)\s*([^|]*)\|\s*$", re.M)

    def repl(mm):
        old_si = mm.group(2).strip()
        labels = set(x.strip() for x in old_si.split("+") if x.strip())
        labels.add(label)
        return mm.group(1) + " " + "+".join(sorted(labels)) + " |"

    idx_text2 = pattern.sub(repl, idx_text, count=1)
    b.INDEX.write_text(idx_text2, encoding="utf-8", newline="\n")
    print(f"    已绑定: papers/{dest_name}, 更新了 {citekey} 的 si_files 和 INDEX")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="与 batch_ingest.py 相同的源目录, 用来重新扫出孤儿 SI 列表")
    ap.add_argument("--apply", action="store_true", help="真正写入绑定, 不加则只预览")
    ap.add_argument("--model", default="deepseek-v4-flash")
    args = ap.parse_args()

    import os
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("缺少环境变量 DEEPSEEK_API_KEY")
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    _, weak_si, unmatched = b.group_and_pair(Path(args.source))
    # 弱信号配对(只是同文件夹恰好只剩2个文件, 未经文件名/内容验证)也一起纳入待匹配,
    # 用标题内容匹配去正式确认或推翻这个猜测, 比盲目相信文件夹位置更可靠
    unmatched = list(unmatched) + [s for sis in weak_si.values() for s in sis]
    bib_titles = load_bib_titles()
    if not bib_titles:
        sys.exit("library.bib 里还没有条目, 无法匹配")

    print(f"待匹配 SI 文件: {len(unmatched)} 个(含 {sum(len(v) for v in weak_si.values())} 个弱信号候选), "
          f"库里已有 {len(bib_titles)} 篇可匹配"
          + ("" if args.apply else " (预览模式, 不落盘, 加 --apply 才真正写入)"))

    auto_bound, needs_review, unresolved = 0, 0, 0
    for si_path in unmatched:
        try:
            text = b.extract_pdf_text(si_path, max_pages=1, max_chars=3000)
        except Exception as e:
            print(f"[无法打开文件, 跳过] {si_path.name}: {e}")
            unresolved += 1
            continue
        resp = client.chat.completions.create(
            model=args.model,
            messages=[{"role": "system", "content": IDENTIFY_PROMPT}, {"role": "user", "content": text}],
            temperature=0, response_format={"type": "json_object"}, stream=False,
        )
        result = json.loads(resp.choices[0].message.content)
        cand = (result.get("candidate_title") or "").strip()

        if not cand:
            print(f"[无法识别标题] {si_path}")
            unresolved += 1
            continue

        key, ratio = best_match(cand, bib_titles)
        if key and ratio >= AUTO_APPLY_THRESHOLD:
            print(f"[高置信度匹配 ratio={ratio:.2f}] {si_path.name} -> {key}\n    识别到的标题: {cand[:80]}")
            auto_bound += 1
            if args.apply:
                apply_binding(key, si_path)
        elif key and ratio >= REVIEW_THRESHOLD:
            print(f"[疑似匹配, 需人工确认(不自动绑定)] {si_path.name} -> {key}? ratio={ratio:.2f}\n"
                  f"    识别到的标题: {cand[:80]}\n    库中该 citekey 标题: {bib_titles[key][:80]}")
            needs_review += 1
        else:
            print(f"[暂时配不上] {si_path.name}\n    识别到的标题: {cand[:80]} (库中最佳匹配 ratio={ratio:.2f}, 可能主文献还没入库)")
            unresolved += 1

    print(f"\n汇总: 高置信度自动绑定 {auto_bound} 个, 疑似需人工确认 {needs_review} 个, "
          f"暂时配不上 {unresolved} 个(总共 {len(unmatched)} 个待处理)")


if __name__ == "__main__":
    main()
