#!/usr/bin/env python3
"""一次性迁移: 把已入库文献的 citekey 从旧格式(<姓><年份>_<短标签>)
改成新格式(<年份>-<期刊简称>-<提炼标题>), 见 AGENTS.md citekey 规则(2026-07-17改版)。

对每个旧 citekey, 用已有的 title+journal(不重新读PDF全文)调 DeepSeek 生成 journal_abbr
和 condensed_title, 拼出新 citekey, 然后:
  - papers/<old>.pdf -> papers/<new>.pdf (若有 papers/<old>_SI.* 一并改名)
  - notes/<old>.md -> notes/<new>.md, 更新内部 citekey/si_files 字段
  - library.bib 的 @article{<old>, -> @article{<new>,
  - INDEX.md 对应行的 citekey 列
  - .ingest_state.json 里记录的 citekey 字段(仅信息性, 不影响去重逻辑)
最后重跑 render_readable_notes.py 和 build_keyword_index.sh 重新生成 notes-readable/ 和 KEYWORDS.md。

用法:
    python scripts/rename_citekeys.py            # 只打印映射表(旧->新), 不落盘
    python scripts/rename_citekeys.py --apply     # 真正执行改名
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_ingest as b

from openai import OpenAI

RENAME_PROMPT = """给你一篇论文的标题和期刊名, 生成用于文件命名的两个字段。只输出一个 JSON 对象:
{
  "journal_abbr": "无空格无句点的期刊简称。业界本来就是全大写缩写的期刊保持全大写(JACS/PNAS/EES/ACIE);
     其余用驼峰式截断(ACS Nano→ACSNano, Nature Materials→NatMater, Nature Communications→NatCommun,
     Advanced Materials→AdvMater, Chemical Communications→ChemComm, Nano Lett.→NanoLett,
     ACS Catalysis→ACSCatal); 没见过的期刊按同样规则自己合理简写",
  "condensed_title": "3~6个英文单词提炼标题核心内容, 连字符连接。化学元素符号(Ir/Co/Ru/Ti/Mn等)和
     领域缩写(OER/HER/PEM/PEMWE/AEM/AEMFC/TEM/SEM/DFT/AIMD/XPS/EELS/STEM/MEA/CCM等)必须按规范
     大小写书写(全大写或化学式专有大小写, 不要写成全小写); 其余普通单词 Title Case(首字母大写)"
}
只根据给定标题和期刊判断, 不要编造与标题无关的内容。"""


def parse_bib_entries() -> dict:
    """返回 {citekey: {title, journal, year}}"""
    text = b.BIB.read_text(encoding="utf-8") if b.BIB.exists() else ""
    entries = {}
    for m in re.finditer(
        r"@article\{([^,]+),\s*title\s*=\s*\{([^}]*)\},\s*author\s*=\s*\{[^}]*\},\s*"
        r"journal\s*=\s*\{([^}]*)\},\s*year\s*=\s*\{([^}]*)\}",
        text, re.S,
    ):
        entries[m.group(1)] = {"title": m.group(2), "journal": m.group(3), "year": m.group(4)}
    return entries


def get_new_fields(client, model, title, journal) -> dict:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": RENAME_PROMPT},
            {"role": "user", "content": f"标题: {title}\n期刊: {journal}"},
        ],
        temperature=0, response_format={"type": "json_object"}, stream=False,
    )
    return json.loads(resp.choices[0].message.content)


def rename_everywhere(old: str, new: str):
    # papers/ (主文件 + SI)
    old_pdf = b.PAPERS / f"{old}.pdf"
    if old_pdf.exists():
        old_pdf.rename(b.PAPERS / f"{new}.pdf")
    # 注意: glob 用 shell 通配语法, 不能对 old 做 re.escape(会把 - 转成 \- 导致匹配不到, 静默漏掉 SI)
    for si in b.PAPERS.glob(f"{old}_SI.*"):
        si.rename(b.PAPERS / si.name.replace(old, new, 1))

    # notes/ (更新 citekey 字段和 si_files 里的文件名引用后再改名)
    old_note = b.NOTES / f"{old}.md"
    if old_note.exists():
        text = old_note.read_text(encoding="utf-8")
        text = re.sub(rf"^citekey:\s*{re.escape(old)}\s*$", f"citekey: {new}", text, count=1, flags=re.M)
        text = text.replace(f"{old}_SI", f"{new}_SI")
        old_note.unlink()
        (b.NOTES / f"{new}.md").write_text(text, encoding="utf-8", newline="\n")
        # notes-readable/ 是生成物, 但旧名文件不删会变成孤儿, 这里同步改名(内容随后由
        # render_readable_notes.py 统一重生成)
        old_readable = b.NOTES_READABLE / f"{old}.md"
        if old_readable.exists():
            old_readable.unlink()
        (b.NOTES_READABLE / f"{new}.md").write_text(
            b.make_readable(text), encoding="utf-8", newline="\n")

    # library.bib
    if b.BIB.exists():
        bib_text = b.BIB.read_text(encoding="utf-8")
        bib_text = bib_text.replace(f"@article{{{old},", f"@article{{{new},", 1)
        b.BIB.write_text(bib_text, encoding="utf-8", newline="\n")

    # INDEX.md (citekey 是表格第一列)
    if b.INDEX.exists():
        idx_text = b.INDEX.read_text(encoding="utf-8")
        idx_text = re.sub(rf"^\|\s*{re.escape(old)}\s*\|", f"| {new} |", idx_text, count=1, flags=re.M)
        b.INDEX.write_text(idx_text, encoding="utf-8", newline="\n")

    # .ingest_state.json (仅信息性字段, 用 old_pdf 已经改名前记录的源路径做key不变, 只改 value 里的 citekey)
    if b.STATE.exists():
        state = json.loads(b.STATE.read_text(encoding="utf-8"))
        changed = False
        for src, info in state.items():
            if isinstance(info, dict) and info.get("citekey") == old:
                info["citekey"] = new
                if "si" in info and info["si"]:
                    info["si"] = [s.replace(old, new, 1) for s in info["si"]]
                changed = True
        if changed:
            b.save_state(state)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行改名, 不加则只预览映射表")
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条(0=全部), 用于小规模验证")
    args = ap.parse_args()

    import os
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("缺少环境变量 DEEPSEEK_API_KEY")
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=30.0, max_retries=1)

    entries = parse_bib_entries()
    # 已经是新格式(年份开头)的跳过, 避免重复处理
    old_format = {ck: info for ck, info in entries.items() if not re.match(r"^[0-9]{4}-", ck)}
    if args.limit:
        old_format = dict(list(old_format.items())[: args.limit])

    print(f"library.bib 共 {len(entries)} 条, 其中旧格式 citekey {len(old_format)} 条需要迁移"
          + ("" if args.apply else " (预览模式, 不落盘, 加 --apply 才真正写入)"), flush=True)

    taken = set(entries.keys())
    mapping = {}
    for i, (old_key, info) in enumerate(old_format.items(), 1):
        print(f"[{i}/{len(old_format)}] 处理中: {old_key} ...", flush=True)
        try:
            fields = get_new_fields(client, args.model, info["title"], info["journal"])
        except Exception as e:
            print(f"  出错, 跳过: {e}", flush=True)
            continue
        new_key = b.make_citekey(info["year"], fields.get("journal_abbr"), fields.get("condensed_title"), taken)
        taken.add(new_key)
        mapping[old_key] = new_key
        print(f"  {old_key}\n  -> {new_key}", flush=True)

        # 边算边改: 每算完一个立刻落盘改名, 中途暂停也不会丢失已完成的工作/重复花 API 调用
        if args.apply:
            rename_everywhere(old_key, new_key)
            print("  已改名: papers/pdf + notes/ + notes-readable/ + library.bib + INDEX.md", flush=True)

    if args.apply:
        print(f"\n已迁移 {len(mapping)} 条 citekey。建议接着跑:")
        print("  bash scripts/build_keyword_index.sh")
        print("  bash scripts/check_library.sh")
    else:
        print(f"\n共 {len(mapping)} 条待迁移。确认无误后加 --apply 重跑。")


if __name__ == "__main__":
    main()
