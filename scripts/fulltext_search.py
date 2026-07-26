#!/usr/bin/env python3
"""
全文搜索——搜 notes/ 笔记正文内容(标题/作者/关键词以外的深度内容)。

现有检索只能按标题/作者/关键词/标签筛选, 笔记正文里提到的"Ir dissolution"、
"lattice oxygen mechanism"、"cryo-EM 观察到..."这类细节搜不到。
这个脚本建一个倒排索引(token → citekey 列表), 支持多关键词 AND/OR 搜索,
按命中词频排序返回结果。

用法:
    python scripts/fulltext_search.py "lattice oxygen"
    python scripts/fulltext_search.py "Ir dissolution PEM" --mode or   # 任一词命中即可
    python scripts/fulltext_search.py "operando XAS" --top 20
    python scripts/fulltext_search.py "cryo" --body-only               # 只搜正文(排除frontmatter)

依赖: 无额外依赖, 纯标准库 + re。
中文分词用简单字符 bigram 方案(不依赖 jieba), 英文用 \\w+ 正则分词。
"""
import argparse
import io
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Windows 控制台默认 GBK, 强制 UTF-8 输出避免 UnicodeEncodeError
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
NOTES_DIR = ROOT / "notes"
INDEX_CACHE = ROOT / "embeddings" / "fulltext_index.json"


def tokenize(text: str) -> list[str]:
    """简单分词: 英文按 \\w{2,} 提取, 中文按 bigram 切分。
    不用 jieba 等外部分词库, 保持零依赖。"""
    tokens = []
    # 英文词(2字符以上), 统一小写
    tokens.extend(re.findall(r"[a-zA-Z]\w{1,}", text.lower()))
    # 中文 bigram(连续中文字符每两个一组)
    cn_chars = re.findall(r"[\u4e00-\u9fff]+", text)
    for seg in cn_chars:
        for i in range(len(seg) - 1):
            tokens.append(seg[i : i + 2])
        if len(seg) == 1:
            tokens.append(seg)
    # 数字串(如年份、性能数值)
    tokens.extend(re.findall(r"\d{2,}", text))
    return tokens


def build_index(notes_dir: Path = NOTES_DIR) -> dict:
    """扫描 notes/ 下所有 .md 文件, 构建倒排索引: {token: {citekey: count}}。"""
    index = defaultdict(lambda: defaultdict(int))
    if not notes_dir.exists():
        return {}
    for f in notes_dir.glob("*.md"):
        citekey = f.stem
        text = f.read_text(encoding="utf-8", errors="replace")
        # 跳过 frontmatter(YAML 部分)
        m = re.match(r"^---\n.*?\n---\n?(.*)$", text, re.DOTALL)
        body = m.group(1) if m else text
        for token in tokenize(body):
            index[token][citekey] += 1
    return {tok: dict(ck_map) for tok, ck_map in index.items()}


def save_index(index: dict, path: Path = INDEX_CACHE):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=None), encoding="utf-8")


def load_index(path: Path = INDEX_CACHE) -> dict:
    if not path.exists():
        return build_index()
    return json.loads(path.read_text(encoding="utf-8"))


def search(query: str, index: dict, mode: str = "and", top_n: int = 10,
           notes_dir: Path = NOTES_DIR) -> list[dict]:
    """搜索倒排索引, 返回 [{citekey, score, snippet}, ...]。"""
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    # 每篇论文的累计命中分数
    scores = defaultdict(int)
    for qt in query_tokens:
        matches = index.get(qt, {})
        if mode == "and" and not matches:
            return []  # AND 模式下任一词无匹配 → 空结果
        for citekey, count in matches.items():
            scores[citekey] += count

    if mode == "and":
        # 只保留所有查询词都命中的论文
        required_tokens = set(query_tokens)
        scores = {
            ck: sc for ck, sc in scores.items()
            if all(ck in index.get(qt, {}) for qt in required_tokens)
        }

    # 按分数降序排序
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_n]

    results = []
    for citekey, score in ranked:
        # 生成片段(snippet): 找正文中包含查询词的句子
        note_path = notes_dir / f"{citekey}.md"
        snippet = ""
        if note_path.exists():
            text = note_path.read_text(encoding="utf-8", errors="replace")
            m = re.match(r"^---\n.*?\n---\n?(.*)$", text, re.DOTALL)
            body = m.group(1) if m else text
            # 找包含查询词的上下文(前后各50字符)
            for qt in query_tokens:
                pattern = re.escape(qt)
                match = re.search(pattern, body, re.IGNORECASE)
                if match:
                    start = max(0, match.start() - 40)
                    end = min(len(body), match.end() + 60)
                    snippet = "..." + body[start:end].replace("\n", " ").strip() + "..."
                    break

        # 从 frontmatter 读标题
        title = citekey
        if note_path.exists():
            text = note_path.read_text(encoding="utf-8", errors="replace")
            tm = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
            if tm:
                title = tm.group(1).strip()

        results.append({"citekey": citekey, "score": score, "title": title, "snippet": snippet})

    return results


def main():
    parser = argparse.ArgumentParser(description="全文搜索 notes/ 笔记正文")
    parser.add_argument("query", help="搜索关键词(空格分隔多个)")
    parser.add_argument("--mode", choices=["and", "or"], default="and",
                        help="and=所有词都命中才返回, or=任一词命中即可 (默认: and)")
    parser.add_argument("--top", type=int, default=15, help="返回前 N 条结果 (默认: 15)")
    parser.add_argument("--rebuild", action="store_true", help="强制重建索引(否则使用缓存)")
    parser.add_argument("--json", action="store_true", dest="json_out", help="输出 JSON 格式")
    args = parser.parse_args()

    if args.rebuild or not INDEX_CACHE.exists():
        print("正在构建全文索引...", file=sys.stderr)
        index = build_index()
        save_index(index)
        print(f"索引构建完成, {len(index)} 个 token", file=sys.stderr)
    else:
        index = load_index()

    results = search(args.query, index, mode=args.mode, top_n=args.top)

    if args.json_out:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        if not results:
            print(f"未找到包含 \"{args.query}\" 的笔记")
        else:
            print(f"找到 {len(results)} 篇匹配 (查询: \"{args.query}\", 模式: {args.mode}):\n")
            for r in results:
                print(f"  [{r['score']:>3}] {r['citekey']}")
                print(f"        {r['title']}")
                if r['snippet']:
                    print(f"        {r['snippet']}")
                print()


if __name__ == "__main__":
    main()
