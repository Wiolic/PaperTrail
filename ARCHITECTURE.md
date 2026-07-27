# 系统架构说明（PaperTrail）

> 面向"要不要/怎么给这套系统做前端改造"的决策者，以及任何想理解内部机制的开发者。不是用户说明书（那是 `README.md`/`QUICKSTART.md`），本文件面向"接下来要改造/重写这套系统的开发者（人或 AI）"。

---

## 1. 这是什么系统

一套"LLM 负责体力活、人/Agent CLI 负责判断"的个人文献知识库：把论文 PDF 变成结构化 Markdown 笔记，配一套检索/综述/引文/查新/系统性学术搜索脚本，外加一个本地 Streamlit 网页操作面板。

三条腿：**数据格式规范（AGENTS.md）+ 脚本工具箱（scripts/）+ 两种交互界面（对话 / 网页面板）**。

- **Agent CLI**（Claude Code/Codex/Kimi）：对话式操作，做查重、DOI 核实、SI 配对等需要判断的活
- **LLM API**（DeepSeek，通过 `scripts/ds.py`）：被脚本调用，做批量的、格式化的抽取
- **Streamlit 面板**（`scripts/ui/app.py`）：表单/按钮封装常用脚本，不需要盯着终端

---

## 2. 数据层

```
library.bib          全库 BibTeX（生成物，追加式）
INDEX.md             总索引表（生成物，按年份倒序）
KEYWORDS.md          关键词/标签/表征方法索引（build_keyword_index.sh 生成，勿手编）

inbox/               入口：新 PDF 待整理；inbox/.meta_cache/ 缓存 pdf-meta 的 JSON
papers/<citekey>.pdf         正式馆藏 PDF
notes/<citekey>.md            ★ 唯一真源：YAML frontmatter + Markdown 正文，不折行
notes-readable/<citekey>.md   notes/ 的派生物：正文按60字符折行，脚本自动同步
extracted-text/<citekey>.txt  每篇PDF全文缓存，避免加字段/改逻辑要重新读PDF
embeddings/                   语义检索索引（index.faiss/metadata.json/vectors.npy）

topics/              主题综述页
exports/             导出的成品文档（docx/xlsx）
data/                结构化数值数据层（performance.csv）
```

### citekey 格式

`<年份>-<期刊简称>-<提炼标题>`，如 `2025-JACS-Ir-Dissolution-PEMWE`。贯穿五处文件的主键，`check_library.sh` 核对一致性。

### 笔记 frontmatter 字段

`schema/note-frontmatter.schema.json` 是权威定义。核心字段：citekey, title, authors, authors_full[], year, journal, doi, tags[], keywords[], 类型(enum), 方法关键词, 表征方法[], 体系, status, rating, related[], si_files[], added。正文固定七节。

### 标签词表是动态的、自动学习的

`load_live_tag_vocab()`（`scripts/batch_ingest.py`）每次构建 prompt 时扫一遍 `notes/*.md` 的 `tags` 字段，取"静态种子表 `TAG_VOCAB` ∪ 库里实际出现过的全部标签"作为当前受控词表塞进 prompt。效果：LLM 第一次给某个新材料/新体系新造一个标签后，这个标签立刻对后续所有入库可见——不需要手动把新词写回 `TAG_VOCAB` 常量。**任何重写都要保留"词表从磁盘现算"这个设计**，不要退回静态列表（退回静态列表会导致同一种材料的论文被打上不一致的标签，检索时互相找不到彼此——这是本项目踩过的真实坑）。

---

## 3. 内容生产管线（入库）

三条并存的路线：

- **路线 A（人工交互）**：`ds.py pdf-meta` 抽元数据 → Agent CLI 审核 → `ingest_from_meta.py` 落盘六处
- **路线 B（无人值守批量）**：`batch_ingest.py --source <dir> --until-done`，断点续跑
- **路线 C**：其他 Agent 直接照 AGENTS.md 规范生成文本

### `batch_ingest.py` 关键实现

- `group_and_pair(source)` 扫描 PDF/docx 配对 SI。**注意**：排除本库产出目录时必须用具体目录集合（`{PAPERS, NOTES, NOTES_READABLE, EXTRACTED_TEXT}`），不能用 `ROOT.resolve() not in parents`（`inbox/` 本身是 ROOT 子目录会导致恒为 False）
- 去重：DOI 优先 + 标题相似度兜底（`is_title_duplicate()` + `find_title_duplicate()`）
- `EXTRACT_SYSTEM_PROMPT` 由 `build_extract_system_prompt()` 每次现算 `load_live_tag_vocab()`
- **模型分工原则**：纯结构化抽取用 flash，总结输出（读者会看的内容）用 pro

---

## 4. 系统性学术检索（`academic_search.py`）

替代 WebSearch 作为"扩充/查新"的主要发现引擎：

### OpenAlex 系统检索

- 按期刊 + 日期范围 + 关键词 cursor 遍历全部匹配论文（不受 ~10 条/次的限制）
- **中文关键词翻译**：`expand_keywords()` 优先 DeepSeek flash 翻译，回退到内置领域词典（`_ZH_TERM_DICT`，60+ 条电催化/PEM 领域术语）
- **AND 相关性过滤**：`match_tracker` 跟踪每篇论文匹配了哪些关键词组，`min_match` 控制最少匹配几个才保留，按 `relevance_score` 降序排列
- URL 编码：filter 不编码（`:` `,` `|` 是语法符号），search 用 `quote(kw, safe='')` 正确编码空格和中文
- 需要 `OPENALEX_API_KEY` 环境变量（免费 key，无 key 可跑但 rate limit 极低 ~10 次/天）

### Semantic Scholar 引用图谱

- `--citation-graph` 模式：从库内已有论文 DOI 出发（优先 status=read），查引用/被引链
- 需要 `SEMANTIC_SCHOLAR_API_KEY`（无 key 用 1 req/s 免费额度）

### 搜索输出

输出 JSON 直接喂 `scan_new_papers.py` 做 Crossref 核验 + 去重 + 分类。

---

## 5. 语义检索（`build_embedding_index.py` + `semantic_search.py`）

- 本地 `sentence-transformers` 多语言模型（`paraphrase-multilingual-MiniLM-L12-v2`）
- 纯本地推理，不调云端 embedding API
- 按 `notes/*.md` 的 mtime 做增量更新（只重新生成变了的笔记）
- FAISS 索引每次全量重建（几百篇量级几秒）
- `semantic_search.py --explain` 额外调 deepseek-v4-flash 生成相关性说明

---

## 6. 脚本完整清单

| 脚本 | 职责 |
|---|---|
| `academic_search.py` | OpenAlex 系统检索 + Semantic Scholar 引用图谱 + 中文翻译 + AND 过滤 |
| `batch_ingest.py` | 无人值守批量入库（断点续跑） |
| `ds.py` | 通用 LLM API 调用（chat/json/pdf-meta） |
| `ingest_from_meta.py` | 把 pdf-meta JSON 组装成六处文件 |
| `scan_new_papers.py` | Crossref 核验 + 去重 + 分类，导出 xlsx |
| `scan_state.py` | 记录每领域上次扫描日期 |
| `parse_search_results.py` | WebSearch 原始结果 → LLM 抽取候选 |
| `find_citations.py` | 逐处找引文 + support/contradict/unclear 判断 + 枚举句拆分 |
| `export_for_endnote.py` | 导出 RIS/BibTeX |
| `word_insert_citation.py` | Word 光标处插编号引用（仅 Windows） |
| `word_auto_cite.py` | 自动扫 Word 全文识别待引用位置（仅 Windows） |
| `build_embedding_index.py` | 语义检索索引构建（增量更新） |
| `semantic_search.py` | 语义检索（FAISS + 可选 --explain） |
| `check_library.sh` | 三方对账 + 标题查重 + notes-readable 同步检查 |
| `build_keyword_index.sh` | 重建 KEYWORDS.md |
| `render_readable_notes.py` | notes/ → notes-readable/ 全量同步 |
| `regenerate_notes.py` | 用缓存全文重写笔记正文（断点续跑） |
| `match_orphan_si.py` | 孤立 SI 标题相似度匹配 |
| `extract_performance.py` | 批量抽取结构化数值到 csv |
| `build_topic_digest.py` | 主题综述：筛笔记 + 查重 + 可选起草初稿 |
| `md_to_docx.py` | Markdown → docx（python-docx） |
| `find_duplicate_titles.py` | 全库标题相似度查重 |
| `resolve_duplicate.py` | 合并重复 citekey（五处文件） |
| `rename_journal_abbr.py` | 批量改写期刊简称 |
| `export_referable_folder.py` | 按条件筛选导出 PDF+笔记 |
| `ui/app.py` | Streamlit 网页面板 |
| `ui/run_hidden.vbs` | 隐藏窗口启动 Streamlit |

---

## 7. 网页面板（`scripts/ui/app.py`）

### 七个标签页

1. **总览**：库存统计（`notes_dir_fingerprint()` 驱动的缓存扫描）、拖拽 PDF、入库按钮（检测 Agent CLI）、"最近新增"卡片
2. **文献库**：搜索（标题+**作者**+关键词同一个框）/标签筛选/期刊筛选/排序/分页 + 右侧阅读窗格（sticky 定位 + 独立滚动）+ 就地编辑 + 🧠 语义检索
3. **体检**：跑 `check_library.sh`
4. **引文献**：贴正文 → 拆句 → 逐条候选 → LLM 判断，**实时进度条 + 停止按钮**
5. **扩充/查新**：三引擎（OpenAlex / 引用图谱 / WebSearch），**实时进度条 + 停止按钮**
6. **Word 引用**：插编号引用 / 自动扫全文
7. **命令**：自由命令 / 自然语言翻译

### 核心技术机制

**a) 数据缓存与失效**

`load_all_papers(_cache_bust)` 用 `@st.cache_data` 缓存。缓存 key 用 `notes_dir_fingerprint()`（notes/*.md 文件数 + 最新 mtime），从磁盘现算，和会话无关。**教训**：之前用 `session_state["papers_cache_bust"]` 当 key，导致全新浏览器会话命中旧缓存（因为 key 是会话私有的但缓存是进程共享的）。

**b) 流式子进程 + 进度条 + 停止按钮**

`run_streaming()` 函数：
- 子进程设置 `PYTHONUNBUFFERED=1` 环境变量，**强制 Python 不缓冲 stdout**（否则 print() 输出攒在管道里，父进程 readline() 读不到——这是进度条卡死的根因）
- 后台线程 + `queue.Queue` 异步读 stdout，主线程 `queue.get(timeout=0.5)` 非阻塞轮询
- 每 0.5s 检查 `session_state[stop_flag]`，为 True 时 `proc.terminate()` 终止子进程
- 解析进度格式：`OpenAlex [xxx]: N pages, M papers` / `核验进度: N/M` / `S2 引用图谱: 已处理 N/M`

**停止按钮工作原理**：点击 → 设 `session_state["_oa_stop_requested"] = True` → `st.rerun()` → Streamlit 中断当前执行从头重跑 → 循环里的 stop_flag 检查在下次迭代时检测到 True → 终止子进程。

**c) 阅读窗格**

`position: sticky` + `align-self: flex-start` + `overflow-y: auto` 加在外层 `.stColumn` 上（不是加在自己的容器 div 上——那个没有富余高度，sticky 不生效）。用 `:has()` 选择器精确选中目标列。

**d) 跨 rerun 状态维护**

- `restore_active_tab()`：注入 JS + `session_state["active_tab"]` 记住当前标签页
- `inject_scroll_preserver()`：capture 阶段监听器保存滚动位置，rerun 后恢复
- `inject_outside_click_closer()`：监听 document 点击，命中空白区域程序化触发关闭按钮
- 每次 `st.components.v1.html()` 调用加时间戳 nonce，防止 Streamlit 复用旧 iframe

**e) 语义检索 UI**

直接 `import semantic_search as ss` 复用 `generate_reason()`，FAISS 索引用 `st.cache_resource` 缓存，缓存 key 是 `embeddings_fingerprint()`（index.faiss 的 mtime）。expander 状态用 `session_state["semantic_expander_open"]` 跨 rerun 保持展开。

---

## 8. 如果做真前端：后端 API 需要包什么

原则：**后端继续 Python，直接复用现有脚本函数**，底层脚本仍可独立命令行调用。

```
GET  /api/stats                      库存统计
GET  /api/agent-cli-detected         { detected: "claude"|null }
GET  /api/papers                     列表(搜索/筛选/排序/分页)
GET  /api/papers/{citekey}           单篇详情(含正文七节)
PUT  /api/papers/{citekey}           编辑保存(tags+正文小节)
POST /api/inbox/upload               (multipart) 存入inbox/
POST /api/inbox/clear                清空inbox/
POST /api/ingest                     启动 batch_ingest 后台 job
POST /api/check                      启动 check_library 后台 job
POST /api/search/openalex            启动 academic_search OpenAlex job
POST /api/search/citation-graph      启动引用图谱 job
POST /api/search/semantic            语义检索（直接调用不走 job）
POST /api/citations                  启动 find_citations job
POST /api/scan/parse                 parse_search_results
POST /api/scan/run                   scan_new_papers
POST /api/word/insert|rebuild|auto   Word 引用相关
POST /api/command/run                任意命令
POST /api/command/translate          自然语言→命令翻译
GET  /api/jobs/{id}                  通用后台任务状态(SSE/WebSocket推送)
POST /api/jobs/{id}/stop             终止后台任务
```

后台任务（`/api/jobs/*`）值得抽象成通用 JobManager 类——现有"引文献""扩充查新""体检""入库""OpenAlex检索"全部是同一种"起 subprocess、流式读输出、可中途停"的模式。

---

## 9. 已知架构债务

- `app.py` 接近 1600 行单文件，7 个标签页逻辑堆在一起
- `build_keyword_index.sh` 很慢（Git Bash for 循环 + subshell），值得用 Python 重写
- `.ingest_state.json` 等状态文件的 key 格式不统一（绝对路径 vs 相对路径）
- KEYWORDS.md 是纯生成物但没有版本校验

---

## 10. 不受前端选型影响、必须保留的部分

- `notes/` 唯一真源地位、citekey 五处一致性、frontmatter schema
- `AGENTS.md` / `CLAUDE.md` / `ARCHITECTURE.md`（面向对话和开发者的文档）
- 所有 `scripts/*.py` / `*.sh` 独立可命令行调用的能力
- `batch_ingest.py` 的"只读源、只复制、不删除"红线
- 标签词表动态计算（从磁盘现算，不是静态列表）
- 缓存 key 从磁盘现算（不用 session_state）的失效策略
