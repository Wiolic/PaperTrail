#!/usr/bin/env python3
"""搜索同义词展开工具: 输入一个关键词, 自动展开为同义词组, 全库搜索匹配的笔记。

用法:
  python scripts/expand_search.py "Ir degradation"
  python scripts/expand_search.py "proton transport" --dry-run
  python scripts/expand_search.py DFT --field keywords

功能:
  1. 查 vocab/scientific_synonyms.yaml 找到输入词的同义词组
  2. 用所有同义词在 notes/*.md 的 frontmatter 里搜索 (keywords/tags/表征方法)
  3. 返回匹配的 citekey 列表 (去重)

依赖: PyYAML (pip install pyyaml)
"""
import argparse
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

try:
    import yaml
except ImportError:
    print("ERROR: 需要 PyYAML. 运行: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

VOCAB_PATH = Path(__file__).resolve().parent.parent / "vocab" / "scientific_synonyms.yaml"
NOTES_DIR = Path(__file__).resolve().parent.parent / "notes"


def load_vocab() -> dict:
    """加载同义词库, 返回 {任意写法 -> 条目} 的快速查找表。"""
    with open(VOCAB_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    lookup = {}
    for _key, entry in raw.items():
        canonical = entry.get("canonical", "").strip().lower()
        if canonical:
            lookup[canonical] = entry
        for syn in entry.get("synonyms", []):
            lookup[syn.strip().lower()] = entry
    return lookup


def expand_query(query: str, vocab: dict) -> tuple[list[str], dict | None]:
    """展开查询词为同义词列表。返回 (所有搜索词, 匹配的vocab条目)。"""
    q_lower = query.strip().lower()
    entry = vocab.get(q_lower)
    if not entry:
        # 尝试部分匹配
        for key, e in vocab.items():
            if q_lower in key or key in q_lower:
                entry = e
                break
    if not entry:
        return [query], None

    terms = [entry["canonical"]]
    terms.extend(entry.get("synonyms", []))
    return terms, entry


def parse_frontmatter(filepath: Path) -> dict:
    """简易 YAML frontmatter 解析, 提取 keywords/tags/表征方法。"""
    text = filepath.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end < 0:
        return {}
    fm_text = text[3:end]

    result = {}
    # 提取简单字段 (行内数组或标量)
    for line in fm_text.split("\n"):
        line = line.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if key in ("keywords", "tags", "表征方法"):
            # 行内数组 [a, b, c]
            if val.startswith("["):
                items = [x.strip().strip("'\"") for x in val.strip("[]").split(",") if x.strip()]
                result[key] = items
            else:
                result[key] = [val] if val else []
        elif key == "citekey":
            result["citekey"] = val
        elif key == "title":
            result["title"] = val
    return result


def search_notes(terms: list[str], fields: list[str] | None = None) -> list[dict]:
    """在 notes/ 中搜索包含任一搜索词的笔记。"""
    if fields is None:
        fields = ["keywords", "tags", "表征方法"]

    terms_lower = [t.lower() for t in terms]
    matches = []
    seen = set()

    for f in sorted(NOTES_DIR.glob("*.md")):
        fm = parse_frontmatter(f)
        if not fm:
            continue
        citekey = fm.get("citekey", f.stem)
        if citekey in seen:
            continue

        matched_terms = set()
        for field in fields:
            values = fm.get(field, [])
            for v in values:
                v_lower = v.lower()
                for term in terms_lower:
                    if term in v_lower or v_lower in term:
                        matched_terms.add(term)
                        break

        if matched_terms:
            seen.add(citekey)
            matches.append({
                "citekey": citekey,
                "title": fm.get("title", ""),
                "matched_terms": sorted(matched_terms),
            })

    return matches


def main():
    parser = argparse.ArgumentParser(description="搜索同义词展开工具")
    parser.add_argument("query", help="搜索词 (如 'Ir degradation', 'proton transport', 'DFT')")
    parser.add_argument("--dry-run", action="store_true", help="只展示展开的词组, 不搜索")
    parser.add_argument("--field", nargs="*", help="搜索哪些字段 (默认: keywords tags 表征方法)")
    parser.add_argument("--format", choices=["list", "table"], default="table", help="输出格式")
    args = parser.parse_args()

    vocab = load_vocab()
    terms, entry = expand_query(args.query, vocab)

    print(f"\n🔍 搜索: {args.query}")
    if entry:
        print(f"📚 同义词组: {entry['canonical']}")
        print(f"   展开为: {', '.join(terms)}")
        related = entry.get("related", [])
        if related:
            print(f"   相关概念: {', '.join(related)}")
    else:
        print("⚠️  未找到同义词组, 使用原词搜索")

    if args.dry_run:
        print("\n✅ --dry-run 模式, 不执行搜索")
        return

    matches = search_notes(terms, args.field)
    print(f"\n📄 找到 {len(matches)} 篇匹配文献:\n")

    if args.format == "table":
        print(f"{'citekey':<55} {'匹配词'}")
        print("-" * 90)
        for m in matches:
            print(f"{m['citekey']:<55} {', '.join(m['matched_terms'])}")
    else:
        for m in matches:
            print(f"  - {m['citekey']}  ({', '.join(m['matched_terms'])})")

    # 统计
    all_terms_hit = set()
    for m in matches:
        all_terms_hit.update(m["matched_terms"])
    print(f"\n📊 统计: {len(matches)} 篇文献, 覆盖 {len(all_terms_hit)}/{len(terms)} 个搜索词")
    missing = set(t.lower() for t in terms) - set(x.lower() for x in all_terms_hit)
    if missing:
        print(f"   未命中的词: {', '.join(sorted(missing))}")


if __name__ == "__main__":
    main()
