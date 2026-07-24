#!/usr/bin/env python3
"""
"识别打开的 Word 文档里需要插入文献的位置, 一键添加建议的引用" —— 2026-07-20 加。

背景: word_insert_citation.py 解决了"我已经知道要插哪篇, 帮我插到光标处"; find_citations.py
解决了"这段话能引哪些文献, 给我判断方向"; 这个脚本把两者串起来, 直接对着一份**已经打开的**
Word 文档全文扫一遍——找出哪些句子看起来需要引用支撑, 对每句去库里找候选文献并判断
support/contradict/unclear, 只对判断为 support 的候选**自动**在文档里对应句子后面插入引用标记
+ 维护 References 小节, 不需要你自己一句句读、一句句复制粘贴去跑 find_citations.py 再手动定位插入。

复用: 段落拆句用 find_citations.py 的 segment_paragraph()/DRAFT_SYSTEM, 候选检索用它的
search_candidates(); 插入标记/维护 References 用 word_insert_citation.py 的
insert_marker_at_range()/citemap 机制。两边逻辑都不重复实现。

⚠️ 默认是**预览模式**(不动文档), 只打印"哪几句会插入哪几篇、哪几句跳过(找不到候选/都是
unclear或contradict/在文档里定位不到原句)"——这是自动扫全文+自动写入的功能, 参考仓库里
其他"涉及批量修改"脚本的默认预览+`--apply`才执行的惯例, 不做完预览就默认落盘, 避免LLM
判断有偏差时一次性往你的真实论文稿子里插了一堆不该插的引用。看着预览结果没问题再加 --apply
正式执行。

用法:
  python scripts/word_auto_cite.py --doc "论文初稿.docx"                 # 预览, 不改文档
  python scripts/word_auto_cite.py --doc "论文初稿.docx" --apply         # 确认插入
  python scripts/word_auto_cite.py --doc "论文初稿.docx" --apply --style nature --top-journals

定位原句这一步靠字符串精确匹配(Word Find 找 segment 阶段摘录的原文片段), 如果 LLM 摘录的
句子和文档原文有细微出入(标点/空格/引号全半角不同), 会定位失败并跳过, 这种情况下自己手动
用 word_insert_citation.py 插那一句。

不做的事: 不会自动保存文档(Ctrl+S 自己来); 每句只自动插入第一条 support 候选, 不是把所有
support 候选都塞进去——多篇支撑同一句话的情况请看预览输出自己用 word_insert_citation.py 补插。
"""

import argparse
import re
import sys
if sys.platform == "win32":
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", write_through=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                      errors="replace", write_through=True)
    except Exception:
        pass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_citations import (  # noqa: E402
    segment_paragraph, search_candidates, build_digest_text, run_draft, load_top_journals,
)
from word_insert_citation import (  # noqa: E402
    parse_bib_entries, find_word_document, citemap_path, load_citemap, save_citemap,
    find_references_heading_start, insert_marker_at_range, STYLES,
)

CITATION_LINE_RE = re.compile(
    r"-\s*\*\*(?P<citekey>[^*]+)\*\*.*?\[(?P<verdict>support|contradict|unclear)\]", re.IGNORECASE)


def parse_verdicts(citations_text: str) -> list[tuple[str, str]]:
    """从 DRAFT_SYSTEM 输出的 Markdown 列表里抽出 [(citekey, verdict), ...]。"""
    return [(m.group("citekey").strip(), m.group("verdict").lower()) for m in CITATION_LINE_RE.finditer(citations_text)]


def locate_sentence(doc, body_end: int, sentence: str):
    """在文档正文范围(0~body_end)里精确查找这句话, 返回其结束位置的 Range(插入点); 找不到返回 None。"""
    snippet = sentence.strip()
    if not snippet:
        return None
    rng = doc.Range(0, body_end)
    if not rng.Find.Execute(FindText=snippet, MatchCase=False, MatchWildcards=False):
        return None
    found = doc.Range(rng.Start, rng.End)
    found.Collapse(0)
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc", help="Word 里同时开着多个文档时, 用文件名(或其中一部分)指定操作哪个")
    ap.add_argument("--tags", default="", help="逗号分隔标签, 进一步收窄候选检索范围(可选)")
    ap.add_argument("--top-journals", action="store_true", help="候选只从 scripts/top_journals.txt 里的期刊选")
    ap.add_argument("--style", default=None, choices=list(STYLES), help="参考文献格式, 不给就沿用文档已有样式")
    ap.add_argument("--segment-model", default="deepseek-v4-pro")
    ap.add_argument("--draft-model", default="deepseek-v4-pro")
    ap.add_argument("--apply", action="store_true", help="真正写入文档(不给这个参数只预览, 不改文档)")
    args = ap.parse_args()

    try:
        import win32com.client  # noqa: F401
    except ImportError:
        sys.exit("缺少依赖: pip install pywin32(仅 Windows 可用)")

    word, doc = find_word_document(args.doc)
    ref_start = find_references_heading_start(doc)
    full_text = doc.Content.Text
    body_text = full_text[: ref_start] if ref_start is not None else full_text
    body_text = body_text.replace("\r", "\n").strip()
    if not body_text:
        sys.exit("文档正文是空的, 没有可分析的内容。")

    bib_entries = parse_bib_entries()
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    journal_set = load_top_journals() if args.top_journals else None

    import ds

    print(f"正在用 {args.segment_model} 拆分全文里的独立论点(文档长度 {len(body_text)} 字符)...")
    claims = segment_paragraph(ds, args.segment_model, body_text)
    to_process = [c for c in claims if c.get("needs_citation", True)]
    print(f"拆出 {len(claims)} 条论点, {len(to_process)} 条需要引用, 逐条检索候选并判断...")

    # 枚举句被 segment_paragraph 拆成多条, 每条挂在句中不同短语(anchor_phrase)上。定位插入
    # 位置时优先用 anchor_phrase(如 "alloying"), 这样引用编号插在该项短语之后而不是整句句尾,
    # 符合正式论文里逐项挂引用的写法; anchor_phrase 缺失时退回用整条 text 定位。
    plan = []  # [(sentence, locate_target, citekey, verdict)]
    for i, c in enumerate(to_process, 1):
        sentence = c["text"]
        anchor = (c.get("anchor_phrase") or "").strip() or sentence
        print(f"[{i}/{len(to_process)}] {sentence[:60]}...")
        selected, dupes = search_candidates(c["keyword_regex"], tags, journal_set)
        if not selected:
            plan.append((sentence, anchor, None, "no_candidates"))
            continue
        digest_text = build_digest_text(sentence, selected, dupes)
        citations_text = run_draft(ds, args.draft_model, sentence, digest_text)
        verdicts = parse_verdicts(citations_text)
        support = [ck for ck, v in verdicts if v == "support" and ck in bib_entries]
        if not support:
            plan.append((sentence, anchor, None, "no_support"))
            continue
        plan.append((sentence, anchor, support[0], "support"))

    n_support = sum(1 for _, _, _, status in plan if status == "support")
    print(f"\n共 {len(plan)} 处处理完毕, {n_support} 处有 support 候选可插入, "
          f"{len(plan) - n_support} 处跳过(无候选或都是 contradict/unclear)。")

    if not args.apply:
        print("\n--- 预览(未修改文档, 加 --apply 正式执行) ---")
        for sentence, anchor, citekey, status in plan:
            tag = {"support": f"→ 建议插入 {citekey}（锚点: {anchor[:40]}）",
                   "no_support": "→ 跳过(无 support 候选)",
                   "no_candidates": "→ 跳过(库里没检索到候选)"}[status]
            print(f"「{sentence[:70]}」 {tag}")
        return

    map_path = citemap_path(doc)
    citemap = load_citemap(map_path)
    if args.style:
        citemap["style"] = args.style
    style = citemap["style"]

    inserted, not_found = [], []
    for sentence, anchor, citekey, status in plan:
        if status != "support":
            continue
        ref_start = find_references_heading_start(doc)  # References 小节可能已被插入, 每次重新定位边界
        body_end = ref_start if ref_start is not None else doc.Content.End
        # 优先在 anchor 短语后插入(枚举句每项各自挂引用); anchor 定位不到再退回整句。
        rng = locate_sentence(doc, body_end, anchor)
        if rng is None and anchor != sentence:
            rng = locate_sentence(doc, body_end, sentence)
        if rng is None:
            not_found.append(sentence)
            continue
        marker = insert_marker_at_range(word, doc, rng, [citekey], citemap, bib_entries, style)
        inserted.append((sentence, citekey, marker))

    save_citemap(map_path, citemap)

    print(f"\n已插入 {len(inserted)} 处引用（样式: {style}）：")
    for sentence, citekey, marker in inserted:
        print(f"  {marker} 「{sentence[:60]}」← {citekey}")
    if not_found:
        print(f"\n{len(not_found)} 句在文档里精确定位失败(可能是摘录和原文有细微出入), 跳过, "
              f"需要的话自己用 word_insert_citation.py 手动插：")
        for sentence in not_found:
            print(f"  「{sentence[:70]}」")

    print("\n记得自己检查一下插入位置和排版是否合适, 然后 Ctrl+S 保存(这个脚本不会自动保存文档)。"
          "全文引用位置改动较大时, 建议之后跑一次 word_insert_citation.py --rebuild 确认编号连续。")


if __name__ == "__main__":
    main()
