#!/usr/bin/env python3
"""
系统性学术检索脚本 — 用 OpenAlex / Semantic Scholar API 穷举全部匹配论文，
替代 WebSearch 作为"扩充/查新"的主要发现引擎。

两个搜索引擎：
  - OpenAlex：按期刊+日期范围+关键词返回全部论文（cursor 分页，不受 ~10 条限制）
  - Semantic Scholar：从库内已有论文的 DOI 出发，通过引用图谱发现相关论文

两种模式：
  1. 系统检索（替代 WebSearch 的"扩充/查新"）：
     python scripts/academic_search.py --top-journals \
         --keywords "oxygen evolution,catalyst,PEM,acidic OER" \
         --from-date 2021-01-01 --out candidates.json
  2. 引用图谱扩展（全新能力）：
     python scripts/academic_search.py --citation-graph \
         --from-date 2025-01-01 --out candidates.json

输出 JSON 格式兼容 scan_new_papers.py --candidates，直接喂给它做 Crossref 核验+去重。

认证：
  - OpenAlex：环境变量 OPENALEX_API_KEY（免费 key 即可，openalex.org/settings/api 申请）
    没有 key 也能跑，rate limit 很低（$0.01/天 vs $1/天）
  - Semantic Scholar：环境变量 SEMANTIC_SCHOLAR_API_KEY（无 key 用 1 req/s 免费额度）
"""

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

# Windows 终端 GBK 编码修复: 强制 UTF-8 输出
# write_through=True 确保子进程/管道模式下不被缓冲（配合 PYTHONUNBUFFERED=1）
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", write_through=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                      errors="replace", write_through=True)
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
TOP_JOURNALS_FILE = SCRIPTS_DIR / "top_journals.txt"
SOURCE_CACHE_FILE = SCRIPTS_DIR / ".openalex_source_cache.json"
BIB = ROOT / "library.bib"

# ── 关键词翻译/扩展 ─────────────────────────────────────────────────────────────

# 中文→英文术语词典（电催化/PEM电解水领域，按需扩充）
_ZH_TERM_DICT: dict[str, list[str]] = {
    # 材料体系
    "铱基": ["iridium"], "铱": ["iridium"], "Ir基": ["iridium"],
    "钌基": ["ruthenium"], "钌": ["ruthenium"], "Ru基": ["ruthenium"],
    "钴基": ["cobalt"], "镍基": ["nickel"], "锰基": ["manganese"],
    "铁基": ["iron"], "铂基": ["platinum"],
    "非贵金属": ["non-precious metal", "earth-abundant"],
    "贵金属": ["noble metal", "precious metal"],
    "单原子": ["single atom", "single-atom"],
    "纳米颗粒": ["nanoparticle"], "纳米片": ["nanosheet"],
    "纳米线": ["nanowire"], "纳米管": ["nanotube"],
    "氧化物": ["oxide"], "氢氧化物": ["hydroxide"],
    "钙钛矿": ["perovskite"], "尖晶石": ["spinel"],
    "合金": ["alloy"], "掺杂": ["doping", "doped"],
    "核壳": ["core-shell"], "中空": ["hollow"],
    "负载": ["supported", "loading"],
    # 反应/应用
    "催化剂": ["catalyst", "electrocatalyst"],
    "催化": ["catalysis", "catalytic"],
    "析氧反应": ["oxygen evolution reaction", "OER"],
    "析氧": ["oxygen evolution", "OER"],
    "析氢反应": ["hydrogen evolution reaction", "HER"],
    "析氢": ["hydrogen evolution", "HER"],
    "氧还原": ["oxygen reduction", "ORR"],
    "全解水": ["overall water splitting"],
    "水分解": ["water splitting"],
    "水氧化": ["water oxidation"],
    "电解水": ["water electrolysis"],
    "酸性": ["acidic"], "碱性": ["alkaline"],
    "稳定性": ["stability", "durability"],
    "耐久性": ["durability"], "降解": ["degradation"],
    "活性": ["activity"], "过电位": ["overpotential"],
    "溶解": ["dissolution"], "腐蚀": ["corrosion"],
    # 器件/系统
    "质子交换膜": ["proton exchange membrane", "PEM"],
    "阴离子交换膜": ["anion exchange membrane", "AEM"],
    "膜电极": ["membrane electrode assembly", "MEA"],
    "电解槽": ["electrolyzer", "electrolyser"],
    "燃料电池": ["fuel cell"],
    # 表征/方法
    "原位": ["in situ", "operando"], "工况": ["operando"],
    "电化学": ["electrochemical"],
    "第一性原理": ["DFT", "first-principles"],
    "密度泛函": ["DFT", "density functional theory"],
    "分子动力学": ["molecular dynamics", "AIMD"],
}

# DeepSeek 翻译提示词
_EXPAND_SYSTEM = """You are a scientific literature search expert. Given a research topic description
(possibly in Chinese or mixed Chinese-English), expand it into a JSON list of English keyword
groups for searching academic databases (OpenAlex). Each group should be a short English phrase
(1-3 words) that captures one aspect: material system, reaction type, application, method, etc.
Return ONLY a valid JSON array of strings, e.g. ["iridium", "oxygen evolution", "PEM", "acidic OER"].
Focus on the most discriminative terms (3-8 groups). Do NOT include overly broad terms alone."""


def _has_chinese(text: str) -> bool:
    return any('\u4e00' <= c <= '\u9fff' for c in text)


def expand_keywords(raw_keywords: list[str]) -> list[str]:
    """
    将用户输入的关键词(可能含中文)扩展为英文关键词组。
    优先用 DeepSeek flash 翻译(如果 API key 可用)，否则用词典。
    返回去重后的英文关键词列表。
    """
    all_expanded: list[str] = []

    # 检查是否有任何中文字符需要翻译
    needs_translation = any(_has_chinese(kw) for kw in raw_keywords)

    if needs_translation:
        # 尝试 DeepSeek 翻译
        ds_result = _expand_via_deepseek(raw_keywords)
        if ds_result:
            print(f"  DeepSeek 翻译: {raw_keywords} -> {ds_result}")
            return ds_result
        # 回退到词典
        print("  DeepSeek 不可用，使用词典翻译")

    for kw in raw_keywords:
        if not _has_chinese(kw):
            # 纯英文，直接使用
            all_expanded.append(kw)
            continue

        # 中文关键词：词典查找
        expanded = _dict_expand(kw)
        all_expanded.extend(expanded)
        print(f"  词典翻译: '{kw}' -> {expanded}")

    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for kw in all_expanded:
        kw_lower = kw.lower().strip()
        if kw_lower and kw_lower not in seen:
            seen.add(kw_lower)
            unique.append(kw.strip())

    return unique


def _dict_expand(text: str) -> list[str]:
    """用词典将中文文本拆成英文关键词组。"""
    results: list[str] = []
    # 提取文本中已有的英文部分(如 "PEM" 在 "PEM Ir基催化剂" 中)
    en_parts = re.findall(r'[A-Za-z][A-Za-z0-9_]+', text)
    results.extend(en_parts)

    # 按词典匹配中文片段
    matched_positions: set[int] = set()
    for term, translations in _ZH_TERM_DICT.items():
        start = 0
        while True:
            idx = text.find(term, start)
            if idx == -1:
                break
            results.extend(translations)
            for i in range(idx, idx + len(term)):
                matched_positions.add(i)
            start = idx + len(term)

    if not results:
        # 词典完全没匹配，尝试按单字查
        for char in text:
            if char in _ZH_TERM_DICT:
                results.extend(_ZH_TERM_DICT[char])

    return results


def _expand_via_deepseek(raw_keywords: list[str]) -> list[str] | None:
    """用 DeepSeek flash 翻译/扩展关键词，返回 None 表示不可用。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None

    try:
        import urllib.request as _ur
        query = ",".join(raw_keywords)
        payload = json.dumps({
            "model": "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": _EXPAND_SYSTEM},
                {"role": "user", "content": query},
            ],
            "temperature": 0.3,
            "stream": False,
        }).encode("utf-8")

        req = _ur.Request(
            "https://api.deepseek.com/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with _ur.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        content = data["choices"][0]["message"]["content"].strip()
        # 解析 JSON 数组
        keywords = json.loads(content)
        if isinstance(keywords, list) and all(isinstance(k, str) for k in keywords):
            return keywords
    except Exception as e:
        print(f"  DeepSeek 翻译失败: {e}", file=sys.stderr)

    return None


# ── OpenAlex ──────────────────────────────────────────────────────────────────

OPENALEX_BASE = "https://api.openalex.org"
# polite pool 需要 mailto 参数（无 key 时也能用，配额更高）
OPENALEX_MAILTO = "papertrail@example.com"  # 替换成你自己的邮箱


def _openalex_headers() -> dict:
    h = {"Accept": "application/json"}
    key = os.environ.get("OPENALEX_API_KEY", "")
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _append_mailto(url: str) -> str:
    """给 OpenAlex URL 追加 mailto 参数，进入 polite pool（配额 10 req/s）。"""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}mailto={OPENALEX_MAILTO}"


def _http_get_json(url: str, headers: dict | None = None, timeout: float = 30, retries: int = 3) -> dict | None:
    url = _append_mailto(url)  # 所有 OpenAlex 请求都带 mailto
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # 优先用 Retry-After 头，否则指数退避（10s起步）
                retry_after = e.headers.get("Retry-After")
                wait = int(retry_after) if retry_after else min(10 ** (attempt + 1), 120)
                if attempt < retries:
                    print(f"  [429 rate limit] waiting {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                # 最后一次重试也失败，多等一会再放弃
                print(f"  [429 rate limit] 重试 {retries} 次仍失败，跳过此请求", file=sys.stderr)
            else:
                print(f"  [HTTP error] {url[:80]}... -> {e}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"  [HTTP error] {url[:80]}... -> {e}", file=sys.stderr)
            return None
    return None


def resolve_openalex_source_id(journal_name: str, cache: dict) -> str | None:
    """将期刊名解析成 OpenAlex source id，结果缓存在 cache dict 里。"""
    # 查缓存
    if journal_name in cache:
        return cache[journal_name]

    # 调 API 搜索
    encoded = urllib.parse.quote(journal_name)
    url = f"{OPENALEX_BASE}/sources?search={encoded}&per_page=5"
    data = _http_get_json(url, _openalex_headers())
    if not data:
        return None

    for src in data.get("results", []):
        display_name = (src.get("display_name") or "").strip()
        # 精确匹配或去掉 "The" / "Journal of" 后的模糊匹配
        if display_name.lower() == journal_name.lower():
            source_id = src.get("id", "")
            cache[journal_name] = source_id
            print(f"  [OK] {journal_name} -> {source_id}")
            return source_id
        # 宽松匹配：期刊名去掉常见前缀后比较
        cleaned = re.sub(r"^(The |Journal of )", "", journal_name, flags=re.IGNORECASE).strip()
        if display_name.lower() == cleaned.lower():
            source_id = src.get("id", "")
            cache[journal_name] = source_id
            print(f"  [OK] {journal_name} -> {source_id} (via '{display_name}')")
            return source_id

    # 如果精确匹配失败，取第一个结果但标记为"模糊"
    if data.get("results"):
        src = data["results"][0]
        source_id = src.get("id", "")
        display_name = src.get("display_name", "")
        cache[journal_name] = source_id
        print(f"  [~] {journal_name} -> {source_id} (fuzzy match '{display_name}')", file=sys.stderr)
        return source_id

    cache[journal_name] = None
    print(f"  [X] {journal_name} -> not found in OpenAlex", file=sys.stderr)
    return None


def load_source_cache() -> dict:
    if SOURCE_CACHE_FILE.exists():
        return json.loads(SOURCE_CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_source_cache(cache: dict):
    SOURCE_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def openalex_search(
    source_ids: list[str],
    keywords: list[str],
    from_year: int,
    to_year: int,
    max_pages: int = 50,
    min_match: int = 1,
) -> list[dict]:
    """
    对每个 (source_id, keyword) 组合用 cursor 分页遍历全部结果。
    min_match >= 2 时启用 AND 逻辑：论文必须匹配至少 min_match 个关键词组才保留。
    返回的每条包含 relevance_score (匹配了几个关键词组)。
    """
    # 收集所有结果，跟踪每篇论文匹配了哪些关键词组
    all_papers: dict[str, dict] = {}        # dedup_key -> paper dict
    match_tracker: dict[str, set] = {}       # dedup_key -> set of matched keyword indices

    for source_id in source_ids:
        for kw_idx, kw in enumerate(keywords):
            # 构建 filter
            filters = [
                f"primary_location.source.id:{source_id}",
                f"publication_year:{from_year}-{to_year}",
                "type:article|review",
            ]
            filter_str = ",".join(filters)
            # filter 不编码: OpenAlex 需要冒号/逗号/管道符作为语法符号
            # search 用 quote 编码(safe='' 确保空格也编码, 中文等非ASCII字符也编码)
            encoded_search = urllib.parse.quote(kw, safe='')

            cursor = "*"
            page_count = 0
            result_count = 0

            while cursor and page_count < max_pages:
                url = (
                    f"{OPENALEX_BASE}/works?"
                    f"filter={filter_str}"
                    f"&search={encoded_search}"
                    f"&per_page=100"
                    f"&cursor={cursor}"
                    f"&select=id,doi,title,publication_year,primary_location,authorships"
                )
                data = _http_get_json(url, _openalex_headers())
                if not data:
                    break

                results = data.get("results", [])
                if not results:
                    break

                for work in results:
                    doi = (work.get("doi") or "").replace("https://doi.org/", "").strip()
                    title_raw = (work.get("title") or "").strip()
                    title = re.sub(r"\s+", " ", title_raw)
                    year = work.get("publication_year")

                    # 去重 key：优先 DOI，没有就用标题小写
                    dedup_key = doi.lower() if doi else title.lower()
                    if not dedup_key:
                        continue

                    # 记录这篇论文匹配了哪个关键词组
                    if dedup_key not in match_tracker:
                        match_tracker[dedup_key] = set()
                    match_tracker[dedup_key].add(kw_idx)

                    if dedup_key in all_papers:
                        continue  # 已经存过，只更新 match_tracker

                    # 提取期刊名
                    journal = ""
                    loc = work.get("primary_location") or {}
                    src = loc.get("source") or {}
                    journal = (src.get("display_name") or "").strip()

                    # 提取作者
                    authors = []
                    for auth in (work.get("authorships") or []):
                        author = auth.get("author") or {}
                        name = (author.get("display_name") or "").strip()
                        if name:
                            authors.append(name)

                    all_papers[dedup_key] = {
                        "doi": doi or None,
                        "title": title,
                        "journal": journal,
                        "year": year,
                        "authors": authors,
                        "source": "openalex",
                    }
                    result_count += 1

                cursor = data.get("meta", {}).get("next_cursor")
                page_count += 1

                # 礼貌限速：请求间稳定延迟，避免触发 429
                time.sleep(1.0)  # polite pool 配额 10 req/s，保守用 1s

            total_key = f"{source_id}+{kw}"
            print(f"  OpenAlex [{total_key}]: {page_count} pages, {result_count} papers")

            # 关键词间加延迟避免连续请求触发 429
            time.sleep(0.5)

    # 写入 relevance_score 并按 min_match 过滤
    final_results: list[dict] = []
    n_total = len(all_papers)
    n_filtered = 0

    for dedup_key, paper in all_papers.items():
        score = len(match_tracker.get(dedup_key, set()))
        if score < min_match:
            n_filtered += 1
            continue
        paper["relevance_score"] = score
        paper["matched_keywords"] = [keywords[i] for i in sorted(match_tracker[dedup_key])]
        final_results.append(paper)

    # 按相关性得分降序排列（匹配越多越靠前）
    final_results.sort(key=lambda p: -p["relevance_score"])

    print(f"  Total: {n_total} papers, {n_filtered} filtered (min_match={min_match}), "
          f"{len(final_results)} returned")

    return final_results


def load_top_journals() -> list[str]:
    """读 scripts/top_journals.txt，跳过注释和空行。"""
    if not TOP_JOURNALS_FILE.exists():
        print(f"[错误] 找不到 {TOP_JOURNALS_FILE}", file=sys.stderr)
        return []
    lines = []
    for line in TOP_JOURNALS_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


# ── Semantic Scholar（引用图谱模式）─────────────────────────────────────────

S2_BASE = "https://api.semanticscholar.org/graph/v1"


def _s2_headers() -> dict:
    h = {"Accept": "application/json"}
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    if key:
        h["x-api-key"] = key
    return h


def load_bib_dois() -> list[str]:
    """从 library.bib 提取所有 DOI。"""
    if not BIB.exists():
        return []
    text = BIB.read_text(encoding="utf-8")
    return [d.strip().strip('"{}') for d in re.findall(r"doi\s*=\s*[{\"]([^}\"]+)[}\"]", text)]


def load_bib_dois_with_status() -> list[dict]:
    """从 notes/ 读带 status/rating 信息的 DOI 列表，用于 citation-graph 模式筛选。"""
    notes_dir = ROOT / "notes"
    if not notes_dir.exists():
        return load_bib_dois() and [{"doi": d} for d in load_bib_dois()]

    results = []
    for md_file in notes_dir.glob("*.md"):
        text = md_file.read_text(encoding="utf-8")
        doi_match = re.search(r"^doi:\s*(.+)$", text, re.MULTILINE)
        status_match = re.search(r"^status:\s*(.+)$", text, re.MULTILINE)
        rating_match = re.search(r"^rating:\s*(.+)$", text, re.MULTILINE)
        if doi_match:
            doi = doi_match.group(1).strip().strip('"{}')
            if doi.lower() in {"n/a", "na", "unknown", "none", "null", ""}:
                continue
            results.append({
                "doi": doi,
                "status": (status_match.group(1).strip() if status_match else "unread"),
                "rating": (rating_match.group(1).strip() if rating_match else ""),
            })
    return results


def s2_citation_graph(dois: list[str], from_year: int, max_papers: int = 100) -> list[dict]:
    """
    对每个 DOI 查 Semantic Scholar 的 citations（谁引了它）和 references（它引了谁），
    筛选 from_year 之后的论文，合并去重。
    """
    all_results: dict[str, dict] = {}
    headers = _s2_headers()
    has_key = bool(os.environ.get("SEMANTIC_SCHOLAR_API_KEY"))

    fields = "title,authors,year,externalIds,citationCount,publicationVenue"

    for i, doi in enumerate(dois[:max_papers]):
        # 查 citations（谁引了这篇）
        for direction in ("citations", "references"):
            url = f"{S2_BASE}/paper/DOI:{doi}/{direction}?fields={fields}&limit=100"
            data = _http_get_json(url, headers)
            if not data:
                # 限速后重试一次
                time.sleep(1.5 if not has_key else 0.3)
                data = _http_get_json(url, headers)
                if not data:
                    continue

            items = data.get("data", [])
            for item in items:
                paper = item.get("citingPaper") or item.get("citedPaper") or {}
                title = (paper.get("title") or "").strip()
                year = paper.get("year")
                if not title or (year and year < from_year):
                    continue

                ext_ids = paper.get("externalIds") or {}
                paper_doi = ext_ids.get("DOI", "")
                dedup_key = paper_doi.lower() if paper_doi else title.lower()
                if dedup_key in all_results:
                    continue

                venue = paper.get("publicationVenue") or {}
                journal = (venue.get("name") or "").strip()

                authors = []
                for auth in (paper.get("authors") or []):
                    name = (auth.get("name") or "").strip()
                    if name:
                        authors.append(name)

                all_results[dedup_key] = {
                    "doi": paper_doi or None,
                    "title": title,
                    "journal": journal,
                    "year": year,
                    "authors": authors,
                    "source": "semantic_scholar",
                }

            # 限速
            time.sleep(1.0 if not has_key else 0.15)

        if (i + 1) % 10 == 0:
            print(f"  S2 引用图谱: 已处理 {i + 1}/{min(len(dois), max_papers)} 篇")

    print(f"  S2 引用图谱: 共 {len(all_results)} 条新结果")
    return list(all_results.values())


# ── 分类（复用 scan_new_papers.py 的逻辑）────────────────────────────────────

IR_WORD_RE = re.compile(r"\biridium\b", re.IGNORECASE)
RU_WORD_RE = re.compile(r"\bruthenium\b", re.IGNORECASE)
ELEMENT_TOKEN_RE = re.compile(r"[A-Z][a-z]?")
NON_PGM_RE = re.compile(
    r"\bcobalt\b|\bnickel\b|\bmanganese\b|\biron\b|\bCoO|\bNiO|\bMnO|\bFeO|non-PGM|non-precious|"
    r"earth-abundant|platinum-group-metal-free", re.IGNORECASE)


def classify_category(title: str) -> str:
    tokens = set(ELEMENT_TOKEN_RE.findall(title or ""))
    has_ir = bool(IR_WORD_RE.search(title or "")) or "Ir" in tokens
    has_ru = bool(RU_WORD_RE.search(title or "")) or "Ru" in tokens
    if has_ir and has_ru:
        return "Ir-Ru-mixed"
    if has_ir:
        return "Ir-based"
    if has_ru:
        return "Ru-based"
    if NON_PGM_RE.search(title or ""):
        return "Non-PGM"
    return "Unclear"


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    # 期刊范围
    journal_group = ap.add_mutually_exclusive_group()
    journal_group.add_argument("--top-journals", action="store_true",
                               help=f"使用 {TOP_JOURNALS_FILE.name} 里的期刊白名单")
    journal_group.add_argument("--journals", default="",
                               help="自定义期刊列表，逗号分隔")

    # 搜索参数
    ap.add_argument("--keywords", default="",
                    help="逗号分隔的关键词组，每组独立搜索后合并去重")
    ap.add_argument("--from-date", default="",
                    help="起始日期 YYYY-MM-DD（'扩充'用5年前，'查新'用 scan_state 记录）")
    ap.add_argument("--to-date", default="",
                    help="截止日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--max-pages", type=int, default=50,
                    help="每组关键词最多翻几页（默认50，每页100条）")

    # 引用图谱模式
    ap.add_argument("--citation-graph", action="store_true",
                    help="从库内已有论文 DOI 出发查引用/被引论文（Semantic Scholar）")
    ap.add_argument("--citation-max-papers", type=int, default=100,
                    help="引用图谱模式最多查几篇库内论文（默认100，按 status=read/rating 优先）")

    # 引擎选择
    ap.add_argument("--engine", choices=["openalex", "semantic_scholar", "both"],
                    default="openalex",
                    help="搜索引擎（默认 openalex；citation-graph 模式自动用 semantic_scholar）")

    # 相关性过滤
    ap.add_argument("--min-match", type=int, default=0,
                    help="论文至少匹配几个关键词组才保留（0=自动：有关键词时默认2）")

    # 输出
    ap.add_argument("--out", required=True,
                    help="输出 JSON 路径，直接喂 scan_new_papers.py --candidates")

    args = ap.parse_args()

    # ── 解析日期 ──
    if args.from_date:
        from_date = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    else:
        from_date = date(date.today().year - 5, 1, 1)
    to_date = datetime.strptime(args.to_date, "%Y-%m-%d").date() if args.to_date else date.today()
    from_year = from_date.year
    to_year = to_date.year

    print(f"搜索范围: {from_date} ~ {to_date} (年份 {from_year}-{to_year})")

    all_candidates = []

    # ── OpenAlex 系统检索 ──
    if args.engine in ("openalex", "both") and not args.citation_graph:
        # 确定期刊列表
        if args.top_journals:
            journal_names = load_top_journals()
            print(f"期刊白名单: {len(journal_names)} 个期刊")
        elif args.journals:
            journal_names = [j.strip() for j in args.journals.split(",") if j.strip()]
            print(f"自定义期刊: {len(journal_names)} 个")
        else:
            print("[错误] 请指定 --top-journals 或 --journals", file=sys.stderr)
            sys.exit(1)

        # 解析关键词（支持中文自动翻译）
        raw_keywords = [k.strip() for k in args.keywords.split(",") if k.strip()] if args.keywords else [""]
        if raw_keywords == [""]:
            print("[警告] 未指定关键词，将搜索期刊内全部论文（结果可能很多）", file=sys.stderr)
            keywords = [""]
            min_match = 1
        else:
            print(f"原始关键词: {raw_keywords}")
            # 自动翻译中文关键词为英文
            keywords = expand_keywords(raw_keywords)
            print(f"扩展后关键词组: {keywords}")
            # min_match 自动计算：0=自动，有关键词时默认 2
            min_match = args.min_match if args.min_match > 0 else min(2, len(keywords))
            if len(keywords) == 1:
                min_match = 1  # 只有一个关键词时不能要求匹配2个

        print(f"相关性过滤: min_match={min_match} (论文至少匹配 {min_match} 个关键词组才保留)")

        # 解析 source IDs
        cache = load_source_cache()
        source_ids = []
        for name in journal_names:
            sid = resolve_openalex_source_id(name, cache)
            if sid:
                source_ids.append(sid)
        save_source_cache(cache)
        print(f"已解析 {len(source_ids)}/{len(journal_names)} 个期刊 source ID")

        if not source_ids:
            print("[错误] 没有任何期刊解析成功", file=sys.stderr)
            sys.exit(1)

        print(f"\n开始 OpenAlex 检索 ({len(source_ids)} journals x {len(keywords)} keywords)...")
        results = openalex_search(source_ids, keywords, from_year, to_year, args.max_pages, min_match)
        print(f"OpenAlex 共返回 {len(results)} 条结果")

        for r in results:
            r["category"] = classify_category(r.get("title", ""))
            # 输出格式兼容 scan_new_papers.py
            entry = {"category": r["category"], "source": "openalex"}
            if r.get("doi"):
                entry["doi"] = r["doi"]
            else:
                entry["title"] = r.get("title", "")
                entry["journal_hint"] = r.get("journal", "")
                entry["year_hint"] = r.get("year")
            all_candidates.append(entry)

    # ── Semantic Scholar 引用图谱 ──
    if args.citation_graph or args.engine == "semantic_scholar":
        print("\n开始 Semantic Scholar 引用图谱扩展...")
        bib_records = load_bib_dois_with_status()

        # 优先选 status=read 或有 rating 的论文
        scored = []
        for rec in bib_records:
            score = 0
            if rec.get("status") == "read":
                score += 10
            if rec.get("rating"):
                score += len(rec["rating"].replace(" ", ""))  # ★ 数量
            scored.append((score, rec))
        scored.sort(key=lambda x: -x[0])

        selected_dois = [r["doi"] for _, r in scored[:args.citation_max_papers]]
        print(f"从库内 {len(bib_records)} 篇中选出 {len(selected_dois)} 篇高价值论文查引用图谱")

        s2_results = s2_citation_graph(selected_dois, from_year)
        print(f"Semantic Scholar 共返回 {len(s2_results)} 条去重结果")

        for r in s2_results:
            r["category"] = classify_category(r.get("title", ""))
            entry = {"category": r["category"], "source": "semantic_scholar"}
            if r.get("doi"):
                entry["doi"] = r["doi"]
            else:
                entry["title"] = r.get("title", "")
                entry["journal_hint"] = r.get("journal", "")
                entry["year_hint"] = r.get("year")
            all_candidates.append(entry)

    # ── 写输出 ──
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(all_candidates, ensure_ascii=False, indent=2), encoding="utf-8")

    # 统计摘要
    n_with_doi = sum(1 for c in all_candidates if "doi" in c)
    n_openalex = sum(1 for c in all_candidates if c.get("source") == "openalex")
    n_s2 = sum(1 for c in all_candidates if c.get("source") == "semantic_scholar")
    print(f"\n已写入 {out_path}: 共 {len(all_candidates)} 条候选")
    print(f"  OpenAlex: {n_openalex}, Semantic Scholar: {n_s2}")
    print(f"  有 DOI: {n_with_doi}, 仅标题: {len(all_candidates) - n_with_doi}")
    print(f"\n下一步: python scripts/scan_new_papers.py --candidates {out_path} --out exports/xxx.xlsx")


if __name__ == "__main__":
    main()
