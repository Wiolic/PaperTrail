# 架构说明

这份文档是给要读代码、改代码、或者想把这套系统换一层前端的人看的。用户向导在 [`QUICKSTART.md`](QUICKSTART.md)，数据格式的权威定义在 [`AGENTS.md`](AGENTS.md)——本文件不重复那两份文档的内容，只讲"这套东西内部是怎么接起来的"。

---

## 1. 整体形状

三层，互相独立、可以单独替换：

```
数据层        notes/ 里的一堆 Markdown 文件, 加上从它们派生出来的索引/向量/缓存
脚本层        scripts/*.py + scripts/*.sh, 每个都能独立当命令行工具跑
交互层        对话(任何 Agent CLI 读 AGENTS.md 照做) + 网页面板(scripts/ui/app.py)
```

关键约束：**脚本层不依赖交互层**。所有真正的逻辑（抽取元数据、判断查重、构建索引、找引用）都在 `scripts/` 下的普通 Python 脚本里，网页面板只是拼了一层表单去调用这些脚本；对话式的 Agent CLI 也是直接跑这些脚本，或者照着 `AGENTS.md` 里写的规则手动生成同样格式的文本。三种使用方式（命令行/网页/对话）产出的数据格式完全一致，因为它们最终都是同一套脚本在写文件。

---

## 2. 数据层：一切从 `notes/*.md` 派生

```
notes/<citekey>.md          ★ 唯一真源: YAML frontmatter + 固定七节 Markdown 正文, 不折行
notes-readable/<citekey>.md   派生物: 同样内容, 正文按 60 字符折行给人读, 由 render_readable_notes.py 生成
extracted-text/<citekey>.txt  派生物: PDF 抽出来的纯文字缓存, 避免以后改逻辑要重新解析 PDF
embeddings/                    派生物: notes/ 的语义向量索引 (index.faiss + metadata.json + vectors.npy)
library.bib                   派生物: 从 notes/ frontmatter 追加生成的 BibTeX 库
INDEX.md                       派生物: 按年份排序的总索引表
KEYWORDS.md                    派生物: 标签/关键词/表征方法的倒排索引
```

"派生"的意思是：这几样东西全部可以从 `notes/*.md` 重新生成，删掉不会丢数据。真正手写、需要备份的只有 `notes/`（以及最初的 `papers/*.pdf`）。

`citekey`（形如 `2025-JACS-Ir-Dissolution-PEMWE`）是贯穿 `papers/`、`notes/`、`notes-readable/`、`library.bib`、`INDEX.md` 五处的主键，`check_library.sh` 负责核对这五处是否互相对得上。

frontmatter 字段的权威定义在 `schema/note-frontmatter.schema.json`，具体有哪些字段、枚举值是什么见 `AGENTS.md`，这里不重复。

---

## 3. 入库管线怎么跑起来的

三条路线，选哪条取决于你是想要人工审核每一步，还是想让它无人值守跑完一整个文件夹：

- **`ds.py pdf-meta <PDF>` → 人工审核 → `ingest_from_meta.py`**：单篇/小批量场景。`ds.py` 调 LLM 把 PDF 首页文字转成结构化 JSON，人（或对话中的 Agent CLI）看一眼查重/核实 DOI 没问题，再交给 `ingest_from_meta.py` 落盘六处文件。
- **`batch_ingest.py --source <目录> --until-done`**：无人值守全自动，扫一整个文件夹，逐篇跑完上面同样的流程不用人盯着，处理进度记在 `scripts/.ingest_state.json`，中断重跑自动跳过已处理的文件。
- **任意其他 Agent 直接照 `AGENTS.md` 的规范手写**：如果连读 PDF 的工具都没有，也可以让人把 PDF 文字贴给它，它只负责按规范吐出三段文本（笔记正文/bib 条目/索引行），由外壳程序负责落盘。

`batch_ingest.py` 内部几个值得知道的实现细节：

- **标签词表是动态算出来的，不是写死的常量**。`load_live_tag_vocab()` 每次构建给 LLM 的 prompt 之前，会现扫一遍 `notes/*.md` 的 `tags` 字段，把"内置种子词表 ∪ 库里实际已经出现过的所有标签"作为这次 prompt 里的候选词表。效果是：LLM 第一次给某个新材料造出一个新标签之后，这个词立刻对下一篇论文的 prompt 可见——不会出现同一种东西第一篇论文打了"XX"、后面十篇又各打了不同的近义词，导致检索时这些论文互相找不到彼此的问题。**任何重写都不要退回成静态列表**，这是踩过的真实的坑。
- **SI（补充材料）配对**用同名规则匹配主文件和它的支持信息文件，配不上的候选留在 `inbox/` 里不处理，不会硬凑。
- **去重**先按 DOI 精确匹配，DOI 缺失或对不上再退到标题相似度兜底判断。
- **排除本库自己产出目录时要用具体的目录集合**（`{PAPERS, NOTES, NOTES_READABLE, EXTRACTED_TEXT}` 逐一比较），不能简单判断"是不是在 ROOT 目录之下"——因为 `inbox/` 本身就在 `ROOT` 下面，这样判断会导致扫描永远返回空列表。
- **模型分工**：纯结构化抽取（判断字段该填什么）用便宜的小模型；需要人读的总结性文字（三句话总结这类）用质量更高的模型。

---

## 4. 检索是怎么做的

系统里其实有三套完全独立的检索方式，互相不依赖：

**关键词检索**——最基础的一种，直接在 frontmatter 字段（标题/作者/标签/关键词/期刊）上做字符串匹配，网页面板的搜索框和 `fulltext_search.py`／`expand_search.py`（同义词展开）都是这个思路的变体。

**语义检索**（`build_embedding_index.py` + `semantic_search.py`）——用本地跑的多语言 `sentence-transformers` 模型把每篇笔记的标题/标签/关键词/正文编码成向量，存进一个 FAISS 索引；查询时把问题也编码成向量，做余弦相似度检索。全程本地推理，不调任何云端 embedding API。增量更新的逻辑是：按 `notes/*.md` 的文件修改时间跟上次记录的时间比对，只重新编码真正变过的笔记，索引本身因为便宜（几百篇量级几秒钟）每次都整个重建，不做增量索引维护。可选加 `--explain` 会额外调一次便宜的模型生成"为什么相关"的一句话说明。

**系统性学术检索**（`academic_search.py`）——跟前两种不一样，这个不是搜你自己的库，而是搜 OpenAlex/Semantic Scholar 这样的外部文献数据库，用来发现你还没收录的新论文。按"期刊 + 日期范围 + 关键词"遍历全部匹配结果（不像普通搜索引擎只给前几条），支持中文关键词自动翻译、支持从库里已有论文出发查引用/被引关系。搜出来的候选交给 `scan_new_papers.py` 做 Crossref 核验和去重，只留下库里真的没有的新论文。

---

## 5. 网页面板（`scripts/ui/app.py`）

一个约 1800 行的单文件 Streamlit 应用，七个标签页：总览、文献库、体检、引文献、扩充查新、Word 引用、命令。技术上有几个绕不开的坑，记录在这里省得以后重踩：

**缓存失效不能靠 session_state**。`load_all_papers()` 用 `st.cache_data` 缓存全部笔记的解析结果，缓存的 key 必须是从磁盘现算出来的指纹（笔记文件数 + 最新修改时间），不能用存在 `session_state` 里的值——因为 `st.cache_data` 的缓存是整个 Python 进程共享的，而 `session_state` 是按浏览器会话隔离的，用会话状态当 key 会导致任何还没手动触发过一次刷新的全新会话，都命中进程里第一次调用时缓存下来的那份旧数据，不管磁盘上实际发生了什么变化。

**后台跑脚本要读到实时进度，得强制关掉 Python 的输出缓冲**。入库、引文献、扩充查新这几个功能跑的是耗时的子进程，面板要一边跑一边显示进度条、还要能中途按停止键终止。子进程启动时必须设 `PYTHONUNBUFFERED=1` 环境变量，否则脚本里的 `print()` 输出会攒在管道缓冲区里，父进程读不到实时输出，进度条会看起来卡死。父进程用一个后台线程 + 队列异步读子进程的 stdout，主线程轮询队列更新进度；停止按钮触发时设一个标志位，下一次轮询检测到就把子进程杀掉。

**阅读窗格要固定在视口内、和左边列表各自滚动，`position: sticky` 得加在正确的元素上**。直接加在笔记内容自己的容器 div 上是没用的——这个 div 因为设了 `max-height` 收缩到跟内容一样高，它没有比自己父元素更多的空间可以"粘"。真正有富余空间、值得设成 sticky 的是外层被 flex 布局拉伸到跟左边列表一样高的那个列容器，同时要给这个 sticky 元素加 `align-self: flex-start` 取消默认的拉伸行为（否则它自己也会被撑到跟左边列表一样高，同样没有可粘贴的余量）。定位这个外层容器用 CSS `:has()` 选择器，因为 Streamlit 自动生成的列容器没法直接用 `key=` 参数去指定样式。

**跨 rerun 的 UI 状态**（当前选中哪个标签页、滚动位置、展开/折叠的面板）都不是 Streamlit 自带能力，得手动用 `session_state` 记录 + 注入 JS 在前端"重放"这个状态。比较容易踩的坑是 `st.expander(expanded=...)` 这个参数只在渲染那一刻生效一次，点击某个按钮触发 `st.rerun()` 之后如果没有显式把这个状态存进 `session_state` 再传回去，展开的内容会在下一次渲染时被重置回默认的折叠状态——语义检索结果被藏起来看不到，就是这个原因导致的。

---

## 6. 脚本清单

按用途分组（每个脚本文件头部的 docstring 有更详细的说明和 `--help`）：

**入库**：`batch_ingest.py`（无人值守批量）、`ingest_from_meta.py`（单篇落盘）、`ds.py`（通用 LLM 调用）、`regenerate_notes.py`（用缓存全文重写笔记，断点续跑）、`match_orphan_si.py`（跨文件夹配对孤立的 SI）、`reingest_oer.py`（用新版逻辑重新处理某个子集的历史论文，可以照抄这个模式做类似的批量重跑）

**检索**：`semantic_search.py` + `build_embedding_index.py`（语义检索）、`fulltext_search.py`（笔记正文全文搜索）、`expand_search.py`（同义词展开搜索）、`academic_search.py`（OpenAlex/Semantic Scholar 系统检索）、`scan_new_papers.py` + `scan_state.py`（查新去重 + 记录扫描进度）、`parse_search_results.py`（把网页搜索的原始结果丢给 LLM 抽成候选列表）、`citation_network.py`（构建引用关系图，生成可交互 HTML）

**写作**：`find_citations.py`（逐句找引用）、`word_insert_citation.py` + `word_auto_cite.py`（Word 插引用，仅 Windows）、`export_for_endnote.py`（导出 RIS/BibTeX）、`build_topic_digest.py`（主题综述筛笔记）、`md_to_docx.py`（Markdown 转 docx）

**维护**：`check_library.sh` + `validate_notes.py`（体检/格式校验）、`build_keyword_index.sh`（重建关键词索引）、`render_readable_notes.py`（notes → notes-readable 同步）、`find_duplicate_titles.py` + `resolve_duplicate.py`（查重/合并重复 citekey）、`backfill_doi.py` + `backfill_fields.py`（补全缺失字段）、`auto_link_related.py`（自动关联相似论文）、`extract_performance.py`（批量抽取结构化数值到 csv）、`rename_journal_abbr.py` / `rename_citekeys.py` / `remove_citekeys.py`（批量改名/删除，均默认预览模式）、`export_referable_folder.py`（按条件导出子集）、`map_characterization.py` / `reconcile_state.py`（历史维护脚本）

**面板**：`ui/app.py`（Streamlit 应用本体）、`ui/run_hidden.vbs`（Windows 上隐藏命令行窗口启动）

---

## 7. 如果要换一层真正的前端

现在的网页面板是 Streamlit——好处是不用额外写前后端分离的代码，坏处是复杂交互（独立滚动、跨 rerun 状态）要绕不少弯子。如果决定换成真正的 SPA + 后端 API，原则是**后端继续是 Python，直接 import 现有脚本的函数**，不要重写业务逻辑，脚本本身仍然保留独立命令行调用的能力。大致的接口形状：

```
GET  /api/papers                     列表(支持搜索/筛选/排序/分页)
GET  /api/papers/{citekey}           单篇详情
PUT  /api/papers/{citekey}           编辑保存
POST /api/inbox/upload               上传PDF到inbox/
POST /api/ingest                     启动批量入库(后台任务)
POST /api/search/semantic            语义检索(同步返回,不需要走后台任务)
POST /api/search/academic            启动OpenAlex系统检索(后台任务)
POST /api/citations                  启动引文献检索(后台任务)
POST /api/check                      启动体检(后台任务)
GET  /api/jobs/{id}                  查询某个后台任务的进度(建议用SSE推送)
POST /api/jobs/{id}/stop             终止某个后台任务
```

"入库"、"扩充查新"、"引文献"、"体检"这几个耗时操作本质上都是同一种模式——起一个子进程、流式读输出、支持中途停止——值得抽象成一个通用的任务管理器，而不是每个功能各写一套。

---

## 8. 不管前端怎么换，这些东西不能动

- `notes/` 是唯一真源的地位，以及 citekey 在五处文件间的一致性
- frontmatter 的 schema 定义（`schema/note-frontmatter.schema.json`）
- 所有 `scripts/*.py`/`*.sh` 保持可以独立命令行调用的能力，不要变成只能被面板调用的私有函数
- `batch_ingest.py`"只读源目录、只复制文件、绝不移动删除源文件"这条红线
- 标签词表从磁盘现算并自动学习新词的机制，不要退回静态列表
- 缓存失效判断从磁盘现算（文件指纹），不依赖 session/请求上下文
