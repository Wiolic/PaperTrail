#!/usr/bin/env python3
"""
主题综述工作流 —— 第一步:筛选相关笔记 + 抽取结构化摘录, 供人工/LLM 写综述用。

用法:
  python scripts/build_topic_digest.py --tags OER,PEMWE,IrOx --keyword-regex "Ir.*synthes|合成|制备" --out topics/_digest_ir_pemwe.md
  python scripts/build_topic_digest.py --title-regex "iridium|Ir[A-Z]" --tags PEMWE --out digest.md

做什么:
  1. 扫描 notes/*.md, 按 --tags(任意命中即选中, 逗号分隔) 和/或 --title-regex / --keyword-regex
     (在 title/体系/keywords 字段中匹配) 筛出候选笔记。
  2. 对每篇候选笔记抽取 citekey/title/year/journal/tags/体系/方法关键词/方法要点/关键图表与数据,
     按年份排序输出到一份 Markdown 摘要文件, 供人工或喂给 DeepSeek(ds.py chat)起草综述正文。
  3. 用标题相似度(difflib, 阈值 0.90)在筛出的候选集合内部查重, 提示可能的重复收录
     (例如同一篇论文因来源不同被入库两次), 查重结果打印到 stderr, 不写入摘要文件。
  4. 标记出笔记正文关键小节为空/占位符的 citekey(说明该篇之前只填了 frontmatter,
     正文没有实质内容), 摘要文件里会注明, 写综述时不要凭空编造这些笔记的内容。

不做什么(这是人/LLM 判断的活, 不适合脚本化):
  - 不做分类归纳、不写对比表、不生成综述正文本身。
  - 不判断"哪些方法是同一大类"。
  这些交给 Claude/DeepSeek 基于本脚本输出的摘要来完成。

产出摘要文件后, 典型下一步:
  - 我(Claude)直接读摘要文件, 归类、写 topics/<主题>.md, 再按需转 docx(见 anthropic-skills:docx)。
  - 或 python scripts/ds.py chat --system "..." --user "$(cat digest.md)" --out draft.md 起草初稿, 再人工核校。
"""

import argparse
import difflib
import re
import sys
from pathlib import Path

NOTES_DIR = Path(__file__).resolve().parent.parent / "notes"

PLACEHOLDER_MARKERS = ("<!-- 计算:", "")  # 空字符串命中"该小节内容为空"


def parse_frontmatter_field(text: str, field: str) -> str:
    m = re.search(rf"^{re.escape(field)}:[ \t]*(.*)$", text, re.MULTILINE)  # [ \t]* 不用 \s*, 避免吃掉换行符跨行误匹配
    return m.group(1).strip() if m else ""


def extract_section(text: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL | re.MULTILINE)
    if not m:
        return ""
    body = m.group(1).strip()
    if body.startswith("<!--") or not body:
        return ""
    return body


def load_note(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return {
        "citekey": parse_frontmatter_field(text, "citekey"),
        "title": parse_frontmatter_field(text, "title"),
        "year": parse_frontmatter_field(text, "year"),
        "journal": parse_frontmatter_field(text, "journal"),
        "tags": parse_frontmatter_field(text, "tags"),
        "keywords": parse_frontmatter_field(text, "keywords"),
        "体系": parse_frontmatter_field(text, "体系"),
        "方法关键词": parse_frontmatter_field(text, "方法关键词"),
        "方法要点": extract_section(text, "方法要点"),
        "关键图表与数据": extract_section(text, "关键图表与数据"),
    }


def matches(note: dict, tags: list[str], title_re: re.Pattern | None, keyword_re: re.Pattern | None) -> bool:
    if tags:
        note_tags = [t.strip().strip("[]") for t in note["tags"].strip("[]").split(",")]
        note_tags_lower = {t.strip().lower() for t in note_tags}
        if not any(t.lower() in note_tags_lower for t in tags):
            return False
    if title_re and not title_re.search(note["title"]):
        return False
    if keyword_re:
        haystack = " ".join([note["title"], note["体系"], note["keywords"]])
        if not keyword_re.search(haystack):
            return False
    return bool(tags or title_re or keyword_re)


def find_near_duplicates(notes: list[dict], threshold: float = 0.90) -> list[tuple[str, str, float]]:
    dupes = []
    for i in range(len(notes)):
        for j in range(i + 1, len(notes)):
            a, b = notes[i]["title"], notes[j]["title"]
            if not a or not b:
                continue
            ratio = difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
            if ratio >= threshold:
                dupes.append((notes[i]["citekey"], notes[j]["citekey"], ratio))
    return dupes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tags", default="", help="逗号分隔标签, 任意命中即选中, 如 OER,PEMWE,IrOx")
    ap.add_argument("--title-regex", default="", help="标题字段的正则筛选(忽略大小写)")
    ap.add_argument("--keyword-regex", default="", help="在 title/体系/keywords 中匹配的正则(忽略大小写)")
    ap.add_argument("--out", required=True, help="输出摘要 Markdown 文件路径")
    ap.add_argument("--draft", action="store_true",
                     help="2026-07-19 加: 摘要写完后, 再调 DeepSeek(默认 v4-pro)基于摘要起草一版"
                          "分类/对比/趋势总结初稿, 写到 <out同名>.draft.md。这是初稿, 不是成品——"
                          "Claude 仍要通读核对(citekey出处对不对/有没有编造/分类是否合理)、补观点分歧"
                          "和数据缺口小节再定稿成 topics/<主题>.md, 但比从摘要从零手写省不少 token")
    ap.add_argument("--draft-model", default="deepseek-v4-pro")
    args = ap.parse_args()

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    title_re = re.compile(args.title_regex, re.IGNORECASE) if args.title_regex else None
    keyword_re = re.compile(args.keyword_regex, re.IGNORECASE) if args.keyword_regex else None

    if not (tags or title_re or keyword_re):
        sys.exit("至少指定 --tags / --title-regex / --keyword-regex 之一")

    selected = []
    for f in sorted(NOTES_DIR.glob("*.md")):
        note = load_note(f)
        if matches(note, tags, title_re, keyword_re):
            selected.append(note)

    selected.sort(key=lambda n: (n["year"], n["citekey"]))

    dupes = find_near_duplicates(selected)
    if dupes:
        print(f"警告: 筛出的 {len(selected)} 篇里发现 {len(dupes)} 组标题高度相似, 可能是重复收录:", file=sys.stderr)
        for a, b, ratio in dupes:
            print(f"  {a}  <->  {b}   相似度={ratio:.3f}", file=sys.stderr)

    lines = [f"# 主题摘要 (共 {len(selected)} 篇)", ""]
    if dupes:
        lines.append("> 注意: 以下 citekey 对标题高度相似, 疑似重复收录, 写综述前请人工核实, 不要当成两篇不同工作引用:")
        for a, b, ratio in dupes:
            lines.append(f"> - {a} <-> {b} (相似度 {ratio:.3f})")
        lines.append("")

    empty_notes = []
    for n in selected:
        lines.append(f"## {n['citekey']} ({n['year']})")
        lines.append(f"- title: {n['title']}")
        lines.append(f"- journal: {n['journal']}")
        lines.append(f"- tags: {n['tags']}")
        lines.append(f"- 体系: {n['体系']}")
        lines.append(f"- 方法关键词: {n['方法关键词']}")
        if n["方法要点"]:
            lines.append(f"- 方法要点: {n['方法要点']}")
        else:
            lines.append("- 方法要点: (空/占位符, 该笔记正文未填, 写综述时跳过或注明"
                         "'资料不足待精读', 不要编造)")
            empty_notes.append(n["citekey"])
        if n["关键图表与数据"]:
            lines.append(f"- 关键图表与数据: {n['关键图表与数据']}")
        lines.append("")

    if empty_notes:
        lines.insert(1, f"> {len(empty_notes)} 篇笔记正文关键小节为空(仅有 frontmatter): "
                         f"{', '.join(empty_notes)} —— 建议后续精读补充。")
        lines.insert(2, "")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    digest_text = "\n".join(lines)
    out_path.write_text(digest_text, encoding="utf-8")
    print(f"已写入 {out_path} ({len(selected)} 篇, {len(empty_notes)} 篇正文为空, {len(dupes)} 组疑似重复)")

    if args.draft:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import ds  # noqa: E402

        system = (
            "你在协助起草一份文献主题综述初稿(中文)。给定一批论文的结构化摘要(citekey/标题/年份/期刊/"
            "体系/方法要点/关键图表与数据), 请: 1) 按方法/思路给这些论文归出3~6个大类, 每类起个简短名字; "
            "2) 每类写一段思路小结 + 用要点列出代表性论文(每条必须用 [[citekey]] 标注出处, citekey 必须"
            "原样照抄摘要里给的, 不要编造或改写citekey); 3) 写一节'方法-性能关联的整体趋势'总结跨类别的"
            "共性/演变趋势; 4) 用 Markdown 输出, 标题层级用 ##/###。"
            "严禁编造摘要之外的内容——每个结论都要能在给定摘要里找到依据, 摘要信息不够就如实说'资料不足', "
            "不要为了行文流畅编数据或结论。这是初稿, 后面人工会核对, 但初稿本身不能有编造。"
        )
        print(f"正在用 {args.draft_model} 起草初稿...")
        client = ds.get_client()
        draft = ds.call(client, args.draft_model, system, digest_text, temperature=0, json_mode=False)
        draft_path = out_path.with_suffix(out_path.suffix + ".draft.md")
        draft_path.write_text(draft, encoding="utf-8")
        print(f"已写入初稿 {draft_path} —— 仍需人工核对 citekey 出处/补观点分歧和数据缺口小节后再定稿")


if __name__ == "__main__":
    main()
