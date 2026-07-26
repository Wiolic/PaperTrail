#!/usr/bin/env python3
"""
引用图谱——构建论文关系网络, 生成交互式 HTML(vis.js), 类似 PaperConnect。

数据来源(三层):
1. notes/ 的 related 字段(人工/AI 标注的关系, 最可靠)
2. TF-IDF 文本相似度(auto_link_related.py 同一套, 阈值可配)
3. (可选) Semantic Scholar API 的真实引用/被引关系(需要 DOI, 较慢)

生成 exports/citation_network.html, 双击浏览器打开即可交互:
- 节点 = 论文(颜色按年份渐变, 大小按连接数)
- 边 = 论文关系(related 字段实线, 相似度虚线)
- 点击节点 → 右侧面板显示标题/期刊/年份/关键词/摘要
- 搜索框过滤论文
- 缩放/拖拽/框选

用法:
    python scripts/citation_network.py                     # 只用 related + TF-IDF
    python scripts/citation_network.py --use-s2            # 额外查 Semantic Scholar(慢)
    python scripts/citation_network.py --sim-threshold 0.2 # 相似度阈值(默认 0.2)
    python scripts/citation_network.py --open              # 生成后自动打开浏览器
"""
import argparse
import io
import json
import re
import sys
import webbrowser
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = ROOT / "notes"
EXPORTS_DIR = ROOT / "exports"
OUTPUT_HTML = EXPORTS_DIR / "citation_network.html"
CACHE_JSON = EXPORTS_DIR / "citation_network_cache.json"  # 图数据缓存, 避免每次重算 TF-IDF
VIS_JS_LOCAL = EXPORTS_DIR / "vis-network.min.js"  # 本地 vis.js, 不依赖 CDN
VIS_JS_CDN = "https://cdn.jsdelivr.net/npm/vis-network/standalone/umd/vis-network.min.js"


def ensure_vis_js():
    """确保 exports/ 下有本地 vis-network.min.js (首次自动从 CDN 下载)。"""
    if VIS_JS_LOCAL.exists() and VIS_JS_LOCAL.stat().st_size > 100_000:
        return  # 已有且大小合理
    try:
        import urllib.request
        print("首次运行: 下载 vis-network.min.js 到本地...")
        urllib.request.urlretrieve(VIS_JS_CDN, str(VIS_JS_LOCAL))
        print(f"  已下载: {VIS_JS_LOCAL.name} ({VIS_JS_LOCAL.stat().st_size // 1024} KB)")
    except Exception as e:
        print(f"  下载失败({e}), HTML 将回退到 CDN 引用", file=sys.stderr)


def load_notes() -> list[dict]:
    notes = []
    if not NOTES_DIR.exists():
        return notes
    for f in sorted(NOTES_DIR.glob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
        if not m:
            continue
        fm_text, body = m.group(1), m.group(2)

        def _field(name):
            fm = re.search(rf"^{re.escape(name)}:\s*(.+)$", fm_text, re.MULTILINE)
            return fm.group(1).strip() if fm else ""

        def _list_field(name):
            raw = _field(name)
            if raw.startswith("[") and raw.endswith("]"):
                return [x.strip().strip('"').strip("'") for x in raw[1:-1].split(",") if x.strip()]
            return []

        year_str = _field("year")
        try:
            year = int(year_str)
        except ValueError:
            year = 0

        notes.append({
            "citekey": f.stem,
            "title": _field("title"),
            "journal": _field("journal"),
            "year": year,
            "tags": _list_field("tags"),
            "keywords": _list_field("keywords"),
            "related": _list_field("related"),
            "doi": _field("doi"),
            "body_head": body[:500].replace("\n", " ").strip(),
        })
    return notes


def compute_tfidf_edges(notes: list[dict], threshold: float = 0.2) -> list[dict]:
    """用 TF-IDF 计算相似度, 返回超过阈值的边 [{from, to, value, type}, ...]。"""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    texts = []
    for n in notes:
        parts = [n["title"]] * 3 + n["keywords"] * 2 + n["tags"] * 2 + [n["body_head"]]
        texts.append(" ".join(parts))

    vectorizer = TfidfVectorizer(max_features=5000, ngram_range=(1, 2),
                                  stop_words="english", lowercase=True)
    tfidf_matrix = vectorizer.fit_transform(texts)
    sim_matrix = cosine_similarity(tfidf_matrix)

    edges = []
    citekeys = [n["citekey"] for n in notes]
    for i in range(len(notes)):
        for j in range(i + 1, len(notes)):
            score = sim_matrix[i][j]
            if score >= threshold:
                edges.append({
                    "from": citekeys[i], "to": citekeys[j],
                    "value": round(score, 3), "type": "similarity",
                })
    return edges


def fetch_s2_citations(notes: list[dict], delay: float = 1.0) -> list[dict]:
    """(可选)查 Semantic Scholar API, 获取真实引用/被引关系。需要 DOI。"""
    import time
    import urllib.request

    edges = []
    doi_to_ck = {n["doi"]: n["citekey"] for n in notes if n["doi"] and n["doi"].lower() != "n/a"}
    all_citekeys = set(n["citekey"] for n in notes)
    processed = set()

    for note in notes:
        doi = note["doi"]
        ck = note["citekey"]
        if not doi or doi.lower() == "n/a" or ck in processed:
            continue
        processed.add(ck)

        url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=citations,references"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PaperTrail/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            # 引用的论文
            for ref in (data.get("references") or []):
                ref_doi = ref.get("externalIds", {}).get("DOI", "")
                if ref_doi in doi_to_ck:
                    ref_ck = doi_to_ck[ref_doi]
                    if ref_ck != ck:
                        edges.append({"from": ck, "to": ref_ck, "value": 1, "type": "citation"})

            # 被引的论文
            for cit in (data.get("citations") or []):
                cit_doi = cit.get("externalIds", {}).get("DOI", "")
                if cit_doi in doi_to_ck:
                    cit_ck = doi_to_ck[cit_doi]
                    if cit_ck != ck:
                        edges.append({"from": cit_ck, "to": ck, "value": 1, "type": "citation"})

            print(f"  S2: {ck} → {len(data.get('references') or [])} refs, "
                  f"{len(data.get('citations') or [])} citations", file=sys.stderr)
        except Exception:
            pass
        time.sleep(delay)

    return edges


def generate_html(notes: list[dict], edges: list[dict],
                  focus_citekey: str = "", streamlit_url: str = "") -> str:
    """生成 vis.js 交互式 HTML。
    focus_citekey: 从哪个笔记进来的, 高亮该节点并自动选中。
    streamlit_url: PaperTrail 的 URL, 用于"回到笔记"按钮(如 http://localhost:8501)。
    """
    # 准备节点数据
    nodes_data = []
    edge_counts = {}
    for e in edges:
        edge_counts[e["from"]] = edge_counts.get(e["from"], 0) + 1
        edge_counts[e["to"]] = edge_counts.get(e["to"], 0) + 1

    years = [n["year"] for n in notes if n["year"] > 0]
    min_year = min(years) if years else 2000
    max_year = max(years) if years else 2026

    for n in notes:
        count = edge_counts.get(n["citekey"], 0)
        # 蓝色系配色: 旧文献浅蓝 → 新文献深蓝, 在浅色背景上保证对比度
        if n["year"] > 0 and max_year > min_year:
            ratio = (n["year"] - min_year) / (max_year - min_year)
        else:
            ratio = 0.5
        # HSL 蓝色系: H=210, S=50-70%, L=60%(旧/浅)→38%(新/深)
        lightness = 60 - 22 * ratio
        saturation = 50 + 20 * ratio
        color = f"hsl(210, {saturation:.0f}%, {lightness:.0f}%)"

        # 高亮当前节点
        is_focus = n["citekey"] == focus_citekey
        if is_focus:
            color = "#B8706E"  # 莫兰蒂红高亮

        # 标签: 期刊简称 + 年份
        abbr = n["citekey"].split("-")[1] if "-" in n["citekey"] else ""
        label = f"{abbr} {n['year']}" if n["year"] else abbr

        nodes_data.append({
            "id": n["citekey"],
            "label": label,
            "title": n["title"],
            "color": color,
            "size": max(12, min(35, 12 + count * 3)) if is_focus else max(8, min(30, 8 + count * 3)),
            "year": n["year"],
            "journal": n["journal"],
            "tags": n["tags"],
            "keywords": n["keywords"],
            "doi": n["doi"],
            "body_head": n["body_head"][:200],
            "isFocus": is_focus,
        })

    # 去重边
    seen_edges = set()
    unique_edges = []
    for e in edges:
        key = tuple(sorted([e["from"], e["to"]]))
        if key in seen_edges:
            continue
        seen_edges.add(key)
        unique_edges.append(e)

    nodes_json = json.dumps(nodes_data, ensure_ascii=False)
    edges_json = json.dumps(unique_edges, ensure_ascii=False)

    # 内嵌 vis.js 到 HTML, 避免 file:// 协议下浏览器拒绝加载同目录 JS
    vis_js_inline = ""
    if VIS_JS_LOCAL.exists() and VIS_JS_LOCAL.stat().st_size > 100_000:
        vis_js_inline = VIS_JS_LOCAL.read_bytes().decode("utf-8", errors="replace")
    vis_script = f"<script>{vis_js_inline}</script>" if vis_js_inline else f'<script src="{VIS_JS_CDN}"></script>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>PaperTrail 引用图谱</title>
{vis_script}
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #F4F1EC; }}
#app {{ display: flex; height: 100vh; }}
#graph {{ flex: 1; background: #F4F1EC; }}
#panel {{ width: 400px; background: #fff; border-left: 1px solid #DCE3E4; padding: 20px; overflow-y: auto; color: #3B4A54; }}
#search {{ width: 100%; padding: 10px 14px; border: 1px solid #DCE3E4; border-radius: 8px; font-size: 14px; margin-bottom: 12px; background: #F4F1EC; color: #3B4A54; outline: none; }}
#search:focus {{ border-color: #6E8CA0; }}
#search::placeholder {{ color: #A0AEBB; }}
#info h2 {{ font-size: 16px; color: #3B4A54; margin-bottom: 8px; line-height: 1.4; }}
#info .meta {{ color: #7A8A99; font-size: 13px; margin-bottom: 8px; }}
#info .meta code {{ color: #6E8CA0; background: #EDF2F5; padding: 1px 5px; border-radius: 3px; }}
#info .tags span {{ display: inline-block; background: #DCE3E4; color: #5A7A8A; border-radius: 4px; padding: 2px 8px; margin: 2px; font-size: 12px; font-weight: 600; }}
#info .keywords {{ color: #7A8A99; font-size: 12px; margin: 8px 0; }}
#info .snippet {{ color: #5A6A74; font-size: 13px; line-height: 1.6; margin-top: 10px; padding: 12px; background: #F4F1EC; border-radius: 8px; border: 1px solid #DCE3E4; }}
#info .doi a {{ color: #6E8CA0; font-size: 12px; text-decoration: none; }}
#info .doi a:hover {{ text-decoration: underline; }}
#info .back-btn {{ display: inline-block; margin-top: 14px; padding: 8px 16px; background: #6E8CA0; color: #fff; border-radius: 6px; text-decoration: none; font-size: 13px; font-weight: 600; cursor: pointer; border: none; }}
#info .back-btn:hover {{ background: #5A7A8A; }}
.empty {{ color: #A0AEBB; text-align: center; margin-top: 40px; }}
#stats {{ color: #7A8A99; font-size: 12px; margin-bottom: 12px; }}
#focus-banner {{ background: #B8706E; color: #fff; padding: 8px 14px; border-radius: 6px; margin-bottom: 12px; font-size: 13px; font-weight: 600; display: none; }}
#credit {{ position: absolute; bottom: 16px; left: 20px; color: #A0AEBB; font-size: 11px; }}
</style>
</head>
<body>
<div id="app">
  <div id="graph"></div>
  <div id="panel">
    <div id="focus-banner"></div>
    <input id="search" type="text" placeholder="搜索论文标题/关键词...">
    <div id="stats"></div>
    <div id="info"><p class="empty">点击节点查看论文详情</p></div>
  </div>
  <div id="credit">PaperTrail · Designed & built by <b>Eggy</b>, powered by Claude & Qoder</div>
</div>
<script>
const STREAMLIT_URL = '{streamlit_url}';
const FOCUS_CITEKEY = '{focus_citekey}' || (function(){{  // 优先用命令行传入, 否则读 URL hash
  var m = location.hash.match(/focus=([^&]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}})();
const nodesData = {nodes_json};
const edgesData = {edges_json};

const nodes = new vis.DataSet(nodesData);
const edges = new vis.DataSet(edgesData.map(e => ({{
  from: e.from, to: e.to,
  dashes: e.type === 'similarity',
  width: e.type === 'citation' ? 2.5 : 1.2,
  color: {{ color: e.type === 'citation' ? '#8AAEC4' : '#C8D6E0', opacity: 0.8 }},
  title: e.type + ' (' + e.value + ')'
}})));

const container = document.getElementById('graph');
const network = new vis.Network(container, {{ nodes, edges }}, {{
  physics: {{
    solver: 'forceAtlas2Based',
    forceAtlas2Based: {{ gravitationalConstant: -30, springLength: 120, springConstant: 0.04 }},
    stabilization: {{ iterations: 200 }}
  }},
  interaction: {{ hover: true, tooltipDelay: 100 }},
  nodes: {{ font: {{ size: 11, color: '#3B4A54' }}, borderWidth: 1.5, shape: 'dot', borderWidthSelected: 3,
    color: {{ highlight: {{ background: '#B8706E', border: '#9A5A58' }}, hover: {{ background: '#A0C4E0', border: '#6E8CA0' }} }}
  }},
}});

document.getElementById('stats').textContent = nodesData.length + ' 篇论文, ' + edgesData.length + ' 条关系';

// 点击节点显示详情 + 返回按钮
function showNodeInfo(id) {{
  const node = nodesData.find(n => n.id === id);
  if (!node) return;
  const tagsHtml = node.tags.map(t => '<span>' + t + '</span>').join('');
  const doiHtml = node.doi && node.doi !== 'N/A'
    ? '<div class="doi">🔗 <a href="https://doi.org/' + node.doi + '" target="_blank">' + node.doi + '</a></div>' : '';
  const backUrl = STREAMLIT_URL ? STREAMLIT_URL + '/?citekey=' + encodeURIComponent(id) : '';
  const backBtn = backUrl
    ? '<button class="back-btn" onclick="window.location.href=\\'' + backUrl + '\\'">📖 回到 PaperTrail 查看笔记</button>' : '';
  document.getElementById('info').innerHTML =
    '<h2>' + node.title + '</h2>' +
    '<div class="meta">' + (node.journal || '') + ' · ' + (node.year || '?') + ' · <code>' + id + '</code></div>' +
    '<div class="tags">' + tagsHtml + '</div>' +
    '<div class="keywords">' + node.keywords.join(', ') + '</div>' +
    doiHtml +
    '<div class="snippet">' + node.body_head + '</div>' +
    backBtn;
}}

network.on('click', function(params) {{
  if (params.nodes.length === 0) return;
  showNodeInfo(params.nodes[0]);
}});

// 如果有 focus citekey, 自动选中并显示信息
var _focusApplied = false;
function applyFocus() {{
  if (_focusApplied || !FOCUS_CITEKEY) return;
  const focusNode = nodesData.find(n => n.id === FOCUS_CITEKEY);
  if (!focusNode) return;
  _focusApplied = true;
  // 动态更新节点颜色为莫兰蒂红 + 放大
  nodes.update({{ id: FOCUS_CITEKEY, color: '#B8706E', size: Math.max(16, focusNode.size + 6) }});
  network.selectNodes([FOCUS_CITEKEY]);
  network.focus(FOCUS_CITEKEY, {{ scale: 1.2, animation: {{ duration: 800, easingFunction: 'easeInOutQuad' }} }});
  showNodeInfo(FOCUS_CITEKEY);
  const banner = document.getElementById('focus-banner');
  banner.textContent = '📍 当前论文: ' + focusNode.title;
  banner.style.display = 'block';
}}
if (FOCUS_CITEKEY) {{
  network.once('stabilizationIterationsDone', applyFocus);
  // 兆底: 事件有时不触发, 1.5 秒后强制应用
  setTimeout(applyFocus, 1500);
}}

document.getElementById('search').addEventListener('input', function() {{
  const q = this.value.toLowerCase();
  if (!q) {{
    nodes.forEach(n => nodes.update({{ id: n.id, hidden: false }}));
    return;
  }}
  nodesData.forEach(n => {{
    const match = (n.title + ' ' + n.keywords.join(' ') + ' ' + n.tags.join(' ') + ' ' + n.id).toLowerCase().includes(q);
    nodes.update({{ id: n.id, hidden: !match }});
  }});
}});
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="引用图谱: 生成论文关系网络交互式可视化")
    parser.add_argument("--use-s2", action="store_true", help="额外查 Semantic Scholar 真实引用(慢)")
    parser.add_argument("--sim-threshold", type=float, default=0.2,
                        help="TF-IDF 相似度阈值 (默认: 0.2)")
    parser.add_argument("--open", action="store_true", help="生成后自动在浏览器打开")
    parser.add_argument("--focus", type=str, default="",
                        help="高亮并选中某个 citekey 节点 (从笔记页面进来时传入)")
    parser.add_argument("--streamlit-url", type=str, default="",
                        help="PaperTrail 的 URL, 用于图谱右侧面板的'回到笔记'按钮")
    parser.add_argument("--use-cache", action="store_true",
                        help="优先从缓存加载图数据(跳过 TF-IDF 计算), 只重新生成 HTML")
    args = parser.parse_args()

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ensure_vis_js()

    # 尝试从缓存加载
    notes, all_edges = None, None
    if args.use_cache and CACHE_JSON.exists():
        try:
            cache = json.loads(CACHE_JSON.read_text(encoding="utf-8"))
            notes = cache["notes"]
            all_edges = cache["edges"]
            print(f"从缓存加载: {len(notes)} 篇笔记, {len(all_edges)} 条边")
        except Exception:
            pass

    if notes is None:
        notes = load_notes()
        if not notes:
            print("notes/ 为空。")
            return
        print(f"加载 {len(notes)} 篇笔记")

        # 1. related 字段的边
        related_edges = []
        all_citekeys = set(n["citekey"] for n in notes)
        for n in notes:
            for rel in n["related"]:
                if rel in all_citekeys:
                    related_edges.append({"from": n["citekey"], "to": rel, "value": 1, "type": "related"})
        print(f"related 字段: {len(related_edges)} 条关系")

        # 2. TF-IDF 相似度边
        print("计算 TF-IDF 相似度...")
        sim_edges = compute_tfidf_edges(notes, threshold=args.sim_threshold)
        print(f"TF-IDF 相似度: {len(sim_edges)} 条边 (阈值 ≥ {args.sim_threshold})")

        # 3. (可选) Semantic Scholar
        s2_edges = []
        if args.use_s2:
            print("查询 Semantic Scholar API (可能需要几分钟)...")
            s2_edges = fetch_s2_citations(notes)
            print(f"S2 引用关系: {len(s2_edges)} 条边")

        all_edges = related_edges + sim_edges + s2_edges

        # 保存缓存
        try:
            CACHE_JSON.write_text(json.dumps({"notes": notes, "edges": all_edges},
                                              ensure_ascii=False), encoding="utf-8")
            print(f"图数据已缓存到 {CACHE_JSON.name}")
        except Exception:
            pass

    print(f"\n总计 {len(all_edges)} 条关系边, 生成 HTML...")
    html = generate_html(notes, all_edges, focus_citekey=args.focus, streamlit_url=args.streamlit_url)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"✓ 已生成: {OUTPUT_HTML}")
    print(f"  双击文件即可在浏览器中交互浏览")

    if args.open:
        webbrowser.open(str(OUTPUT_HTML))


if __name__ == "__main__":
    main()
