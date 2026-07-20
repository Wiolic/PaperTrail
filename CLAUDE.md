# 文献管理系统 — 架构维护者说明（模板）

> 这是给 Claude Code（或其他支持项目级指令文件的 Agent CLI）用的角色说明模板。克隆本仓库、
> 初始化自己的文献库后，把下面方括号 `[...]` 里的占位内容换成你自己的情况即可。

本库是「笔记与知识层」：`[你的文献管理软件，如 EndNote/Zotero]` 继续负责文献收集和 Word 引文；本库负责精读笔记、标签检索、主题综述和自然语言问答。

**数据格式与内容生产流程的唯一权威说明是 [`AGENTS.md`](AGENTS.md)**——citekey 规则、笔记 frontmatter 字段、bib 格式、入库/精读步骤都写在那里，供任意 LLM/API 照做。本文件只讲**架构维护者（我）的角色**：不写笔记内容，负责搭骨架、维护索引一致性、体检、按需求调整目录结构和规范本身。改规则时**只改 `AGENTS.md`**，本文件保持简短，不要和它重复/冲突。

## ★ 红线 ★（与 AGENTS.md 一致，双重保险）

- 若 PDF 来源于外部只读系统（EndNote 库、共享网盘等），**绝不修改、移动、删除**那里的任何文件——只能复制。
- 本库内也不做批量删除；要清理先列清单给用户确认。

## 目录结构

```
AGENTS.md      ★ 数据格式与内容生产规范(唯一真源,给 DeepSeek/任意 API 读)
CLAUDE.md      本文件:架构维护者(我)的角色说明
INDEX.md       总索引表
library.bib    主 BibTeX 库
inbox/         入口:新 PDF 待整理
papers/        正式馆藏 PDF, <citekey>.pdf
notes/         笔记, <citekey>.md, 唯一真源(不折行)
notes-readable/ notes/ 的生成物, 正文按60字符折行给人看, 勿手编(见 AGENTS.md)
extracted-text/ 每篇论文PDF抽取文字缓存, <citekey>.txt, 以后加字段/改逻辑优先复用这个, 不用重新读PDF
topics/        主题综述页
exports/       导出给用户的成品文档(如 docx 综述), 生成物, 不是真源
data/          结构化数据层(可选), 如按论文抽取的性能指标/数值 csv, 受控词表见对应脚本
templates/     note-template.md (与 AGENTS.md 字段定义保持同步)
KEYWORDS.md    关键词索引(脚本自动生成,勿手编)
schema/        note-frontmatter.schema.json (供程序化校验 frontmatter)
scripts/       见下方"脚本一览"
prompts/       操作指南与可复制提示词
```

## 我的职责

1. **初次/结构变更**：搭目录、维护 `templates/`、`schema/`、`scripts/`；这些改动后同步更新 `AGENTS.md` 对应描述，避免两边漂移。
2. **体检**：用户说"体检"时跑 `bash scripts/check_library.sh`（三方对账 + 全库标题查重）+ `bash scripts/build_keyword_index.sh`（重建 `KEYWORDS.md`），报告 papers/notes/bib 三方不一致（孤儿 PDF、缺笔记、bib 缺条目、重复 DOI/标题），需要时按 schema 抽查 frontmatter 是否合规。发现关键词近义词泛滥时提示用户合并。
3. **索引整理**：`INDEX.md` 按年份倒序排版、去重、格式统一（内容生产只管追加，排序/清理是我的事）。
4. **检索与综述**：用户直接问文献内容/要主题综述时，我读 `notes/` 回答，回答必须带 citekey 出处；`topics/<主题>.md` 综述页含对比表、观点分歧、空白点，引用笔记用 `[[citekey]]`。
   - **标准工作流**：① `python scripts/build_topic_digest.py --tags <标签> --keyword-regex <正则> --out <摘要路径>` 按标签/关键词筛出候选笔记、抽取方法要点摘录，并自动做标题相似度查重；② 我读摘要人工归类、写 `topics/<主题>.md`；③ 若要 docx 交付物，用 `python scripts/md_to_docx.py topics/<主题>.md --out exports/<文件名>.docx` 转换（没装 pandoc/node/LibreOffice 时用 python-docx 实现）。
5. **内容生产：优先派给 LLM API 做体力活**：整理 inbox、批量入库这类任务，不是自己读完整篇 PDF 再手写元数据（贵、慢），而是用 `scripts/ds.py` 把可外包的体力活丢给 DeepSeek（或换成任何 OpenAI 兼容 API）：
   - `python scripts/ds.py pdf-meta <PDF路径>` —— 抽首页文字 + 调 LLM 按 `batch_ingest.py` 里定义的字段返回 JSON 元数据，我审核结果（查重、DOI 是否可信、SI 配对判断这些仍由我做判断，不甩给 LLM），确认无误后自己写入 `papers/`、`library.bib`、`notes/`、`INDEX.md`。
   - `python scripts/ds.py chat`/`json` —— 通用调用，起草摘要、建议关键词、批量性能数据抽取（`extract_performance.py`）等零碎/重复性任务都能这样外包。
   - `scripts/batch_ingest.py` 是无人值守全自动批处理路线，量大、不需要人工逐篇交互时用这条；人工交互任务用 `ds.py` 这条。
   - 若用户只是要"读某篇文献写详细笔记"这种需要深度理解、判断、和已有笔记关联的任务，自己读 PDF 做，不外包给 LLM（LLM 适合做批量的、格式化的抽取，不适合替代精读判断）。
   - **原则：只要是"可以明确定义输入输出格式、不需要跨笔记判断"的活（元数据抽取、摘要起草、关键词建议、结构化数值抽取），都优先派给 LLM API 处理，节省人力和高阶模型的调用成本；需要跨笔记比对、查重判断、可靠性核实（如 DOI 核对）这类需要"记住上下文+做判断"的活，自己做。**

## 常用工作流（指令名可以自己改，思路照搬即可）

- **"入库"**：把 `inbox/` 里的 PDF 按 `AGENTS.md`"入库任务的标准步骤"处理——`ds.py pdf-meta` 抽取元数据/正文 + 我审核查重/DOI/SI 配对 + 写入 papers/library.bib/notes/notes-readable/extracted-text/INDEX.md，成功入库和确认重复的都直接删除 `inbox/` 里的源文件，只有存疑未配对的留着。
  - **接续能力**：`ds.py pdf-meta` 的输出建议缓存到 `inbox/.meta_cache/<PDF文件名>.json` 而不是只存在会话临时目录——这样中途中断重连后，已经花 LLM 调用抽取过的文件不用重新抽取，直接复用缓存 JSON 进入审核+`ingest_from_meta.py` 落盘这一步。一篇成功落盘后连同源 PDF 一起删除对应缓存；跳过/存疑的缓存也保留着等下次一起处理。
  - 日常单篇/小批量交互式入库用 `ds.py pdf-meta` + `ingest_from_meta.py`（省去手动重打七节正文），无人值守大批量用 `batch_ingest.py`。
- **"体检"**：跑 `bash scripts/check_library.sh`（三方对账 + 全库标题查重）+ `bash scripts/build_keyword_index.sh`（重建 `KEYWORDS.md`）。
- **主题综述**：① `build_topic_digest.py --tags <标签> --keyword-regex <正则> --out <路径> [--draft]` 筛候选笔记+自动查重，可选让 LLM 顺带起草分类初稿；② 核对 `[[citekey]]` 引用真实存在、补写需要判断的小节，定稿成 `topics/<主题>.md`；③ `md_to_docx.py` 转 docx 交付物。
- **"引文献"**：给一句/一段要写进论文的话逐句添加可引用的文献，不是简单关键词命中就算——`find_citations.py --claim "<陈述句>" --keyword-regex "<正则>" --out <路径> --draft` 单句模式，或 `--paragraph "<整段正文>"` 让脚本先自动拆分成多条论点再逐条判断 support/contradict/unclear，最终引用决定权在人。**输出格式是按原文顺序逐句对照的清单**（"原文第1句：...→引文列表；原文第2句：...→无需引用；..."），不是先分组再罗列——这样可以直接顺着原文读下来核对，不用自己对照分散的论点编号。
- **导出给 EndNote/文献管理软件**：`export_for_endnote.py --citekeys <逗号分隔citekey> --out exports/<文件名>.ris` 导出一份只含推荐引用的 RIS/BibTeX 文件供一次性批量导入，替代逐篇手动搜索。
- **"扩充"/"查新"**（搜某领域近N年 top journals 全量文献 / 只搜库上次扫描以来的新文章）：① 先跑 `scan_state.py show --field "<领域名>"` 看上次扫到哪天；② 用 WebSearch/浏览器按"期刊+关键词"多角度检索，同一领域按子类分别搜（不要只锚定单一关键词，容易漏掉措辞不同但相关的论文）；③ 把原始搜索结果存文件，跑 `parse_search_results.py --raw-file <文件> --context "<搜索目标>" --out candidates.json`，用便宜的 LLM 从噪声文字里抽出候选论文列表；④ 跑 `scan_new_papers.py --candidates candidates.json --out exports/<文件名>.xlsx`，对有 DOI 的候选查 Crossref 核验，对只有标题的候选做 Crossref 标题反查（不用 LLM 猜 DOI），核对完按 DOI/标题相似度去重、自动粗分类，同时输出 `.summary.txt`（只列需要下载的新条目，我读这个汇报即可，不用打开完整 xlsx）；⑤ 跑 `scan_state.py record --field "<领域名>" --date <今天> ...` 记录本次扫描供下次查新用。

## 脚本一览（`scripts/`）

- `check_library.sh`（Git Bash）：三方对账 + notes-readable 同步检查 + 全库标题查重
- `build_keyword_index.sh`（Git Bash）：重建 `KEYWORDS.md`
- `batch_ingest.py`：无人值守批量入库，写 notes/ 同时生成 notes-readable/
- `ds.py`：通用 LLM API 调用工具（chat/json/pdf-meta 三个子命令）
- `ingest_from_meta.py`：把 `ds.py pdf-meta` 产出的 JSON 直接组装成 papers/notes/notes-readable/extracted-text/bib/INDEX 六处，省去人工重打七节正文
- `render_readable_notes.py`：从 notes/ 全量重新同步 notes-readable/
- `regenerate_notes.py`：用 extracted-text 全文重新调用 LLM 重写笔记正文/关键词，断点续跑
- `match_orphan_si.py`：跨文件夹配不上的 SI 用标题相似度匹配已入库文献
- `extract_performance.py`：批量抽取结构化数值数据到 `data/*.csv`（[领域定制] 见文件内 SYSTEM_PROMPT）
- `build_topic_digest.py`：主题综述第一步，按 tags/关键词筛笔记 + 抽方法要点摘录 + 自动标题查重，`--draft` 可选起草分类初稿
- `find_citations.py`：给陈述句/整段正文逐句添加可引用的文献，判断 support/contradict/unclear
- `export_for_endnote.py`：把指定 citekey 列表导出成 RIS/BibTeX 供文献管理软件批量导入
- `scan_new_papers.py`：候选论文的 Crossref 核验/去重/分类，导出待下载 xlsx + 精简 summary
- `scan_state.py`：记录每个领域上次扫描到哪天，供"查新"只搜增量
- `parse_search_results.py`：把 WebSearch 原始结果交给便宜的 LLM 抽取候选论文列表，供 `scan_new_papers.py` 用
- `md_to_docx.py`：把 `topics/` 综述 Markdown 转 docx（python-docx 实现，无需 pandoc/node/LibreOffice）
- `find_duplicate_titles.py`：全库标题相似度查重，接入 `check_library.sh`
- `resolve_duplicate.py`：合并两个确认重复的 citekey（较优字段合并进 winner + 删 loser 五处文件），预览模式默认不落盘，加 `--apply` 执行
- `rename_journal_abbr.py`：批量改写 citekey 里的期刊简称（如统一混用的两种缩写）
- `export_referable_folder.py`：按标签/期刊/标题正则筛选，把 PDF+notes-readable 一起导出到独立文件夹
- 其余脚本（`backfill_fields.py`/`map_characterization.py`/`reconcile_state.py`/`remove_citekeys.py`/`rename_citekeys.py`）是历史维护脚本，用途见各自文件头部 docstring

## 环境依赖

- 需要 `DEEPSEEK_API_KEY`（或你换用的其他 LLM API 的 key）环境变量。
- Python 脚本依赖：见仓库根目录 `requirements.txt`（`openai`、`python-docx`）。
- 若在 Git Bash 里找不到 `python`/`python3`，Windows 上可以用 `py` 启动器。
