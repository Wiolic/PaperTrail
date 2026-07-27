#!/usr/bin/env python3
"""批量粗建档: 扫描源目录 PDF(+配套SI) -> DeepSeek 抽元数据 -> 生成 citekey ->
复制主PDF与SI / 追加 library.bib / 建 notes 骨架 / 追加 INDEX.md。规则来源: ../AGENTS.md。

用法:
    python scripts/batch_ingest.py --source "D:\\你的PDF源目录" --limit 15
    python scripts/batch_ingest.py --source ... --limit 15 --dry-run   # 只打印不落盘

只读源目录, 只复制文件, 绝不移动/删除源文件(AGENTS.md 红线)。
断点续跑: 已处理过的源文件记录在 scripts/.ingest_state.json, 重跑自动跳过。

SI(补充材料)配对: 见 AGENTS.md "SI(补充材料)绑定"一节, 本脚本按同名规则实现:
  1. 同一来源文件夹只有两个候选文件(pdf/docx/doc) -> 直接配对(主PDF + 另一个当SI)
  2. 文件名带 SI/ESI/supp/supporting/supplementary/supplemental 标记 -> 去掉标记词后按文件名相似度配对
  3. .docx/.doc 默认当 SI 候选, 不当独立文献处理
  4. 配不上的 SI 候选一律不瞎配, 汇总到"存疑"清单里由人工确认
"""
import argparse
import difflib
import json
import re
import sys
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

try:
    import fitz  # pymupdf
except ImportError:
    sys.exit("缺少依赖: pip install pymupdf")

try:
    from openai import OpenAI
except ImportError:
    sys.exit("缺少依赖: pip install openai")

ROOT = Path(__file__).resolve().parent.parent
PAPERS = ROOT / "papers"
NOTES = ROOT / "notes"              # 唯一真源: 不分行, 供 Claude/脚本读取解析
NOTES_READABLE = ROOT / "notes-readable"  # 生成物: 正文按 READABLE_WIDTH 折行, 供人眼阅读
EXTRACTED_TEXT = ROOT / "extracted-text"  # 缓存: 每篇PDF抽出的文字原文, 避免以后加字段/改逻辑要重新读PDF
BIB = ROOT / "library.bib"
INDEX = ROOT / "INDEX.md"
TEMPLATE = ROOT / "templates" / "note-template.md"
READABLE_WIDTH = 60
STATE = Path(__file__).resolve().parent / ".ingest_state.json"

TAG_VOCAB = ["OER", "HER", "PEMWE", "AEMFC", "PEM界面", "膜降解", "IrOx", "CoOx", "RuOx",
             "IrCoOx", "溶出", "烧结", "DFT", "AIMD", "NEB", "STEM", "cryoEM",
             "原位表征", "EELS", "综述", "方法学",
             # 库范围不限于电催化, 补充通用大类; 都配不上就按论文实际学科新增, 不要硬套
             "电池", "2D材料", "MXene", "钙钛矿", "结构生物学", "高分子", "单原子催化",
             "拉曼光谱", "半导体光电"]


def load_live_tag_vocab() -> list:
    """TAG_VOCAB 只是个兜底种子表, 真正的"当前受控词表"应该是库里已经实际用过的全部标签——
    否则会重复踩 MXene 那个坑: 某篇论文当初被 LLM 新造了一个"MXene"标签, 但因为这个词从没被
    写回 TAG_VOCAB, 后续入库的其他 15 篇 MXene 论文的 prompt 里根本看不到"MXene"这个词已经
    存在, 只能看到宽泛的"2D材料"在词表里, 于是习惯性地只打了这个更保险的大类标签, 导致明明是
    同一种材料的论文却没有一个可复用的专属标签能把它们检索到一起。

    做法: 每次构建 prompt 时动态扫一遍 notes/*.md 的 tags 字段, 把库里真实出现过的标签和
    TAG_VOCAB 种子表取并集, 这样任何一次新造的标签(哪怕只被用过一次)从下一篇论文开始就会
    出现在"优先复用"的词表里, 不需要人工把新词手动加回 TAG_VOCAB 代码——词表自己滚雪球式
    地跟着库内容同步。"""
    seen = set(TAG_VOCAB)
    if NOTES.exists():
        for f in NOTES.glob("*.md"):
            text = f.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^tags:\s*\[(.*?)\]", text, re.MULTILINE)
            if not m:
                continue
            for tok in m.group(1).split(","):
                tok = tok.strip().strip('"').strip("'")
                if tok:
                    seen.add(tok)
    return sorted(seen)

# [领域定制] 下面这段是"我的课题背景"提示词, 用于让 LLM 判断"与我课题的关联"这一项。
# 首次使用时请把这段换成你自己的研究方向描述——写清楚你的课题、关键锚点词(便于 LLM 识别
# 相关文献)、相关性判断标准(内圈/中圈/外圈), 越具体检索和相关性判断越准。也可以单独维护一份
# 更详细的背景文档(如 prompts/我的课题背景.md), 这里放浓缩版, 两处保持同步。
USER_RESEARCH_CONTEXT = (
    "[请在此填写你的研究方向、正在做的具体课题、判断文献相关性时应关注的关键锚点词等背景信息。"
    "例如: '用户是材料/催化方向的研究者, 围绕XX体系的YY性能开展研究, 关键锚点词包括...'. "
    "写得越具体, LLM 判断'与我课题的关联'时就越准确。]"
)

SI_MARKERS = {"si", "esi", "esm", "sup", "supp", "suppmat", "supporting", "supplementary", "supplemental"}  # esm=Springer/Nature; sup/suppmat=Wiley "xxx-sup-0001-suppmat" 惯例
SI_EXTS = {".docx", ".doc"}
CANDIDATE_EXTS = {".pdf", ".docx", ".doc"}

def build_extract_system_prompt() -> str:
    """每次调用都重新扫一遍库里当前实际用过的标签(见 load_live_tag_vocab), 拼进 tags 字段
    的 prompt 里——不能在模块加载时算一次就缓存死, 否则新论文引入的标签(比如 MXene)在
    写回 notes/ 之后, 依然不会被后续的入库调用看到, 词表就没法自然生长。"""
    tag_vocab = load_live_tag_vocab()
    return f"""你在协助把一篇论文的全文(2026-07-19 起默认传入全文, 含讨论/结论/参考文献列表;
若确实只拿到部分文本, 缺的部分如实说明, 不要假装读了全文)整理进文献库, 既要抽元数据, 也要写精读笔记正文。
只输出一个 JSON 对象, 不要任何解释文字、不要 markdown 代码块围栏。

背景(用于判断"与我课题的关联"一项): {USER_RESEARCH_CONTEXT}

字段:
{{
  "is_journal_article": true/false, "这篇是否是发表在学术期刊上的正式论文(研究论文或综述都算true)。
     不算的情况(填false): 教材/书籍章节、仪器操作/培训手册、技术报告、学位论文、会议海报或幻灯片、
     产品说明书、专利文本等非期刊出版物。摘录内容明显缺少期刊论文的典型特征(没有摘要段、没有期刊
     header、不像投稿格式) 就判 false。填 false 时其余字段能填多少填多少, 不必强求完整。",
  "title": "论文英文原题",
  "authors_display": "\\\"第一作者 et al.\\\" 或前三位作者分号分隔",
  "authors_full": ["完整作者名单, 按原文顺序, 每人一个数组元素, 格式 \\\"名 姓\\\"(如 \\\"Dongwon Shin\\\"), 用于按作者检索, 缺失作者信息就尽量按原文出现的全部列出, 不要只写第一作者"],
  "year": 4位整数,
  "journal": "期刊全名, 如 ACS Nano / Nature Materials / Journal of the American Chemical Society",
  "doi": "DOI字符串, 确实找不到则填 \\\"N/A\\\"",
  "journal_abbr": "无空格无句点的期刊简称, 用于 citekey。业界本来就是全大写缩写的期刊保持全大写(JACS/PNAS/EES/ACIE); 其余用驼峰式截断(ACS Nano→ACSNano, Nature Materials→NatMater, Nature Communications→NatCommun, Advanced Materials→AdvMater, Chemical Communications→ChemComm, Nano Lett.→NanoLett, ACS Catalysis→ACSCatal); 没见过的期刊按同样规则自己合理简写",
  "condensed_title": "3~6个英文单词提炼标题核心内容, 连字符连接, 用于 citekey。化学元素符号(Ir/Co/Ru/Ti/Mn等)和领域缩写(OER/HER/PEM/PEMWE/AEM/AEMFC/TEM/SEM/DFT/AIMD/XPS/EELS/STEM/MEA/CCM等)必须按规范大小写书写(全大写或化学式专有大小写, 不要写成全小写); 其余普通单词 Title Case(首字母大写)。如 Ir-Nanosheets-TiO2-PEMWE 或 Ordered-MEA-Nafion-Array",
  "tags": ["受控大类,1~4个。优先从这个词表选(含库里已实际用过的全部标签,不只是初始种子表): {', '.join(tag_vocab)}；论文所属学科词表里都没有贴切的,才新增1个简短标签,不要为了凑数硬套电催化标签。"
           "重要(2026-07-21加,踩过的坑): 广义大类标签(如'2D材料')和具体材料子类标签不是互斥关系,不能只打了泛泛的大类就算完事——"
           "如果论文的具体研究对象有一个公认的专有名称(如 MXene、LDH、石墨烯、氧化石墨烯、六方氮化硼、钙钛矿的具体亚型等),"
           "必须把这个具体名称也作为一个标签加进去,不能让读者只看到'2D材料'却不知道具体是哪种材料,导致同类论文按材料检索不到彼此。"
           "判断标准: 只要这个具体材料名称在库里可能反复出现(不是只此一篇的生僻叫法),就该给它一个可复用的专属标签,而不是让它消失在一个更大的伞形分类里。"],
  "keywords": ["5~10个自由细粒度关键词, 是这篇论文最核心的检索抓手。要求(2026-07-21 加强): (1)覆盖四类维度各取最具体的: 【材料/体系】具体到组分与形貌(如 'Co-doped RuO2 nanosheet' 而非泛泛的 'catalyst'), 【机理/现象】具体到过程(如 'lattice oxygen mechanism'/'proton shuttling' 而非泛泛的 'mechanism'), 【方法/技术】文中真正起关键作用的方法名(如 'operando XAS'/'DFT+U'), 【应用/性能指标】如 'acidic OER'/'PEMWE durability'; (2)英文为主, 用领域内规范写法(名词短语, 不要整句), 化学式/缩写按标准大小写; (3)同义词归一化(2026-07-23 加强): 必须使用以下规范写法, 括号内是常见变体但不要用的写法——DFT(不是first-principles/ab initio calculations), Ir dissolution(不是Ir leaching/Ir loss/iridium migration), Ru dissolution(不是Ru leaching), oxygen vacancy(不是O vacancy), PEMWE(不是PEM water electrolysis/proton exchange membrane water electrolysis), AEMWE(不是AEM water electrolysis), MEA(不是membrane electrode assembly), anion exchange membrane(不是AEM/anion-exchange membrane), lattice oxygen mechanism(不是LOM/lattice oxygen participation), adsorbate evolution mechanism(不是AEM pathway), oxide path mechanism(不是OPM), proton transport(不是proton conduction/proton hopping/Grotthuss mechanism 单独出现时), accelerated stress test(不是AST/accelerated degradation test), turnover frequency(不是TOF); (4)只写论文确实做了/讨论了的, 边缘一提的不算核心, 宁缺毋滥不凑数不瞎编"],
  "类型": "从 [实验, 计算, 计算+实验, 建模, 综述] 里选一个, 判定标准(2026-07-21 明确): 【计算】特指材料/原子尺度的第一性原理或分子模拟——DFT/DFT+U/AIMD/MD/相场/蒙特卡洛/机器学习势等直接算材料的结构-能量-性质, 只有全篇是这类纯理论材料计算、无湿实验时才填'计算'; 【实验】有湿法合成/电化学测试/器件测试/谱学表征等实体实验; 【计算+实验】材料计算和实验兼有; 【建模】非材料原子尺度的系统级/宏观建模——如能源-经济集成评估模型(IAM)、技术经济分析、工艺流程模拟、纯数据统计/机器学习建模(不含材料原子计算)等, 这类论文虽然也'算', 但不是材料计算, 不要误填'计算'; 【综述】综述/展望类文章。",
  "方法关键词": "判定标准: 先根据类型决定侧重, 实验类和计算+实验类以实验方法为主、计算方法为辅。具体规则: 【实验类】写主要实验表征手段和关键实验条件(如 'SEM/TEM/XRD/XPS/Raman/FT-IR; CV/LSV/EIS电化学测试; alkaline OER at 1 A/cm2'), 不要写计算方法(DFT/VASP/PBE等是计算工具不是实验方法); 【计算+实验类】先写实验方法(表征+电化学), 再补一句计算方法(如 'SEM/TEM/XRD/XPS; CV/LSV; DFT+U, VASP'), 实验为主计算为辅; 【纯计算类】写泛函/U值/AIMD等方法学关键词; 【建模类】写模型名/情景设计/优化方法",
  "表征方法": ["标准化表征技术列表, 供按手段检索(如'哪些文献用了 operando XAS')。规则(2026-07-21 加强): (1)只收录论文里实际用来获取数据的表征/分析技术, 一次通读全文(含方法节/图注/SI 描述)把真正用到的都收全, 不要只凭摘要漏掉方法节才提到的技术; (2)命名标准化: 纯缩写(DEMS/XPS/TEM/XRD/NMR/SEM/AFM/EELS/EXAFS/XANES/ICP-MS 等)直接大写书写; 同一技术只用一种规范写法(HAADF-STEM 不写成 'STEM-HAADF'; 'X射线衍射'统一成 'XRD'); (3)带'原位/工况/非原位'前缀的, 前缀与技术名之间用空格: 中文'原位'→'in situ'、'工况下'→'operando'、'非原位/离线'→'ex situ'(写 'in situ Raman'/'operando XRD'/'ex situ XPS', 不写 'insitu-Raman'/'InsituXRD'/粘连或连字符形式); (4)区分表征技术与理论方法——DFT/AIMD 属'方法关键词'不进这个字段; (5)原文含糊(只说'多种谱学表征'没点名具体技术)就不编, 纯计算/建模论文或全文未点名任何具体技术则填 []"],
  "体系": "研究对象简述, 如 IrO2(110)表面 或 Ir-Co掺杂IrOx纳米颗粒",

  "summary_3lines": "三句话总结这篇论文做了什么/怎么做的/结论是什么, 中文, 术语可保留英文",
  "problem_conclusion": "研究问题与核心结论, 中文, 3~6句, 说清楚要解决什么问题、核心发现是什么",
  "method_points": "方法要点, 中文为主。计算类写泛函/+U值/k点密度/ENCUT/模型尺寸(slab层数/真空层/超胞), 文中没提到的具体数值就写'原文未提供该数值,建议查全文/SI'而不要编; 实验类写关键表征手段/条件/样品制备",
  "key_results": "关键图表与数据: 文中给出的关键数值结果(活性/稳定性时长/机理证据等具体数字,带原文单位), 中文描述; 图表本身是图像文本抽取不到, 只描述文字里能读到的数据结论",
  "relevance": "与用户课题(见上方背景)的关联度和潜在借鉴点; 如果这篇确实跟OER/PEMWE/DFT计算关系不大, 如实说'与当前OER/PEMWE计算课题关联度较低, 可能是收藏的其他方向文献', 不要为了显得有用而牵强附会",
  "caveats": "质疑与局限: 摘录文字里提到的局限性, 或有依据的方法论质疑; 摘录范围内看不出局限就写'原文摘录部分未见明确讨论局限性, 建议精读全文/结论部分核实'",
  "further_reading": "值得追的参考文献: 摘录文字里提到的关键被引work(作者+年份等线索); 摘录范围内没有足够线索就写'摘录部分未见明确引用线索, 建议查全文参考文献列表'"
}}

规则:
1. 元数据字段(title/authors/year/journal/doi)缺失填 "unknown", 绝不编造 DOI 或年份。
2. summary_3lines/problem_conclusion 等正文字段基于给定摘录如实总结, 摘录信息不够就明确说"原文摘录部分信息不足", 不要脑补编造内容或数值。
3. 只根据给定文字判断, 不要联想或调用摘录之外的知识编造论文没提到的具体内容。"""


# ---------- SI 配对 ----------

def _tokens(stem: str):
    return [t for t in re.split(r"[^a-z0-9]+", stem.lower()) if t]


def _is_si_token(t: str) -> bool:
    return (
        t in SI_MARKERS
        or re.fullmatch(r"mmc\d+", t) is not None       # Elsevier "mmc1/mmc2..." 补充材料惯例
        or re.fullmatch(r"moesm\d+", t) is not None      # Springer/Nature "MOESM1/MOESM2..." 补充材料惯例
    )


def has_si_marker(stem: str) -> bool:
    return any(_is_si_token(t) for t in _tokens(stem))


def strip_si_tokens(stem: str) -> str:
    kept = [t for t in _tokens(stem) if not _is_si_token(t)]
    return "".join(kept)


def pair_files(files: list[Path]):
    """返回 (confident_pairs, weak_pairs, unmatched_si)。
    confident_pairs: 文件名有实际关联证据(SI标记词+相似文件名), 直接采信自动绑定。
    weak_pairs: 只是"同文件夹恰好只有2个文件"这个弱信号(见下方注释), 不自动绑定, 只报告给人工确认。"""
    marked = {f for f in files if has_si_marker(f.stem)}
    non_pdf = {f for f in files if f.suffix.lower() in SI_EXTS}
    si_pool = marked | non_pdf
    mains = [f for f in files if f not in si_pool and f.suffix.lower() == ".pdf"]

    confident: dict = {}
    weak: dict = {}
    remaining_si = set(si_pool)

    FNAME_SIM_THRESHOLD = 0.82  # 高阈值+长度门槛, 见下方三条踩坑记录, 宁可漏配去 unmatched 里人工/内容核实, 也不要乱配
    for m in mains:
        norm_m = strip_si_tokens(m.stem)
        # 文件名归一化后做相似度匹配, 连续踩过三个坑:
        # 1) strip_si_tokens 对纯中文/纯符号文件名(没有a-z0-9片段)返回空字符串, 而 "" 是任何字符串
        #    的子串, "" in norm_m 恒为True, 导致任意文件被误判成匹配。
        # 2) 短数字片段(如年份"2021")会凑巧作为子串出现在完全不相关文件的编号里, 造成误配对。
        # 3) 就算换成 difflib 相似度, 阈值0.6/长度门槛6字符仍然太松——学术文件名普遍共享大量数字/
        #    常见缩写字符, 两个完全不相关的短文件名也可能凑出 >0.6 的相似度。
        # 现在要求两边都至少10字符 + 相似度阈值提到0.82(能扛住"共享后缀模板"式的假匹配, 比如Wiley
        # 全部SI文件都长得像"xxx-sup-0001-suppmat", 这类模板词已经在 has_si_marker 里过滤掉了,
        # 剩下要比对的应该是真正的文章编号/标题片段)。宁可漏配丢进 unmatched 让内容比对(match_orphan_si.py)
        # 或人工去确认, 也不要在这里乱配。
        if len(norm_m) < 10 or norm_m.isdigit():
            continue
        matched = []
        for s in remaining_si:
            norm_s = strip_si_tokens(s.stem)
            if len(norm_s) < 10 or norm_s.isdigit():
                continue
            if difflib.SequenceMatcher(None, norm_m, norm_s).ratio() >= FNAME_SIM_THRESHOLD:
                matched.append(s)
        if matched:
            confident[m] = matched
            remaining_si -= set(matched)

    # 弱信号: 文件夹里恰好只剩1主1候选、且文件名毫无关联(适用于EndNote"一篇文献一个文件夹"这种规整
    # 来源; 对随手堆放的下载文件夹这个信号不可靠, 实测踩过坑: 论文和无关的行政文档(如"博士研究生资格
    # 考试工作方案.doc")恰好被扔进同一文件夹, 被误判成SI关系), 只报告不自动绑定
    if len(files) == 2 and len(mains) == 1 and len(remaining_si) == 1:
        m = mains[0]
        weak[m] = list(remaining_si)
        remaining_si.clear()

    return confident, weak, sorted(remaining_si, key=str)


# 用户明确要求不收录的来源文件夹(内容多为仪器手册/教材而非期刊论文, 2026-07-18排除)
EXCLUDED_FOLDER_NAMES = {"电镜资料"}


def group_and_pair(source: Path):
    """扫描 source 下所有 pdf/docx/doc, 按父目录分组配对。
    返回 (main_to_si: dict[Path -> list[Path]](只含高置信度配对, 会被自动绑定),
          weak_si: dict[Path -> list[Path]](弱信号配对, 只报告不自动绑定),
          unmatched_si: list[Path])"""
    # 防止 --source 指向的目录覆盖到本库自己已经生成的内容(papers/等), 导致重复入库自己的产出。
    # 踩过的坑(2026-07-21发现): 最初写成 "ROOT.resolve() not in p.resolve().parents"——但 inbox/
    # 本身就是 ROOT 的子目录, 所以"--source inbox"(几乎是唯一的实际用法, 网页面板和大多数工作流
    # 都这么调)下, inbox/ 里每一个文件的 parents 天然都包含 ROOT, 这条判断永远为 False, 导致
    # all_files 永远是空列表——batch_ingest.py 表现为"点入库/跑命令毫无反应", 报"没有待处理的
    # 新主文献", 但其实是源目录扫描阶段就把所有文件都误排除了, 根本没跑到状态判断那一步。
    # 正确的做法是只排除"本库自己已经生成的产出目录"(papers/等), 不是排除"ROOT 底下的任何位置"。
    reserved_dirs = {d.resolve() for d in (PAPERS, NOTES, NOTES_READABLE, EXTRACTED_TEXT)}
    all_files = [
        p for p in source.rglob("*")
        if p.is_file() and p.suffix.lower() in CANDIDATE_EXTS
        and not any(rd == p.resolve() or rd in p.resolve().parents for rd in reserved_dirs)
        and not (EXCLUDED_FOLDER_NAMES & set(p.parts))
    ]
    by_folder: dict = {}
    for f in all_files:
        by_folder.setdefault(f.parent, []).append(f)

    main_to_si: dict = {}
    weak_si: dict = {}
    unmatched_si: list = []

    for folder, files in by_folder.items():
        confident, weak, leftover = pair_files(files)
        main_to_si.update(confident)
        weak_si.update(weak)
        unmatched_si.extend(leftover)

    # 每个文件夹里未被配对为 SI 的普通 pdf, 也是独立主文献(只是没有 SI)
    all_mains = [f for f in all_files if f.suffix.lower() == ".pdf" and not has_si_marker(f.stem)]
    for m in all_mains:
        main_to_si.setdefault(m, [])

    return main_to_si, weak_si, unmatched_si


# ---------- 常规入库逻辑 ----------

def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# DOI 缺失时模型可能返回的各种占位值(提示词要求写"N/A", 但模型不总是遵守), 一律不参与查重比较,
# 否则多篇"抽不出DOI"的不同论文会互相被误判成重复(实测踩过这个坑: 两篇论文都填了 "unknown" 导致后一篇被误跳过)
MISSING_DOI_VALUES = {"n/a", "na", "unknown", "none", "null", "", "not available", "not found", "not provided"}


def is_missing_doi(doi: str) -> bool:
    return (doi or "").strip().lower() in MISSING_DOI_VALUES


def existing_dois() -> set:
    if not BIB.exists():
        return set()
    text = BIB.read_text(encoding="utf-8")
    found = (d.lower() for d in re.findall(r"doi\s*=\s*\{([^}]+)\}", text, re.I))
    return {d for d in found if not is_missing_doi(d)}


CROSSREF_MATCH_THRESHOLD = 0.90  # 返回结果标题和查询标题的相似度阈值, 达不到就不采信(Crossref模糊搜索会给错误匹配, 见 AGENTS.md 踩坑记录)


def short_authors_display(authors_full: list[str]) -> str:
    if not authors_full:
        return ""
    if len(authors_full) == 1:
        return authors_full[0]
    return f"{authors_full[0]} et al."


def lookup_metadata_via_crossref(title: str, timeout: float = 10.0) -> dict | None:
    """DeepSeek API 本身不能联网搜索, 用 Crossref 的免费公开API按标题正查(不需要API key)。
    取前几条结果, 跳过 type=="component" 的记录(Crossref 把 Supporting Information 也单独收录,
    标题和正文一模一样但 DOI 是 xxx.s001 这种后缀, 实测过这是 Crossref 常见的返回坑), 只挑第一个
    journal-article。找到后必须核对返回标题与查询标题的相似度, 达不到阈值就不采信(避免模糊匹配
    返回不相关论文)。查不到/网络出错都返回 None, 不抛异常打断整个批次。

    2026-07-18 改版: Crossref 从"DOI 缺失时兜底"升级为"citekey 三要素(年份/期刊/DOI)的权威源"——
    只要 DeepSeek 抽出了标题就正查一次, 命中则用 Crossref 的 doi/title/journal/year/author 覆盖
    DeepSeek 抽取结果(AI 抽取只保留 Crossref 给不了的字段: 表征方法/摘要要点/笔记正文等), 这样
    citekey 的三个组成部分全部来自权威源, 比只依赖AI抽取更不容易出现"张冠李戴"式的错误(见
    AGENTS.md 踩坑记录: Single-Atom-Nano-Islands 那次 DOI 张冠李戴)。返回 None 时上层应该原样
    保留 DeepSeek 抽取结果, 不能因为 Crossref 查不到就判定论文有问题。"""
    title = (title or "").strip()
    if not title:
        return None
    import urllib.parse
    import urllib.request

    url = "https://api.crossref.org/works?" + urllib.parse.urlencode({
        "query.bibliographic": title,
        "rows": 5,
    })
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Claude_reference literature tool (mailto:wioliche@gmail.com)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("message", {}).get("items", [])
        norm_query = re.sub(r"\s+", " ", title).strip().lower()
        for item in items:
            if item.get("type") == "component":  # SI/补充材料记录, 跳过
                continue
            returned_title = (item.get("title") or [""])[0]
            ratio = difflib.SequenceMatcher(
                None, norm_query, re.sub(r"\s+", " ", returned_title).strip().lower()
            ).ratio()
            if ratio >= CROSSREF_MATCH_THRESHOLD:
                authors_full = [
                    " ".join(p for p in (a.get("given"), a.get("family")) if p)
                    for a in item.get("author", []) if a.get("family")
                ]
                year = None
                for date_field in ("published-print", "published-online", "published", "created"):
                    parts = item.get(date_field, {}).get("date-parts")
                    if parts and parts[0]:
                        year = parts[0][0]
                        break
                return {
                    "doi": item.get("DOI"),
                    "title": returned_title,
                    "journal": (item.get("container-title") or [""])[0],
                    "year": year,
                    "authors_full": authors_full,
                }
        return None
    except Exception:
        return None


def lookup_doi_via_crossref(title: str, timeout: float = 10.0) -> str | None:
    """向后兼容包装: 只要 DOI 字符串。新代码请用 lookup_metadata_via_crossref。"""
    result = lookup_metadata_via_crossref(title, timeout)
    return result["doi"] if result else None


TITLE_DUP_THRESHOLD = 0.90  # 标题相似度查重阈值(应对无DOI论文绕过DOI查重的情况, 见 AGENTS.md 踩坑记录)

# 标题抽取失败时的占位值。不能参与标题查重比较, 否则两篇标题都抽取失败(都填"unknown")的
# 不同论文会互相被误判成重复——这是和 MISSING_DOI_VALUES 完全同类的坑, 已实测踩过一次
# (41467_2021_26336_MOESM1_ESM.pdf 因为是没识别出来的SI附件, 标题抽取失败返回unknown,
# 结果和另一篇同样抽取失败的论文互相撞车被跳过)。
MISSING_TITLE_VALUES = {"unknown", "n/a", "na", "none", "null", "", "untitled", "not available", "not found"}


def is_missing_title(title: str) -> bool:
    return (title or "").strip().lower() in MISSING_TITLE_VALUES


def existing_titles_with_doi() -> dict:
    """返回 {归一化标题: doi(小写, 缺失则为'n/a')}, 解析自 library.bib。
    要同时带 doi 是因为查重不能只看"新论文自己有没有DOI"——实测踩过坑: 已有条目 doi=N/A,
    新论文这次抽到了(哪怕是抽错的)DOI, 会导致标题查重被跳过, 同一篇论文进库3次。"""
    if not BIB.exists():
        return {}
    text = BIB.read_text(encoding="utf-8")
    result = {}
    for m in re.finditer(r"@article\{[^,]+,.*?title\s*=\s*\{([^}]*)\}.*?doi\s*=\s*\{([^}]*)\}", text, re.S):
        title, doi = m.group(1), m.group(2)
        norm = re.sub(r"\s+", " ", title).strip().lower()
        if norm and not is_missing_title(norm):
            result[norm] = doi.strip().lower()
    return result


def find_title_duplicate(title: str, title_doi_map: dict):
    """返回 (matched_norm_title, existing_doi) 或 None。"""
    if is_missing_title(title):
        return None  # 标题本身抽取失败, 没法可靠比对, 不要拿占位值互相误判
    norm = re.sub(r"\s+", " ", title or "").strip().lower()
    if not norm:
        return None
    best, best_ratio = None, 0.0
    for t, d in title_doi_map.items():
        ratio = difflib.SequenceMatcher(None, norm, t).ratio()
        if ratio >= TITLE_DUP_THRESHOLD and ratio > best_ratio:
            best, best_ratio = (t, d), ratio
    return best


def existing_citekeys() -> set:
    keys = set()
    if BIB.exists():
        keys |= set(re.findall(r"@article\{([^,]+),", BIB.read_text(encoding="utf-8")))
    if NOTES.exists():
        keys |= {p.stem for p in NOTES.glob("*.md")}
    return keys


def extract_pdf_text(pdf_path: Path, max_pages: int | None = None, max_chars: int = 200_000) -> str:
    """抽全文文字(2026-07-19 起默认全文, 不再只抽前几页)。
    历史教训: 之前默认只抽前8页/20000字符, "质疑与局限"/"值得追的参考文献"等依赖讨论章节和
    参考文献列表的字段系统性地只能写"摘录部分未见..."这种占位话——不是 DeepSeek 偷懒, 是给它的
    输入本来就没有全文最后部分的内容。max_chars=200000(约5万token)是防御极端超长PDF(如附带
    大量SI页码或扫描噪声)的兜底上限, 常规论文(15~40页)的全文远用不到这个上限。
    max_pages 保留参数位置供需要时限制(如只想要前N页), 默认 None 表示不限页数。"""
    doc = fitz.open(pdf_path)
    text = ""
    page_limit = doc.page_count if max_pages is None else min(max_pages, doc.page_count)
    for i in range(page_limit):
        text += doc[i].get_text()
        if len(text) >= max_chars:
            break
    doc.close()
    return text[:max_chars]


def call_deepseek(client: OpenAI, model: str, pdf_text: str, reasoning_effort: str = "low") -> dict:
    # 注: 据 DeepSeek 文档(2026-07-17查), 目前 low/medium 会向后兼容映射成 high, 只有 high/max 真正生效;
    # 这里仍传 low, 面向未来一旦该参数真正分级生效就自动降低成本/延迟, 现在大概率是no-op。
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": build_extract_system_prompt()},
            {"role": "user", "content": pdf_text},
        ],
        temperature=0,
        response_format={"type": "json_object"},
        reasoning_effort=reasoning_effort,
    )
    return json.loads(resp.choices[0].message.content)


def make_citekey(year, journal_abbr: str, condensed_title: str, taken: set) -> str:
    """格式(2026-07-17改版): <年份>-<期刊简称>-<提炼标题>, 见 AGENTS.md citekey 规则。
    journal_abbr/condensed_title 直接采信 DeepSeek 生成的大小写(元素符号/OER等缩写应已是规范大小写),
    这里只清理非法文件名字符, 不强制转小写(会破坏 TiO2/OER 这类大小写)。"""
    abbr = re.sub(r"[^A-Za-z0-9]", "", journal_abbr or "Journal") or "Journal"
    title = re.sub(r"[^A-Za-z0-9-]", "", (condensed_title or "Paper").replace(" ", "-"))
    title = re.sub(r"-+", "-", title).strip("-") or "Paper"
    base = f"{year}-{abbr}-{title}"
    key = base
    n = 2
    while key in taken:
        key = f"{base}-{n}"
        n += 1
    return key


def bib_entry(citekey: str, meta: dict) -> str:
    # BibTeX 惯例是列全部作者(用 "and" 连接), 截断显示是引用样式的事, 不是 .bib 文件的事;
    # authors_full 缺失(如旧数据回填前)才退回 authors_display("et al." 简写)
    authors_full = meta.get("authors_full") or []
    author_field = " and ".join(authors_full) if authors_full else meta.get("authors_display", "")
    return (
        f"@article{{{citekey},\n"
        f"  title   = {{{meta['title']}}},\n"
        f"  author  = {{{author_field}}},\n"
        f"  journal = {{{meta['journal']}}},\n"
        f"  year    = {{{meta['year']}}},\n"
        f"  doi     = {{{meta['doi']}}}\n"
        f"}}\n"
    )


def si_label(si_dest_names: list[str]) -> str:
    exts = set()
    for name in si_dest_names:
        ext = Path(name).suffix.lower()
        exts.add("Word" if ext in SI_EXTS else "PDF")
    if not exts:
        return ""
    return "+".join(sorted(exts))


def note_content(citekey: str, meta: dict, si_dest_names: list[str]) -> str:
    tags = meta.get("tags") or []
    tags_str = "[" + ", ".join(tags) + "]"
    keywords = meta.get("keywords") or []
    keywords_str = "[" + ", ".join(keywords) + "]"
    characterization = meta.get("表征方法") or []
    characterization_str = "[" + ", ".join(characterization) + "]"
    authors_full = meta.get("authors_full") or []
    # 作者名可能含逗号(如 "Smith, Jr.")等特殊字符, 加引号避免破坏 YAML 行内数组解析
    authors_full_str = "[" + ", ".join(f'"{a}"' for a in authors_full) + "]"
    si_str = "[" + ", ".join(si_dest_names) + "]"
    today = date.today().isoformat()
    frontmatter = f"""---
citekey: {citekey}
title: {meta['title']}
authors: {meta['authors_display']}
authors_full: {authors_full_str}
year: {meta['year']}
journal: {meta['journal']}
doi: {meta['doi']}
tags: {tags_str}
keywords: {keywords_str}
类型: {meta['类型']}
方法关键词: {meta['方法关键词']}
表征方法: {characterization_str}
体系: {meta['体系']}
status: skimmed
rating:
related: []
si_files: {si_str}
added: {today}
---"""
    body = f"""

## 三句话总结
{meta.get('summary_3lines', '')}

## 研究问题与核心结论
{meta.get('problem_conclusion', '')}

## 方法要点
{meta.get('method_points', '')}

## 关键图表与数据
{meta.get('key_results', '')}

## 与我课题的关联
{meta.get('relevance', '')}

## 质疑与局限
{meta.get('caveats', '')}

## 值得追的参考文献
{meta.get('further_reading', '')}
"""
    return frontmatter + body


# ---------- 人类可读版本(折行) ----------

_WORD_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.+/]*|.", re.S)


def wrap_line(text: str, width: int = READABLE_WIDTH) -> str:
    """按字符数折行, 每行不超过 width 个字符。中英文混排: 把连续的英文/数字/常见连接符
    (如 Nafion, IrO2-dissolution, 10.1021/xxx)当一个不可拆的 token 处理, 避免把英文单词
    从中间切断(如 Nafion 拆成 Na/fion); 中文按单字自由折行。"""
    if not text:
        return text
    tokens = _WORD_TOKEN_RE.findall(text)
    out_lines: list[str] = []
    line = ""
    for tok in tokens:
        if line and len(line) + len(tok) > width:
            out_lines.append(line)
            line = ""
        line += tok
        if len(line) >= width and len(tok) >= width:
            # 单个 token 本身就超过 width(如长 DOI/URL), 没法再拆, 只能让它单独成一行
            out_lines.append(line)
            line = ""
    if line:
        out_lines.append(line)
    return "\n".join(l.lstrip(" ") if i > 0 else l for i, l in enumerate(out_lines))


def make_readable(markdown_text: str, width: int = READABLE_WIDTH) -> str:
    """frontmatter 和标题行原样保留(YAML/标题不能被折行破坏), 只把正文段落折行。"""
    if markdown_text.startswith("---"):
        end = markdown_text.index("---", 3) + 3
        fm, body = markdown_text[:end], markdown_text[end:]
    else:
        fm, body = "", markdown_text
    out = [fm]
    for line in body.split("\n"):
        if line.startswith("#") or line.strip() == "":
            out.append(line)
        else:
            out.append(wrap_line(line, width))
    return "\n".join(out)


def write_note(citekey: str, markdown_text: str):
    """同时写不分行的唯一真源(notes/)和折行的人读版本(notes-readable/)。"""
    NOTES.mkdir(exist_ok=True)
    NOTES_READABLE.mkdir(exist_ok=True)
    (NOTES / f"{citekey}.md").write_text(markdown_text, encoding="utf-8", newline="\n")
    (NOTES_READABLE / f"{citekey}.md").write_text(
        make_readable(markdown_text), encoding="utf-8", newline="\n")


def index_line(citekey: str, meta: dict, si_dest_names: list[str]) -> str:
    tags = ", ".join(meta.get("tags") or [])
    return f"| {citekey} | {meta['year']} | {meta['title']} | {tags} | skimmed | {si_label(si_dest_names)} |\n"


def append_index(line: str):
    text = INDEX.read_text(encoding="utf-8")
    text = text.rstrip("\n") + "\n" + line
    INDEX.write_text(text, encoding="utf-8", newline="\n")


def sync_index_count():
    text = INDEX.read_text(encoding="utf-8")
    # citekey 首字母必为大写(见 schema), 借此排除表头行"| citekey | ..."(小写 c 开头)
    n = len(re.findall(r"^\|\s*[A-Z]", text, re.M))
    text = re.sub(r"共 \d+ 篇", f"共 {n} 篇", text, count=1)
    INDEX.write_text(text, encoding="utf-8", newline="\n")


def render_done_table(done: list) -> str:
    """把本批新建条目渲染成 Markdown 表格(citekey/年份/期刊/标题/标签/SI), 标题过长截断。"""
    def trunc(s: str, n: int = 50) -> str:
        s = s or ""
        return s if len(s) <= n else s[: n - 1] + "…"

    lines = ["| citekey | 年份 | 期刊 | 标题 | 标签 | SI |", "|---|---|---|---|---|---|"]
    for d in done:
        lines.append(f"| {d['citekey']} | {d['year']} | {d['journal']} | {trunc(d['title'])} "
                      f"| {d['tags']} | {d['si']} |")
    return "\n".join(lines)


def run_one_batch(client, args, state, dois, citekeys, title_doi_map, main_to_si, all_mains, unmatched_si, weak_si=None) -> int:
    """跑一批(最多 args.limit 篇), 返回本批实际处理的主文献数(0 表示没有可处理的了)。"""
    # error 状态的文件不算"已处理", 下次重跑时应该重试(比如 API 连接失败是临时性的)
    # 但重试超过 3 次仍然失败的就不再自动重试, 避免 --until-done 无限循环
    _DONE_STATUSES = {"done", "skipped_duplicate", "skipped_non_journal"}
    _MAX_RETRIES = 3
    todo = [p for p in all_mains
            if str(p) not in state
            or (state[str(p)].get("status") not in _DONE_STATUSES
                and state[str(p)].get("retries", 0) < _MAX_RETRIES)
            ][: args.limit]

    if not todo:
        print("没有待处理的新主文献(全部已在 state 记录里, 或源目录没有匹配文件)。")
        if unmatched_si:
            print(f"\n[!] 有 {len(unmatched_si)} 个文件疑似 SI 但配不上主文献, 需人工确认:")
            for f in unmatched_si:
                print(f"    - {f}")
        if weak_si:
            print(f"\n[?] 有 {len(weak_si)} 组弱信号疑似SI配对(仅因同文件夹只有2个文件, 未做文件名/内容验证), 不会自动绑定, 需人工确认:")
            for m, sis in weak_si.items():
                print(f"    - {m.name}  <->  {[s.name for s in sis]}")
        return 0

    print(f"源目录共 {len(all_mains)} 篇主文献(含 {sum(1 for v in main_to_si.values() if v)} 篇带SI), "
          f"已处理 {len(state)} 篇, 本批处理 {len(todo)} 篇。")

    done, skipped, errors, non_journal = [], [], [], []

    # ---- Phase 1: 并发抽取元数据 (DeepSeek + Crossref) ----
    # DeepSeek API 每次调用 ~10-30s, Crossref ~0.5-1s, 这是最大瓶颈。
    # 并发 3 路(避免触发 DeepSeek 速率限制), 只跑 API 调用部分,
    # 去重/写文件等共享状态操作留在 Phase 2 串行处理。
    _print_lock = threading.Lock()
    _INGEST_WORKERS = 3

    def _fetch_metadata(idx: int, pdf_path: Path) -> tuple[int, Path, str | None, dict | None]:
        """并发任务: 抽取 PDF 文字 + DeepSeek 元数据 + Crossref 核验。
        返回 (idx, pdf_path, text, meta)。text/meta 为 None 表示出错/跳过。"""
        try:
            text = extract_pdf_text(pdf_path)
            if len(text.strip()) < 50:
                return idx, pdf_path, None, None
            meta = call_deepseek(client, args.model, text, args.reasoning_effort)
            # Crossref 核验(不影响后续流程, 查不到就保留 DeepSeek 抽取结果)
            title_for_lookup = meta.get("title", "")
            if not is_missing_title(title_for_lookup):
                cr = lookup_metadata_via_crossref(title_for_lookup)
                if cr:
                    meta["doi"] = cr["doi"] or meta.get("doi")
                    meta["title"] = cr["title"] or meta["title"]
                    meta["journal"] = cr["journal"] or meta.get("journal")
                    meta["year"] = cr["year"] or meta.get("year")
                    if cr.get("authors_full"):
                        meta["authors_full"] = cr["authors_full"]
                        meta["authors_display"] = short_authors_display(cr["authors_full"])
            with _print_lock:
                print(f"  [API 完成 {idx+1}/{len(todo)}] {pdf_path.name}", flush=True)
            return idx, pdf_path, text, meta
        except Exception as e:
            with _print_lock:
                print(f"  [API 失败 {idx+1}/{len(todo)}] {pdf_path.name}: {e}", flush=True)
            return idx, pdf_path, None, {"__error__": str(e)}

    fetch_results = [None] * len(todo)
    with ThreadPoolExecutor(max_workers=_INGEST_WORKERS) as pool:
        futures = {pool.submit(_fetch_metadata, i, p): i for i, p in enumerate(todo)}
        for future in as_completed(futures):
            idx, pdf_path, text, meta = future.result()
            fetch_results[idx] = (pdf_path, text, meta)

    # ---- Phase 2: 串行去重 + 入库写文件 ----
    for pdf_path, text, meta in fetch_results:
        si_sources = main_to_si.get(pdf_path, [])
        try:
            # Phase 1 的失败/跳过在这里处理
            if text is None and meta is not None and "__error__" in meta:
                errors.append((pdf_path.name, meta["__error__"]))
                prev_retries = state.get(str(pdf_path), {}).get("retries", 0)
                state[str(pdf_path)] = {"status": "error", "error": meta["__error__"], "retries": prev_retries + 1}
                continue
            if text is None:
                errors.append((pdf_path.name, "PDF 抽不出文字(可能是扫描件), 跳过"))
                state[str(pdf_path)] = {"status": "error", "error": "PDF 抽不出文字(可能是扫描件)", "retries": _MAX_RETRIES}
                continue

            if args.skip_non_journal and meta.get("is_journal_article") is False:
                non_journal.append((pdf_path.name, meta.get("title", "unknown")))
                state[str(pdf_path)] = {"status": "skipped_non_journal", "title": meta.get("title", "")}
                continue

            # Crossref 核验已在 Phase 1 并发完成, 这里直接用 meta 里已核验的结果
            doi = (meta.get("doi") or "N/A").strip()

            if not is_missing_doi(doi) and doi.lower() in dois:
                skipped.append((pdf_path.name, f"DOI 重复: {doi}"))
                state[str(pdf_path)] = {"status": "skipped_duplicate"}
                continue

            # 标题相似度兜底查重: 不能只在"新论文自己没DOI"时才做——踩过的坑: 已有条目 doi=N/A,
            # 新论文这次抽到了(哪怕是抽错/抽串味的)DOI, 导致标题比对被跳过, 同一篇论文进库3次
            # (Heterophase-RuO2 那次两次抽出的DOI一个是"eaea4543"一个是"aea4543", 显然抽取本身
            # 就不可靠, 更不能仅凭"这次有抽出DOI"就断定不是重复)。规则: 标题高度相似时,
            # 只要任意一边(新论文或已有条目)缺DOI, 或者两边DOI相同, 就判定重复; 两边DOI都存在
            # 且明显不同才放行入库(但打印警告, 可能是DOI提取错误或真的是不同论文, 交给人工核实)。
            title_for_check = meta.get("title", "")
            title_match = find_title_duplicate(title_for_check, title_doi_map)
            if title_match:
                matched_title, existing_doi = title_match
                if is_missing_doi(doi) or is_missing_doi(existing_doi) or doi.lower() == existing_doi:
                    skipped.append((pdf_path.name, f"标题高度相似疑似重复: {title_for_check[:60]}"))
                    state[str(pdf_path)] = {"status": "skipped_duplicate"}
                    continue
                else:
                    errors.append((pdf_path.name,
                                   f"标题与已有文献高度相似但DOI不同(新: {doi}, 已有: {existing_doi}), "
                                   f"可能是DOI提取错误或确实是不同论文, 已照常入库但请人工核实: {title_for_check[:60]}"))

            citekey = make_citekey(meta.get("year", "unknown"), meta.get("journal_abbr"),
                                    meta.get("condensed_title"), citekeys)
            citekeys.add(citekey)
            if not is_missing_doi(doi):
                dois.add(doi.lower())
            if not is_missing_title(title_for_check):
                title_doi_map[re.sub(r"\s+", " ", title_for_check).strip().lower()] = doi.lower()

            si_dest_names = [f"{citekey}_SI{p.suffix.lower()}" for p in si_sources]

            if args.dry_run:
                si_info = f" (+ SI: {[p.name for p in si_sources]})" if si_sources else ""
                print(f"[dry-run] {pdf_path.name}{si_info} -> {citekey}\n"
                      f"{json.dumps(meta, ensure_ascii=False, indent=2)}\n")
                continue

            shutil.copy2(pdf_path, PAPERS / f"{citekey}.pdf")
            for si_src, si_dest_name in zip(si_sources, si_dest_names):
                shutil.copy2(si_src, PAPERS / si_dest_name)

            EXTRACTED_TEXT.mkdir(exist_ok=True)
            (EXTRACTED_TEXT / f"{citekey}.txt").write_text(text, encoding="utf-8", newline="\n")

            with BIB.open("a", encoding="utf-8", newline="\n") as f:
                f.write("\n" + bib_entry(citekey, meta))
            write_note(citekey, note_content(citekey, meta, si_dest_names))
            append_index(index_line(citekey, meta, si_dest_names))

            state[str(pdf_path)] = {"status": "done", "citekey": citekey, "si": si_dest_names}
            done.append({
                "citekey": citekey,
                "year": meta.get("year", ""),
                "journal": meta.get("journal", ""),
                "title": meta.get("title", ""),
                "tags": ", ".join(meta.get("tags") or []),
                "si": si_label(si_dest_names),
            })
        except Exception as e:
            errors.append((pdf_path.name, str(e)))
            prev_retries = state.get(str(pdf_path), {}).get("retries", 0)
            state[str(pdf_path)] = {"status": "error", "error": str(e), "retries": prev_retries + 1}

    if not args.dry_run:
        sync_index_count()
        save_state(state)

    print(f"\n本批完成: 新建 {len(done)} 篇, 跳过重复 {len(skipped)} 篇, 非期刊论文跳过 {len(non_journal)} 篇, 出错 {len(errors)} 篇")
    if done:
        print(render_done_table(done))
    if skipped:
        print("跳过(重复):")
        for name, reason in skipped:
            print(f"  - {name}: {reason}")
    if non_journal:
        print("跳过(非期刊论文):")
        for name, title in non_journal:
            print(f"  - {name}: {title}")
    if errors:
        print("出错/存疑(需人工看):")
        for name, reason in errors:
            print(f"  - {name}: {reason}")
    if unmatched_si:
        print(f"\n[!] 有 {len(unmatched_si)} 个文件疑似 SI 但配不上任何主文献, 需人工确认(不会自动处理):")
        for f in unmatched_si:
            print(f"    - {f}")
    if weak_si:
        print(f"\n[?] 有 {len(weak_si)} 组弱信号疑似SI配对(仅因同文件夹只有2个文件, 未做文件名/内容验证), 不会自动绑定, 需人工确认:")
        for m, sis in weak_si.items():
            print(f"    - {m.name}  <->  {[s.name for s in sis]}")
    remaining = len(all_mains) - len(state)
    if remaining > 0 and not args.dry_run:
        print(f"\n源目录还剩约 {remaining} 篇未处理。")

    return len(todo)


def main():
    sys.stdout.reconfigure(encoding="utf-8", write_through=True)  # write_through 防止子进程管道模式缓冲
    ap = argparse.ArgumentParser(description="批量粗建档: PDF(+SI) 源目录 -> 文献库")
    ap.add_argument("--source", required=True, help="PDF/SI 源目录(递归扫描), 只读")
    ap.add_argument("--limit", type=int, default=15, help="每批最多处理几篇主文献, 默认15")
    # 每篇同时产出元数据(爬取)和七节精读笔记正文(总结输出, 读者真正会看), 默认用 pro 档
    # 保质量, 不用便宜的 flash——量大时该多花的钱不省, 省的是"读一堆搜索结果人工挑候选"这类
    # 纯体力活的钱(那些脚本, 如 parse_search_results.py, 仍然默认 flash)。
    ap.add_argument("--model", default="deepseek-v4-pro")
    ap.add_argument("--reasoning-effort", default="low",
                     help="DeepSeek 推理强度: low/medium/high/max(据文档目前low/medium会映射成high)")
    ap.add_argument("--base-url", default="https://api.deepseek.com")
    ap.add_argument("--no-skip-non-journal", dest="skip_non_journal", action="store_false",
                     help="默认非期刊论文(教材/手册/学位论文/报告等)会被跳过不入库; 加此参数关闭该过滤, 照常全部入库")
    ap.set_defaults(skip_non_journal=True)
    ap.add_argument("--dry-run", action="store_true", help="只打印抽取结果, 不写入文件")
    ap.add_argument("--until-done", action="store_true", help="自动连续跑完所有批次(每批结束打印分隔标记), 不用重复手动执行")
    args = ap.parse_args()

    import os
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("请先设置环境变量 DEEPSEEK_API_KEY")
    client = OpenAI(api_key=api_key, base_url=args.base_url)

    source = Path(args.source)
    if not source.exists():
        sys.exit(f"源目录不存在: {source}")

    PAPERS.mkdir(exist_ok=True)
    NOTES.mkdir(exist_ok=True)
    NOTES_READABLE.mkdir(exist_ok=True)
    EXTRACTED_TEXT.mkdir(exist_ok=True)

    state = load_state()
    dois = existing_dois()
    citekeys = existing_citekeys()
    title_doi_map = existing_titles_with_doi()
    main_to_si, weak_si, unmatched_si = group_and_pair(source)
    all_mains = sorted(main_to_si.keys())

    batch_num = 0
    while True:
        batch_num += 1
        print(f"\n========== 批次 {batch_num} 开始 ==========", flush=True)
        n = run_one_batch(client, args, state, dois, citekeys, title_doi_map, main_to_si, all_mains, unmatched_si, weak_si)
        print(f"========== 批次 {batch_num} 结束 (本批处理 {n} 篇) ==========", flush=True)
        if not args.until_done or n == 0 or args.dry_run:
            break


if __name__ == "__main__":
    main()
