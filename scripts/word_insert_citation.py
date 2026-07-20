#!/usr/bin/env python3
"""
"在 Word 里光标停住的地方插入引文" —— 2026-07-20 加, 2026-07-20 晚加 --style/--rebuild,
目的是替代"从这边拿到 citekey 推荐 -> 去 EndNote 里手动搜索/导入 -> 用 Cite While You Write
插入"这几步, 不依赖装 EndNote/Zotero 这类文献管理软件, 直接通过 Word 的 COM 接口操作一个
**已经打开着的** Word 文档。这个文件也被 word_auto_cite.py 当模块导入复用(解析bib/连接Word/
插入标记/维护References这套逻辑两边共用, 不要重复实现)。

原理: 用 pywin32 (win32com.client) 连接到正在运行的 Word 应用, 抓住 ActiveDocument 当前的
Selection(也就是你光标停的地方), 在那插入编号标记; 同时在文档末尾维护一个 "References" 小节,
按引用顺序编号列出对应文献。哪个 citekey 对应几号、用的什么样式, 记在文档同目录下的一个
"<文档名>.citemap.json" 隐藏状态文件里(不是 Word 文档本身的一部分)。

前提条件:
  - Windows + 已安装 Microsoft Word, 且**目标 docx 文件已经在 Word 里打开着**(这个脚本只是
    "遥控"一个已打开的 Word 窗口, 不会自己启动 Word 或打开文件——避免误操作到你没预期的文档)。
  - 光标要停在你想插入引用的位置, 跑这个脚本之前不要再点别的地方。
  - `pip install pywin32`(仅 Windows 需要, 其余平台没有 Word COM 接口, 用不了这个脚本)。

用法:
  python scripts/word_insert_citation.py --citekeys 2024-Science-Foo-Bar
  python scripts/word_insert_citation.py --citekeys 2024-Science-Foo-Bar,2023-JACS-Baz-Qux
  python scripts/word_insert_citation.py --citekeys <...> --doc "论文初稿.docx"   # 多个Word窗口开着时指定操作哪个
  python scripts/word_insert_citation.py --citekeys <...> --style nature          # 换参考文献格式

支持的 --style（日常写作单一编号格式为主，支持增删重排，见下方 --rebuild）:
  numbered(默认)  [n] 括注编号, 参考文献列全名作者
  nature          上标编号(无括号), 参考文献仿 Nature 风格(姓在前+名字缩写, >2作者用et al.)
  wiley           [n] 括注编号, 参考文献仿 Wiley/Angewandte 风格(名字缩写在前+姓)
  gbt7714         [n] 括注编号, 参考文献仿中国国标 GB/T 7714 数字顺序制格式(Title[J]标注)
  ⚠️ 这几种是**近似风格**, 不是严格实现对应期刊/标准的官方格式规范(卷期页码本库的 library.bib
  没有存储, 显示不出来, 需要投稿前自己核对补齐)——够日常写作用, 不保证直接符合投稿要求。

--rebuild：重新扫描文档里已有的编号标记, 按它们在正文里第一次出现的先后顺序重新连续编号
(处理"我删了/挪动了某处引用, 后面编号要跟着变"这种情况), 同时重建 References 小节。
  python scripts/word_insert_citation.py --rebuild --doc "论文初稿.docx"
  ⚠️ 只支持 numbered/gbt7714 这种"[n]"括号标记的样式——nature/wiley 用的上标编号在 Word
  文本层面和普通数字没法区分, 没法可靠地重新定位, 这两种样式改了正文顺序后需要自己手动调整。
  跑 --rebuild 前建议先 Ctrl+S 保存一下当前版本, 万一结果不对可以撤销(Ctrl+Z)或者重新打开
  刚保存的文件。

不做的事: 不会自动保存文档(改完你自己看一眼、按 Ctrl+S)。不是 Word 域代码(field code), 不能
像 EndNote 那样"切换引用样式自动重排全文"——每次换 --style 相当于用新样式重新生成参考文献列表,
不会保留旧样式的手动调整。
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIB = ROOT / "library.bib"


def parse_bib_entries() -> dict:
    """解析 library.bib, 返回 {citekey: {title, author, journal, year, doi}}。"""
    text = BIB.read_text(encoding="utf-8")
    entries = {}
    for m in re.finditer(r"@article\{([^,]+),(.*?)\n\}", text, re.DOTALL):
        citekey = m.group(1).strip()
        body = m.group(2)
        fields = {}
        for fm in re.finditer(r"(\w+)\s*=\s*\{([^}]*)\}", body):
            fields[fm.group(1).strip().lower()] = fm.group(2).strip()
        entries[citekey] = fields
    return entries


def _authors_list(e: dict) -> list:
    return [a.strip() for a in e.get("author", "").split(" and ") if a.strip()]


def _surname_initials(name: str) -> str:
    """"Jonah Erlebacher" -> "Erlebacher, J."(最后一个词当姓, 其余取首字母)——和
    export_for_endnote.py 的 RIS 作者转换用的是同一套简单启发式, 复合姓/中间名等
    边界情况不保证完全准确, 供参考格式用够用。"""
    parts = name.split()
    if len(parts) < 2:
        return name
    surname = parts[-1]
    initials = "".join(f"{p[0].upper()}." for p in parts[:-1] if p)
    return f"{surname}, {initials}"


def _doi_part(e: dict) -> str:
    doi = e.get("doi", "")
    return f", doi:{doi}" if doi and doi.upper() != "N/A" else ""


def _fmt_numbered(n: int, e: dict) -> str:
    authors = _authors_list(e)
    author_str = f"{authors[0]} et al." if len(authors) > 3 else (", ".join(authors) or "作者未知")
    return f"{n}. {author_str}, 《{e.get('title', '')}》, {e.get('journal', '')}, {e.get('year', '')}{_doi_part(e)}."


def _fmt_nature(n: int, e: dict) -> str:
    authors = [_surname_initials(a) for a in _authors_list(e)]
    if not authors:
        author_str = "作者未知"
    elif len(authors) == 1:
        author_str = authors[0]
    elif len(authors) == 2:
        author_str = f"{authors[0]} & {authors[1]}"
    else:
        author_str = f"{authors[0]} et al."
    return f"{n}. {author_str} {e.get('title', '')}. {e.get('journal', '')} ({e.get('year', '')}){_doi_part(e)}."


def _fmt_wiley(n: int, e: dict) -> str:
    def initials_first(name: str) -> str:
        parts = name.split()
        if len(parts) < 2:
            return name
        surname = parts[-1]
        initials = ". ".join(p[0].upper() for p in parts[:-1] if p) + "."
        return f"{initials} {surname}"

    authors = [initials_first(a) for a in _authors_list(e)]
    author_str = f"{authors[0]} et al." if len(authors) > 3 else (", ".join(authors) or "作者未知")
    return f"[{n}] {author_str}, {e.get('journal', '')}, {e.get('year', '')}{_doi_part(e)}."


def _fmt_gbt7714(n: int, e: dict) -> str:
    def surname_initials_nospace(name: str) -> str:
        parts = name.split()
        if len(parts) < 2:
            return name
        surname = parts[-1]
        initials = "".join(p[0].upper() for p in parts[:-1] if p)
        return f"{surname} {initials}"

    authors = [surname_initials_nospace(a) for a in _authors_list(e)]
    author_str = ", ".join(authors[:3]) + (", et al" if len(authors) > 3 else "") if authors else "作者未知"
    return f"[{n}] {author_str}. {e.get('title', '')}[J]. {e.get('journal', '')}, {e.get('year', '')}{_doi_part(e)}."


STYLES = {
    "numbered": {"format_ref": _fmt_numbered, "superscript": False, "bracket": True},
    "nature": {"format_ref": _fmt_nature, "superscript": True, "bracket": False},
    "wiley": {"format_ref": _fmt_wiley, "superscript": False, "bracket": True},
    "gbt7714": {"format_ref": _fmt_gbt7714, "superscript": False, "bracket": True},
}


def marker_text(numbers: list, style_cfg: dict) -> str:
    nums = ",".join(str(n) for n in numbers)
    return f"[{nums}]" if style_cfg["bracket"] else nums


def find_word_document(doc_name: str | None):
    """连接到正在运行的 Word 应用, 返回目标 Document 对象(不会自己新开 Word 进程/开文件)。"""
    import win32com.client

    try:
        word = win32com.client.GetActiveObject("Word.Application")
    except Exception:
        sys.exit("没有找到正在运行的 Word。请先在 Word 里打开你要插入引用的文档, 再跑这个脚本。")

    if word.Documents.Count == 0:
        sys.exit("Word 已经打开了, 但没有任何文档。请先打开你要插入引用的 docx 文件。")

    if doc_name:
        for d in word.Documents:
            if doc_name.lower() in d.Name.lower():
                return word, d
        sys.exit(f"Word 里没有找到名字包含「{doc_name}」的已打开文档, 当前打开的是: "
                  + ", ".join(d.Name for d in word.Documents))

    if word.Documents.Count > 1:
        names = ", ".join(d.Name for d in word.Documents)
        sys.exit(f"Word 里同时开着多个文档({names}), 请加 --doc 指定操作哪一个"
                  "(比如 --doc \"论文初稿.docx\"，写文件名里能唯一识别的一部分即可)。")

    return word, word.ActiveDocument


def citemap_path(doc) -> Path:
    doc_path = Path(doc.FullName) if doc.Path else None
    return (doc_path.parent / f"{doc_path.stem}.citemap.json") if doc_path else \
        Path(__file__).resolve().parent / ".word_citemap_untitled.json"


def load_citemap(map_path: Path) -> dict:
    """v2 格式: {"style": "numbered", "map": {citekey: number}}。兼容旧版直接
    {citekey: number} 的扁平格式(2026-07-20 --style/--rebuild 之前写的文件), 读到就迁移。"""
    if not map_path.exists():
        return {"style": "numbered", "map": {}}
    data = json.loads(map_path.read_text(encoding="utf-8"))
    if "map" in data and "style" in data:
        return data
    return {"style": "numbered", "map": data}  # 旧扁平格式迁移


def save_citemap(map_path: Path, citemap: dict):
    map_path.write_text(json.dumps(citemap, ensure_ascii=False, indent=2), encoding="utf-8")


def find_references_heading_start(doc):
    """返回 References 标题段落的起始字符位置; 没有就返回 None(表示文档末尾还没有这个小节)。"""
    text = doc.Content.Text.replace("\r", "\n")
    idx = 0
    for line in text.split("\n"):
        if line.strip() == "References":
            return idx
        idx += len(line) + 1
    return None


def ensure_references_section(doc):
    """确认文档末尾有咱们维护的 "References" 标题, 没有就添加。Word 段落分隔符是 \\r
    不是 \\n, 按 \\r 手动切行比对字符串, 不要用 re.MULTILINE 的 ^$ 锚点(匹配不到)。"""
    if find_references_heading_start(doc) is not None:
        return
    end_range = doc.Content
    end_range.Collapse(0)  # wdCollapseEnd
    end_range.InsertParagraphAfter()
    end_range.Collapse(0)
    end_range.InsertAfter("References")
    heading_range = doc.Content
    heading_range.Collapse(0)
    heading_range.MoveStart(1, -len("References"))  # wdCharacter
    try:
        heading_range.Style = doc.Styles("Heading 1")
    except Exception:
        pass
    end_range = doc.Content
    end_range.Collapse(0)
    end_range.InsertParagraphAfter()


def append_reference_entry(doc, ref_text: str):
    end_range = doc.Content
    end_range.Collapse(0)
    end_range.InsertAfter(ref_text)
    end_range.Collapse(0)
    end_range.InsertParagraphAfter()


def rebuild_references_section(doc, entries: list):
    """entries: [(n, ref_text), ...]，按顺序重建 References 小节(先删旧的整段, 再重写)。"""
    start = find_references_heading_start(doc)
    if start is not None:
        kill_range = doc.Range(start, doc.Content.End)
        kill_range.Delete()
    ensure_references_section(doc)
    for _, ref_text in entries:
        append_reference_entry(doc, ref_text)


def insert_marker_at_range(word, doc, rng, citekeys: list, citemap: dict, bib_entries: dict, style: str):
    """在给定 Range(通常是当前光标 Selection.Range，或自动定位到的某句话末尾)插入引用标记,
    需要的话追加 References 条目, 就地更新 citemap(不落盘, 调用方负责 save_citemap)。
    返回插入的标记文本。"""
    style_cfg = STYLES[style]
    cmap = citemap["map"]
    numbers, new_entries = [], []
    for citekey in citekeys:
        if citekey in cmap:
            numbers.append(cmap[citekey])
        else:
            n = (max(cmap.values()) if cmap else 0) + 1
            cmap[citekey] = n
            numbers.append(n)
            new_entries.append((n, citekey))

    text = marker_text(numbers, style_cfg)
    insert_start = rng.End
    rng.InsertAfter(text)
    if style_cfg["superscript"]:
        doc.Range(insert_start, insert_start + len(text)).Font.Superscript = True

    if new_entries:
        ensure_references_section(doc)
        for n, citekey in sorted(new_entries):
            append_reference_entry(doc, style_cfg["format_ref"](n, bib_entries[citekey]))

    return text


def do_rebuild(word, doc, citemap: dict, bib_entries: dict):
    style = citemap.get("style", "numbered")
    style_cfg = STYLES[style]
    if not style_cfg["bracket"]:
        sys.exit(f"当前样式 {style} 用的是上标编号(无括号), 在纯文本里没法可靠定位已插入的标记, "
                  f"--rebuild 只支持 numbered/wiley/gbt7714 这类 \"[n]\" 括号样式。")

    ref_start = find_references_heading_start(doc)
    body_text = doc.Content.Text[: (ref_start if ref_start is not None else len(doc.Content.Text))]

    marker_re = re.compile(r"\[(\d+(?:,\d+)*)\]")
    seen_markers, ordered_citekeys = [], []
    rev_map = {v: k for k, v in citemap["map"].items()}
    for m in marker_re.finditer(body_text):
        old_text = m.group(0)
        if old_text not in seen_markers:
            seen_markers.append(old_text)
        for num_str in m.group(1).split(","):
            citekey = rev_map.get(int(num_str))
            if citekey and citekey not in ordered_citekeys:
                ordered_citekeys.append(citekey)

    if not ordered_citekeys:
        print("文档正文里没扫到任何这套系统插入过的 [n] 标记, 没有可重编号的内容。")
        return citemap

    new_numbers = {citekey: i + 1 for i, citekey in enumerate(ordered_citekeys)}

    # 计算每个旧标记字符串对应的新标记字符串(标记内可能是"[2,5]"这种组合, 整体替换)
    old_to_new_text = {}
    for old_text in seen_markers:
        nums = [int(x) for x in old_text.strip("[]").split(",")]
        new_nums = [new_numbers[rev_map[n]] for n in nums if rev_map.get(n) in new_numbers]
        old_to_new_text[old_text] = marker_text(new_nums, style_cfg)

    # 两阶段替换, 避免"1"和"7"互换编号时字符串互相覆盖: 先换成占位符再换成最终文本
    body_range_end = ref_start if ref_start is not None else doc.Content.End
    for i, old_text in enumerate(seen_markers):
        placeholder = f"⦃RENUM{i}⦄"
        rng = doc.Range(0, body_range_end)
        while rng.Find.Execute(FindText=old_text, MatchCase=True, MatchWildcards=False):
            rng.Text = placeholder
            body_range_end += len(placeholder) - len(old_text)
            rng.Collapse(0)
            rng.End = body_range_end
    for i, old_text in enumerate(seen_markers):
        placeholder = f"⦃RENUM{i}⦄"
        new_text = old_to_new_text[old_text]
        rng = doc.Range(0, doc.Content.End)
        while rng.Find.Execute(FindText=placeholder, MatchCase=True, MatchWildcards=False):
            rng.Text = new_text
            rng.Collapse(0)
            rng.End = doc.Content.End

    entries = [(new_numbers[c], style_cfg["format_ref"](new_numbers[c], bib_entries[c])) for c in ordered_citekeys]
    entries.sort(key=lambda x: x[0])
    rebuild_references_section(doc, entries)

    print(f"重新扫描到 {len(ordered_citekeys)} 篇不重复的引用文献, 已按正文出现顺序重新连续编号"
          f"(1~{len(ordered_citekeys)}), References 小节已重建。")
    return {"style": style, "map": new_numbers}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--citekeys", help="逗号分隔的 citekey 列表, 在光标处一起插入")
    group.add_argument("--rebuild", action="store_true", help="重新扫描文档里已有标记, 按出现顺序重新编号")
    ap.add_argument("--doc", help="Word 里同时开着多个文档时, 用文件名(或其中一部分)指定操作哪个")
    ap.add_argument("--style", default=None, choices=list(STYLES),
                     help="参考文献格式, 不给就沿用这篇文档之前用过的样式(第一次用默认 numbered)")
    args = ap.parse_args()

    bib_entries = parse_bib_entries()

    if args.citekeys:
        citekeys = [c.strip() for c in args.citekeys.split(",") if c.strip()]
        if not citekeys:
            sys.exit("没有给 --citekeys")
        missing = [c for c in citekeys if c not in bib_entries]
        if missing:
            sys.exit(f"library.bib 里找不到这些 citekey, 检查拼写: {', '.join(missing)}")

    try:
        import win32com.client  # noqa: F401
    except ImportError:
        sys.exit("缺少依赖: pip install pywin32(仅 Windows 可用, 这是操作 Word COM 接口需要的库)")

    word, doc = find_word_document(args.doc)
    map_path = citemap_path(doc)
    citemap = load_citemap(map_path)
    if args.style:
        citemap["style"] = args.style
    style = citemap["style"]

    if args.rebuild:
        citemap = do_rebuild(word, doc, citemap, bib_entries)
        save_citemap(map_path, citemap)
        print(f"记得自己检查一下排版, 然后 Ctrl+S 保存。编号状态记在 {map_path.name} 里。")
        return

    rng = word.Selection.Range
    text = insert_marker_at_range(word, doc, rng, citekeys, citemap, bib_entries, style)
    save_citemap(map_path, citemap)

    print(f"已在光标处插入 {text}（样式: {style}）。"
          f" 记得自己检查一下排版, 然后 Ctrl+S 保存(这个脚本不会自动保存文档)。"
          f" 编号状态记在 {map_path.name} 里, 同一篇文档下次再插入引用时会接着这个继续编号,"
          f" 增删/挪动了引用位置想重新排连续编号就跑 --rebuild。")


if __name__ == "__main__":
    main()
