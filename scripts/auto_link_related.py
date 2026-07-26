#!/usr/bin/env python3
"""
自动关联相似论文——基于 TF-IDF 计算论文间的文本相似度, 为 related 字段为空的论文
填充 top-N 最相似的论文。让论文关系网自动浮现, 不用手动逐篇填 related。

相似度特征来源: 标题 + keywords + tags + 正文前 500 字符。
纯标题相似度权重更高(keywords/tags 是人工标注的, 质量高于正文 TF-IDF)。

用法:
    python scripts/auto_link_related.py               # 预览模式, 只看会填哪些
    python scripts/auto_link_related.py --apply        # 真正写入 notes/
    python scripts/auto_link_related.py --top 3        # 每篇只关联 top 3 相似论文(默认 5)
    python scripts/auto_link_related.py --threshold 0.3 # 相似度低于 0.3 的不关联
    python scripts/auto_link_related.py --fill-only    # 只填 related 为空的论文(不覆盖已有)
"""
import argparse
import io
import json
import re
import sys
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = ROOT / "notes"


def load_notes() -> list[dict]:
    """加载所有笔记, 返回 [{citekey, title, keywords, tags, body_head, related, path}, ...]。"""
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

        related = _list_field("related")
        notes.append({
            "citekey": f.stem,
            "title": _field("title"),
            "keywords": _list_field("keywords"),
            "tags": _list_field("tags"),
            "body_head": body[:500],
            "related": related,
            "path": f,
        })
    return notes


def build_feature_text(note: dict) -> str:
    """拼一篇论文的特征文本, 用于 TF-IDF 向量化。标题和关键词重复以增加权重。"""
    parts = []
    # 标题重复 3 次(权重最高)
    if note["title"]:
        parts.extend([note["title"]] * 3)
    # keywords 重复 2 次
    parts.extend(note["keywords"] * 2)
    # tags 重复 2 次
    parts.extend(note["tags"] * 2)
    # 正文前 500 字符(只取一次)
    parts.append(note["body_head"])
    return " ".join(parts)


def compute_similarities(notes: list[dict]) -> list[list[tuple[str, float]]]:
    """用 sklearn TF-IDF 计算余弦相似度矩阵, 返回每篇论文的 top-N 相似列表。"""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    texts = [build_feature_text(n) for n in notes]
    vectorizer = TfidfVectorizer(
        max_features=5000,
        ngram_range=(1, 2),
        stop_words="english",
        lowercase=True,
    )
    tfidf_matrix = vectorizer.fit_transform(texts)
    sim_matrix = cosine_similarity(tfidf_matrix)
    return sim_matrix


def update_note_related(note_path: Path, related_list: list[str]):
    """更新笔记 frontmatter 中的 related 字段。"""
    text = note_path.read_text(encoding="utf-8")
    related_str = "[" + ", ".join(f'"{ck}"' for ck in related_list) + "]"
    # 替换现有 related 行
    new_text, count = re.subn(
        r"^related:\s*.*$",
        f"related: {related_str}",
        text, count=1, flags=re.MULTILINE,
    )
    if count == 0:
        # 没有 related 字段, 在 added 行后插入
        new_text = re.sub(
            r"^(added:.+)$",
            rf"\1\nrelated: {related_str}",
            text, count=1, flags=re.MULTILINE,
        )
    note_path.write_text(new_text, encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description="自动关联相似论文(TF-IDF 余弦相似度)")
    parser.add_argument("--apply", action="store_true", help="真正写入 notes/(默认预览)")
    parser.add_argument("--top", type=int, default=5, help="每篇关联 top N 相似论文 (默认: 5)")
    parser.add_argument("--threshold", type=float, default=0.15,
                        help="相似度低于此值的不关联 (默认: 0.15)")
    parser.add_argument("--fill-only", action="store_true",
                        help="只填 related 为空的论文(不覆盖已有的)")
    args = parser.parse_args()

    notes = load_notes()
    if not notes:
        print("notes/ 为空。")
        return

    print(f"加载 {len(notes)} 篇笔记, 计算 TF-IDF 相似度矩阵...")
    sim_matrix = compute_similarities(notes)
    print(f"计算完成。\n")

    citekey_to_idx = {n["citekey"]: i for i, n in enumerate(notes)}
    updated, skipped = 0, 0

    for i, note in enumerate(notes):
        if args.fill_only and note["related"]:
            skipped += 1
            continue

        # 排除自己, 按相似度降序
        sims = [(j, sim_matrix[i][j]) for j in range(len(notes)) if j != i]
        sims.sort(key=lambda x: -x[1])
        top_sims = [(notes[j]["citekey"], score) for j, score in sims[:args.top] if score >= args.threshold]

        if not top_sims:
            continue

        new_related = [ck for ck, _ in top_sims]
        old_related = note["related"]

        # 如果新结果和已有的一样, 跳过
        if set(new_related) == set(old_related):
            continue

        updated += 1
        print(f"  {note['citekey']}")
        if old_related:
            print(f"    旧: {old_related}")
        print(f"    新: {new_related}")
        for ck, score in top_sims:
            print(f"      → {ck} (相似度: {score:.3f})")
        print()

        if args.apply:
            update_note_related(note["path"], new_related)

    print(f"=== 汇总 ===")
    print(f"总论文数:     {len(notes)}")
    print(f"需要更新:     {updated}")
    print(f"跳过(已有):   {skipped}")
    if not args.apply and updated:
        print(f"\n预览模式, 以上 {updated} 篇未写入。加 --apply 真正写入。")


if __name__ == "__main__":
    main()
