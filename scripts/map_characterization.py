#!/usr/bin/env python3
"""表征方法词表归一化: 把 notes/*.md frontmatter 里的表征方法自由文本映射到 vocab/characterization.yaml
规范写法, 消除 "Raman"/"Raman spectroscopy"/"operando XRD"/"Operando X-ray diffraction" 这类漂移
(见 CLAUDE.md 反馈第4条)。

原理: 纯词表查找(精确匹配规范名或别名, 大小写不敏感), 不调用 DeepSeek——这是确定性映射问题,
LLM 反而会引入新的不一致。"in situ X"/"operando X"/"ex situ X" 前缀先剥离、单独归一化(小写+空格),
剩余部分查表, 查到后把前缀原样拼回去, 绝不合并 in situ 和 operando(是不同实验条件)。

映射不上的词条: 保留原文, 加 "unmapped:" 前缀写回笔记(不阻塞/不丢失信息), 同时汇总到
vocab/unmapped_characterization.md 供人工决定是扩充词表还是论文写法本身有问题。

用法:
    python scripts/map_characterization.py            # 处理全部 notes/*.md, 直接改写
    python scripts/map_characterization.py --dry-run  # 只打印会改动什么, 不写入
"""
import argparse
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "notes"
VOCAB_FILE = ROOT / "vocab" / "characterization.yaml"
UNMAPPED_FILE = ROOT / "vocab" / "unmapped_characterization.md"

PREFIXES = ["in situ", "operando", "ex situ"]


def load_alias_map() -> dict:
    vocab = yaml.safe_load(VOCAB_FILE.read_text(encoding="utf-8"))
    alias_map = {}
    for canonical, spec in vocab.items():
        alias_map[canonical.lower()] = canonical
        for alias in (spec or {}).get("aliases") or []:
            alias_map[alias.lower()] = canonical
    return alias_map


def split_prefix(term: str):
    low = term.strip().lower()
    for p in PREFIXES:
        if low.startswith(p + " ") or low.startswith(p + "-"):
            rest = term.strip()[len(p):].lstrip(" -")
            return p, rest
    return None, term.strip()


def map_term(term: str, alias_map: dict) -> tuple[str, bool]:
    """返回 (映射后的字符串, 是否成功映射)。"""
    prefix, base = split_prefix(term)
    canonical = alias_map.get(base.lower())
    if canonical is None:
        return (f"unmapped:{term.strip()}", False)
    result = f"{prefix} {canonical}" if prefix else canonical
    return (result, True)


FRONTMATTER_LIST_RE = re.compile(r'^表征方法:\s*\[(.*)\]\s*$', re.M)


def parse_list_line(inner: str) -> list[str]:
    if not inner.strip():
        return []
    # 笔记里的表征方法是简单逗号分隔, 个别项可能带引号(含逗号的极少见, 目前没有), 按逗号切足够
    return [t.strip().strip('"').strip() for t in inner.split(",") if t.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    alias_map = load_alias_map()
    unmapped_seen = set()
    changed_files = 0
    total_terms = 0
    total_mapped = 0

    for note_path in sorted(NOTES.glob("*.md")):
        text = note_path.read_text(encoding="utf-8")
        m = FRONTMATTER_LIST_RE.search(text)
        if not m:
            continue
        terms = parse_list_line(m.group(1))
        if not terms:
            continue

        new_terms = []
        file_changed = False
        for t in terms:
            total_terms += 1
            mapped, ok = map_term(t, alias_map)
            if ok:
                total_mapped += 1
            else:
                unmapped_seen.add(t.strip())
            if mapped != t:
                file_changed = True
            new_terms.append(mapped)

        if file_changed:
            changed_files += 1
            new_line = "表征方法: [" + ", ".join(new_terms) + "]"
            if args.dry_run:
                print(f"[{note_path.stem}] {terms} -> {new_terms}")
            else:
                new_text = FRONTMATTER_LIST_RE.sub(new_line.replace("\\", "\\\\"), text, count=1)
                note_path.write_text(new_text, encoding="utf-8", newline="\n")

    print(f"\n共扫描 {sum(1 for _ in NOTES.glob('*.md'))} 篇笔记, {total_terms} 个表征方法词条, "
          f"{total_mapped} 个成功映射到规范词表, {len(unmapped_seen)} 个未映射(词表可能需要扩充)。")
    print(f"{'(dry-run, 未写入)' if args.dry_run else f'实际改写 {changed_files} 篇笔记文件'}")

    if unmapped_seen:
        if not args.dry_run:
            ROOT.joinpath("vocab").mkdir(exist_ok=True)
            existing = set()
            if UNMAPPED_FILE.exists():
                existing = {l.strip("- ").strip() for l in UNMAPPED_FILE.read_text(encoding="utf-8").splitlines() if l.startswith("-")}
            all_unmapped = sorted(existing | unmapped_seen)
            UNMAPPED_FILE.write_text(
                "# 未能映射到 vocab/characterization.yaml 的表征方法词条\n\n"
                "人工核实后要么把规范写法加进词表(连同同义词), 要么这里保留说明这确实是词表没覆盖的技术。\n\n"
                + "\n".join(f"- {t}" for t in all_unmapped) + "\n",
                encoding="utf-8", newline="\n",
            )
        print("\n未映射词条:")
        for t in sorted(unmapped_seen):
            print(f"  - {t}")


if __name__ == "__main__":
    main()
