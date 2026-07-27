# PaperTrail — 对话驱动的文献精读笔记系统

一套**跟 AI 对话就能用**的个人文献知识库框架：把论文 PDF 变成结构化精读笔记，自动分类打标签，配一个**开箱即用的本地网页操作面板**，也可以直接用大白话指挥 AI 帮你检索、去重体检、写论文时逐句找引文、在 Word 里插引用、系统性发现领域新文献、生成主题综述。

> ## 🚀 只想赶紧用起来？→ 直接看 **[QUICKSTART.md](QUICKSTART.md)**
>
> 核心流程只有三步：**装 AI 助手 → 下载仓库 → 粘贴开场白**。**不用会写代码，也不用记命令。**

---

## ⭐ headline：入库即自动分类

拖一篇 PDF 进 `inbox/`，系统自动帮你判断：

- **文章类型**（计算/实验/综述……）
- **关键词与标签**（不是死列表——库里新出现的领域词会被记住，下次同类论文自动复用同一个标签，不会各说各话）
- **表征方法**（SEM/XPS/operando XAS……从正文识别）
- **与你课题的关联度**（按你自己定义的相关性标准打分）

结果落成一份带完整 YAML frontmatter 的 Markdown 笔记，你不用手填任何一个字段。

---

## 🖥️ 网页操作面板（推荐入口）

双击 `PaperTrail Launcher.bat`，或对 AI 说"打开操作面板"：

| 标签页 | 功能 |
|--------|------|
| **📊 总览** | 库存统计、拖拽 PDF 入库、"最近新增" |
| **📚 文献库** | 按标题/作者/关键词搜索、标签/期刊筛选 + 阅读窗格 + 就地编辑 + 🧠 语义检索 |
| **🩺 体检** | 三方对账 + 标题查重 |
| **📝 引文献** | 贴正文 → 逐处找引文 + 方向判断，**进度条 + 停止按钮** |
| **🔎 扩充/查新** | OpenAlex / 引用图谱 / WebSearch 三引擎，**进度条 + 停止按钮** |
| **📄 Word** | 插编号引用 / 自动扫全文 |
| **⚙️ 命令** | 自然语言→命令，**先确认再执行** |

面板完全独立运行，不依赖 Agent CLI 在后台开着——总览/文献库/体检只读本地文件即可用；引文献/扩充查新/命令翻译需要配好 API Key。

---

## 两个 AI 分工（对话模式）

不想用网页面板、想直接对着 AI 说话时：

- **Agent CLI（总管）** —— Claude Code / Codex CLI / Kimi Code 任一。跟你对话、做判断（查重、DOI 核实、SI 配对这类需要"记住上下文"的活）
- **大模型 API（工人）** —— DeepSeek（默认）或任何 OpenAI 兼容服务。跑批量体力活（元数据抽取、摘要起草）

**不锁定供应商**：改一行 `base_url` 就能换模型。

> 本仓库只含**系统骨架**，不含论文数据。

---

## 功能一览（输入 → 输出）

### 1. 入库：PDF → 结构化精读笔记

**输入**：把 PDF 拖进 `inbox/`（或拖进网页面板），对 AI 说「入库」

**输出**：`notes/<citekey>.md`，YAML 元数据 + 七节正文：

```yaml
---
citekey: 2025-NatCatal-Tailored-Water-Co3O4-PEMWE
title: Tailored water–surface interactions on cobalt oxide for stable PEMWE
authors: Luqi Wang et al.
year: 2025
journal: Nature Catalysis
doi: 10.1038/s41929-025-01476-6
tags: [Co3O4, OER, PEMWE]
keywords: [La-doped Co3O4, interfacial water, hydrogen-bond network, ...]
类型: 计算+实验
方法关键词: SEM/TEM/HAADF-STEM; in situ SERS/ATR-SEIRAS/XAS; DFT+U
表征方法: [SEM, TEM, HAADF-STEM, XRD, XPS, in situ SERS, ...]
体系: La and Ca co-doped Co3O4 spinel catalyst
status: skimmed
---

## 三句话总结
本文报道了一种镧和钙共掺杂的Co3O4催化剂，通过调控催化剂表面与水分子的
相互作用来抑制钴的溶解，从而提高酸性OER稳定性...

## 研究问题与核心结论
## 方法要点
## 关键图表与数据
## 与我课题的关联
## 质疑与局限
## 值得追的参考文献
```

同时自动更新 `papers/`、`library.bib`、`INDEX.md`、`notes-readable/`、`extracted-text/` 五处文件，五处 citekey 保持一致。

**标签词表会自动学习**：第一次遇到新材料/新体系时 LLM 会新造一个标签，这个标签立刻被记入"当前受控词表"，后续同类论文的入库 prompt 里就能看到这个词已经存在、直接复用——不会出现同一种材料被打成好几个不同标签、检索时互相找不到的情况。

---

### 2. 检索：关键词 / 作者 / 语义模糊问题

**网页面板文献库页**支持三种搜法混用：
- 关键词/标题/**作者**搜索框（输入作者姓名也能命中）
- 标签、期刊、Top Journals、年份筛选
- 🧠 语义检索：输入模糊科研问题（不需要关键词命中）

```
py scripts/semantic_search.py "哪些论文研究了 Ir 溶解机理"
```
```
🧠 语义检索 (top-5):
  1. [0.87] 2025-NatCommun-Accelerated-Ir-Dissolution-Organic-Compounds
     — 有机物加速Ir溶解的机制研究
  2. [0.82] 2024-JACS-Operando-Ir-Nanoparticles-OER
     — operando 追踪Ir纳米颗粒溶解过程
  ...
```

本地 `sentence-transformers` 多语言 embedding 模型，纯本地推理，不调云端 API；`notes/*.md` 改动后增量重新计算受影响的向量。

---

### 3. 引文献：贴正文 → 逐句找引文

**输入**：
```
引文献：IrO2 是 PEMWE 阳极最广泛使用的催化剂，但其高成本和稀缺性
限制了大规模应用。近年来，多种策略被提出以降低 Ir 载量。
```

**输出**：
```
句1: "IrO2 是 PEMWE 阳极最广泛使用的催化剂..."
  ✅ 2024-NatCatal-Active-Site-Density-Energetics-Water-Oxidation-Iridium-Oxides
     — support: 讨论了IrO2作为标准PEMWE阳极催化剂的地位
句2: "多种策略被提出以降低 Ir 载量..."
  ✅ 2025-JACS-Ir-Single-Atoms-Oxygen-Coupling
     — support: 报道了Ir单原子分散策略
```

---

### 4. 查新/扩充：关键词 → 候选论文清单

**输入**：对 AI 说「查新 酸性OER Ir催化剂 领域」

**输出**：
```
🔍 OpenAlex 检索中...
找到 47 篇候选，Crossref 核验后：
  ✅ 新增 12 篇（库中未收录）
  ⏭️ 跳过 35 篇（已入库）
  导出到 exports/酸性OER-Ir-查新-2026-07.xlsx
```

---

### 5. 体检：一句话 → 全库健康检查

```
🩺 体检报告：
  papers/:   414 篇   notes/:    414 篇   library.bib: 414 条   INDEX.md:  414 行
  ✅ 三方对账一致   ✅ 未发现重复 citekey   ✅ notes-readable/ 已同步
  ⚠️ 发现 2 组标题高度相似（可能是重复收录），建议核实
```

---

### 6. Word 插引用（Windows）

对 AI 说「在 Word 插入 2025-NatCatal-Tailored-Water-Co3O4-PEMWE 引用」→ 在光标处插入 `[23]`，文末自动维护 References。

---

## 核心设计

- `notes/` 唯一真源：YAML frontmatter + Markdown 七节式正文
- 五处 citekey 一致性（papers/notes/notes-readable/bib/INDEX）
- 标签词表从磁盘现算并自动学习新词（非静态列表）
- API 不锁定（标准 OpenAI 接口）
- 语义检索纯本地推理，无云端依赖

完整规范见 [`AGENTS.md`](AGENTS.md) 和 [`CLAUDE.md`](CLAUDE.md)；技术细节见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

---

## 目录结构

```
QUICKSTART.md           ★ 新手开始
README.md               本文件
AGENTS.md               数据格式规范（给 AI）
CLAUDE.md               架构维护者说明
ARCHITECTURE.md         系统架构（给开发者）
templates/              笔记模板
schema/                 frontmatter JSON Schema
scripts/                全部工具脚本
scripts/ui/app.py       Streamlit 网页面板
prompts/                Agent CLI 指南
requirements.txt        Python 依赖
LICENSE                 MIT
```

---

## 脚本一览

| 脚本 | 用途 |
|---|---|
| `academic_search.py` | OpenAlex + 引用图谱 + 中文翻译 + AND 过滤 |
| `batch_ingest.py` | 批量入库（断点续跑，标签自动学习） |
| `ds.py` | LLM API 调用（chat/json/pdf-meta） |
| `ingest_from_meta.py` | JSON → 六处文件 |
| `scan_new_papers.py` | Crossref 核验 + 去重 → xlsx |
| `scan_state.py` | 记录上次扫描日期 |
| `parse_search_results.py` | 搜索结果 → 候选列表 |
| `find_citations.py` | 逐处找引文 + 方向判断 |
| `export_for_endnote.py` | 导出 RIS/BibTeX |
| `word_insert_citation.py` | Word 插编号引用（Windows） |
| `word_auto_cite.py` | 自动扫 Word 插引用（Windows） |
| `build_embedding_index.py` | 语义索引构建（增量） |
| `semantic_search.py` | 语义检索（FAISS） |
| `check_library.sh` | 体检：对账 + 查重 |
| `build_keyword_index.sh` | 重建关键词索引 |
| `render_readable_notes.py` | notes → notes-readable 同步 |
| `regenerate_notes.py` | 重写笔记正文（断点续跑） |
| `match_orphan_si.py` | 孤立 SI 匹配 |
| `extract_performance.py` | 抽取结构化数值 |
| `build_topic_digest.py` | 主题综述筛笔记 |
| `md_to_docx.py` | Markdown → docx |
| `resolve_duplicate.py` | 合并重复 citekey |
| `export_referable_folder.py` | 筛选导出 |
| `ui/app.py` | 网页面板 |

每个脚本有 `--help`；删除/改名默认预览，`--apply` 才执行。

---

## 💰 成本

DeepSeek：**400 篇 ≈ 10 元人民币**。OpenAlex/Semantic Scholar 免费。语义检索本地推理，零 API 成本。

---

## License

MIT。论文 PDF 受出版商版权约束——**不要放进公开仓库。**
