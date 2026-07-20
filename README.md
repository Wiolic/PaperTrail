# LLM-Assisted Literature Notes System

一个用 LLM 驱动的**文献精读笔记库框架**：PDF 入库、结构化笔记生成、标签/关键词检索、主题综述、逐句添加引文、去重体检，全部脚本化、可断点续跑。

> **📖 零编程基础？直接看 [QUICKSTART.md](QUICKSTART.md)** —— 从装 Agent CLI、注册 API Key 到跑完第一次入库，一步步照着做，30~60 分钟能用起来。**如果你已经有 Claude Code 或 Codex CLI，上手门槛几乎为零**：把这个仓库丢给它、说一声"帮我照着 QUICKSTART.md 把这套系统装起来"，剩下的事它会自己做，你不需要会写代码、不需要懂 git，只要会打字聊天。

**不锁定单一 Agent CLI**：`AGENTS.md`（数据格式与内容生产规范）是任何支持项目级指令文件的 Agent CLI 都能读的通用格式，Claude Code / Codex CLI 都能直接用；`CLAUDE.md`（架构维护者角色说明）Claude Code 会自动加载，Codex 用户第一次对话时明说一句"也读一下 CLAUDE.md"即可获得同样的能力，具体差别见 QUICKSTART.md。**用 Codex CLI 的看 [prompts/codex-cli-guide.md](prompts/codex-cli-guide.md)**——配置模型 provider、日常指令示例、审批/沙箱注意事项都写在里面。

**不锁定 DeepSeek**：核心调用逻辑在 `scripts/ds.py` 里用的是标准 OpenAI SDK 接口，换成 `base_url` 就能接 Kimi、通义千问、GLM、OpenAI、本地 vLLM/Ollama 等任何 OpenAI 兼容的 API——项目默认配 DeepSeek 只是因为它便宜，不是架构上绑死了它。

不是一个"上传论文自动总结"的一次性小工具，而是一套**长期维护型个人文献知识库**的工作流规范 —— 灵感类似 Zotero/Obsidian，但笔记生产主要由 LLM 完成、人工负责审核和架构一致性维护。

> 本仓库只包含**系统骨架**（脚本 + 规范文档 + 模板），不含实际的论文 PDF、笔记内容或文献元数据 —— 这些是使用者自己的文献库数据，请另建私有仓库或本地目录存放，本框架负责生产和维护它们。

## 💰 处理一篇论文大概花多少钱

批量入库（`ds.py pdf-meta` / `batch_ingest.py`）用的是便宜的模型档位（如 DeepSeek 的 `deepseek-v4-flash` 这一档），单篇成本可以按下面的方式自己估算：

| 环节 | token 量级 | 说明 |
|---|---|---|
| 输入（论文全文） | 常规论文（15~40 页）约 8,000~20,000 tokens，脚本设了 200,000 字符（约 5 万 token）的兜底上限防超长 PDF | 全文一次性传入，不是只读摘要 |
| 输出（结构化元数据 + 七节精读笔记） | 约 1,500~3,000 tokens | JSON 格式，字段固定，不是自由发挥的长文 |

按"便宜档"模型（DeepSeek 这类国产模型每百万 token 输入几毛钱、输出一元出头的量级，具体以你用的 API 商当前定价页为准）估算，**单篇论文的入库成本大概是几分钱人民币、甚至更低的量级**。

**实测参考**：入库 **400 篇论文**，调用 DeepSeek 的总花费大约 **10 元人民币**——平均下来单篇不到 3 分钱。

想要更精确的数字，用这个公式自己套：

```
单篇成本 ≈ (输入token数 / 1,000,000) × 输入单价 + (输出token数 / 1,000,000) × 输出单价
```

把 `scripts/ds.py` 换成你自己的 API/模型时，把这两个单价换成你实际用的定价页数字即可。像"引文献"逐句添加引文、"扩充/查新"候选抽取这类功能，单次调用的 token 量级比整篇入库还小（只处理摘要片段或搜索结果文字），成本可以忽略不计。

## ⭐ 重点功能：写论文时逐句添加引文

写论文最烦的一步不是"找不到文献"，而是"关键词能搜到一堆论文，但哪几篇的结论方向真的和我这句话一致"——同一种现象（比如某个表征信号随电位的变化）在不同体系里经常被不同论文报告成相反的结论，单纯关键词命中会把方向相反的文献也拉进引用列表。

把你正在写的一整段论文正文丢给它，直接说"引文献"：

```bash
python scripts/find_citations.py --paragraph "你的一整段英文/中文正文" \
  --out topics/_citations.md --draft
```

它会自动把整段拆成一句句独立论点，逐句去库里（必要时联网）找候选文献，再判断每一篇是 **support**（方向一致可以引）/ **contradict**（数据或结论矛盾，会说明矛盾在哪个分句）/ **unclear**（信息不足），跳过作者自己的论点表述（不需要外部引用的部分）不占位凑数。**输出是按原文顺序逐句对照的清单**，读起来就是：

```
原文第1句：The Ir L3-edge XANES white line shifts to higher energy with increasing potential...
- **citekey1** — 《论文标题》(期刊, 年份) [support]: 理由...
- **citekey2** — 《论文标题》(期刊, 年份) [contradict]: 理由...

原文第2句：This is our proposed novel mechanism...
（无需引用——这是作者自己的论点表述）

原文第3句：...
- **citekey3** — 《论文标题》(期刊, 年份) [support]: 理由...
```

判断结果仍需要你自己核对一遍再定引用，不是拿来直接照抄发表的，但省掉了"逐句想关键词、逐篇读摘要判断方向"的体力活。核对完想要的引用后，还能一步导出成 EndNote/Zotero 能直接批量导入的 RIS/BibTeX 文件（见下方脚本一览的 `export_for_endnote.py`），不用再去文献管理软件里逐篇手动搜索。

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
- **添加引文分方向判断，不是关键词命中就算**：`find_citations.py` 对每篇候选给 support/contradict/unclear 判断而不是笼统"相关"，见上方"重点功能"。
- **可复用的维护脚本**：合并重复收录、批量改期刊缩写、导出主题文件夹、生成主题综述 docx，都是参数化脚本，不是一次性代码。
- **API 不锁定单一供应商**：`scripts/ds.py` 走标准 OpenAI SDK 接口，换 `base_url`/`model` 即可接任何 OpenAI 兼容的 LLM 服务。

完整规范见 [`AGENTS.md`](AGENTS.md)（数据格式与内容生产流程，任何 LLM/API 照做）和 [`CLAUDE.md`](CLAUDE.md)（给 Claude Code 之类 Agent CLI 的架构维护者角色说明）。

## 快速开始

```bash
git clone <this-repo> my-literature-library
cd my-literature-library

# 建自己的数据目录(不随仓库分发)
mkdir -p inbox papers notes notes-readable extracted-text topics exports data

pip install -r requirements.txt
export DEEPSEEK_API_KEY="sk-..."   # 换用其他 OpenAI 兼容 API(Kimi/通义/GLM/OpenAI/本地模型等)
                                    # 只需要改 scripts/ds.py 里的 base_url 和读取的环境变量名
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
| `ingest_from_meta.py` | 把 `ds.py pdf-meta` 的 JSON 直接组装成六处文件，省去人工重打正文 |
| `render_readable_notes.py` | `notes/` → `notes-readable/` 全量重新同步 |
| `regenerate_notes.py` | 用已缓存的全文重新调用 LLM 重写笔记正文，断点续跑 |
| `match_orphan_si.py` | 跨文件夹配不上的 SI 附件用标题相似度匹配 |
| `extract_performance.py` | 批量抽取结构化数值数据到 csv([领域定制]示例) |
| `build_topic_digest.py` | 主题综述第一步：按标签/关键词筛笔记 + 自动查重，可选起草分类初稿 |
| `find_citations.py` | 给一句/一段话逐句添加可引用的文献，区分 support/contradict/unclear |
| `export_for_endnote.py` | 导出指定 citekey 列表为 RIS/BibTeX，供文献管理软件批量导入 |
| `scan_new_papers.py` | 候选论文 Crossref 核验 + 去重 + 分类，导出待下载 xlsx |
| `scan_state.py` | 记录每个领域上次扫描到哪天，供"只搜增量"用 |
| `parse_search_results.py` | 把搜索引擎原始结果交给便宜的 LLM 抽取候选论文列表 |
| `md_to_docx.py` | Markdown 综述转 docx(python-docx 实现) |
| `find_duplicate_titles.py` | 全库标题相似度查重(独立于体检也可单跑) |
| `resolve_duplicate.py` | 合并两个确认重复的 citekey，自动处理五处文件 |
| `rename_journal_abbr.py` | 批量改写 citekey 里的期刊简称 |
| `export_referable_folder.py` | 按条件筛选，导出 PDF+笔记到独立文件夹 |

每个脚本都有 `--help` 和文件头 docstring 说明用法；涉及删除/改名的脚本默认预览模式，加 `--apply` 才真正执行。

零编程基础、想直接照着操作的，看 [QUICKSTART.md](QUICKSTART.md)。

## License

MIT，见 [LICENSE](LICENSE)。你自己文献库里的论文 PDF 本身仍受各自出版商版权约束，不受本仓库许可证覆盖 —— 不要把 PDF 原文/全文提取内容放进公开仓库。
