#!/usr/bin/env python3
"""
"扫顶刊新文章"工作流的第二步：把候选论文列表(DOI 或原始网页文字片段)整理成一份
待人工下载的 Excel 清单，并标注哪些已经在库里(避免重复劳动)。

背景: 联网检索本身(WebSearch/WebFetch)只有 Claude/Agent 能做，这个脚本接手之后的
体力活——把候选信息核实成干净的结构化记录、查重、导出 Excel。

三种候选输入方式：
  1. 有 DOI 的候选：脚本直接查 Crossref 拿权威的 title/authors/journal/year，
     不经过 LLM(比 LLM 复述更准，没有编造风险)。
  2. 只有标题、没有 DOI 的候选(2026-07-20 加, 通常是 `parse_search_results.py` 抽出来的)：
     脚本用 Crossref 按标题反查(`query.bibliographic`)，排除 `type=="component"`(SI 记录)，
     标题相似度 ≥0.90 才采信，找不到就保留标题继续走(status 会是 new，需要人工核实)。
     这一步同样不经过 LLM，比让 LLM 猜 DOI 可靠。
  3. 没有 DOI、只有从网页抓下来的一段乱文字的候选：交给 DeepSeek(scripts/ds.py)
     做结构化抽取("文本抓取等重复工作交给 DeepSeek 做"就是指这一步)。

用法:
  1. 把候选列表写成 JSON(见 --candidates)：
     [
       {"doi": "10.1002/anie.202507468"},
       {"raw_text": "从网页复制的一段没有清晰 DOI 的文字...", "link": "https://...", "category": "Ir-based"}
     ]
     candidate 可选带 "category" 字段(我在搜索时按搜到的关键词/子类手动标注, 比如按 Ir/Ru/非贵金属
     分别搜索时直接标好), 不带就用标题关键词自动粗分类(Ir/Ru/PGM关键词命中判断, 都不命中标"Unclear")。
  2. python scripts/scan_new_papers.py --candidates candidates.json --out exports/new_papers_2025.xlsx

输出 Excel 列: Title, Authors, Journal, Year, DOI, Link, Status, Category, Note
  Status: "new"(库里没有, 需要人工下载) / "already_in_library"(按 DOI 或标题相似度匹配到已有 citekey)
  Category(2026-07-20 加): Ir-based / Ru-based / Non-PGM / Unclear —— 自动分类基于标题关键词,
  不是语义理解, 边界情况(如同时含Ir和Ru的三元体系)需要人工核实, 不要照抄当最终结论。
"""

import argparse
import json
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
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
BIB = ROOT / "library.bib"

CROSSREF_HEADERS = {"User-Agent": "literature-scan/1.0 (mailto:example@example.com)"}


def crossref_lookup(doi: str) -> dict | None:
    url = f"https://api.crossref.org/works/{doi}"
    req = urllib.request.Request(url, headers=CROSSREF_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[警告] Crossref 查询 {doi} 失败: {e}", file=sys.stderr)
        return None
    msg = data.get("message", {})
    title = (msg.get("title") or [""])[0]
    title = re.sub(r"<[^>]+>", "", title)  # 去掉 <i>/<sub> 等 HTML 标签
    title = re.sub(r"\s+", " ", title).strip()  # 折叠标签留下的空白/换行
    authors = msg.get("author") or []
    authors_full = [f"{a.get('given','').strip()} {a.get('family','').strip()}".strip() for a in authors]
    journal = (msg.get("container-title") or [""])[0]
    year = None
    for key in ("published-print", "published-online", "published"):
        if key in msg and msg[key].get("date-parts"):
            year = msg[key]["date-parts"][0][0]
            break
    return {
        "title": title,
        "authors": authors_full,
        "journal": journal,
        "year": year,
        "doi": doi,
        "link": f"https://doi.org/{doi}",
    }


CROSSREF_MATCH_THRESHOLD = 0.90


def crossref_title_lookup(title: str, timeout: float = 10.0) -> dict | None:
    """按标题反查Crossref拿DOI, 复用 batch_ingest.py 里验证过的逻辑: 排除 SI 的 component 记录,
    标题相似度不够高就不采信, 避免模糊匹配返回不相关论文。查不到返回 None(不是错误, 调用方
    应保留原标题继续走, 让人工后续确认)。

    优化(2026-07-23): 搜索结果已包含完整元数据(title/authors/journal/year/DOI),
    不再额外调 crossref_lookup(doi)做第二次请求——这是之前最贵的冗余操作。"""
    import difflib
    import urllib.parse

    title = (title or "").strip()
    if not title:
        return None
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode({"query.bibliographic": title, "rows": 5})
    req = urllib.request.Request(url, headers=CROSSREF_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[警告] Crossref 标题反查失败 「{title[:50]}...」: {e}", file=sys.stderr)
        return None
    norm_query = re.sub(r"\s+", " ", title).strip().lower()
    for item in data.get("message", {}).get("items", []):
        if item.get("type") == "component":
            continue
        returned_title = (item.get("title") or [""])[0]
        ratio = difflib.SequenceMatcher(None, norm_query, re.sub(r"\s+", " ", returned_title).strip().lower()).ratio()
        if ratio >= CROSSREF_MATCH_THRESHOLD:
            doi = item.get("DOI")
            if not doi:
                continue
            # 直接从搜索结果提取元数据, 不再额外调 crossref_lookup(doi)
            returned_title = re.sub(r"<[^>]+>", "", returned_title)
            returned_title = re.sub(r"\s+", " ", returned_title).strip()
            authors = item.get("author") or []
            authors_full = [f"{a.get('given','').strip()} {a.get('family','').strip()}".strip() for a in authors]
            journal = (item.get("container-title") or [""])[0]
            year = None
            for key in ("published-print", "published-online", "published"):
                if key in item and item[key].get("date-parts"):
                    year = item[key]["date-parts"][0][0]
                    break
            return {
                "title": returned_title,
                "authors": authors_full,
                "journal": journal,
                "year": year,
                "doi": doi,
                "link": f"https://doi.org/{doi}",
            }
    return None


def deepseek_extract(raw_text: str, link: str) -> dict:
    """没有 DOI 时, 把抓下来的乱文字交给 DeepSeek 结构化抽取。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ds  # noqa: E402

    system = (
        "从下面这段网页/搜索结果文字里抽取一篇学术论文的元数据, 返回 JSON: "
        '{"title": "...", "authors": ["Name1","Name2"], "journal": "...", "year": 数字或null, "doi": "字符串或N/A"}。'
        "只抽取文字里明确出现的信息, 没有把握的字段留空/null, 不要编造。"
    )
    client = ds.get_client()
    # 纯结构化抽取(爬取范畴), 用便宜的 flash 档; deepseek-chat 是旧模型名, 已弃用。
    result = ds.call(client, "deepseek-v4-flash", system, raw_text, temperature=0, json_mode=True)
    parsed = json.loads(result)
    parsed.setdefault("link", link)
    return parsed


def load_bib_dois() -> set:
    if not BIB.exists():
        return set()
    text = BIB.read_text(encoding="utf-8")
    return {d.strip().strip('"{}').lower() for d in re.findall(r"doi\s*=\s*[{\"]([^}\"]+)[}\"]", text)}


def load_bib_titles() -> list:
    if not BIB.exists():
        return []
    text = BIB.read_text(encoding="utf-8")
    return re.findall(r"title\s*=\s*\{([^}]+)\}", text)


IR_WORD_RE = re.compile(r"\biridium\b", re.IGNORECASE)
RU_WORD_RE = re.compile(r"\bruthenium\b", re.IGNORECASE)
# 化学式里的元素符号token化: 大写字母开头, 后面跟0~1个小写字母或数字/下标, 用于从
# "IrRu"/"PtRuIr"/"Ir0.5Ru0.5O2" 这类连写化学式里正确切出 "Ir"/"Ru" 各自的token,
# 不能简单用 lookbehind(会被"IrRu"里"Ru"前面的小写r误判成"不是新元素符号开头")。
ELEMENT_TOKEN_RE = re.compile(r"[A-Z][a-z]?")
NON_PGM_RE = re.compile(
    r"\bcobalt\b|\bnickel\b|\bmanganese\b|\biron\b|\bCoO|\bNiO|\bMnO|\bFeO|non-PGM|non-precious|"
    r"earth-abundant|platinum-group-metal-free", re.IGNORECASE)


def classify_category(title: str) -> str:
    tokens = set(ELEMENT_TOKEN_RE.findall(title))
    has_ir = bool(IR_WORD_RE.search(title)) or "Ir" in tokens
    has_ru = bool(RU_WORD_RE.search(title)) or "Ru" in tokens
    if has_ir and has_ru:
        return "Ir-Ru-mixed"
    if has_ir:
        return "Ir-based"
    if has_ru:
        return "Ru-based"
    if NON_PGM_RE.search(title):
        return "Non-PGM"
    return "Unclear"


def is_duplicate(record: dict, bib_dois: set, bib_titles: list) -> bool:
    import difflib

    doi = (record.get("doi") or "").strip().lower()
    if doi and doi not in {"", "n/a", "na", "unknown", "none", "null"} and doi in bib_dois:
        return True
    title = (record.get("title") or "").strip()
    if not title:
        return False
    for t in bib_titles:
        if difflib.SequenceMatcher(None, title.lower(), t.lower()).ratio() >= 0.90:
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates", required=True, help="候选列表 JSON 文件路径")
    ap.add_argument("--out", required=True, help="输出 xlsx 路径")
    args = ap.parse_args()

    candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    # 统计候选来源（兼容 academic_search.py 输出的 source 字段）
    from collections import Counter
    source_counts = Counter(c.get("source", "parse_search_results") for c in candidates)
    print(f"候选来源分布: {dict(source_counts)}")
    bib_dois = load_bib_dois()
    bib_titles = load_bib_titles()

    total_cand = len(candidates)
    # 并发处理 Crossref 请求 (最大 5 并发, 避免触发 429)
    _MAX_WORKERS = 5

    def _process_one(i: int, c: dict) -> tuple[int, dict]:
        """处理单个候选: Crossref 查询 + 去重分类。返回 (index, record)。"""
        if c.get("doi"):
            rec = crossref_lookup(c["doi"])
            if rec is None:
                rec = {"title": "", "authors": [], "journal": "", "year": None,
                       "doi": c["doi"], "link": f"https://doi.org/{c['doi']}"}
        elif c.get("title") and not c.get("raw_text"):
            rec = crossref_title_lookup(c["title"])
            if rec is None:
                rec = {"title": c["title"], "authors": [], "journal": c.get("journal_hint") or "",
                       "year": c.get("year_hint"), "doi": "N/A", "link": ""}
        else:
            rec = deepseek_extract(c.get("raw_text", ""), c.get("link", ""))
        rec["status"] = "already_in_library" if is_duplicate(rec, bib_dois, bib_titles) else "new"
        rec["category"] = c.get("category") or classify_category(rec.get("title", ""))
        return i, rec

    records = [None] * total_cand
    done_count = 0
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(_process_one, i, c): i for i, c in enumerate(candidates)}
        for future in as_completed(futures):
            idx, rec = future.result()
            records[idx] = rec
            done_count += 1
            if done_count % 5 == 0 or done_count == total_cand:
                print(f"  核验进度: {done_count}/{total_cand}")

    try:
        from openpyxl import Workbook
    except ImportError:
        sys.exit("缺少依赖: pip install openpyxl")

    wb = Workbook()
    ws = wb.active
    ws.title = "candidates"
    ws.append(["Title", "Authors", "Journal", "Year", "DOI", "Link", "Status", "Category", "Note"])
    for rec in records:
        ws.append([
            rec.get("title", ""),
            "; ".join(rec.get("authors") or []),
            rec.get("journal", ""),
            rec.get("year") or "",
            rec.get("doi", ""),
            rec.get("link", ""),
            rec.get("status", ""),
            rec.get("category", ""),
            "",
        ])
    for col, width in zip("ABCDEFGHI", [55, 35, 30, 8, 22, 35, 18, 14, 30]):
        ws.column_dimensions[col].width = width

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)

    # 精简文本摘要(2026-07-20 加): 只列 status=new 的条目, 按 category 分组, 供 Claude 直接读这个
    # 就能汇报结果, 不用再打开/解析整份 xlsx(那样会把大量已跳过的条目也读进上下文, 浪费 token)。
    from collections import defaultdict
    by_category = defaultdict(list)
    for r in records:
        if r["status"] == "new":
            by_category[r.get("category", "Unclear")].append(r)

    summary_lines = [f"共 {len(records)} 条候选, {sum(len(v) for v in by_category.values())} 条需要下载"
                      f"(按分类列出, {len(records) - sum(len(v) for v in by_category.values())} 条已在库跳过):"]
    for cat, recs in sorted(by_category.items()):
        summary_lines.append(f"\n## {cat} ({len(recs)}篇)")
        for r in recs:
            summary_lines.append(f"- {r.get('title','')} | {r.get('journal','')} {r.get('year','')} | {r.get('doi','')}")
    summary_path = out_path.with_suffix(".summary.txt")
    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")

    n_new = sum(len(v) for v in by_category.values())
    print(f"已写入 {out_path} 和 {summary_path}：共 {len(records)} 条，{n_new} 条需要下载，"
          f"{len(records) - n_new} 条已在库里(跳过)。汇报结果时读 .summary.txt 即可, 不用打开 xlsx。")


if __name__ == "__main__":
    main()
