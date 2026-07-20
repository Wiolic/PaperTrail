# 对话驱动的文献精读笔记系统

一套**跟 AI 对话就能用**的个人文献知识库框架：把论文 PDF 变成结构化精读笔记，然后用大白话指挥 AI 帮你检索、去重体检、写论文时逐句找引文、在 Word 里插引用、发现领域新文献、生成主题综述——全部脚本化、可断点续跑，还带一个本地网页操作面板。

> ## 🚀 只想赶紧用起来？→ 直接看 **[QUICKSTART.md](QUICKSTART.md)**
>
> 核心流程只有三步：**装一个 AI 助手 → 下载本仓库 → 粘贴一段开场白让 AI 帮你配好环境**。之后你就用"入库""体检""帮我找这句话能引哪些文献"这样的大白话跟它对话。**不用会写代码、不用懂命令行。** QUICKSTART 里给了可以直接复制的开场白和每一步的具体做法。

---

## 这套系统的核心理念：两个 AI 分工

- **Agent CLI（总管）** —— Claude Code / Codex CLI / Kimi Code 任一个。跟你对话、读你本地文件、做需要判断的活：查重、核实 DOI、配对补充材料、归纳综述。
- **大模型 API（工人）** —— DeepSeek（默认，最便宜）或任何 OpenAI 兼容服务。被总管调用，便宜地跑批量体力活：读 PDF、抽元数据、起草笔记。

你几乎从不直接碰命令行——你对总管说人话，总管决定要不要调工人、要不要跑哪个脚本。**这不是"上传论文自动总结"的一次性小工具，而是一套长期维护型个人文献库的工作流规范**（灵感类似 Zotero/Obsidian，但笔记生产主要由 AI 完成、人负责审核和一致性维护）。

**不锁定任何一家**：Agent CLI 三选一都行；模型 API 改 `scripts/ds.py` 一行 `base_url` 就能从 DeepSeek 换成 Kimi/通义/GLM/OpenAI/本地 Ollama。

> 本仓库只包含**系统骨架**（脚本 + 规范文档 + 模板），不含任何论文 PDF、笔记内容或文献元数据——那些是你自己的库数据，另存本地/私有仓库。

---

## ⭐ 能帮你做什么

### 1. 写论文时逐句（甚至逐词）找引文，还分辨方向

写论文最烦的不是"找不到文献"，而是"关键词能搜到一堆，但哪几篇的结论真的跟我这句话一致"——同一个现象在不同体系里常被报告成相反结论，纯关键词命中会把方向相反的文献也塞进引用列表。

把你正在写的一整段丢给 AI、说"引文献"，它会：
- 自动把整段拆成一个个需要引用的点，
- **枚举句逐项拆分**：像 "...strategies including alloying, morphological engineering, valence-state modulation..." 这种一句列举多种手段、每项各挂一个引用编号的句子，会拆成每一项单独找引文，并告诉你**引用编号该插在哪个词后面**，
- 对每篇候选判断 **support**（方向一致可引）/ **contradict**（矛盾，说明矛盾在哪个分句）/ **unclear**（信息不足），
- 跳过作者自己的论点表述（不需要外部引用的部分），不占位凑数。

输出是按原文顺序逐处对照的清单，你核对一遍就能定引用，省掉"逐句想关键词、逐篇读摘要判断方向"的体力活。

### 2. 不装 EndNote/Zotero，直接在 Word 里插引用

核对完想要的引用后，三条路都能走：
- **导出 RIS/BibTeX**（`export_for_endnote.py`）供 EndNote/Zotero 一次性批量导入；
- **直接在打开着的 Word 文档光标处插入**编号引用 + 自动维护文末 References（`word_insert_citation.py`，支持 `numbered`/`nature`/`wiley`/`gbt7714` 多种格式、`--rebuild` 重新连续编号，仅 Windows）；
- **更懒**：`word_auto_cite.py` 对整篇 Word 扫一遍，自动识别待引用句、检索候选、把 support 的引用插到对应句子后（默认预览，确认再 `--apply`）。

覆盖"日常写作、单一编号格式、支持增删重排"的核心需求，但不是 EndNote 的完全替代（不是 Word 域代码，不能一键切换样式全文重排），投特殊样式期刊前请人工再核对。

### 3. "扩充"/"查新"：自动发现领域新文献

搜某方向近几年顶刊全量文献（扩充）或只搜库上次扫描以来的新增（查新），自动跟库里去重、按你的分类词表粗分类，产出精简待下载清单。有 DOI 的走 Crossref 核验，只有标题的走 Crossref 标题反查——**都不靠 LLM 猜 DOI**（猜错比没有更糟）。

### 4. 本地网页操作面板

不想全程打字，就让 AI"打开操作面板"（Windows 也可以直接双击 `PaperTrail Launcher.bat`）。一个本地网页仪表盘：浏览/搜索/筛选/排序全部文献、读笔记、拖 PDF 进 inbox、跑体检/引文献/扩充查新。**"入库"仍走对话**（查重/核实需要判断，表单干不了）。

---

## 核心设计

- **`notes/` 是唯一真源**：每篇是 `<citekey>.md`，YAML frontmatter（结构化元数据）+ Markdown 正文（七节式摘要），citekey 格式 `<年份>-<期刊简称>-<提炼标题>`。
- **`notes-readable/` 是只读派生版**：正文按 60 字符折行给人读，脚本自动从 `notes/` 同步生成，永不手改。
- **五处一致性**：`papers/<citekey>.pdf`、`notes/<citekey>.md`、`notes-readable/<citekey>.md`、`library.bib` 条目、`INDEX.md` 行，citekey 逐字一致，`check_library.sh` 自动核对。
- **查重不止查 DOI**：DOI 缺失/错误时用标题相似度兜底（踩过坑之后加的）。
- **引文分方向判断**：`find_citations.py` 给每篇候选 support/contradict/unclear，不是笼统"相关"。
- **API 不锁定单一供应商**：`scripts/ds.py` 走标准 OpenAI 接口，换 `base_url`/`model` 即可。

完整规范见 [`AGENTS.md`](AGENTS.md)（数据格式与内容生产流程，唯一权威）和 [`CLAUDE.md`](CLAUDE.md)（AI 作为"架构维护者"的角色说明）。

---

## 目录结构

```
QUICKSTART.md         ★ 新手从这里开始（对话式上手）
README.md             本文件（总览）
AGENTS.md             数据格式与内容生产规范（唯一权威，给 AI 读）
CLAUDE.md             架构维护者角色说明（给 Agent CLI 读）
PaperTrail Launcher.bat       双击打开网页操作面板（Windows）
templates/            笔记模板
schema/               frontmatter JSON Schema，供程序化校验
scripts/              全部工具脚本（见下）
prompts/              Codex CLI 等的操作指南
requirements.txt      Python 依赖
LICENSE               MIT
```

克隆后你自己的数据目录（`inbox/ papers/ notes/ notes-readable/ extracted-text/ topics/ exports/ data/` 及 `library.bib`/`INDEX.md`/`KEYWORDS.md`）由 QUICKSTART Step 4 里的 AI 帮你创建，不随仓库分发（已在 `.gitignore` 排除）。

---

## 脚本一览

| 脚本 | 用途 |
|---|---|
| `check_library.sh` | 体检：papers/notes/bib 三方对账 + 全库标题查重 + SI 绑定核对 |
| `build_keyword_index.sh` | 重建关键词索引 |
| `batch_ingest.py` | 无人值守批量入库（断点续跑） |
| `ds.py` | 通用 LLM API 调用（chat / json / pdf-meta） |
| `ingest_from_meta.py` | 把 `ds.py pdf-meta` 的 JSON 直接组装成六处文件 |
| `render_readable_notes.py` | `notes/` → `notes-readable/` 全量同步 |
| `regenerate_notes.py` | 用缓存全文重新调 LLM 重写笔记正文，断点续跑 |
| `match_orphan_si.py` | 用标题相似度给孤立 SI 附件配对已入库文献 |
| `extract_performance.py` | 批量抽取结构化数值到 csv（[领域定制] 示例） |
| `build_topic_digest.py` | 主题综述第一步：筛笔记 + 查重，可选起草分类初稿 |
| `find_citations.py` | 给一句/一段话逐处找可引文献，分 support/contradict/unclear |
| `export_for_endnote.py` | 导出指定 citekey 为 RIS/BibTeX 供批量导入 |
| `word_insert_citation.py` | 在打开的 Word 光标处插编号引用+维护 References（仅 Windows） |
| `word_auto_cite.py` | 自动扫描 Word 全文识别待引用位置并一键插入（仅 Windows，默认预览） |
| `scan_new_papers.py` | 候选论文 Crossref 核验 + 去重 + 分类，导出 xlsx |
| `scan_state.py` | 记录每个领域上次扫描到哪天，供"只搜增量" |
| `parse_search_results.py` | 把搜索原始结果交给便宜 LLM 抽取候选列表 |
| `md_to_docx.py` | Markdown 综述转 docx（python-docx） |
| `find_duplicate_titles.py` | 全库标题相似度查重 |
| `resolve_duplicate.py` | 合并两个确认重复的 citekey，处理五处文件 |
| `rename_journal_abbr.py` | 批量改写 citekey 里的期刊简称 |
| `export_referable_folder.py` | 按条件筛选，导出 PDF+笔记到独立文件夹 |
| `ui/app.py` | 本地网页操作面板（Streamlit） |

每个脚本都有 `--help` 和文件头 docstring；涉及删除/改名的默认预览模式，加 `--apply` 才执行。**但日常你不用记这些——对 AI 说人话即可。**

---

## 💰 处理一篇论文大概花多少钱

批量入库用的是便宜的模型档位，单篇成本可按下面估算：

| 环节 | token 量级 | 说明 |
|---|---|---|
| 输入（论文全文） | 常规论文约 8,000~20,000 tokens（脚本设 20 万字符兜底上限防超长 PDF） | 全文一次性传入 |
| 输出（结构化元数据 + 七节笔记） | 约 1,500~3,000 tokens | JSON 格式，字段固定 |

按 DeepSeek 这类国产模型的定价，**单篇入库成本大概几分钱人民币**。**实测：入库 400 篇约花 10 元**，平均单篇不到 3 分钱。

```
单篇成本 ≈ (输入token / 1e6) × 输入单价 + (输出token / 1e6) × 输出单价
```

换成你自己的 API 时，把两个单价换成你实际定价页的数字即可。引文献、扩充查新这类功能单次 token 量比整篇入库还小，成本可忽略。

---

## License

MIT，见 [LICENSE](LICENSE)。你库里的论文 PDF 本身仍受各自出版商版权约束，不受本仓库许可证覆盖——**不要把 PDF 原文/全文提取内容放进公开仓库。**
