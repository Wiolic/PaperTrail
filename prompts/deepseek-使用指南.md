# 用 Codex CLI + DeepSeek 生产文献库内容

> 把下面的路径占位符（`<你的库目录>`、`<PDF源目录>` 等）换成你自己的实际路径。

## 好消息：AGENTS.md 不用你手动喂

Codex CLI 有个约定：在某个目录下运行 `codex` 时，会**自动读取该目录的 `AGENTS.md`** 作为项目指令——和 Claude Code 自动读 `CLAUDE.md` 是同一套机制。本仓库的 [AGENTS.md](../AGENTS.md) 本来就是照这个通用惯例起的名，所以你**不需要**把规范整段粘进对话当 system prompt——只要在库目录里跑 `codex`，红线、citekey 规则、frontmatter 字段、入库/精读步骤会自动生效。

## 一次性配置：把 Codex 的模型换成 DeepSeek（或任何 OpenAI 兼容 API）

DeepSeek API 是 OpenAI 兼容接口，`base_url` 是 `https://api.deepseek.com`（不带 `/v1`）。Codex CLI 支持自定义 model provider。编辑 Codex 的配置文件（一般是 `%USERPROFILE%\.codex\config.toml`），加一段：

```toml
[model_providers.deepseek]
name = "DeepSeek"
base_url = "https://api.deepseek.com"
env_key = "DEEPSEEK_API_KEY"

model_provider = "deepseek"
model = "deepseek-chat"     # 精读要更强推理可换更贵的推理模型
```

具体字段名以你装的 Codex 版本文档为准（跑 `codex --help` 或查自带文档），不同版本可能略有出入。

`DEEPSEEK_API_KEY` 需要作为环境变量配置好（Windows: `setx DEEPSEEK_API_KEY "sk-..."`，新终端窗口生效）。

## 日常使用

1. 终端 `cd` 到你的库目录
2. 跑 `codex`（自动读 AGENTS.md，规则已生效）
3. 直接自然语言下任务，比如：

**批量粗建档**（默认策略：不精读全文，见 AGENTS.md「批量入库策略」）
```
把 <PDF源目录> 下所有 PDF 做粗建档。
每批 15 篇，处理完一批停下汇报（本批 citekey 列表 / 跳过的重复项 / 存疑字段），等我说继续再下一批。
[领域定制]关键数值字段(如泛函/U值/slab尺寸)一律留空,不许猜。
```

**精读指定文献**
```
精读 papers/ 里的 <citekey1> 和 <citekey2>。
补 keywords 前先看 KEYWORDS.md 复用已有词。完成后把每篇「三句话总结」贴给我。
```

**只处理某个子目录**
```
把 <另一个PDF子目录> 里的 PDF 做粗建档，规则同上，每批 15 篇。
```

## 安全注意（叠加 AGENTS.md 里的红线，双保险）

- Codex 有审批/沙箱模式（多数版本支持"每步询问" vs "自动执行"一类选项，具体 flag 名以 `codex --help` 为准）。**第一次批量跑之前**，确认它的写权限范围能覆盖你的库目录（要往 `papers/`/`notes/` 写文件），但**不要给它对任何外部只读文献源目录的写权限**——理论上 AGENTS.md 的红线会让它只读只复制，但批量跑的第一轮建议你全程盯一遍，确认它确实照做。
- 复制源 PDF 时，让它从源目录**复制**到 `papers/`，不要移动、不要在源目录改名。

## 路线 B：无人值守批量脚本

`pip install -r requirements.txt` 装好依赖，`DEEPSEEK_API_KEY` 配置好之后，`scripts/batch_ingest.py` 可直接用。原理：抽每篇 PDF 前几页文字 → 调 LLM（JSON 模式，只做元数据抽取，不猜数值参数）→ 生成 citekey → 查重 → SI 配对 → 复制 PDF(+SI)、追加 bib、建笔记骨架、追加 INDEX。断点续跑：处理过的源文件记在 `scripts/.ingest_state.json`，重跑自动跳过已处理的。

**试跑（不落盘，先看抽取结果对不对）**：
```powershell
python scripts\batch_ingest.py --source "<PDF源目录>" --limit 3 --dry-run
```

**正式批量跑**（默认每次 15 篇，跑完看汇总，确认没问题再重跑同一条命令处理下一批）：
```powershell
python scripts\batch_ingest.py --source "<PDF源目录>" --limit 15
```

跑完记得回来这边说"体检"，核对 `papers/notes/bib` 三方一致性、扫一遍关键词有没有堆近义词。

这条路线不经过 Codex，也不依赖它读 `AGENTS.md`——脚本内置了同一套规则（citekey 格式、frontmatter 字段、标签词表、粗建档不猜数值），两条路线可以混用，互不冲突（citekey/DOI 查重逻辑通用）。

## 体检交给你的 Agent CLI

不管用哪条路线生产了多少内容，想核对 `papers/`/`notes/`/`library.bib` 是否一致、关键词有没有堆近义词，回到 Claude Code（或你用的 Agent CLI）说"体检"，跑 `scripts/check_library.sh` + `scripts/build_keyword_index.sh`，按 schema 抽查 frontmatter。

---

## 路线 C：直接让 Claude Code（或你的 Agent CLI）驱动 DeepSeek 干活（推荐用于交互式整理）

跟 Codex（路线 A）不同，这条路线不用你另开一个 Codex 窗口——直接在 Claude Code 里说"整理 inbox"、"把这几篇入库"，Agent 会用 `scripts/ds.py` 调 DeepSeek 做体力活（读 PDF 首页、抽元数据），自己审核结果（查重、DOI 可信度、SI 配对这些判断由 Agent 做，不甩给 DeepSeek），确认后自己写 `papers/`/`library.bib`/`notes/`/`INDEX.md`。适合几篇到十几篇的交互式整理，边做边跟你确认；几十上百篇的无人值守批量还是用路线 B（`batch_ingest.py`）更合适。

`ds.py` 也是通用工具，可以拿它做其他零碎调用（起草摘要草稿、建议关键词等），不限于 pdf-meta：

```powershell
python scripts\ds.py pdf-meta "论文.pdf" --out meta.json    # 抽元数据(JSON)
python scripts\ds.py json --system "..." --user "..."       # 通用JSON调用
python scripts\ds.py chat --system "..." --user "..."       # 通用文本调用
```

**核心原则**：只要是"可以明确定义输入输出格式、不需要跨笔记判断"的活（元数据抽取、摘要起草、关键词建议、结构化数值抽取），都优先派给 LLM API 处理；需要跨笔记比对、查重判断、可靠性核实的活，让 Agent CLI 自己做。

---

## 备用：无文件权限时的手动模板

万一某次你只是在 DeepSeek 网页/App 里临时问一篇（没有 Agent CLI 在手边），可以手动粘这段再贴论文正文，把它返回的三段内容自己存盘：

```
你在协助维护一个[你的研究方向]文献库。我会把一篇论文的文字粘给你，你只输出三段：

【notes 正文】完整 Markdown，YAML frontmatter 含：
citekey(格式 年份-期刊简称-提炼标题, 如 2025-ACSNano-Example-Topic；领域专有名词/缩写
必须大写规范书写, 不要全小写)、title、authors("第一作者 et al.")、
authors_full(完整作者名单数组, 每人一个元素, 按原文顺序全部列出)、year、journal、doi(无则N/A)、
tags(受控大类数组, 1~4个)、keywords(5~10个细粒度词数组)、
[领域定制字段, 如 类型/方法关键词/表征方法/体系]、
status: skimmed(AI生成未经人工精读, 别标read)、rating(可选)、related([])、
si_files([]，有SI才填)、added(今天日期)。正文小节固定：
三句话总结/研究问题与核心结论/方法要点/关键图表与数据/与我课题的关联/质疑与局限/值得追的参考文献。
读不到的数值不许编，写"原文未给"。

【bib】一条 @article{citekey, ...} 含 title/author(用authors_full全部作者以" and "连接,
不要用"et al."截断)/journal/year/doi。

【INDEX 行】| citekey | year | title | tag1, tag2 | skimmed | SI标记(有则PDF/Word,无则留空) |

论文文字：
（粘贴）
```
