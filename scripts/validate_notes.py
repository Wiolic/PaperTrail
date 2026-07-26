#!/usr/bin/env python3
"""
全库笔记格式校验——扫描 notes/ 下所有 .md 文件, 检查 YAML frontmatter 的必填字段、
类型、枚举值等, 输出问题清单。体检(check_library.sh)可以调用这个脚本做深度校验。

检查项:
- 必填字段: citekey, title, authors, year, journal, doi, tags, keywords, 类型, 方法关键词, 体系, status, added
- 字段类型: year=int, tags=list, keywords=list, 表征方法=list, si_files=list, authors_full=list
- 枚举值: status in (unread, skimmed, read), 类型 in (计算, 实验, 计算+实验, 建模, 综述)
- 格式: added=YYYY-MM-DD, doi 非空
- keywords 数量: 5~10(空数组标记警告)

用法:
    python scripts/validate_notes.py              # 扫全库, 输出问题
    python scripts/validate_notes.py --strict     # 严格模式(连 warnings 也算失败)
    python scripts/validate_notes.py --json       # 输出 JSON 格式(供其他脚本消费)
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

REQUIRED_FIELDS = [
    "citekey", "title", "authors", "year", "journal", "doi",
    "tags", "keywords", "类型", "方法关键词", "体系", "status", "added",
]
LIST_FIELDS = ["tags", "keywords", "表征方法", "si_files", "authors_full"]
VALID_STATUS = {"unread", "skimmed", "read"}
VALID_TYPE = {"计算", "实验", "计算+实验", "建模", "综述"}


def parse_frontmatter(text: str) -> dict | None:
    """简单解析 YAML frontmatter(不用 pyyaml, 避免额外依赖)。"""
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None
    fm_text = m.group(1)
    fields = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # key: value 或 key: [list]
        km = re.match(r"^(\w[\w\u4e00-\u9fff]*)\s*:\s*(.+)$", line)
        if km:
            key, val = km.group(1), km.group(2).strip()
            fields[key] = val
    return fields


def validate_note(citekey: str, fm: dict) -> list[dict]:
    """校验一篇笔记的 frontmatter, 返回 [{level, field, message}, ...]。
    level: 'error' 或 'warning'。"""
    issues = []

    # 必填字段
    for field in REQUIRED_FIELDS:
        if field not in fm or not fm[field]:
            issues.append({"level": "error", "field": field, "message": f"缺少必填字段: {field}"})

    # 列表字段格式(应该以 [ 开头)
    for field in LIST_FIELDS:
        if field in fm:
            val = fm[field]
            if not (val.startswith("[") and val.endswith("]")):
                issues.append({"level": "error", "field": field, "message": f"应为列表格式 [a, b, c], 实际: {val[:50]}"})

    # year 类型检查
    if "year" in fm:
        try:
            int(fm["year"])
        except ValueError:
            issues.append({"level": "error", "field": "year", "message": f"year 应为整数, 实际: {fm['year']}"})

    # status 枚举
    if "status" in fm and fm["status"] not in VALID_STATUS:
        issues.append({"level": "error", "field": "status", "message": f"status 应为 {VALID_STATUS}, 实际: {fm['status']}"})

    # 类型 枚举
    if "类型" in fm and fm["类型"] not in VALID_TYPE:
        issues.append({"level": "warning", "field": "类型", "message": f"类型 应为 {VALID_TYPE}, 实际: {fm['类型']}"})

    # added 日期格式
    if "added" in fm:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", fm["added"]):
            issues.append({"level": "error", "field": "added", "message": f"added 应为 YYYY-MM-DD, 实际: {fm['added']}"})

    # keywords 数量检查
    if "keywords" in fm:
        val = fm["keywords"]
        if val.startswith("[") and val.endswith("]"):
            items = [x.strip().strip('"').strip("'") for x in val[1:-1].split(",") if x.strip()]
            if len(items) == 0:
                issues.append({"level": "warning", "field": "keywords", "message": "keywords 为空数组, 建议填 5~10 个"})
            elif len(items) < 5:
                issues.append({"level": "warning", "field": "keywords", "message": f"keywords 只有 {len(items)} 个, 建议 5~10 个"})

    # doi 检查(不是 N/A 但也太短)
    if "doi" in fm:
        doi = fm["doi"].strip()
        if doi and doi.lower() not in ("n/a", "na", "unknown") and len(doi) < 8:
            issues.append({"level": "warning", "field": "doi", "message": f"DOI 看起来太短: {doi}"})

    return issues


def main():
    parser = argparse.ArgumentParser(description="全库笔记格式校验")
    parser.add_argument("--strict", action="store_true", help="严格模式(warnings 也算失败)")
    parser.add_argument("--json", action="store_true", dest="json_out", help="输出 JSON 格式")
    args = parser.parse_args()

    if not NOTES_DIR.exists():
        print(f"notes/ 目录不存在: {NOTES_DIR}")
        sys.exit(1)

    all_files = sorted(NOTES_DIR.glob("*.md"))
    print(f"扫描 {len(all_files)} 篇笔记...\n")

    total_errors = 0
    total_warnings = 0
    report = {}

    for f in all_files:
        citekey = f.stem
        text = f.read_text(encoding="utf-8", errors="replace")
        fm = parse_frontmatter(text)
        if fm is None:
            issues = [{"level": "error", "field": "(file)", "message": "无法解析 frontmatter"}]
        else:
            issues = validate_note(citekey, fm)

        errors = [i for i in issues if i["level"] == "error"]
        warnings = [i for i in issues if i["level"] == "warning"]
        total_errors += len(errors)
        total_warnings += len(warnings)

        if issues:
            report[citekey] = issues
            if not args.json_out:
                print(f"  {citekey}")
                for issue in issues:
                    marker = "✗" if issue["level"] == "error" else "⚠"
                    print(f"    {marker} [{issue['field']}] {issue['message']}")
                print()

    if args.json_out:
        summary = {
            "total_files": len(all_files),
            "files_with_issues": len(report),
            "total_errors": total_errors,
            "total_warnings": total_warnings,
            "issues": report,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"=== 校验汇总 ===")
        print(f"总文件数:   {len(all_files)}")
        print(f"有问题文件: {len(report)}")
        print(f"错误(error):   {total_errors}")
        print(f"警告(warning): {total_warnings}")
        if total_errors == 0 and (total_warnings == 0 or not args.strict):
            print("\n✓ 全库格式校验通过" + ("(严格模式)" if args.strict else ""))
        else:
            print(f"\n✗ 发现 {total_errors} 个错误" + (f", {total_warnings} 个警告" if total_warnings else ""))

    exit_code = 1 if total_errors > 0 else (1 if args.strict and total_warnings > 0 else 0)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
