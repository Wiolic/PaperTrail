#!/usr/bin/env python3
"""
语义检索——用一句模糊的中文/英文科研问题描述找相关论文，不要求关键词完全命中。

背景: 现有检索(标题/作者/期刊/tags/keywords)都是关键词匹配, 查"Ir dissolution"命中不了
写作"catalyst reconstruction"或"lattice oxygen"的论文——哪怕它们讨论的其实是同一件事。
这个脚本用 build_embedding_index.py 生成的向量索引做语义相似度检索, 不要求措辞重合。

前提: 先跑一次 `python scripts/build_embedding_index.py` 生成 embeddings/ 索引
(新增/修改笔记后重新跑一次它做增量更新, semantic_search.py 本身不会自动重建索引)。

用法:
    python scripts/semantic_search.py "酸性PEM电解槽中Ir催化剂降解机制"
    python scripts/semantic_search.py "Ir oxide degradation mechanism" --top-k 5
    python scripts/semantic_search.py "..." --explain          # 额外调deepseek-v4-flash
                                                                # 给每条结果生成一句"为什么相关"
    python scripts/semantic_search.py "..." --json              # 输出JSON而不是人读的文本

不改变 notes/ 格式、不引入数据库——排名结果里的 title 是索引构建时缓存的快照, 更权威的
信息请用 citekey 去 notes/<citekey>.md 里看。
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "notes"
EMB_DIR = ROOT / "embeddings"
INDEX_PATH = EMB_DIR / "index.faiss"
META_PATH = EMB_DIR / "metadata.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_index():
    if not INDEX_PATH.exists() or not META_PATH.exists():
        sys.exit("找不到 embeddings/index.faiss，先跑一次 `python scripts/build_embedding_index.py` 建索引。")
    import faiss
    index = faiss.read_index(str(INDEX_PATH))
    meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    return index, meta


def embed_query(query: str, model_name: str) -> np.ndarray:
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(model_name)
    vec = model.encode([query], normalize_embeddings=True, convert_to_numpy=True)
    return vec.astype(np.float32)


def get_note_field(citekey: str, field: str) -> str:
    """按需去 notes/ 读单个字段(用于 --explain 时抓摘要/关键图表数据这类判断依据),
    只在真正要用到时读, 不在批量排名阶段读全部笔记(避免不必要的磁盘IO)。"""
    import re
    path = NOTES / f"{citekey}.md"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.search(rf"^{re.escape(field)}:[ \t]*(.*)$", text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    # 正文小节(如"三句话总结")按 ## 标题 切
    m2 = re.search(rf"^## {re.escape(field)}\n(.*?)(?=\n## |\Z)", text, re.MULTILINE | re.DOTALL)
    return m2.group(1).strip() if m2 else ""


EXPLAIN_SYSTEM = (
    "你在协助判断一篇论文的摘要为什么在语义上和用户的检索问题相关。给定检索问题和候选论文的"
    "标题+三句话总结, 用一句中文话说明这篇论文和检索问题在语义上的关联点(哪怕关键词不完全"
    "重合, 也要点出内在的机理/方法/体系联系)。如果读完摘要看不出明显关联, 如实说"
    "'语义相似度较高但摘要中未见直接关联, 建议人工核实', 不要为了显得相关而牵强附会。只输出"
    "这一句话, 不要任何前后缀说明。"
)


def generate_reason(ds_module, model: str, query: str, citekey: str, title: str) -> str:
    summary = get_note_field(citekey, "三句话总结")
    client = ds_module.get_client()
    user = f"检索问题: {query}\n\n候选论文标题: {title}\n候选论文三句话总结: {summary or '(未找到)'}"
    try:
        return ds_module.call(client, model, EXPLAIN_SYSTEM, user, temperature=0, json_mode=False).strip()
    except Exception as e:
        return f"(生成理由失败: {e})"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", help="自然语言检索问题, 中英文均可")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--explain", action="store_true",
                     help="额外调用deepseek-v4-flash给每条结果生成一句'为什么相关'说明"
                          "(每条结果一次API调用, 量级很小, 成本可忽略)")
    ap.add_argument("--explain-model", default="deepseek-v4-flash",
                     help="生成'为什么相关'这句解释用的是判断性极低的短任务, 用便宜档flash足够")
    ap.add_argument("--json", action="store_true", help="输出JSON而不是人读的文本")
    args = ap.parse_args()

    index, meta = load_index()
    query_vec = embed_query(args.query, meta["model"])
    top_k = min(args.top_k, index.ntotal)
    scores, indices = index.search(query_vec, top_k)

    citekeys = meta["citekeys"]
    titles = meta.get("titles", {})

    results = []
    ds = None
    if args.explain:
        import ds as ds_module
        ds = ds_module

    for rank, (idx, score) in enumerate(zip(indices[0], scores[0]), 1):
        if idx < 0:
            continue
        citekey = citekeys[idx]
        title = titles.get(citekey, citekey)
        entry = {"rank": rank, "citekey": citekey, "title": title, "similarity": round(float(score), 4)}
        if args.explain:
            entry["reason"] = generate_reason(ds, args.explain_model, args.query, citekey, title)
        results.append(entry)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    print(f"检索问题: {args.query}\n（索引共 {index.ntotal} 篇笔记，模型: {meta['model']}）\n")
    for r in results:
        print(f"{r['rank']}.")
        print(f"citekey: {r['citekey']}")
        print(f"title: {r['title']}")
        print(f"similarity: {r['similarity']}")
        if "reason" in r:
            print(f"reason: {r['reason']}")
        print()
    if not args.explain:
        print("（未加 --explain，没有生成'为什么相关'说明；结果仅供参考，语义相似度高不等于"
              "真正相关，建议人工核对候选笔记再引用。）")


if __name__ == "__main__":
    main()
