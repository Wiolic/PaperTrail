# LLM-Assisted Literature Notes System

一个用 Claude Code / DeepSeek（或任何 OpenAI 兼容 LLM API）驱动的**文献精读笔记库框架**：PDF 入库、结构化笔记生成、标签/关键词检索、主题综述、去重体检，全部脚本化、可断点续跑。

不是一个"上传论文自动总结"的一次性小工具，而是一套**长期维护型个人文献知识库**的工作流规范 —— 灵感类似 Zotero/Obsidian，但笔记生产主要由 LLM 完成、人工负责审核和架构一致性维护。

> 本仓库只包含**系统骨架**（脚本 + 规范文档 + 模板），不含实际的论文 PDF、笔记内容或文献元数据 —— 这些是使用者自己的文献库数据，请另建私有仓库或本地目录存放，本框架负责生产和维护它们。

## 这套系统解决什么问题

管文献不难，难的是随着库变大之后：
- 笔记格式各篇不一致，没法批量检索/统计
- 同一篇论文因为来源不同被重复收录，人工很难发现
- 补充材料（SI）和主文献散落、配对关系丢失
- 想做"某个主题的综述"，得把几十篇笔记翻出来手动整理
- 从 PDF 提炼结构化信息（元数据、方法学、数值型实验数据）纯人工做太慢，全自动做又容易出错/编造

这套框架用「LLM 负责体力活（抽取、起草），人/高阶 Agent 负责判断（查重、核实、综述归纳）」的分工，加上一套统一的 Markdown + YAML frontmatter + BibTeX 格式，把这些问题变成可以脚本化检测和修复的东西。

## 核心设计

- **`notes/` 是唯一真源**：每篇笔记是 `<citekey>.md`，YAML frontmatter（结构化元数据）+ Markdown 正文（七节式摘要），citekey 格式 `<年份>-<期刊简称>-<提炼标题>`。
- **`notes-readable/` 是只读派生版**：正文按 60 字符折行给人阅读，脚本自动从 `notes/` 同步生成，永远不手改。
- **五处一致性**：`papers/<citekey>.pdf`、`notes/<citekey>.md`、`notes-readable/<citekey>.md`、`library.bib` 条目、`INDEX.md` 行，citekey 必须逐字一致 —— `check_library.sh` 自动核对。
- **查重不止查 DOI**：DOI 缺失/提取错误时用标题相似度兜底查重（这是踩过坑之后加的，见 `AGENTS.md` 里的具体案例）。
- **可复用的维护脚本**：合并重复收录、批量改期刊缩写、导出主题文件夹、生成主题综述 docx，都是参数化脚本，不是一次性代码。

完整规范见 [`AGENTS.md`](AGENTS.md)（数据格式与内容生产流程，任何 LLM/API 照做）和 [`CLAUDE.md`](CLAUDE.md)（给 Claude Code 之类 Agent CLI 的架构维护者角色说明）。

## 快速开始

```bash
git clone <this-repo> my-literature-library
cd my-literature-library

# 建自己的数据目录(不随仓库分发)
mkdir -p inbox papers notes notes-readable extracted-text topics exports data

pip install -r requirements.txt
export DEEPSEEK_API_KEY="sk-..."   # 或改 scripts/ds.py 换成你用的 LLM API
```

然后：
1. 把待整理的 PDF 丢进 `inbox/`
2. 用你的 Agent CLI（Claude Code / Codex / 其他）读取 `AGENTS.md` 作为项目指令，让它按"入库任务的标准步骤"处理，或直接跑 `python scripts/batch_ingest.py --source inbox --limit 15` 做无人值守批量入库
3. 定期跑 `bash scripts/check_library.sh` 体检

## 克隆后必须做的事（去掉领域特定内容）

本仓库的 `AGENTS.md`/`templates/note-template.md` 里标了 **`[领域定制]`** 的部分（原型是电催化/材料科学文献库）是示例内容，换成你自己领域的：
- frontmatter 里的领域专属结构化字段（原例：`类型`/`方法关键词`/`表征方法`/`体系`）
- 标签词表（`AGENTS.md` 末尾的标签词表一节）
- `scripts/top_journals.txt` 里的"顶刊"名单
- `scripts/extract_performance.py`、`scripts/ds.py` 里 SYSTEM_PROMPT 提到的领域术语（如果你要用结构化数值抽取功能）

其余部分（citekey 规则、SI 绑定、查重、体检、综述工作流）是领域无关的，不需要改。

## 目录结构

```
AGENTS.md            数据格式与内容生产规范(给 LLM 读)
CLAUDE.md             架构维护者角色说明(给 Agent CLI 读)
templates/            笔记模板 + frontmatter schema
schema/               JSON Schema, 供程序化校验
scripts/              全部工具脚本(见下)
prompts/              可复制的操作提示词
requirements.txt
LICENSE
```

## 脚本一览

| 脚本 | 用途 |
|---|---|
| `check_library.sh` | 体检：papers/notes/bib 三方对账 + 全库标题查重 + SI 绑定核对 |
| `build_keyword_index.sh` | 重建关键词索引 |
| `batch_ingest.py` | 无人值守批量入库(断点续跑) |
| `ds.py` | 通用 LLM API 调用(chat / json / pdf-meta) |
| `render_readable_notes.py` | `notes/` → `notes-readable/` 全量重新同步 |
| `match_orphan_si.py` | 跨文件夹配不上的 SI 附件用标题相似度匹配 |
| `extract_performance.py` | 批量抽取结构化数值数据到 csv |
| `build_topic_digest.py` | 主题综述第一步：按标签/关键词筛笔记 + 自动查重 |
| `md_to_docx.py` | Markdown 综述转 docx(python-docx 实现) |
| `find_duplicate_titles.py` | 全库标题相似度查重(独立于体检也可单跑) |
| `resolve_duplicate.py` | 合并两个确认重复的 citekey，自动处理五处文件 |
| `rename_journal_abbr.py` | 批量改写 citekey 里的期刊简称 |
| `export_referable_folder.py` | 按条件筛选，导出 PDF+笔记到独立文件夹 |

每个脚本都有 `--help` 和文件头 docstring 说明用法；涉及删除/改名的脚本默认预览模式，加 `--apply` 才真正执行。

## License

MIT，见 [LICENSE](LICENSE)。你自己文献库里的论文 PDF 本身仍受各自出版商版权约束，不受本仓库许可证覆盖 —— 不要把 PDF 原文/全文提取内容放进公开仓库。
