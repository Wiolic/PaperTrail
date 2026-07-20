#!/usr/bin/env python3
"""
"引文献" —— 给一句(或一整段)要写进论文的文字, 从库里(+可选联网候选)找可引用的证据文献,
并标注每篇是支持(support)/矛盾(contradict)/方向不明(unclear), 而不是简单关键词命中就算数。

背景: 单纯关键词/标签匹配只能告诉你"哪些论文提到了相关技术", 不能告诉你"哪些论文的结论
方向和你要写的这句话一致"。真实踩过的坑: 同一类表征(Ir L3-edge XANES 白线随电位正移)在不同
体系里被不同论文报告成相反的趋势——如果只按关键词把两篇都列进引用列表, 会引出一篇实际反对
你论点的文献。这个脚本把"筛出候选"和"判断方向"分成两步, 方向判断默认用更强的模型(v4-pro)
且要求给出理由, 但最终决定权在人。

两种用法:

1) 单句模式(已知要核实哪句话、大概该用什么关键词搜):
   python scripts/find_citations.py --claim "陈述句原文" \\
     --keyword-regex "XANES|white line|oxidation state|价态" --top-journals \\
     --out topics/_citations_x.md --draft

2) 整段模式(2026-07-19 加, 2026-07-20 改进输出格式, 不用自己先拆句子/想关键词, 一整段丢进来):
   python scripts/find_citations.py --paragraph "一整段英文/中文论文正文" \\
     --out topics/_citations_paragraph.md --draft
   先调 DeepSeek 把整段拆成若干条"需要引用支撑的独立论点"(通常对应原文的一句或一个分句,
   每条自动生成建议检索关键词正则), 再对每条论点各自跑一遍单句模式的候选筛选+
   support/contradict/unclear判断。**核心交付物是按原文顺序逐句对照的清单**：
     原文第1句：<原文>
     - **citekey** — 《标题》(期刊, 年份) [support/contradict/unclear]: 理由
     原文第2句：<原文>
     （无需引用——这是作者自己的论点）
     ...
   哪些分句其实是作者自己的论点/创新点陈述、不需要外部引用, 会在这份逐句清单里直接标注
   "无需引用"并说明原因, 不会被悄悄跳过、也不占位凑数。每条论点的候选检索详情(关键词/完整
   候选列表)放在输出文件末尾单独一节, 不影响开头逐句清单的阅读连贯性。

做什么(单句模式核心逻辑, 整段模式内部逐条复用):
  1. 按 --keyword-regex(必填) 和可选的 --tags / --top-journals 筛出候选笔记。
  2. 抽取每篇候选的 关键图表与数据/方法要点/体系/表征方法, 写入摘要文件。
  3. 若加 --draft: 把陈述句和候选摘要一起交给 DeepSeek(默认 v4-pro), 对每篇候选判断
     support/contradict/unclear 并给理由, 输出必须带论文标题。

不做什么: 不自动帮你把结论写进论文——判断"这篇矛盾但是不是我这句话真正要引用的意思"这种
细致的语义辨析仍需要人工看一眼。库内摘要判断有时会偏保守(只要摘要没出现一模一样的措辞就打
unclear), 人工基于笔记全文内容可以override这个判断, 不要盲目照抄输出。库外(联网找的)候选
本工具不负责——找到候选DOI/标题后需要另外验证, 见 AGENTS.md"找新文章"工作流。
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_topic_digest import load_note, find_near_duplicates  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = ROOT / "notes"
TOP_JOURNALS_FILE = Path(__file__).resolve().parent / "top_journals.txt"

DRAFT_SYSTEM = (
    "你在协助核实一句要写进论文的陈述句能引用哪些文献。给定陈述句和一批候选文献的摘要"
    "(citekey/体系/方法要点/关键图表与数据), 对每篇候选判断:\n"
    "- support: 该文献的数据/结论方向和陈述句一致, 可以直接引用\n"
    "- contradict: 该文献的数据或结论与陈述句方向相反或矛盾(哪怕只是部分矛盾), 具体说明"
    "矛盾在陈述句的哪个分句\n"
    "- unclear: 提到了相关技术/现象但摘要信息不足以判断方向是否一致\n"
    "每条给出一句话理由, 理由必须基于摘要里实际出现的内容, 不许编造摘要没提到的信息。"
    "特别注意: 如果一篇文献的某个观测数据支持陈述句的一部分, 但其整体结论反对陈述句的"
    "另一部分, 要明确拆开说, 不要笼统打一个标签。**输出必须带论文标题**(标题从候选摘要里"
    "原样照抄, 不要翻译或缩写), 用 Markdown 列表输出, 每条格式: `- **citekey** — 《论文标题》 "
    "(期刊, 年份) [support/contradict/unclear]: 理由`。"
)

SEGMENT_SYSTEM = (
    "你在协助分析一段论文正文, 判断这段话里有哪些地方需要引用文献支撑, 哪些不需要"
    "(比如作者自己论文的核心创新点表述、纯逻辑推导、无需引用的过渡句)。只输出一个 JSON 对象:\n"
    '{"claims": [{'
    '"text": "该引用点对应的原文片段(尽量原样摘录, 可以是完整句子或子句)", '
    '"anchor_phrase": "这条引用在句中具体挂在哪个词/短语上(正式论文里引用编号会紧跟在它后面), '
    "整句性论点若没有更细的锚点, 就填该句主干或留成和 text 一致\", "
    '"needs_citation": true/false, '
    '"reason_if_not": "如果 needs_citation 为 false, 一句话说明为什么(如\'这是作者自己的创新点表述\')", '
    '"keyword_regex": "如果 needs_citation 为 true, 给一个用于在文献库里检索相关论文的正则表达式"'
    "(英文关键词为主, 用 | 分隔同义/相关词, 覆盖该引用点涉及的核心概念/技术/材料)}]}\n"
    "拆分粒度: 一段话通常能拆出 2~5 条需要引用的独立论点, 不要把同一个意思的两个分句拆成两条, "
    "也不要遗漏明显不同的事实性论断。\n"
    "**枚举句要逐项拆分(重要)**: 如果一句话用 'including A, B, C...' 或逐项列举多种手段/策略/现象/"
    "材料, 而每一项在正式论文里都会各自挂一个引用编号, 那么每一项都必须拆成一条独立 claim, "
    "anchor_phrase 填该项的短语, keyword_regex 针对该项单独检索——绝不能把整个枚举句只拆成一条"
    "笼统的 claim, 那样会漏掉每一项各自需要的引用。\n"
    "示例: 输入 'Extensive efforts have been devoted to improving Ir-based catalysts through strategies "
    "including alloying, morphological engineering, valence-state modulation, defect and heteroatom doping, "
    "core-shell architecture design, and single-atom catalysis.' 应拆成 6 条 claim, "
    "anchor_phrase 分别为 alloying / morphological engineering / valence-state modulation / "
    "defect and heteroatom doping / core-shell architecture design / single-atom catalysis, "
    "每条 keyword_regex 针对该策略(如 alloying 那条: 'Ir.*alloy|iridium alloy|bimetallic')。"
)


def load_top_journals() -> set:
    lines = TOP_JOURNALS_FILE.read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")}


def search_candidates(keyword_regex: str, tags: list[str], journal_set: set | None) -> tuple[list[dict], list]:
    keyword_re = re.compile(keyword_regex, re.IGNORECASE)
    selected = []
    for f in sorted(NOTES_DIR.glob("*.md")):
        note = load_note(f)
        if journal_set is not None and note["journal"] not in journal_set:
            continue
        haystack = " ".join([note["title"], note["体系"], note["keywords"], note["关键图表与数据"]])
        if tags:
            note_tags = {t.strip().lower() for t in note["tags"].strip("[]").split(",")}
            if not any(t.lower() in note_tags for t in tags):
                continue
        if not keyword_re.search(haystack):
            continue
        selected.append(note)
    selected.sort(key=lambda n: (n["year"], n["citekey"]))
    dupes = find_near_duplicates(selected)
    return selected, dupes


def build_digest_text(claim: str, selected: list[dict], dupes: list) -> str:
    lines = [f"# 候选证据文献 (共 {len(selected)} 篇)", "", f"待核实陈述句: {claim}", ""]
    if dupes:
        lines.append("> 疑似重复收录(核对方向判断前先确认哪个是正确版本): " +
                      "; ".join(f"{a}<->{b}" for a, b, _ in dupes))
        lines.append("")
    for n in selected:
        lines.append(f"## {n['citekey']} ({n['year']}, {n['journal']})")
        lines.append(f"- title: {n['title']}")
        lines.append(f"- 体系: {n['体系']}")
        lines.append(f"- 方法要点: {n['方法要点']}")
        lines.append(f"- 关键图表与数据: {n['关键图表与数据']}")
        lines.append("")
    return "\n".join(lines)


def run_draft(ds_module, model: str, claim: str, digest_text: str) -> str:
    client = ds_module.get_client()
    return ds_module.call(client, model, DRAFT_SYSTEM,
                           f"陈述句: {claim}\n\n候选文献:\n{digest_text}",
                           temperature=0, json_mode=False)


def process_one_claim(claim: str, keyword_regex: str, tags: list[str], journal_set: set | None,
                       draft: bool, draft_model: str, ds_module=None) -> tuple[str, str | None]:
    selected, dupes = search_candidates(keyword_regex, tags, journal_set)
    digest_text = build_digest_text(claim, selected, dupes)
    citations_text = None
    if draft:
        citations_text = run_draft(ds_module, draft_model, claim, digest_text)
    return digest_text, citations_text


def segment_paragraph(ds_module, model: str, paragraph: str) -> list[dict]:
    client = ds_module.get_client()
    result = ds_module.call(client, model, SEGMENT_SYSTEM, paragraph, temperature=0, json_mode=True)
    data = json.loads(result)
    return data.get("claims", [])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--claim", help="单句模式: 要核实引用的陈述句原文")
    group.add_argument("--paragraph", help="整段模式: 一整段正文, 自动拆分成多条论点分别处理")
    ap.add_argument("--keyword-regex", help="单句模式必填: 在 title/体系/keywords/关键图表与数据 中匹配的正则")
    ap.add_argument("--tags", default="", help="逗号分隔标签, 任意命中即选中(可选, 进一步收窄)")
    ap.add_argument("--top-journals", action="store_true", help="只保留 scripts/top_journals.txt 里的期刊")
    ap.add_argument("--out", required=True)
    ap.add_argument("--draft", action="store_true", help="调 DeepSeek 判断每篇候选是否支持该陈述句")
    ap.add_argument("--draft-model", default="deepseek-v4-pro")
    ap.add_argument("--segment-model", default="deepseek-v4-pro", help="整段模式拆句用的模型")
    args = ap.parse_args()

    if args.claim and not args.keyword_regex:
        sys.exit("单句模式(--claim)必须同时给 --keyword-regex")

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    journal_set = load_top_journals() if args.top_journals else None
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.claim:
        digest_text, citations_text = process_one_claim(
            args.claim, args.keyword_regex, tags, journal_set, args.draft, args.draft_model,
            ds_module=(__import__("ds") if args.draft else None),
        )
        out_path.write_text(digest_text, encoding="utf-8")
        print(f"已写入 {out_path}")
        if citations_text:
            citations_path = out_path.with_suffix(out_path.suffix + ".citations.md")
            citations_path.write_text(citations_text, encoding="utf-8")
            print(f"已写入 {citations_path} —— 仍需人工核对每条判断, 不要直接照抄引用列表")
        return

    # --- 整段模式 ---
    import ds

    print(f"正在用 {args.segment_model} 拆分段落里的独立论点...")
    claims = segment_paragraph(ds, args.segment_model, args.paragraph)
    if not claims:
        sys.exit("没拆出任何论点, 检查输入段落是否为空")

    n_need = sum(1 for c in claims if c.get("needs_citation", True))
    n_skip = len(claims) - n_need

    # 核心交付物(2026-07-20 加): 按原文顺序逐句对照"原文这句 -> 可以引用哪些文献",
    # 而不是先分组再分别罗列——这样读者可以直接顺着原文读下来, 每句话后面跟着能不能引、
    # 引哪几篇, 不需要自己再去对照分散在各节里的论点编号。
    summary_lines = [f"# 逐处引文对照 (共 {len(claims)} 处引用点, {n_need} 处需要引用, {n_skip} 处跳过)",
                     "", "> 枚举句已按每一项拆成独立引用点, 各项的引用编号应插在其\"引用锚点\"短语之后。", ""]
    detail_lines = ["", "---", "", "## 附：各论点候选文献检索详情(检索关键词/完整候选列表)", ""]

    for i, c in enumerate(claims, 1):
        text = c["text"]
        anchor = (c.get("anchor_phrase") or "").strip()
        summary_lines.append(f"**原文第{i}处**：{text}")
        # 枚举句会被拆成多条, 各挂在句中不同短语上; 把锚点显式标出来, 人工插引用时
        # 才知道这个引用编号该放在哪个词后面(如 "alloying" 后面), 不是整句句尾。
        if anchor and anchor != text:
            summary_lines.append(f"（引用锚点：**{anchor}** ← 引用编号插在这个词/短语之后）")
        if not c.get("needs_citation", True):
            summary_lines.append(f"（无需引用——{c.get('reason_if_not', '作者自己的论点/推导')}）")
            summary_lines.append("")
            continue
        print(f"[{i}/{len(claims)}] 处理: {text[:50]}...")
        digest_text, citations_text = process_one_claim(
            text, c["keyword_regex"], tags, journal_set, args.draft, args.draft_model, ds_module=ds,
        )
        if citations_text:
            summary_lines.append(citations_text.strip())
        else:
            summary_lines.append("（未加 --draft，只筛出候选未判断方向，见文末详情）")
        summary_lines.append("")

        detail_lines.append(f"### 第{i}处候选检索详情：「{text}」")
        detail_lines.append(f"(检索关键词: `{c['keyword_regex']}`)")
        detail_lines.append("")
        detail_lines.append(digest_text)
        detail_lines.append("")

    out_path.write_text("\n".join(summary_lines + detail_lines), encoding="utf-8")
    print(f"已写入 {out_path}（{n_need} 处需要引用, {n_skip} 处跳过）—— "
          f"仍需人工核对每条判断, 不要直接照抄引用列表")


if __name__ == "__main__":
    main()
