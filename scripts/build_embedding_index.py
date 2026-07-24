#!/usr/bin/env python3
"""
语义检索——嵌入索引构建脚本。给 `semantic_search.py` 提供底层的 FAISS 向量索引。

设计原则(与 notes/ 数据格式的关系):
1. notes/ 是唯一真源, 本脚本只读不改。
2. 不修改 notes/ 的 Markdown/frontmatter 结构, embedding 是从这份真源派生出来的附属产物。
3. 不引入数据库当主存储——索引本身是 embeddings/ 目录下的几个文件(FAISS索引 + 一个
   记录citekey映射和mtime的JSON + 一份embedding向量的npy缓存), 删掉整个 embeddings/
   目录、重新跑一次这个脚本就能完全重建, 不丢失任何"真"数据。

增量更新怎么做的: FAISS 本身不太适合"原地更新单个向量"(尤其是要删除/替换的场景),
但从一批已经算好的向量重建一个 IndexFlatIP 几乎是瞬间的事(几千篇论文量级)。所以真正
"增量"的地方是**只有变了的笔记才重新调用 embedding 模型**(通过对比 notes/*.md 的
mtime 和上次记录的 mtime 判断)，未变化的笔记直接复用上次算好、缓存在 vectors.npy
里的向量——重新构建 FAISS 索引这一步永远是全量的(但这一步很快, 不是瓶颈)。

用法:
    python scripts/build_embedding_index.py            # 增量更新(默认)
    python scripts/build_embedding_index.py --force     # 忽略缓存, 全部重新生成embedding

模型: 用本地的 sentence-transformers 多语言模型(不调云端API, 不产生费用, 也符合"不引入
数据库/不依赖外部服务"的原则), 因为库里中英文混杂(标题多为英文, 正文摘要多为中文夹术语),
需要一个真正的多语言模型而不是纯英文模型。
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
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "notes"
EMB_DIR = ROOT / "embeddings"
INDEX_PATH = EMB_DIR / "index.faiss"
META_PATH = EMB_DIR / "metadata.json"
VECTORS_PATH = EMB_DIR / "vectors.npy"

# 多语言模型: 库里标题多为英文、正文摘要多为中文夹术语, 需要真正支持中英双语的模型,
# 而不是纯英文的 all-MiniLM 系列。体积和速度都适合本地CPU跑, 不需要GPU。
MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
EMBED_DIM = 384


def read_frontmatter(text: str) -> tuple[dict, str]:
    """极简 frontmatter 解析, 只取本脚本需要的几个字段, 不依赖PyYAML(库里的frontmatter
    是手写体, 不一定是严格合法YAML, 用正则更宽容, 和 batch_ingest.py/app.py 的做法一致)。"""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    fm_text, body = m.groups()

    def field(name: str) -> str:
        fm = re.search(rf"^{re.escape(name)}:[ \t]*(.*)$", fm_text, re.MULTILINE)
        return fm.group(1).strip() if fm else ""

    def list_field(name: str) -> list:
        raw = field(name)
        if raw.startswith("[") and raw.endswith("]"):
            return [x.strip().strip('"').strip("'") for x in raw[1:-1].split(",") if x.strip()]
        return []

    return {
        "title": field("title"),
        "tags": list_field("tags"),
        "keywords": list_field("keywords"),
    }, body.strip()


def build_embedding_text(fm: dict, body: str) -> str:
    """拼出喂给embedding模型的文本。把最有信息量的字段(标题/标签/关键词)放在最前面——
    多语言MiniLM这类小模型的max_seq_length通常只有128 token左右, 正文一长就会被截断,
    优先保证这几个高信息密度的结构化字段不会被截掉。"""
    parts = [fm.get("title", "")]
    if fm.get("tags"):
        parts.append("标签: " + ", ".join(fm["tags"]))
    if fm.get("keywords"):
        parts.append("关键词: " + ", ".join(fm["keywords"]))
    parts.append(body)
    return "\n".join(p for p in parts if p)


def load_existing_state():
    if META_PATH.exists() and VECTORS_PATH.exists():
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        vectors = np.load(VECTORS_PATH)
        return meta, vectors
    return None, None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="忽略mtime缓存, 全部重新生成embedding")
    args = ap.parse_args()

    if not NOTES.exists():
        sys.exit(f"notes/ 目录不存在: {NOTES}")

    note_files = sorted(NOTES.glob("*.md"))
    if not note_files:
        sys.exit("notes/ 里没有笔记, 无事可做")

    old_meta, old_vectors = (None, None) if args.force else load_existing_state()
    old_mtimes = (old_meta or {}).get("mtimes", {})
    old_citekeys = (old_meta or {}).get("citekeys", [])
    old_vec_by_citekey = (
        {ck: old_vectors[i] for i, ck in enumerate(old_citekeys)}
        if old_vectors is not None else {}
    )

    current_citekeys = [f.stem for f in note_files]
    current_mtimes = {f.stem: f.stat().st_mtime for f in note_files}

    to_embed = []  # [(citekey, text)]
    reused = 0
    for f in note_files:
        ck = f.stem
        if not args.force and old_mtimes.get(ck) == current_mtimes[ck] and ck in old_vec_by_citekey:
            reused += 1
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        fm, body = read_frontmatter(text)
        to_embed.append((ck, build_embedding_text(fm, body)))

    removed = set(old_citekeys) - set(current_citekeys)

    print(f"notes/ 共 {len(current_citekeys)} 篇；{reused} 篇复用缓存向量、"
          f"{len(to_embed)} 篇需要{'重新' if args.force else ''}生成embedding、"
          f"{len(removed)} 篇已从库里移除。")

    if to_embed:
        print(f"加载模型 {MODEL_NAME}(首次运行会从HuggingFace下载, 之后走本地缓存)...")
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(MODEL_NAME)
        t0 = time.time()
        texts = [t for _, t in to_embed]
        new_vecs = model.encode(texts, show_progress_bar=len(texts) > 20,
                                 normalize_embeddings=True, convert_to_numpy=True)
        print(f"生成 {len(to_embed)} 条embedding用时 {time.time() - t0:.1f}s")
    else:
        new_vecs = np.zeros((0, EMBED_DIM), dtype=np.float32)

    new_vec_by_citekey = dict(old_vec_by_citekey)
    for ck in removed:
        new_vec_by_citekey.pop(ck, None)
    for (ck, _), vec in zip(to_embed, new_vecs):
        new_vec_by_citekey[ck] = vec

    # 最终顺序统一按 notes/ 目录当前的排序, 保证 citekeys[i] <-> vectors[i] 的下标关系
    # 每次都是从磁盘当前状态重新推导出来的, 不依赖历史顺序。
    final_citekeys = current_citekeys
    final_vectors = np.stack([np.asarray(new_vec_by_citekey[ck], dtype=np.float32)
                               for ck in final_citekeys])

    import faiss
    dim = final_vectors.shape[1]
    index = faiss.IndexFlatIP(dim)  # 向量已归一化, 内积等价于余弦相似度
    index.add(final_vectors)

    EMB_DIR.mkdir(exist_ok=True)
    faiss.write_index(index, str(INDEX_PATH))
    np.save(VECTORS_PATH, final_vectors)

    # metadata里额外存一份title快照, 供 semantic_search.py 展示结果时不用重新读notes/
    # (纯展示用途的cosmetic缓存, 不是真源；notes/里的title万一之后变了, 只是显示会
    # 暂时不同步, 下次rebuild自然更新, 不影响正确性)。
    titles = {}
    for f in note_files:
        fm, _ = read_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        titles[f.stem] = fm.get("title", f.stem)

    META_PATH.write_text(json.dumps({
        "model": MODEL_NAME,
        "dim": dim,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "citekeys": final_citekeys,
        "mtimes": current_mtimes,
        "titles": titles,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"已写入 {INDEX_PATH.relative_to(ROOT)}（共 {len(final_citekeys)} 篇）。")


if __name__ == "__main__":
    main()
