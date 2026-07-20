# 文献库内容生产规范（面向任意 LLM Agent / API，含 DeepSeek）——模板

> **这是一个可复用的文献笔记库框架**，原型是一个电催化/材料科学方向的文献库，本仓库把领域细节
> 抽掉后作为模板发布。标 **[领域定制]** 的部分（frontmatter 里的 `类型`/`方法关键词`/`表征方法`/`体系`
> 字段、标签词表、citekey 缩写示例）是原库的示例内容，**克隆后请先替换成你自己领域的字段和词表**，
> 其余部分（citekey 规则、SI 绑定、查重逻辑、笔记/notes-readable 双版本机制、体检脚本）是领域无关的。

本文件是本文献库**数据格式与内容生产流程**的唯一权威说明，与调用你的是什么模型/工具无关——纯 Markdown + YAML frontmatter + BibTeX，任何能读写文本文件的程序都可以照此执行。人类维护者用 Claude Code / Codex CLI / Kimi Code（或其他 Agent CLI）负责架构与索引一致性维护（见 `CLAUDE.md`），内容生产（读 PDF、提炼笔记、建 bib 条目）由你负责。

> **给 Codex CLI / Kimi Code 用户的提醒**：本文件（`AGENTS.md`）是这类工具会自动加载的项目指令文件，本规范里定义的内容生产流程（入库、笔记格式）开箱即用。但 `CLAUDE.md` 里的"架构维护者"角色说明（体检、查重、主题综述、引文献核实等进阶操作）**不会**自动加载——第一次对话时明确让它也读一下 `CLAUDE.md`，就能获得同样的操作能力。

## 你是怎么被调用的（前提能力）

本规范假设你运行在一个**有工具的 Agent 外壳里**，具备：读写本文件夹内文本文件、读取 PDF 文本、（可选）联网查 Crossref。裸的 chat API（只有文本进出、无文件权限）无法独立执行入库——那种情况下由外部脚本负责列文件、抽 PDF 文本、把你的输出写回磁盘，你只需按本规范返回**规范格式的笔记正文 + bib 条目 + INDEX 行**三段文本即可。

**如果你不具备读 PDF 或写文件的能力**：不要假装完成，明确说明你缺哪个能力、需要外壳提供什么（例如"请把 PDF 文本粘给我"或"请把我输出的三段内容分别写入 notes/xxx.md、library.bib、INDEX.md"），然后只产出文本，不谎报已落盘。

`scripts/ds.py` 是驱动 DeepSeek API 的调用工具（也可以换成任何 OpenAI 兼容 API），`scripts/batch_ingest.py` 是无人值守批量入库脚本；操作细节与可复制任务提示词见 `prompts/`。

## 目录与你的读写权限

```
<你的库根目录>/
├── AGENTS.md          只读: 本规范
├── CLAUDE.md           只读: 架构维护者(人类用的 Agent CLI)的角色说明
├── INDEX.md            可写: 追加/更新行,不要重排他人写的行
├── library.bib         可写: 追加条目
├── inbox/              读: 待处理 PDF 的来源
├── papers/             写: 归档后的 PDF (改名/复制到这里)
├── notes/              写: 笔记正文, 唯一真源(不折行)
├── notes-readable/     写: notes/ 的生成物, 正文按 60 字符折行给人看; 内容改动一律在 notes/ 做,
│                        改完调用 render_readable_notes.py 同步重生成对应文件
├── extracted-text/     写: 每篇论文抽取出的PDF文字缓存, <citekey>.txt, 入库时顺手存一份;
│                        以后加新字段/改抽取逻辑时优先从这里读文字重新调用 LLM, 不用重新读PDF
├── topics/             写: 主题综述(可选任务)
└── templates/note-template.md   只读: 笔记骨架,复制后填充,不要改模板本身
```

**notes/ 与 notes-readable/ 的关系**：`notes/` 是可被程序解析的唯一真源（frontmatter + 不折行的连续段落，方便 grep/YAML 解析/diff）；`notes-readable/` 是给人阅读用的派生版本，**正文段落每行不超过 60 个字符**（frontmatter 和标题行不折行，因为折行会破坏 YAML 解析）。两个文件夹里同一 citekey 的笔记内容必须一致（只是排版不同）。写笔记时：正常按无折行格式写入 `notes/`，然后运行 `python scripts/render_readable_notes.py` 把 `notes/` 全部重新同步到 `notes-readable/`。**永远不要手工编辑 `notes-readable/` 里的文件**——它是生成物，手改会在下次同步时被覆盖丢失。

**红线（绝对不可违反，克隆后请按自己的目录结构改这一节）**：
- 若本库的 PDF 来源于另一个只读文献管理系统（如 EndNote/Zotero 库、共享网盘目录），**绝不修改、移动、删除**源文件——只能复制。把你自己的源路径写在这里。
- 不要删除 `papers/`、`notes/`、`library.bib`、`INDEX.md` 中已有的条目，只做新增或在明确指令下的修改；批量删除前先列清单给人工确认。
- 不确定某个操作是否安全时，输出你打算做什么并停止，等待人工确认，不要自行执行破坏性操作。

## citekey 规则

格式：`<年份>-<期刊简称>-<提炼标题>`
- **年份**：4 位数字
- **期刊简称**：去空格、去句点、去 "The"/"Journal of" 等虚词。业界本来就是全大写缩写的期刊保持全大写（如 `JACS`、`PNAS`）；其余用驼峰式截断（如 `Nature Materials`→`NatMater`、`Advanced Materials`→`AdvMater`）。**每个期刊只能有一种缩写**——新造缩写前先检查该期刊是否已在库里出现过、用的什么写法（`grep -m1 "^journal:" notes/*.md | sort -u`），避免同一期刊出现两种缩写混用（历史教训：曾经 `ACIE`/`AngewChemIntEd` 两种写法混用，用 `scripts/rename_journal_abbr.py` 才统一）。
- **提炼标题**：3~6 个英文单词，连字符连接，概括标题核心内容
  - **[领域定制]** 领域内常见的专有名词/缩写（如化学元素符号、方法学缩写）按规范大小写书写，不要写成全小写
  - 其余普通单词用 Title Case（首字母大写）
  - 单词之间用连字符连接，不含空格/标点
- 同一年同一期刊多篇标题相近：提炼标题必须能区分主题，撞车时追加 `-2`、`-3` 后缀
- citekey 一旦写入必须在 `papers/<citekey>.pdf`、`notes/<citekey>.md`、`notes-readable/<citekey>.md`、`library.bib` 的 `@article{<citekey>,`、`INDEX.md` 对应行五处保持完全一致（区分大小写）

## 笔记文件格式（`notes/<citekey>.md`）

必须是合法 YAML frontmatter（`---` 包裹）+ Markdown 正文，字段如下（表来自 `templates/note-template.md`，可直接复制该文件后填写）：

```yaml
---
citekey: string, 必填, 见上方规则
title: string, 必填, 论文原题
authors: string, 必填, 格式 "第一作者 et al." 或前三位作者用分号分隔, 简短显示用
authors_full: array of string, 必填, 完整作者名单(按原文出现顺序全部列出), 每人一个数组元素;
  `library.bib` 的 author 字段也用这个, 用 " and " 连接
year: integer, 必填
journal: string, 必填
doi: string, 必填(若确实无 DOI 则填 "N/A")
tags: array of string, 必填, 至少 1 个, 优先从下方词表选, 需要新标签就直接加入并同步补充词表
keywords: array of string, 必填 5~10 个, 自由细粒度关键词; 写前先看 KEYWORDS.md, 能复用已有词就复用
status: enum ["unread","skimmed","read"], 必填, AI 摘录生成的笔记默认 "skimmed", 人工精读全文确认后改 "read"
rating: string, 可选, 用 1~5 个 ★
related: array of string, 可选, 关联笔记的 citekey 列表
si_files: array of string, 必填(无 SI 时填 []), 见下方"SI(补充材料)绑定"一节
added: string, 必填, ISO 日期 YYYY-MM-DD

# --- 以下为 [领域定制] 字段, 原库(电催化/材料科学)示例, 换成你自己领域需要的结构化字段 ---
类型: enum ["计算", "实验", "计算+实验", "建模", "综述"], 必填
  # 计算 = 材料/原子尺度第一性原理或分子模拟(DFT/AIMD/MD 等), 只有全篇纯材料计算才填
  # 实验 = 有实体实验; 计算+实验 = 两者兼有
  # 建模 = 非材料原子尺度的系统级/宏观建模(能源-经济IAM、技术经济分析、纯数据/ML建模), 虽然也"算"但不是材料计算, 别误填"计算"
  # 综述 = 综述/展望类文章
方法关键词: string, 必填  # 计算类写: 泛函/AIMD与否; 实验类写: 主要表征手段; 建模类写: 模型名/情景/优化方法
表征方法: array of string, 必填(无实验表征则填 []), 标准化的表征技术列表
体系: string, 必填, 具体研究对象
---

## 三句话总结
## 研究问题与核心结论
## 方法要点
## 关键图表与数据
## 与我课题的关联
## 质疑与局限
## 值得追的参考文献
```

正文语言、专有名词/公式/参数数值的书写规范请按你自己的使用习惯定，本条是原库的约定（**中文为主**，专业术语/化学式/参数数值保留英文/原始写法），仅供参考。

## SI（补充材料）绑定

很多论文有配套的 Supporting Information / Supplementary Information，文件可能是 PDF 也可能是 Word（`.docx`/`.doc`），必须和主文献绑在一起收录，不能散落或搞丢配对关系。

**命名规则**：SI 文件统一改名为 `papers/<citekey>_SI.<原始扩展名>`，与主 PDF `papers/<citekey>.pdf` 同 citekey、同目录。若一篇论文有多个 SI 文件（如 PDF + Excel），每个都用 `<citekey>_SI.<ext>` 命名，扩展名自然区分，都写进 `si_files` 数组。

**识别一个文件是不是某篇论文的 SI，按下列线索判断，从上到下优先**：

1. **同一来源文件夹只有两个文件**（很多文献管理软件把一篇文献的所有附件放在同一个记录文件夹里）：一个是主 PDF，另一个（不管是 PDF 还是 Word）大概率就是它的 SI，直接配对；不确定时可以打开瞥一眼确认（Word/PDF 首页出现 "Supporting Information"/"Supplementary"/与主标题相同的字样即可确认）。
2. **文件名带 SI 特征词**：文件名（不分大小写）包含 `SI`、`ESI`、`supp`、`supporting`、`supplementary`、`supplemental` 之一，且去掉这些词及分隔符后剩余部分与某篇主 PDF 的文件名高度相似。
3. **内容确认**（前两条不够确定时）：打开候选 SI 文件首页，看是否出现主论文的标题或 DOI，出现则确认配对；读不到或对不上就不要瞎配。
4. **Word 文件默认怀疑是 SI**：正文文献基本都是 PDF，若源里出现 `.docx`/`.doc`，先假设它是某篇的 SI 去找配对对象。

**不确定时**：不要瞎配对。宁可把该文件列进入库汇总的"存疑"清单里报告给用户，附上你怀疑的配对对象和理由，也不要把不相关的 SI 错误地绑到某篇论文上——错配比不配更难发现和纠正。

**落地步骤**：确认配对后，把 SI 文件复制（只复制不移动源文件）并改名为 `papers/<citekey>_SI.<ext>`，在该 citekey 笔记的 frontmatter `si_files` 数组里写入这个文件名；没有 SI 的论文 `si_files: []`。

**跨文件夹配不上的 SI**：`scripts/match_orphan_si.py` 会读取每个孤儿 SI 首页文字，用 LLM 识别其声明所属的主论文标题，再与 `library.bib` 里已入库的标题做相似度匹配。相似度 ≥0.85 才自动绑定，0.55~0.85 之间只报告给人工确认，不自动写入。默认预览模式（不落盘），加 `--apply` 才真正绑定。

## BibTeX 条目格式（追加到 `library.bib`）

```bibtex
@article{2025-NatMater-Example-Citekey,
  title   = {论文英文原题},
  author  = {Zhou, T. and Li, X. and Zhao, J.},
  journal = {Nature Materials},
  year    = {2025},
  doi     = {10.1038/s41563-025-xxxxx-x}
}
```
- entry type 统一用 `@article`（除非确实是会议/书籍，用 `@inproceedings`/`@book`）
- 字段顺序不重要，但 `title/author/journal/year/doi` 必须齐全
- `author` 字段用 `authors_full`（完整作者名单）以 `and` 连接，不要用 "et al." 截断形式
- 追加前必须检查 `library.bib` 里没有相同 DOI（大小写不敏感比较），有重复则跳过并说明

## INDEX.md 更新

在表格末尾追加一行（不要重排已有行）：
```
| <citekey> | <year> | <title> | <tag1>, <tag2> | <status> | <SI标记> |
```
最后一列 SI 标记：有 SI 则写文件类型缩写（`PDF`/`Word`/`PDF+Word`），无 SI 留空。

## 入库任务的标准步骤

1. 确定来源 PDF（`inbox/` 或其他只读源）
2. 从 PDF 首页/已知信息提取：标题、作者、年份、期刊、DOI
3. 若能联网，用 Crossref 核对: `https://api.crossref.org/works/<DOI>` 校正规范元数据；无法联网则以 PDF 内信息为准，DOI 未知填 "N/A"。**PDF 文字里没抽到 DOI 时，改用 Crossref 按标题反查**: `https://api.crossref.org/works?query.bibliographic=<标题>&rows=5`（`batch_ingest.py` 里的 `lookup_doi_via_crossref()` 已实现）。查到后必须核对两件事再采信：① 返回的 `type` 字段不能是 `component`（Crossref 把论文的 Supporting Information 也单独收录索引，DOI 带 `.s001` 这类后缀会被误当成正文 DOI）；② 返回标题与查询标题相似度要够高（阈值 0.90）。两条都满足才采信 DOI，否则保留 "N/A"，不要瞎猜。
4. 按 citekey 规则生成 key，检查 `library.bib` 与 `notes/` 中是否已存在相同 DOI/citekey，若存在则跳过并报告
5. 按上方"SI 绑定"一节判断来源里是否有该论文的 SI，确认配对的复制改名为 `papers/<citekey>_SI.<ext>`；拿不准就不配，列入存疑清单
6. PDF 复制/改名为 `papers/<citekey>.pdf`
7. 追加 `library.bib` 条目
8. 用模板生成 `notes/<citekey>.md`，frontmatter 必须填完整（含 `si_files`），按下方"批量入库策略"填七节正文，status 设 `skimmed`
9. 若用户额外要求人工精读全文核实，按"精读任务"补充/修正正文，status 设 `read`
10. 追加 `INDEX.md` 一行（含 SI 标记列）
11. 输出本次处理汇总：新增 citekey 列表、跳过的重复项、配对上的 SI、任何无法确定的字段或无法确认配对的 SI 文件
12. **确认无误后删除 `inbox/` 里已成功入库的源文件**（papers/notes/bib/INDEX 五处都已落盘、且该篇未被列入"存疑"清单时才删）。跳过的重复项、存疑未配对的文件保留在 `inbox/`，不要删。

## 批量入库策略（"把所有 PDF 入库"时务必遵守）

默认每篇都要填完整七节正文（不是"先粗建档留空、按需精读"的两档模式）：

1. 读每篇 PDF **前 6~8 页**（覆盖摘要/引言/方法/部分结果，不是全文——读不到的内容如实说明，不要编）
2. 提取元数据 + 生成七节正文
3. `status` 设为 `skimmed`（这是 AI 基于摘录生成的笔记，不是人工逐字精读，如实标注状态，不要标 `read`）
4. `keywords` 同步按 5~10 个规则填，不留空

批量作业时（以下是几条真实踩过的坑，建议保留）：
- **分批处理**，每批 10~20 篇，每批结束输出进度小结（已处理/跳过/存疑），不要一口气吞掉整个目录导致上下文溢出或中途失败难以恢复。
- 每篇处理前先查重（DOI / citekey 已存在则跳过并计入"跳过"清单）。**DOI 缺失时（综述、学位论文等常没有 DOI）改用标题相似度查重兜底**——只查 DOI 会漏掉同一篇无 DOI 论文被重复入库多次的情况。
- **DOI/标题"缺失"的占位值不止 "N/A"**：模型有时会写 unknown/none/null 等，这些都不能参与查重比对（否则会反过来把两篇不同的"缺失"论文互相误判为重复）。
- **标题查重不能只在"新论文自己没DOI"时才触发**：曾经的逻辑是"只有当前处理的这篇论文抽不到DOI时才做标题比对"，结果同一篇论文因为不同次抽取到不同（且都不可靠）的DOI而重复入库了 3 次。现在的规则：标题高度相似时，只要任意一方缺 DOI、或者两边 DOI 相同，就判定重复；只有两边都有 DOI 且明显不同，才放行入库并打印警告供人工核实。
- **DOI 提取本身可能被"张冠李戴"，不要无条件信任已入库的 DOI**：处理任何 DOI 冲突/重复告警时，两边的 DOI 都要独立用 `https://api.crossref.org/works/<DOI>` 反查标题核实，不能默认先入库的就是对的。
- **SI 命名惯例因出版商而异**：Springer/Nature 常用 `<文章编号>_MOESM1_ESM.pdf`，Wiley 常用 `<文章编号>-sup-0001-suppmat.pdf/docx`，Elsevier 常用 `mmc1`/`mmc2`。没识别出这些惯例会导致 SI 文件被误当成独立主文献处理。
- **文件名相似度配对在杂乱文件夹里非常不可靠**：短文件名/纯数字片段容易造成误匹配；两边归一化后的文件名建议都要求达到一定长度且非纯数字，用 difflib 相似度且阈值调高，宁可漏配丢进"未匹配"清单让人工确认，也不要在文件名层面强行乱配。
- **"同文件夹只有2个文件就配对"这条规则只适用于规整来源**：杂乱下载目录里容易把论文和无关文档误判成 SI 关系，建议分两档处理——有实际关联证据的才自动绑定，只是"文件夹里恰好剩2个文件"这种弱信号只报告给人工确认。
- **具体数值字段只写摘录文字里明确出现的**，摘录范围内没提到就如实写"原文未提供该数值，建议查全文/SI"，绝不臆测。
- 若用户明确要求"先粗建档就好、不用精读"，退回旧版两档模式：正文留空、`status: unread`，之后按需再精读补正文改 `status: read`。
- **重复收录的判定与取舍**：`scripts/build_topic_digest.py`（或 `scripts/find_duplicate_titles.py` 做全库扫描）会自动做标题相似度查重（difflib，阈值0.90）。确认是真重复后，**用 `scripts/resolve_duplicate.py --winner <保留> --loser <删除>` 处理**，不要手动逐处改 5 个位置（papers/notes/notes-readable/extracted-text/library.bib/INDEX.md）。脚本会自动把"劣势版本"独有的更优字段（有效 DOI、非空 keywords、si_files 并集）合并进保留版本。先跑一遍不加 `--apply` 的预览模式确认合并计划，再执行。
- **受控词表的近义词要在设计阶段就合并**，不要等积累了几十行数据后才发现要清理——新增词表值前，先检查是否已有语义等价的既有值可以复用。

## 精读任务的标准步骤

1. 读 `papers/<citekey>.pdf` 全文
2. 按上方笔记正文七个小节填写
3. **[领域定制]** 补齐领域相关的方法学细节（原库示例：计算类补泛函/+U/k点/模型尺寸；实验类补表征手段/实验条件/样品体系）
4. 补 `keywords`（5~10 个，先看 `KEYWORDS.md` 复用已有词）
5. 更新该笔记 frontmatter 的 `status: read`，可补充 `rating`、`related`
6. 同步更新 `INDEX.md` 对应行的 status 列

## tags 与 keywords 的区别（重要）

- **tags（受控词表，粗）**：只从词表选，1~4 个，用于大类导航和粗筛。要加新 tag 必须说明理由并同步补进词表，保持词表小而稳。
- **keywords（自由词，细）**：不受词表约束，5~10 个，写具体的细粒度概念/方法名/体系名等。目的是精确检索。**写之前先读 `KEYWORDS.md`**，能复用就复用，尽量不造近义词。

## [领域定制] 表征方法命名规范

原库（电催化/材料科学）额外定义了一个 `表征方法` 字段，专门收录论文里实际用到的表征/分析技术，命名要标准化。这是领域相关的规范，非本领域用户可以整段删除或换成自己需要的结构化字段（比如做社会科学文献的可以换成"研究方法/样本量/数据来源"）。

- 纯缩写技术直接大写书写：`XPS`、`TEM`、`XRD` 等
- 带"原位/操作/非原位"前缀的技术：前缀词和技术名之间用**空格**分开，如 `in situ Raman`、`operando XRD`，不要写 `insitu-Raman`
- 同一技术不要既写缩写又写全称造成重复，全库统一
- 原文表述含糊时，只列出文中明确点名的技术，不要为了填满而编造

## [领域定制] 标签词表示例

以下是原库（电催化/材料科学，覆盖面不限于电催化，用户的文献库本身就跨领域）用过的词表，仅供参考格式，**克隆后请替换成你自己领域的标签体系**：

`OER` `HER` `DFT` `原位表征` `综述` `方法学` `电池` `2D材料` `钙钛矿` `结构生物学` `高分子` `单原子催化` `拉曼光谱` `半导体光电` ...

论文明显不属于已有分类时才新增标签，不要为了凑数把无关论文硬塞进已有标签。

## 校验

生产完内容后，人工维护者会跑 `scripts/check_library.sh` 核对 `papers/`、`notes/`、`library.bib` 三方是否一一对应、有无重复 DOI/标题、以及 `notes-readable/` 是否与 `notes/` 同步。请尽量一次性保证同步落地，减少不一致。也可参考 `schema/note-frontmatter.schema.json` 自行校验 frontmatter 格式。
