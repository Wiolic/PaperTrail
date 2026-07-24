#!/usr/bin/env python3
"""
PaperTrail —— 本地网页操作面板, 把最常用的几个脚本包一层表单+结果展示, 不用记命令行参数。
跑法: `streamlit run scripts/ui/app.py`(在库根目录下), 或双击 `PaperTrail Launcher.bat`。

这不是要替代 Claude Code/Codex 这类 Agent CLI——查重判断、DOI 可信度核实、笔记内容审核
这些需要"记住上下文+做判断"的活仍然要靠对话完成, 这个面板只负责把"跑脚本+看结果"这部分
做得顺手一点: 体检、引文献、扩充/查新、Word插入引用、浏览文献库、跑命令。

不做的事: 不做"入库"的审核环节(查重/DOI核实/SI配对这些判断步骤), 那部分本来就该由
Agent CLI 边跑边跟你确认, 塞进一个无对话能力的网页表单里没有意义。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
EXPORTS = ROOT / "exports"
NOTES_DIR = ROOT / "notes"
PAPERS_DIR = ROOT / "papers"

TAB_LABELS = {
    "recent": "📊 总览", "lib": "📚 文献库", "check": "🩺 体检", "cite": "📝 引文献",
    "scan": "🔎 扩充/查新", "word": "📄 Word 插入引用", "cmd": "⚙️ 命令",
    "semantic": "📚 文献库",  # 语义检索结果卡片也在"文献库"页里, 复用同一个tab标签
}

st.set_page_config(page_title="PaperTrail", page_icon="📘", layout="wide")

APP_CSS = """
<style>
:root {
    --morandi-blue-deep: #5B7C99;
    --morandi-blue: #7B93AB;
    --morandi-blue-pale: #C4D1DB;
    --morandi-bg: #F4F1EC;
    --morandi-card: #E6E9E4;
}
.stApp { background-color: var(--morandi-bg); }
h1, h2, h3 { color: var(--morandi-blue-deep) !important; }
div[data-testid="stMetric"] {
    background-color: var(--morandi-card);
    border: 1px solid var(--morandi-blue-pale);
    border-radius: 10px;
    padding: 14px 18px;
}
.stButton>button {
    background-color: var(--morandi-blue);
    color: white;
    border: none;
    border-radius: 8px;
}
.stButton>button:hover {
    background-color: var(--morandi-blue-deep);
    color: white;
}
div[data-testid="stTabs"] button[aria-selected="true"] {
    color: var(--morandi-blue-deep);
    border-bottom-color: var(--morandi-blue-deep);
}
.tag-chip {
    display: inline-block;
    background-color: var(--morandi-blue-pale);
    color: var(--morandi-blue-deep);
    border-radius: 6px;
    padding: 1px 9px;
    margin: 2px 4px 2px 0;
    font-size: 0.78em;
    font-weight: 600;
}
@keyframes pt-slide-in {
    from { transform: translateX(40px); opacity: 0; }
    to { transform: translateX(0); opacity: 1; }
}
div[class*="st-key-reader_pane"] {
    animation: pt-slide-in 0.22s ease-out;
}
/* 2026-07-23加: 两个st.columns()是同一个flex行里的兄弟, 都从行顶部对齐开始——笔记内容
   通常比左边那一长串列表短得多, 滑到列表下方再点"笔记"时, 阅读窗格自己的内容其实已经
   在页面靠上的位置就结束了, 当前视口对着的是它下方的空白, 看起来像"点开了但看不到内容"。
   想让它跟着视口"钉住"、同时内部能独立滚动, position:sticky 不能直接加在我们自己那个
   紧贴内容大小的容器上——踩过的坑: 第一版把 sticky/max-height/overflow 都加在
   `.st-key-reader_pane` 这个div本身, 结果完全不生效(实测滚动时它的位置跟main列表
   一起等量移动, 不是"钉住"的行为)。原因是 sticky 元素能"钉住"多久取决于它自己的
   直接容纳块(通常就是父元素)有多少"富余高度"可以让它在里面游走——而我们自己这个div
   被 max-height 限制成跟内容一样高, 它的直接父元素(Streamlit自动生成的包裹层)也就
   随之收缩成同样高度, 两者高度相等=完全没有富余空间, sticky 等于没有意义。真正有"富余
   高度"的是外层的 `.stColumn`(它被flex的默认 stretch 行为拉伸到和左边main列|一样高,
   比如 12000+px), sticky 必须加在这一层, 且要靠 `align-self: flex-start` 取消默认的
   stretch 拉伸行为(否则sticky内容本身也会被强制拉伸到12000+px高, 同样没有富余空间)。
   用 `:has()` 选中"包含我们那个reader_pane标记div的那个.stColumn"，只影响阅读栏这一列,
   不影响左边的main列。 */
div.stColumn:has(div[class*="st-key-reader_pane"]) {
    position: sticky;
    top: 12px;
    align-self: flex-start;
    max-height: calc(100vh - 24px);
    overflow-y: auto;
}
</style>
"""
st.markdown(APP_CSS, unsafe_allow_html=True)


def find_git_bash() -> str:
    """Windows 上 `bash` 在 PATH 里可能优先解析到 WSL 的 bash.exe(System32下那个), 不是
    Git Bash——这两者行为不兼容, check_library.sh/build_keyword_index.sh 是按 Git Bash
    写的, 用 WSL 跑会报 HCS/CreateVm 相关的环境错误。显式找 Git Bash 的常见安装路径,
    找不到再退回 PATH 里的 "bash"(并寄希望于用户 PATH 顺序正确)。"""
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(candidate).exists():
            return candidate
    return "bash"


GIT_BASH = find_git_bash()

AGENT_CLI_KEYWORDS = ("claude", "codex", "kimi")


def detect_running_agent_cli(timeout: int = 5) -> str | None:
    """检测本机是否有 Claude Code / Codex CLI / Kimi Code 这类 Agent CLI 进程正在跑着。
    用于"入库"按钮判断: 如果有 Agent CLI 在运行, 查重/DOI核实/SI配对这些需要判断的活
    交给那边的对话做更可靠; 只有确实没有 Agent CLI 在跑的时候, 才应该让网页这边直接
    调 API 全自动入库(scripts/batch_ingest.py, 无人审核)。

    按命令行(而不是单纯进程名)里是否包含 claude/codex/kimi 关键词匹配, 因为这几个
    CLI 底层大多跑在 node/python 解释器上, 进程名本身往往是 "node.exe"/"python" 之类
    通用名字, 命令行参数里才能看到具体是哪个工具。检测不到时不视为报错, 直接当作
    "没有 Agent CLI 在跑"处理(宁可多跑一次自动入库, 也不要因为检测失败卡住用户)。
    返回匹配到的关键词(用于提示信息), 没检测到则返回 None。"""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | Select-Object -ExpandProperty CommandLine"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
        else:
            result = subprocess.run(["ps", "-eo", "command"], capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=timeout)
        output = (result.stdout or "").lower()
    except Exception:
        return None

    # 排除面板自己这个 streamlit 进程(命令行里含 "streamlit"/"app.py", 不含 agent 关键词,
    # 一般不会误命中, 这里不做特殊排除, 保持简单)。
    for line in output.splitlines():
        for kw in AGENT_CLI_KEYWORDS:
            if kw in line:
                return kw
    return None


def run(cmd: list, cwd: Path = ROOT, timeout: int = 600):
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError as e:
        return 1, "", f"找不到可执行程序: {e}"
    except subprocess.TimeoutExpired:
        return 1, "", "运行超时"


def run_streaming(cmd: list, cwd: Path = ROOT, timeout: int = 600,
                  progress_total: int = 0, progress_label: str = "",
                  stop_flag: str = ""):
    """
    实时流式运行子进程，边跑边在页面上显示输出和进度条。
    返回 (returncode, full_output_str)。
    progress_total: 预期的总步数(如 期刊数×关键词数)，用于进度条；0=不显示进度条。
    stop_flag: session_state key名，为 True 时中止进程。
    """
    import time as _time
    import threading as _threading
    import queue as _queue
    import re as _re

    output_lines: list[str] = []
    placeholder = st.empty()
    prog_bar = st.progress(0, text=f"{progress_label} 0/{progress_total}") if progress_total > 0 else None
    completed_steps = 0

    # 强制子进程 Python 不缓冲 stdout，否则 print() 输出会攒在管道里读不到
    _env = os.environ.copy()
    _env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=_env,
        )
    except FileNotFoundError as e:
        placeholder.error(f"找不到可执行程序: {e}")
        return 1, ""

    # 后台线程读 stdout 放入队列，避免主线程 readline 阻塞导致停止按钮无法响应
    _q: _queue.Queue = _queue.Queue()

    def _reader():
        try:
            for line in iter(proc.stdout.readline, ""):
                _q.put(line)
        except Exception:
            pass
        _q.put(None)

    _t = _threading.Thread(target=_reader, daemon=True)
    _t.start()

    start_time = _time.time()
    stopped = False
    while True:
        # 检查停止标志
        if stop_flag and st.session_state.get(stop_flag):
            stopped = True
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            output_lines.append("\n[搜索已停止]")
            break

        try:
            item = _q.get(timeout=0.5)
        except _queue.Empty:
            if _time.time() - start_time > timeout:
                proc.terminate()
                output_lines.append("\n[运行超时]")
                break
            continue

        if item is None:
            break

        output_lines.append(item.rstrip("\n"))

        # 解析进度：多种格式支持
        if "OpenAlex [" in item and "pages" in item:
            completed_steps += 1
            if prog_bar and progress_total > 0:
                pct = min(completed_steps / progress_total, 1.0)
                prog_bar.progress(pct, text=f"{progress_label} {completed_steps}/{progress_total}")
        elif "核验进度:" in item:
            m = _re.search(r"核验进度:\s*(\d+)/(\d+)", item)
            if m and prog_bar:
                cur, tot = int(m.group(1)), int(m.group(2))
                prog_bar.progress(min(cur / tot, 1.0), text=f"{progress_label} {cur}/{tot}")
        elif "S2 引用图谱: 已处理" in item or "S2 引用图谱: 共" in item:
            m = _re.search(r"已处理\s*(\d+)/(\d+)", item)
            if m and prog_bar:
                cur, tot = int(m.group(1)), int(m.group(2))
                prog_bar.progress(min(cur / tot, 1.0), text=f"{progress_label} {cur}/{tot}")
        elif "Total:" in item and "filtered" in item:
            if prog_bar:
                prog_bar.progress(1.0, text="检索完成")

        # 实时显示最近 20 行
        recent = output_lines[-20:]
        with placeholder.container():
            st.code("\n".join(recent), language="text")

    _t.join(timeout=3)

    # 最终显示完整输出
    full_output = "\n".join(output_lines)
    with placeholder.container():
        st.code(full_output, language="text")
    if prog_bar:
        prog_bar.progress(1.0, text="已停止" if stopped else "完成")

    return proc.returncode or 0, full_output

# ---------- 后台跑脚本 + 可中止 + 进度展示 ----------
# 用于"引文献"这类耗时较长(要跑多次LLM调用)的任务: 不能用一次性阻塞的 subprocess.run()
# (那样用户在等待期间点不了"停止"按钮), 改成后台线程持续读取子进程stdout, 主线程通过
# session_state存的run_id周期性(sleep+st.rerun)轮询显示最新进度, "停止"按钮直接
# terminate()子进程。
#
# 踩过的坑: 一开始直接写 "_BG_RUNS: dict = {}" 当模块级变量——以为跟一般Python
# 模块一样, 只在第一次import时执行一次、之后重复利用同一个字典。实测发现完全不是这样:
# Streamlit 每次 rerun 是把整个脚本文件从头到尾重新执行一遍(不是只重跑触发回调的那部分),
# 这条赋值语句每次rerun都会真的再跑一次, 把刚存进去、后台线程还在写入的run_id state
# 直接清空覆盖成空字典——表现出来就是"点了生成引用清单, 页面立刻显示'没有生成输出文件
# (可能是被手动停止了)'", 看着像任务瞬间失败, 实际上后台的 find_citations.py 子进程
# 还在正常跑(用Get-CimInstance检查进程仍然存在、几分钟后确实生成了citations.md),
# 只是UI这边引用的字典早被重置成空的了。改用 st.cache_resource 装饰的函数取代
# 裸模块级变量——这是Streamlit官方推荐的"进程内跨rerun/跨session共享可变对象"写法,
# 被装饰的函数体只在第一次调用时真正执行, 之后每次调用都返回同一个缓存对象, 不受
# "整个脚本重新执行"影响。
@st.cache_resource
def _get_bg_runs() -> dict:
    return {}


_BG_RUNS = _get_bg_runs()


def start_bg_process(cmd: list, cwd: Path = ROOT) -> str:
    run_id = uuid.uuid4().hex
    # 强制子进程 Python 不缓冲 stdout（管道模式下默认全缓冲，readline 读不到）
    _env = os.environ.copy()
    _env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", bufsize=1,
                             env=_env)
    state = {"proc": proc, "lines": [], "done": False, "returncode": None, "stopped": False}
    _BG_RUNS[run_id] = state

    def _reader():
        for line in proc.stdout:
            state["lines"].append(line.rstrip("\n"))
        proc.wait()
        state["returncode"] = proc.returncode
        state["done"] = True

    threading.Thread(target=_reader, daemon=True).start()
    return run_id


def stop_bg_process(run_id: str):
    state = _BG_RUNS.get(run_id)
    if not state:
        return
    state["stopped"] = True
    try:
        state["proc"].terminate()
    except Exception:
        pass


def render_bg_progress(run_id: str, step_pattern: str = r"\[(\d+)/(\d+)\]") -> bool:
    """展示某个后台任务当前进度(进度条+最新一行状态文字)+"停止"按钮。返回True表示
    任务已经跑完(done或被停止), 调用方据此决定是否去读输出文件、清理run_id。"""
    state = _BG_RUNS.get(run_id)
    if not state:
        return True
    lines = state["lines"]
    last_line = lines[-1] if lines else "启动中..."
    m = None
    for line in reversed(lines):
        m = re.search(step_pattern, line)
        if m:
            break
    col_bar, col_stop = st.columns([5, 1])
    with col_bar:
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            st.progress(min(cur / total, 1.0) if total else 0.0, text=f"{last_line}")
        else:
            st.progress(0.15 if not state["done"] else 1.0, text=last_line)
    with col_stop:
        if not state["done"]:
            if st.button("⏹ 停止", key=f"stop_{run_id}", use_container_width=True):
                stop_bg_process(run_id)
                st.rerun()
    with st.expander("查看完整日志", expanded=False):
        st.code("\n".join(lines) or "(暂无输出)", language="text")
    if state["done"]:
        if state["stopped"]:
            st.warning("已手动停止。")
        elif state["returncode"] not in (0, None):
            st.warning(f"退出码 {state['returncode']}，看上面日志排查。")
        return True
    time.sleep(1)
    st.rerun()
    return False


# ---------- 笔记数据加载 ----------

def _fm_field(fm_text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}:[ \t]*(.*)$", fm_text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _fm_list_field(fm_text: str, name: str) -> list:
    raw = _fm_field(fm_text, name)
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1]
        return [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
    return []


def notes_dir_fingerprint() -> str:
    """给 notes/ 目录当前状态算一个便宜的"指纹"(文件数 + 最新mtime), 用来当
    load_all_papers() 的缓存key——不能再靠 session_state 里手动维护的
    "papers_cache_bust"来触发刷新了。

    踩过的坑(2026-07-23): st.cache_data 的缓存是**整个进程共享的, 不是按session隔离
    的**, 缓存key就是函数入参。以前用 `st.session_state.get("papers_cache_bust", 0.0)`
    当入参——任何还没点过"刷新库列表"按钮的**全新**浏览器会话, session_state里都没有
    这个键, 于是全部退回同一个默认值 0.0, 命中的是进程里"历史上第一次"用 0.0 调用时
    缓存下来的结果。哪怕入库脚本已经把新论文写进了notes/, 只要这个新会话没主动点过
    刷新, 它拿到的就是那份很久以前缓存的旧快照——不管等多久、开多少个新标签页都一样,
    因为大家共享同一份 cache_bust=0.0 的缓存条目。实测复现:入库成功、inbox正确清空,
    但"最近新增"和"库内文献数"在全新打开的浏览器标签页里依然显示入库前的旧数据。

    改成让入参直接由磁盘上 notes/ 的真实状态算出来, 不再依赖任何人工维护的会话状态:
    只要 notes/ 目录实际发生了变化(加了/删了/改了文件), 这个指纹自然就会变, 不管是
    哪个会话、哪次脚本重跑, 都会算出同一个反映当前磁盘真实状态的值, 缓存自然失效重算,
    不会有"某个会话恰好还没刷新过所以看到旧快照"这种依赖时序的坑。"""
    if not NOTES_DIR.exists():
        return "0:0"
    stats = [p.stat() for p in NOTES_DIR.glob("*.md")]
    if not stats:
        return "0:0"
    return f"{len(stats)}:{max(s.st_mtime for s in stats)}"


@st.cache_data(show_spinner=False)
def load_all_papers(_cache_bust: str = "0:0") -> dict:
    """扫 notes/ 下所有笔记, 解析 frontmatter, 返回 {citekey: {...}}。用 st.cache_data 缓存
    (否则每次点按钮触发 rerun 都要重新解析几百个文件); 想强制刷新就改 _cache_bust(见
    "刷新库列表" 按钮)。"""
    papers = {}
    if not NOTES_DIR.exists():
        return papers
    for f in sorted(NOTES_DIR.glob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
        if not m:
            continue
        fm, body = m.groups()
        authors_full = _fm_list_field(fm, "authors_full")
        papers[f.stem] = {
            "title": _fm_field(fm, "title") or f.stem,
            "journal": _fm_field(fm, "journal"),
            "year": _fm_field(fm, "year"),
            "authors": ", ".join(authors_full) if authors_full else _fm_field(fm, "authors"),
            "doi": _fm_field(fm, "doi"),
            "tags": _fm_list_field(fm, "tags"),
            "keywords": _fm_list_field(fm, "keywords"),
            "added": _fm_field(fm, "added"),
            "类型": _fm_field(fm, "类型"),
            "方法关键词": _fm_field(fm, "方法关键词"),
            "表征方法": _fm_list_field(fm, "表征方法"),
            "体系": _fm_field(fm, "体系"),
            "body": body.strip(),
            "_mtime": f.stat().st_mtime,  # 文件修改时间, 同一天入库的按实际写入顺序排
        }
    return papers


# ---------- 语义检索(embeddings/, 见 scripts/build_embedding_index.py) ----------
# 面板这边不重新实现语义检索逻辑, 直接复用 semantic_search.py 里已经写好、测试过的
# generate_reason()——避免同一套"为什么相关"的 prompt 在两个地方各写一份、以后改一处
# 忘了改另一处。sentence-transformers/faiss 是重依赖(尤其sentence-transformers会牵连
# torch), 只在真正点了"语义检索"按钮时才 import, 不拖慢面板本身的启动和其他标签页。

EMBEDDINGS_DIR = ROOT / "embeddings"


def embeddings_fingerprint() -> str:
    """和 notes_dir_fingerprint() 同样的道理: 缓存key必须是从磁盘现算出来的、和会话
    无关的值, 不能用 session_state——否则用户点"构建/更新语义索引"重建完索引后, 别的
    还没刷新过的会话/下次访问依然会命中重建前缓存的旧 FAISS 索引对象。"""
    p = EMBEDDINGS_DIR / "index.faiss"
    return str(p.stat().st_mtime) if p.exists() else "missing"


@st.cache_resource(show_spinner=False)
def load_semantic_bundle(_fingerprint: str):
    """返回 (faiss_index, metadata_dict, sentence_transformer_model) 或 None(索引还没建)。
    用 st.cache_resource 而不是 st.cache_data——FAISS索引和模型都是不可序列化的"资源"
    对象, 这正是 cache_resource 设计用来缓存的东西(cache_data 会尝试序列化/深拷贝返回值,
    对这类对象不适用)。"""
    index_path = EMBEDDINGS_DIR / "index.faiss"
    meta_path = EMBEDDINGS_DIR / "metadata.json"
    if not index_path.exists() or not meta_path.exists():
        return None
    import faiss
    from sentence_transformers import SentenceTransformer
    index = faiss.read_index(str(index_path))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    model = SentenceTransformer(meta["model"])
    return index, meta, model


def run_semantic_query(query: str, top_k: int, explain: bool) -> list[dict]:
    bundle = load_semantic_bundle(embeddings_fingerprint())
    if bundle is None:
        return []
    index, meta, model = bundle
    query_vec = model.encode([query], normalize_embeddings=True, convert_to_numpy=True).astype("float32")
    k = min(top_k, index.ntotal)
    if k == 0:
        return []
    scores, indices = index.search(query_vec, k)

    citekeys = meta["citekeys"]
    titles = meta.get("titles", {})
    results = []
    ss = None
    ds_module = None
    if explain:
        sys.path.insert(0, str(SCRIPTS))
        import semantic_search as ss  # noqa: E402  # 复用其 generate_reason()/EXPLAIN_SYSTEM
        import ds as ds_module  # noqa: E402

    for idx, score in zip(indices[0], scores[0]):
        if idx < 0:
            continue
        citekey = citekeys[idx]
        entry = {"citekey": citekey, "title": titles.get(citekey, citekey), "similarity": float(score)}
        if explain:
            entry["reason"] = ss.generate_reason(ds_module, "deepseek-v4-flash", query, citekey, entry["title"])
        results.append(entry)
    return results


def render_note_body(body: str):
    """把笔记正文的 "## 小节标题" 改用更小的标题级别渲染(不改动任何文字内容,
    只是显示上字号缩小), 其余内容原样走 markdown。切分逻辑复用 split_note_sections(),
    编辑表单用的是同一份, 避免两处各写一遍切法不一致。"""
    preamble, sections = split_note_sections(body)
    if not sections:
        st.markdown(body)
        return
    if preamble:
        st.markdown(preamble)
    for title, content in sections:
        st.markdown(f"###### {title}")
        if content:
            st.markdown(content)


def tag_chips_html(tags: list) -> str:
    return "".join(f'<span class="tag-chip">{t}</span>' for t in tags)


def _get_streamlit_url() -> str:
    """获取当前 Streamlit 服务的 URL，供引用图谱的'回到笔记'按钮使用。"""
    try:
        ctx = st.context
        if hasattr(ctx, 'url') and ctx.url:
            return ctx.url.rstrip('/')
    except Exception:
        pass
    return "http://localhost:8501"


def open_citation_network(citekey: str = ""):
    """生成引用图谱 HTML 并设置内嵌显示标志。每次都带 --focus 重新生成(有缓存时秒级)。"""
    html_path = ROOT / "exports" / "citation_network.html"
    cache_path = ROOT / "exports" / "citation_network_cache.json"
    first_time = not cache_path.exists()
    with st.spinner("正在生成引用图谱..." if first_time else "正在更新图谱焦点..."):
        cmd = [sys.executable, str(SCRIPTS / "citation_network.py"),
               "--streamlit-url", _get_streamlit_url(), "--use-cache"]
        if citekey:
            cmd += ["--focus", citekey]
        code, out, err = run(cmd)
        if code != 0 or not html_path.exists():
            st.error(f"生成失败：{err or out}")
            return
    st.session_state["show_citation"] = True
    st.rerun()


def open_pdf(citekey: str):
    pdf_path = PAPERS_DIR / f"{citekey}.pdf"
    if not pdf_path.exists():
        st.error("没有找到对应 PDF")
        return
    try:
        os.startfile(str(pdf_path))  # noqa: 仅 Windows, 用系统默认 PDF 阅读器打开
    except AttributeError:
        st.error("打开 PDF 依赖 Windows 的 os.startfile，其他系统请直接去 papers/ 目录手动打开")
    except Exception as e:
        st.error(f"打不开: {e}")


def render_paper_card(citekey: str, meta: dict, key_prefix: str):
    with st.container(border=True):
        col_info, col_btns = st.columns([5, 1.4])
        with col_info:
            st.markdown(f"**{meta['title']}**")
            line = f"{meta['journal'] or '期刊未知'} · {meta['year'] or '年份未知'} · `{citekey}`"
            st.caption(line)
            chips = tag_chips_html(meta["tags"])
            if meta["keywords"]:
                chips += (f'<span style="color:#7a7a7a;font-size:0.8em;margin-left:6px;">'
                          f'{"、".join(meta["keywords"])}</span>')
            if chips:
                st.markdown(chips, unsafe_allow_html=True)
        with col_btns:
            if st.button("📖 笔记", key=f"{key_prefix}_note_{citekey}", use_container_width=True):
                st.session_state["reading_citekey"] = citekey
                st.session_state["active_tab"] = TAB_LABELS.get(key_prefix)
                st.rerun()
            has_pdf = (PAPERS_DIR / f"{citekey}.pdf").exists()
            if st.button("📄 PDF" if has_pdf else "（无PDF）", key=f"{key_prefix}_pdf_{citekey}",
                         use_container_width=True, disabled=not has_pdf):
                st.session_state["active_tab"] = TAB_LABELS.get(key_prefix)
                open_pdf(citekey)


def restore_active_tab():
    """st.tabs() 本身不记跨rerun的"当前选中哪个标签页"状态——每次rerun前端组件都会
    重置回第一个标签页, 导致"在文献库页点开笔记"这类操作触发rerun后画面跳回总览页。
    用注入JS的办法在前端手动"点回"我们记的那个标签页, 弥补st.tabs没有这个能力的问题。
    """
    label = st.session_state.get("active_tab")
    if not label:
        return
    components.html(
        f"""
        <script>
        (function() {{
            const doc = window.parent.document;
            function trySwitch() {{
                const tabs = Array.from(doc.querySelectorAll('div[role="tab"]'));
                const target = tabs.find(t => t.textContent.includes({label!r}));
                if (target && target.getAttribute('aria-selected') !== 'true') {{
                    target.click();
                }}
            }}
            trySwitch();
            setTimeout(trySwitch, 60);
            setTimeout(trySwitch, 250);
        }})();
        </script>
        """,
        height=0,
    )


def inject_scroll_preserver():
    """点"笔记"/关闭窗格这类会触发rerun的操作后, 页面会跳回顶部(rerun导致主区域整个
    重新挂载, 浏览器默认按新DOM树的顶部显示)——尤其是滑到页面下方再点"阅读笔记"体验
    很差。用capture阶段的click监听器在任何点击发生时先把当前滚动位置存进
    sessionStorage(浏览器标签页级别, 刷新rerun不会清空), rerun完成后再把保存的位置
    滚回去, 相当于自己实现"保持滚动位置"。和 inject_outside_click_closer() 一样,
    每次这个组件重新挂载时用"先移除旧监听器再绑定新的"模式, 避免iframe销毁后监听器
    失效的坑。

    第二个坑: 如果每次传给 components.html() 的内容一字不差, Streamlit 前端会认为
    这个组件没变化, 直接复用同一个 iframe 不重新加载——那样的话里面的 restoreScroll()
    调用只在页面第一次打开时真正跑过一次, 之后每次rerun都不会再触发(iframe压根没
    重新加载), 滚动位置自然就恢复不了。解决办法很直接: 每次都塞一个不同的值(当前
    时间戳)进HTML内容里, 内容变了 Streamlit 就会认为组件"更新"了、重新加载iframe,
    里面的脚本(含 restoreScroll 的retry序列)就会跟着每次rerun都重新跑一遍。

    2026-07-23撤回: 之前一度改成"main_pane/reader_pane各自独立滚动容器"(阅读窗格
    固定高度+自己的滚动条, 且每次打开都强制回到顶部)——实际用起来体验是反的: 用户
    想要的是阅读窗格作为页面正常内容的一部分自然跟着整页滚动(不是钉死在右侧的独立
    面板), 打开笔记时也不该不由分说地跳回顶部。改回只有一个滚动目标(浏览器/Streamlit
    的主内容区 [data-testid="stMain"]), 两栏都在这同一个滚动容器里正常参与页面滚动。"""
    nonce = time.time()
    script = """
        <script>
        (function() {
            const doc = window.parent.document;
            const win = window.parent;
            // 真正滚动的不是 window, 是 Streamlit 主内容区自己的滚动容器
            // ([data-testid="stMain"], overflow-y:auto), window.scrollY 恒为0。
            function scroller() { return doc.querySelector('[data-testid="stMain"]'); }

            if (doc.__ptScrollHandler) {
                doc.removeEventListener('click', doc.__ptScrollHandler, true);
            }
            const handler = function() {
                try {
                    const el = scroller();
                    const y = el ? el.scrollTop : 0;
                    win.sessionStorage.setItem('pt_scroll_y', String(y));
                } catch (err) {}
            };
            doc.__ptScrollHandler = handler;
            doc.addEventListener('click', handler, true);

            function restoreScroll() {
                try {
                    const el = scroller();
                    const y = win.sessionStorage.getItem('pt_scroll_y');
                    if (el && y !== null) {
                        el.scrollTop = parseInt(y, 10);
                    }
                } catch (err) {}
            }
            restoreScroll();
            setTimeout(restoreScroll, 60);
            setTimeout(restoreScroll, 200);
            setTimeout(restoreScroll, 450);
        })();
        </script>
        """
    components.html(f"<!-- nonce:{nonce} -->" + script, height=0)


def inject_outside_click_closer():
    """点阅读窗格以外的空白区域也能收起窗格——原生 Streamlit 没有"点击外部关闭"这种
    交互, 用一小段注入的 JS 模拟: 监听整个页面(含父 iframe)的点击, 命中阅读窗格自己
    或任何真正的交互控件(按钮/输入框/链接/标签页等)就放行不管, 命中"空白区域"就
    程序化点一下真正的"✕ 关闭"按钮(Streamlit 按钮响应真实DOM click事件, 派发合成
    click即可触发正常的rerun, 不需要另外维护一套关闭状态逻辑)。

    踩过的坑: 每次阅读窗格重新打开时, `st.components.v1.html` 会重新生成一个新的
    iframe(旧iframe被销毁), 监听函数是在旧iframe的JS上下文里创建的——旧iframe一销毁,
    那个监听函数就失效了(哪怕它注册在父文档上)。之前用一个"只绑定一次"的标记位
    (`__ptOutsideClickBound`)防止重复绑定, 结果第一次关闭后iframe被销毁、监听器失效,
    标记位却仍然是true, 导致第二次打开时不会重新绑定、点空白区域也就没反应了。
    改成每次都先移除上一个(可能已经失效的)监听器引用, 再绑定一个新的, 保证监听器
    始终由"当前还活着"的iframe持有。"""
    components.html(
        """
        <script>
        (function() {
            const doc = window.parent.document;
            if (doc.__ptOutsideClickHandler) {
                doc.removeEventListener('click', doc.__ptOutsideClickHandler);
            }
            const handler = function(e) {
                const pane = doc.querySelector('[class*="st-key-reader_pane"]');
                if (!pane) return;
                if (pane.contains(e.target)) return;
                if (e.target.closest('button, a, input, textarea, select, label, [role="tab"], [role="option"]')) return;
                const closeBtn = Array.from(doc.querySelectorAll('button'))
                    .find(b => b.textContent.includes('关闭'));
                if (closeBtn) closeBtn.click();
            };
            doc.__ptOutsideClickHandler = handler;
            doc.addEventListener('click', handler);
        })();
        </script>
        """,
        height=0,
    )


def split_note_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """把正文拆成(开头无标题的部分, [(小节标题, 小节内容), ...])，供只读渲染和编辑
    表单共用同一份切分逻辑, 不要各写一遍容易切法不一致。"""
    parts = re.split(r"(?m)^##[ \t]+(.+)$", body)
    preamble = parts[0].strip() if parts else ""
    sections = []
    for i in range(1, len(parts), 2):
        title = parts[i].strip()
        content = parts[i + 1].strip() if i + 1 < len(parts) else ""
        sections.append((title, content))
    return preamble, sections


def save_note_edits(citekey: str, new_tags: list[str], new_sections: list[tuple[str, str]], preamble: str):
    """把编辑表单里的改动写回 notes/<citekey>.md(唯一真源), 再同步生成
    notes-readable/<citekey>.md(只读派生版, 不能手改, 但改了 notes/ 之后要重新生成)。
    只改 tags 这一行 frontmatter 和正文小节内容, 其余 frontmatter 字段原样保留。"""
    note_path = NOTES_DIR / f"{citekey}.md"
    text = note_path.read_text(encoding="utf-8")
    m = re.match(r"^(---\n.*?\n---\n?)(.*)$", text, re.DOTALL)
    if not m:
        raise ValueError("笔记文件格式不对(没有 frontmatter), 无法保存")
    header = m.group(1)
    tags_str = ", ".join(t.strip() for t in new_tags if t.strip())
    new_header, n_sub = re.subn(r"^tags:.*$", f"tags: [{tags_str}]", header, count=1, flags=re.MULTILINE)
    if n_sub == 0:
        new_header = header.rstrip("\n") + f"\ntags: [{tags_str}]\n"

    body_parts = [preamble.strip() + "\n\n"] if preamble.strip() else []
    for title, content in new_sections:
        body_parts.append(f"## {title}\n{content.strip()}\n\n")
    new_text = new_header + "".join(body_parts).rstrip() + "\n"
    note_path.write_text(new_text, encoding="utf-8", newline="\n")

    sys.path.insert(0, str(SCRIPTS))
    import batch_ingest as b  # noqa: E402
    NOTES_DIR.parent.joinpath("notes-readable").mkdir(exist_ok=True)
    readable = b.make_readable(new_text)
    (NOTES_DIR.parent / "notes-readable" / f"{citekey}.md").write_text(readable, encoding="utf-8", newline="\n")


def render_reading_pane(all_papers: dict):
    citekey = st.session_state.get("reading_citekey")
    if not citekey or citekey not in all_papers:
        return
    meta = all_papers[citekey]
    inject_outside_click_closer()
    editing = st.session_state.get("editing_citekey") == citekey
    with st.container(border=True, key="reader_pane"):
        top_l, top_r = st.columns([5, 1])
        with top_l:
            st.subheader(meta["title"])
        with top_r:
            if st.button("✕ 关闭", key="close_reading"):
                st.session_state["reading_citekey"] = None
                st.session_state["editing_citekey"] = None
                st.rerun()
        st.caption(f"{meta['journal'] or '期刊未知'} · {meta['year'] or '年份未知'} · "
                   f"{meta['authors'] or '作者未知'} · `{citekey}`")
        if meta["doi"] and meta["doi"].strip().upper() != "N/A":
            doi = meta["doi"].strip()
            st.markdown(f"🔗 [https://doi.org/{doi}](https://doi.org/{doi})")

        if not editing:
            if meta["tags"]:
                st.markdown(tag_chips_html(meta["tags"]), unsafe_allow_html=True)
            col_pdf, col_edit, col_cite = st.columns(3)
            with col_pdf:
                if (PAPERS_DIR / f"{citekey}.pdf").exists():
                    if st.button("📄 打开对应 PDF", key="reading_pdf", use_container_width=True):
                        open_pdf(citekey)
            with col_edit:
                if st.button("✏️ 编辑笔记", key="start_edit", use_container_width=True):
                    st.session_state["editing_citekey"] = citekey
                    st.rerun()
            with col_cite:
                if st.button("🔗 引用图谱", key="reading_citation", use_container_width=True):
                    open_citation_network(citekey)

            # 内嵌显示引用图谱
            if st.session_state.get("show_citation"):
                html_path = ROOT / "exports" / "citation_network.html"
                if html_path.exists():
                    if st.button("✖ 关闭图谱", key="close_citation", use_container_width=True):
                        st.session_state["show_citation"] = False
                        st.rerun()
                    html_content = html_path.read_text(encoding="utf-8")
                    components.html(html_content, height=700, scrolling=True)
                else:
                    st.session_state["show_citation"] = False

            extra = []
            if meta["类型"]:
                extra.append(f"**类型**：{meta['类型']}")
            if meta["方法关键词"]:
                extra.append(f"**方法关键词**：{meta['方法关键词']}")
            if meta["表征方法"]:
                extra.append(f"**表征方法**：{'、'.join(meta['表征方法'])}")
            if meta["体系"]:
                extra.append(f"**体系**：{meta['体系']}")
            if extra:
                st.divider()
                st.markdown("  \n".join(extra))

            st.divider()
            render_note_body(meta["body"] or "*(笔记正文为空)*")
        else:
            st.info("编辑模式：改完点下面「💾 保存」才会真正写入 notes/ 文件，"
                    "「取消」放弃这次修改。")
            tags_input = st.text_input("标签（逗号分隔）", value=", ".join(meta["tags"]),
                                        key=f"edit_tags_{citekey}")
            preamble, sections = split_note_sections(meta["body"])
            edited_sections = []
            for i, (title, content) in enumerate(sections):
                edited_content = st.text_area(title, value=content, height=140, key=f"edit_sec_{citekey}_{i}")
                edited_sections.append((title, edited_content))

            col_save, col_cancel = st.columns(2)
            with col_save:
                if st.button("💾 保存", key="save_edit", use_container_width=True):
                    new_tags = [t.strip() for t in tags_input.split(",") if t.strip()]
                    try:
                        save_note_edits(citekey, new_tags, edited_sections, preamble)
                        st.session_state["editing_citekey"] = None
                        st.success("已保存到 notes/，notes-readable/ 也同步更新了。")
                        st.rerun()
                    except Exception as e:
                        st.error(f"保存失败：{e}")
            with col_cancel:
                if st.button("取消", key="cancel_edit", use_container_width=True):
                    st.session_state["editing_citekey"] = None
                    st.rerun()


# ---------- 看门狗: 浏览器关闭 → 自动杀进程 ----------
# 原理: JS 每 5 秒向本地心跳端口发一次 POST; 浏览器关闭时(beforeunload)发关闭信号;
# Python 端 watchdog 线程持续检查心跳时间戳, 超时(15 秒无心跳)或收到关闭信号时
# os._exit(0) 杀进程 → 黑窗口(CMD)随之关闭。
_HEARTBEAT_PORT = 18501

class _Watchdog:
    _instance = None

    @classmethod
    def start(cls, timeout: int = 15):
        if cls._instance is not None:
            return
        state = {'last_hb': time.time(), 'shutdown': False}

        class _HBHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                path = urlparse(self.path).path
                if path == '/heartbeat':
                    state['last_hb'] = time.time()
                elif path == '/shutdown':
                    state['shutdown'] = True
                self.send_response(200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(b'ok')

            def log_message(self, fmt, *args):
                pass  # 静默, 不往控制台刷日志

        def _serve():
            try:
                srv = HTTPServer(('127.0.0.1', _HEARTBEAT_PORT), _HBHandler)
                srv.serve_forever()
            except OSError:
                pass  # 端口被占用(上次没杀干净), 放弃心跳

        def _watch():
            while True:
                if state['shutdown']:
                    os._exit(0)
                if time.time() - state['last_hb'] > timeout:
                    os._exit(0)
                time.sleep(1)

        threading.Thread(target=_serve, daemon=True).start()
        threading.Thread(target=_watch, daemon=True).start()
        cls._instance = state


ALL_PAPERS = load_all_papers(notes_dir_fingerprint())
inject_scroll_preserver()

# 从引用图谱右侧“回到笔记”按钮跳回来时, URL 带 ?citekey=XXX, 自动打开对应笔记
_qp = st.query_params
if "citekey" in _qp and not st.session_state.get("reading_citekey"):
    _ck = _qp["citekey"]
    if _ck in ALL_PAPERS:
        st.session_state["reading_citekey"] = _ck
        st.session_state["active_tab"] = TAB_LABELS.get("lib")
    # 清除 query param 避免刷新重复触发
    del _qp["citekey"]

# 启动看门狗(只在首次加载时启动, Streamlit rerun 不会重复启动——_instance 检查)
# 超时 60 秒: 打开引用图谱等外部页面时, 后台标签 JS 可能被浏览器节流, 心跳间隔拉长
_Watchdog.start(timeout=60)
components.html(
    """
    <script>
    (function() {
        var BASE = 'http://127.0.0.1:18501';
        function ping(path) {
            try {
                navigator.sendBeacon(BASE + '/' + path, '');
            } catch(e) {
                try { fetch(BASE + '/' + path, {method:'POST', mode:'no-cors'}).catch(function(){}); } catch(e2) {}
            }
        }
        if (!window.__ptWatchdog) {
            window.__ptWatchdog = true;
            setInterval(function(){ ping('heartbeat'); }, 5000);
            window.addEventListener('beforeunload', function(){ ping('shutdown'); });
        }
        ping('heartbeat');
    })();
    </script>
    """,
    height=1,
)

# 顶层布局: 有笔记在阅读时, 右侧留一栏当"阅读窗格"(所有标签页共用同一个阅读窗格,
# 不管从总览的最近新增点进去还是从文献库浏览页点进去, 效果一样)。两栏都是页面正常
# 内容流的一部分, 共用同一个滚动容器(见 inject_scroll_preserver), 不是各自独立
# 固定高度的面板。
if st.session_state.get("reading_citekey"):
    main_col, reader_col = st.columns([3, 2])
else:
    main_col = st.container()
    reader_col = None

with main_col:
    st.title("📘 PaperTrail")
    st.caption("Designed & built by **Eggy**, powered by Claude & Qoder")

    tab_overview, tab_library, tab_check, tab_cite, tab_scan, tab_word, tab_cmd = st.tabs(
        ["📊 总览", "📚 文献库", "🩺 体检", "📝 引文献", "🔎 扩充/查新", "📄 Word 插入引用", "⚙️ 命令"])
    restore_active_tab()

    # ---------- 总览 ----------
    with tab_overview:
        # 上一轮"入库"跑完后会 st.rerun() 来让 ALL_PAPERS 用新的 cache_bust 重新扫描
        # notes/(否则"最近新增"会一直显示入库前的旧数据), 但 rerun 本身会清空那次运行里
        # 已经 st.success()/st.code() 过的内容——所以入库结果先存进 session_state, 这里
        # (新一轮渲染的最上方)取出来展示一次再清掉, 用户才能看到"入库成功/新增了什么"
        # 这条反馈, 而不是页面刷新后什么提示都没有、只能靠自己对比列表猜有没有变化。
        last_result = st.session_state.pop("last_ingest_result", None)
        if last_result:
            if last_result["ok"]:
                st.success(last_result["msg"])
            else:
                st.error(last_result["msg"])
            if last_result.get("log"):
                with st.expander("查看本次入库输出日志", expanded=False):
                    st.code(last_result["log"], language="text")

        st.caption(f"当前识别的文献库目录：`{ROOT}`")
        index_file = ROOT / "INDEX.md"
        if not ALL_PAPERS:
            st.warning(f"在这个目录下没找到 notes/ 或者 notes/ 是空的，所以下面文献数显示 0——"
                       f"不是库真的是空的，是这个面板没找对文件夹。检查一下：`PaperTrail Launcher.bat` "
                       f"是不是直接放在文献库根目录（和 AGENTS.md/notes/ 同一层）下双击的？")
        col1, col2, col3 = st.columns(3)
        inbox_dir = ROOT / "inbox"
        n_inbox = len([p for p in inbox_dir.glob("*.pdf")]) if inbox_dir.exists() else 0
        keywords_file = ROOT / "KEYWORDS.md"
        n_keywords = keywords_file.read_text(encoding="utf-8").count("\n- ") if keywords_file.exists() else 0

        col1.metric("库内文献数", len(ALL_PAPERS))
        col2.metric("inbox 待处理 PDF", n_inbox)
        col3.metric("关键词索引条目（估算）", n_keywords)

        st.subheader("拖拽PDF到这里入库")
        col_drop, col_go, col_clear = st.columns([4, 1, 1])
        with col_drop:
            uploaded = st.file_uploader("拖拽或选择 PDF 文件（可多选）", type=["pdf"],
                                         accept_multiple_files=True, label_visibility="collapsed")
        with col_go:
            st.write("")
            run_ingest = st.button("📥 入库", use_container_width=True,
                                    help="对 inbox/ 里当前所有 PDF 跑无人值守批量入库(scripts/batch_ingest.py)")
        with col_clear:
            st.write("")
            run_clear_inbox = st.button("🗑️ 清理inbox", use_container_width=True,
                                         help="删除 inbox/ 里当前所有文件（含 .meta_cache 缓存），不可撤销")
        if run_clear_inbox:
            st.session_state["active_tab"] = TAB_LABELS["recent"]
            if n_inbox == 0 and not (inbox_dir.exists() and any(inbox_dir.iterdir())):
                st.warning("inbox/ 已经是空的。")
            else:
                removed = []
                for p in sorted(inbox_dir.glob("*")):
                    if p.is_file():
                        p.unlink()
                        removed.append(p.name)
                    elif p.is_dir():
                        shutil.rmtree(p)
                        removed.append(p.name + "/")
                st.success(f"已清空 inbox/，删除了 {len(removed)} 项：{', '.join(removed) if removed else '(无)'}")
                st.rerun()
        if uploaded:
            inbox_dir.mkdir(parents=True, exist_ok=True)
            saved = []
            for f in uploaded:
                dest = inbox_dir / f.name
                dest.write_bytes(f.getbuffer())
                saved.append(f.name)
            st.success(f"已存入 inbox/：{', '.join(saved)}。点右边"
                       f"「📥 入库」按钮直接批量处理，或者回 Agent CLI 对话里说"
                       f"\"入库\"（更适合需要人工核实查重/DOI/SI配对的情况）。")
        def finish_ingest(before_pdfs, stopped=False):
            """入库子进程跑完(或被停止)后的收尾: 清理 inbox 里已处理的文件、汇总结果、
            触发语义索引增量更新。"""
            state_path = SCRIPTS / ".ingest_state.json"
            cleaned, new_citekeys, errored = [], [], []
            if state_path.exists():
                try:
                    ingest_state = json.loads(state_path.read_text(encoding="utf-8"))
                except Exception:
                    ingest_state = {}
                for name in before_pdfs:
                    pdf_path = inbox_dir / name
                    if not pdf_path.exists():
                        continue
                    for key_variant in (str(pdf_path), f"inbox\\{name}", f"inbox/{name}"):
                        entry = ingest_state.get(key_variant)
                        if not entry:
                            continue
                        status = entry.get("status")
                        if status in ("done", "skipped_duplicate"):
                            if status == "done" and entry.get("citekey"):
                                new_citekeys.append(entry["citekey"])
                            pdf_path.unlink()
                            cleaned.append(name)
                        elif status == "error":
                            err_msg = entry.get("error", "未知错误")
                            errored.append(f"{name}: {err_msg}")
                        break
            log_text = "\n".join(_BG_RUNS.get(st.session_state.get("_ingest_run_id", ""), {}).get("lines", []))
            if stopped:
                msg = "入库被手动停止。"
                if new_citekeys:
                    msg += f" 已部分入库：{', '.join(new_citekeys)}。"
                has_error = True
            elif errored:
                msg = f"入库有 {len(errored)} 篇失败：" + "；".join(errored)
                if new_citekeys:
                    msg += f" 成功入库：{', '.join(new_citekeys)}。"
                has_error = True
            else:
                has_error = False
                if cleaned:
                    msg = f"入库完成，已从 inbox/ 清掉 {len(cleaned)} 篇。"
                    if new_citekeys:
                        msg += f" 新增：{', '.join(new_citekeys)}。"
                    else:
                        msg += "（均为已入库的重复文献，去重行为，不是 bug。）"
                else:
                    msg = "入库跑完了（inbox/ 没有可清理的文件，看日志确认原因）。"
            st.session_state["last_ingest_result"] = {"ok": not has_error, "msg": msg, "log": log_text}
            load_all_papers.clear()
            # 有新入库的论文 → 静默触发语义索引增量更新(后台, 不阻塞UI)
            if new_citekeys and (EMBEDDINGS_DIR / "index.faiss").exists():
                start_bg_process([sys.executable, str(SCRIPTS / "build_embedding_index.py")])
            st.rerun()

        def start_ingest():
            """启动后台入库进程(batch_ingest.py), 进度由 render_bg_progress 实时展示。"""
            before_pdfs = sorted(p.name for p in inbox_dir.glob("*.pdf"))
            st.session_state["_ingest_before_pdfs"] = before_pdfs
            st.session_state["_ingest_run_id"] = start_bg_process(
                [sys.executable, str(SCRIPTS / "batch_ingest.py"),
                 "--source", str(inbox_dir), "--until-done"])

        if run_ingest:
            st.session_state["active_tab"] = TAB_LABELS["recent"]
            # 这里不能直接在本次运行里"判断检测到agent就弹按钮、点了按钮再跑ingest"——
            # 点"仍要在网页直接全自动入库"按钮会触发新一轮rerun, 而那一轮里"📥 入库"
            # 按钮本身返回False(这次不是它被点的), 如果只靠run_ingest这个局部变量,
            # 确认按钮所在的整个分支根本不会被重新渲染/判断, 点了等于没反应。改用
            # session_state记住"用户点过入库、正等待确认"这件事, 让它跨rerun持续存在。
            st.session_state["ingest_pending"] = True

        if st.session_state.get("ingest_pending"):
            if n_inbox == 0:
                st.warning("inbox/ 里没有 PDF，先拖几篇进来。")
                st.session_state["ingest_pending"] = False
            else:
                # 检测本机有没有 Claude Code/Codex/Kimi 这类 Agent CLI 在跑着——如果有,
                # 说明用户大概率有一个对话窗口可以做查重/DOI核实/SI配对这些需要判断的活,
                # 网页这边不该抢着直接跑纯API全自动批处理(那样就绕开了人工审核这一步,
                # 违背了"入库不适合塞进无对话能力的网页表单"这条设计原则); 只有确实检测不到
                # Agent CLI 在跑, 才直接用 batch_ingest.py 全自动走 API 入库。
                detected = detect_running_agent_cli()
                if detected:
                    st.session_state["active_tab"] = TAB_LABELS["recent"]
                    st.warning(
                        f"检测到本机有 Agent CLI 在运行（命令行含 \"{detected}\"）。建议回到那个"
                        f"对话窗口说\"入库\"——查重、DOI 核实、SI 配对这些需要判断的活交给对话做"
                        f"更可靠。如果你确定就是要在这里直接跑纯 API 全自动入库（不经人工审核），"
                        f"点下面的按钮确认。"
                    )
                    col_confirm, col_cancel = st.columns(2)
                    with col_confirm:
                        if st.button("仍要在网页直接全自动入库", key="force_api_ingest",
                                      use_container_width=True):
                            st.session_state["ingest_pending"] = False
                            st.session_state["active_tab"] = TAB_LABELS["recent"]
                            start_ingest()
                    with col_cancel:
                        if st.button("取消", key="cancel_api_ingest", use_container_width=True):
                            st.session_state["ingest_pending"] = False
                            st.session_state["active_tab"] = TAB_LABELS["recent"]
                            st.rerun()
                else:
                    st.session_state["ingest_pending"] = False
                    start_ingest()

        # ---- 入库进度(后台进程实时展示) ----
        if st.session_state.get("_ingest_run_id"):
            st.session_state["active_tab"] = TAB_LABELS["recent"]
            _irun = st.session_state["_ingest_run_id"]
            finished = render_bg_progress(_irun, step_pattern=r"[完成失败]\s*(\d+)/(\d+)")
            if finished:
                _state = _BG_RUNS.pop(_irun, {})
                _before = st.session_state.pop("_ingest_before_pdfs", [])
                del st.session_state["_ingest_run_id"]
                finish_ingest(_before, stopped=_state.get("stopped", False))

        if st.button("🔄 刷新库列表", help="notes_dir_fingerprint() 现在每次都会自动感知 notes/ 变化, "
                                          "这个按钮主要是给你手动触发一次rerun用(比如刚在别处改完文件想立刻看到)"):
            st.session_state["active_tab"] = TAB_LABELS["recent"]
            load_all_papers.clear()
            st.rerun()

        st.divider()
        st.subheader("📈 库统计")
        if ALL_PAPERS:
            import pandas as pd
            import altair as alt
            _ck1, _ck2, _ck3 = st.columns(3)
            with _ck1:
                st.markdown("**按年份**")
                _yd = pd.Series([m["year"] or "?" for m in ALL_PAPERS.values()]).value_counts().sort_index()
                _ydf = _yd.iloc[::-1].reset_index()
                _ydf.columns = ["year", "count"]
                st.altair_chart(alt.Chart(_ydf).mark_bar().encode(
                    y=alt.Y("year:N", sort=None), x="count:Q"
                ).properties(height=300), use_container_width=True)
            with _ck2:
                st.markdown("**Top 10 期刊**")
                _jabbr = [ck.split("-")[1] for ck in ALL_PAPERS if "-" in ck]
                _jd = pd.Series(_jabbr).value_counts().head(10)
                _jdf = _jd.reset_index()
                _jdf.columns = ["journal", "count"]
                st.altair_chart(alt.Chart(_jdf).mark_bar().encode(
                    y=alt.Y("journal:N", sort=None), x="count:Q"
                ).properties(height=300), use_container_width=True)
            with _ck3:
                st.markdown("**Top 10 标签**")
                _td = pd.Series(
                    [t for m in ALL_PAPERS.values() for t in m["tags"]]
                ).value_counts().head(10)
                _tdf = _td.reset_index()
                _tdf.columns = ["tag", "count"]
                st.altair_chart(alt.Chart(_tdf).mark_bar().encode(
                    y=alt.Y("tag:N", sort=None), x="count:Q"
                ).properties(height=300), use_container_width=True)

        st.divider()
        st.subheader("最近新增")
        recent = sorted(ALL_PAPERS.items(), key=lambda kv: (kv[1]["added"], kv[1].get("_mtime", 0)), reverse=True)[:5]
        for citekey, meta in recent:
            render_paper_card(citekey, meta, key_prefix="recent")

    # ---------- 文献库浏览 ----------
    with tab_library:
        st.subheader("浏览全部文献")
        if not ALL_PAPERS:
            st.info("还没有笔记数据。")
        else:
            all_tags = sorted({t for meta in ALL_PAPERS.values() for t in meta["tags"]})
            all_journals = sorted({meta["journal"] for meta in ALL_PAPERS.values() if meta["journal"]})
            top_journals_file = SCRIPTS / "top_journals.txt"
            top_journals_set = set()
            if top_journals_file.exists():
                for line in top_journals_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        top_journals_set.add(line)

            def _stay_on_library_tab():
                st.session_state["active_tab"] = TAB_LABELS["lib"]

            # st.expander 默认不记跨rerun的展开状态——点"语义检索"按钮触发的rerun会让
            # 这个expander弹回初始的折叠状态, 把刚算出来的结果和输入框一起隐藏, 用户会
            # 以为"点了没反应"。用session_state记住"这次rerun应该展开"这件事, 在有
            # 检索结果/构建任务在跑的时候强制展开。
            semantic_expanded = bool(
                st.session_state.get("semantic_results") is not None
                or st.session_state.get("semantic_build_run_id")
                or st.session_state.get("semantic_expander_open")
            )
            with st.expander("🧠 语义检索（模糊描述你的研究问题，不要求关键词命中）", expanded=semantic_expanded):
                index_status_path = EMBEDDINGS_DIR / "metadata.json"
                if index_status_path.exists():
                    try:
                        _idx_meta = json.loads(index_status_path.read_text(encoding="utf-8"))
                        st.caption(f"索引已建：{len(_idx_meta.get('citekeys', []))} 篇，"
                                   f"构建于 {_idx_meta.get('built_at', '?')}（模型: {_idx_meta.get('model', '?')}）")
                    except Exception:
                        st.caption("索引文件存在但读取失败，建议重新构建。")
                else:
                    st.caption("还没建过语义索引，先点下面「构建/更新语义索引」（首次会下载embedding模型，需要联网）。")

                if st.button("🔄 构建/更新语义索引", key="build_semantic_index",
                             help="新增/修改笔记后重新点一下做增量更新；只有变化的笔记会重新生成embedding"):
                    st.session_state["active_tab"] = TAB_LABELS["lib"]
                    st.session_state["semantic_expander_open"] = True
                    st.session_state["semantic_build_run_id"] = start_bg_process(
                        [sys.executable, str(SCRIPTS / "build_embedding_index.py")])

                if st.session_state.get("semantic_build_run_id"):
                    finished = render_bg_progress(st.session_state["semantic_build_run_id"])
                    if finished:
                        del st.session_state["semantic_build_run_id"]
                        st.rerun()

                sq1, sq2, sq3 = st.columns([3, 1, 1.6])
                with sq1:
                    semantic_query = st.text_input(
                        "检索问题", placeholder="如：酸性PEM电解槽中Ir催化剂降解机制",
                        key="semantic_query_input")
                with sq2:
                    semantic_topk = st.number_input("返回篇数", min_value=1, max_value=50,
                                                     value=10, key="semantic_topk")
                with sq3:
                    semantic_explain = st.checkbox(
                        "生成相关性说明（较慢，调用deepseek）", key="semantic_explain")

                if st.button("🔍 语义检索", key="run_semantic_search"):
                    st.session_state["active_tab"] = TAB_LABELS["lib"]
                    st.session_state["semantic_expander_open"] = True
                    if not semantic_query.strip():
                        st.warning("先输入一句检索问题。")
                    elif not (EMBEDDINGS_DIR / "index.faiss").exists():
                        st.warning("还没建过语义索引，先点上面「构建/更新语义索引」。")
                    else:
                        with st.spinner("检索中…" + ("（加了相关性说明，会慢一些）" if semantic_explain else "")):
                            st.session_state["semantic_results"] = run_semantic_query(
                                semantic_query.strip(), int(semantic_topk), semantic_explain)

                if st.session_state.get("semantic_results") is not None:
                    results = st.session_state["semantic_results"]
                    col_res_title, col_res_clear = st.columns([4, 1])
                    with col_res_title:
                        st.markdown(f"**语义检索结果（共 {len(results)} 条）**")
                    with col_res_clear:
                        if st.button("清除结果", key="clear_semantic_results"):
                            st.session_state["active_tab"] = TAB_LABELS["lib"]
                            st.session_state["semantic_results"] = None
                            st.session_state["semantic_expander_open"] = False
                            st.rerun()
                    for r in results:
                        ck = r["citekey"]
                        caption = f"相似度 {r['similarity']:.3f}"
                        if r.get("reason"):
                            caption += f" — {r['reason']}"
                        st.caption(caption)
                        if ck in ALL_PAPERS:
                            render_paper_card(ck, ALL_PAPERS[ck], key_prefix="semantic")
                        else:
                            st.warning(f"`{ck}` 在索引里但 notes/ 里找不到对应笔记，索引可能过期，"
                                       f"建议重新构建。")

            f1, f2, f_ft = st.columns([2, 2.5, 2.5])
            with f1:
                keyword_search = st.text_input("搜标题/作者/关键词", placeholder="输入标题、作者或关键词过滤",
                                                on_change=_stay_on_library_tab)
            with f2:
                tag_filter = st.multiselect("按标签筛选", all_tags, on_change=_stay_on_library_tab)
            with f_ft:
                fulltext_query = st.text_input("🔍 搜正文", placeholder="搜笔记正文内容（AND 模式）",
                                               on_change=_stay_on_library_tab, key="ft_search")

            f3, f4, f5 = st.columns([3, 1.4, 2])
            with f3:
                journal_filter = st.multiselect("按期刊筛选", all_journals, on_change=_stay_on_library_tab)
            with f4:
                only_top = st.checkbox("只看 Top Journals", on_change=_stay_on_library_tab)
            with f5:
                sort_key = st.selectbox("排序方式", ["添加时间", "年份", "期刊"], on_change=_stay_on_library_tab)
                sort_desc = st.checkbox("降序", value=True, on_change=_stay_on_library_tab, key="sort_desc")

            filtered = []
            for citekey, meta in ALL_PAPERS.items():
                if tag_filter and not set(tag_filter) & set(meta["tags"]):
                    continue
                if journal_filter and meta["journal"] not in journal_filter:
                    continue
                if only_top and meta["journal"] not in top_journals_set:
                    continue
                if keyword_search:
                    haystack = (meta["title"] + " " + meta["authors"] + " " +
                                " ".join(meta["keywords"])).lower()
                    if keyword_search.lower() not in haystack:
                        continue
                if fulltext_query:
                    # 简单全文搜索: 在笔记正文里查所有关键词(AND 模式)
                    body = (meta.get("body") or "").lower()
                    terms = fulltext_query.lower().split()
                    if not all(t in body for t in terms):
                        continue
                filtered.append((citekey, meta))

            sort_field_map = {
                "添加时间": lambda kv: kv[1]["added"],
                "年份": lambda kv: kv[1]["year"],
                "期刊": lambda kv: kv[1]["journal"],
            }
            filtered.sort(key=sort_field_map[sort_key], reverse=sort_desc)

            st.caption(f"共 {len(filtered)} 篇符合条件（库内总计 {len(ALL_PAPERS)} 篇）")

            PAGE_SIZE = 50
            n_pages = max(1, (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = st.number_input("页码", min_value=1, max_value=n_pages, value=1, step=1,
                                    on_change=_stay_on_library_tab)
            page_items = filtered[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

            for citekey, meta in page_items:
                render_paper_card(citekey, meta, key_prefix="lib")

    # ---------- 体检 ----------
    with tab_check:
        st.subheader("三方对账 + 全库标题查重")
        st.write("跑 `scripts/check_library.sh`（需要 Git Bash 在 PATH 里）。")
        verbose = st.checkbox("显示全部问题（--verbose，默认只列前 5 条）")
        if st.button("运行体检", key="run_check"):
            st.session_state["active_tab"] = TAB_LABELS["check"]
            with st.spinner("体检中，视库大小可能要几十秒..."):
                cmd = [GIT_BASH, str(SCRIPTS / "check_library.sh")] + (["--verbose"] if verbose else [])
                code, out, err = run(cmd)
            if code == 0:
                st.success("跑完了")
            else:
                st.warning(f"退出码 {code}（体检脚本发现问题时退出码非 0 是正常的，看下面输出）")
            st.code(out or "(无输出)", language="text")
            if err:
                st.code(err, language="text")

    # ---------- 引文献 ----------
    with tab_cite:
        st.subheader("贴一段论文正文，逐句生成可引用的文献清单")
        paragraph = st.text_area("论文正文（英文/中文，一整段）", height=180,
                                  placeholder="Iridium-based catalysts have been widely studied for...")
        c1, c2 = st.columns(2)
        with c1:
            top_journals = st.checkbox("只从 top journals 里筛候选")
        with c2:
            tags_input = st.text_input("标签过滤（逗号分隔，可留空）")
        if st.session_state.get("cite_run_id") is None:
            if st.button("生成引用清单", key="run_cite"):
                st.session_state["active_tab"] = TAB_LABELS["cite"]
                if not paragraph.strip():
                    st.error("先贴一段正文")
                else:
                    tmp_dir = Path(tempfile.mkdtemp(prefix="pt_cite_"))
                    out_path = tmp_dir / "citations.md"
                    cmd = [sys.executable, str(SCRIPTS / "find_citations.py"),
                           "--paragraph", paragraph, "--out", str(out_path), "--draft"]
                    if top_journals:
                        cmd.append("--top-journals")
                    if tags_input.strip():
                        cmd += ["--tags", tags_input.strip()]
                    st.session_state["cite_out_path"] = str(out_path)
                    st.session_state["cite_run_id"] = start_bg_process(cmd)
                    st.rerun()
        else:
            st.caption("拆句 → 逐条论点检索候选 → 调 LLM 判断 support/contradict/unclear 中……")
            finished = render_bg_progress(st.session_state["cite_run_id"])
            if finished:
                out_path = Path(st.session_state["cite_out_path"])
                if out_path.exists():
                    st.markdown(out_path.read_text(encoding="utf-8"))
                else:
                    st.info("没有生成输出文件（可能是被手动停止了）。")
                if st.button("清除结果，重新开始", key="reset_cite"):
                    st.session_state["active_tab"] = TAB_LABELS["cite"]
                    _BG_RUNS.pop(st.session_state["cite_run_id"], None)
                    st.session_state["cite_run_id"] = None
                    st.rerun()

    # ---------- 扩充/查新 ----------
    with tab_scan:
        engine_choice = st.radio(
            "搜索引擎", ["OpenAlex 系统检索", "引用图谱扩展 (Semantic Scholar)", "WebSearch 结果解析"],
            horizontal=True, help="OpenAlex: 按期刊+关键词穷举全部匹配论文; 引用图谱: 从库内已有论文DOI出发查引用/被引; WebSearch: 解析粘贴的搜索结果文字")

        # 停止标志：点击停止按钮后触发 rerun，这里检查并显示提示
        if st.session_state.get("_oa_stop_requested"):
            st.session_state["_oa_stop_requested"] = False
            st.warning("⚠️ 上次搜索已被手动停止")

        if engine_choice == "OpenAlex 系统检索":
            st.subheader("OpenAlex 系统性检索（推荐）")
            st.caption("按期刊+日期范围+关键词 cursor 遍历全部匹配论文，不受 ~10 条/次的限制")
            st.info("💡 **支持中文关键词**：中文会自动翻译成英文关键词组。论文必须匹配多个关键词才保留（相关性过滤），结果按匹配度排序。")
            oa_keywords = st.text_input("关键词（支持中英文，逗号分隔）",
                                         placeholder="PEM Ir基催化剂 或 oxygen evolution,iridium,PEM,acidic OER")
            col_oa1, col_oa2 = st.columns(2)
            with col_oa1:
                oa_from = st.text_input("起始日期", placeholder="2021-01-01", key="oa_from")
            with col_oa2:
                oa_to = st.text_input("截止日期（留空=今天）", placeholder="", key="oa_to")
            col_oa3, col_oa4 = st.columns(2)
            with col_oa3:
                oa_top = st.checkbox("使用 top journals 白名单", value=True, key="oa_tj")
            with col_oa4:
                oa_min_match = st.number_input("最少匹配关键词组数（0=自动）", min_value=0, max_value=10, value=0,
                                                key="oa_mm", help="0=自动（有关键词时默认2），论文至少匹配几个关键词组才保留")
            oa_custom = st.text_input("自定义期刊（逗号分隔，不勾白名单时用）", key="oa_custom")
            oa_out_name = st.text_input("输出文件名（不含路径）", placeholder="PEM催化剂_openalex", key="oa_out")
            col_btn1, col_btn2 = st.columns([3, 1])
            with col_btn1:
                _oa_start = st.button("🔍 OpenAlex 检索", key="run_oa_search")
            with col_btn2:
                if st.button("⬛ 停止搜索", key="stop_oa_search"):
                    st.session_state["_oa_stop_requested"] = True
                    st.rerun()
            if _oa_start:
                st.session_state["active_tab"] = TAB_LABELS["scan"]
                st.session_state["_oa_stop_requested"] = False
                if not oa_keywords.strip():
                    st.error("先填关键词")
                else:
                    import tempfile as _tf
                    with _tf.TemporaryDirectory() as tmp:
                        cand_path = Path(tmp) / "candidates.json"
                        cmd = [sys.executable, str(SCRIPTS / "academic_search.py"),
                               "--keywords", oa_keywords.strip(), "--out", str(cand_path)]
                        if oa_top:
                            cmd.append("--top-journals")
                            n_journals = 11  # top_journals.txt 白名单
                        elif oa_custom.strip():
                            cmd += ["--journals", oa_custom.strip()]
                            n_journals = len([j for j in oa_custom.split(",") if j.strip()])
                        else:
                            n_journals = 1
                        if oa_from.strip():
                            cmd += ["--from-date", oa_from.strip()]
                        if oa_to.strip():
                            cmd += ["--to-date", oa_to.strip()]
                        if oa_min_match > 0:
                            cmd += ["--min-match", str(oa_min_match)]

                        # 估算进度条总步数：期刊数 × 关键词组数
                        n_kw = len([k for k in oa_keywords.split(",") if k.strip()])
                        # 中文关键词会被 DeepSeek 翻译成 ~5 个英文关键词
                        if any('\u4e00' <= c <= '\u9fff' for c in oa_keywords):
                            n_kw = max(n_kw, 5)
                        progress_total = n_journals * max(n_kw, 1)

                        # 第一阶段: OpenAlex 检索（实时流式输出）
                        st.markdown("#### ① OpenAlex 检索")
                        code, out = run_streaming(
                            cmd, timeout=600,
                            progress_total=progress_total,
                            progress_label="搜索进度",
                            stop_flag="_oa_stop_requested")

                        # 如果用户点了停止，rerun 会打断执行，这里不会跑到
                        # 但以防万一检查 flag
                        if st.session_state.get("_oa_stop_requested"):
                            st.warning("搜索已停止")
                            st.stop()

                        if cand_path.exists():
                            cand_data = json.loads(cand_path.read_text(encoding="utf-8"))
                            st.success(f"OpenAlex 返回 {len(cand_data)} 条候选")

                            # 第二阶段: Crossref 核验（也流式输出）
                            st.markdown("#### ② Crossref 核验 + 去重")
                            out_xlsx = EXPORTS / f"{oa_out_name.strip() or 'openalex_results'}.xlsx"
                            code2, out2 = run_streaming(
                                [sys.executable, str(SCRIPTS / "scan_new_papers.py"),
                                 "--candidates", str(cand_path), "--out", str(out_xlsx)],
                                timeout=600,
                                progress_total=len(cand_data),
                                progress_label="核验进度",
                                stop_flag="_oa_stop_requested")

                            if st.session_state.get("_oa_stop_requested"):
                                st.warning("核验已停止")
                                st.stop()

                            summary_path = out_xlsx.with_suffix(".summary.txt")
                            if summary_path.exists():
                                st.markdown("### 结果摘要")
                                st.code(summary_path.read_text(encoding="utf-8"), language="markdown")
                            if out_xlsx.exists():
                                st.download_button("下载完整 xlsx", data=out_xlsx.read_bytes(),
                                                    file_name=out_xlsx.name)
                        else:
                            st.warning("未生成候选文件，可能是网络错误或关键词无匹配")

        elif engine_choice == "引用图谱扩展 (Semantic Scholar)":
            st.subheader("引用图谱扩展")
            st.caption("从库内已有高价值论文的 DOI 出发，查谁引了它/它引了谁")
            cg_from = st.text_input("只保留此年份之后的论文", placeholder="2023", key="cg_from")
            cg_max = st.number_input("最多查几篇库内论文", min_value=10, max_value=200, value=100, key="cg_max")
            cg_out_name = st.text_input("输出文件名（不含路径）", placeholder="PEM催化剂_citation_graph", key="cg_out")
            col_cg1, col_cg2 = st.columns([3, 1])
            with col_cg1:
                _cg_start = st.button("🔗 引用图谱扩展", key="run_cg_search")
            with col_cg2:
                if st.button("⬛ 停止搜索", key="stop_cg_search"):
                    st.session_state["_oa_stop_requested"] = True
                    st.rerun()
            if _cg_start:
                st.session_state["active_tab"] = TAB_LABELS["scan"]
                st.session_state["_oa_stop_requested"] = False
                import tempfile as _tf
                with _tf.TemporaryDirectory() as tmp:
                    cand_path = Path(tmp) / "candidates.json"
                    cmd = [sys.executable, str(SCRIPTS / "academic_search.py"),
                           "--citation-graph", "--out", str(cand_path),
                           "--citation-max-papers", str(cg_max)]
                    if cg_from.strip():
                        cmd += ["--from-date", f"{cg_from.strip()}-01-01"]

                    st.markdown("#### ① Semantic Scholar 引用图谱")
                    code, out = run_streaming(
                        cmd, timeout=900,
                        progress_total=cg_max,
                        progress_label="处理论文",
                        stop_flag="_oa_stop_requested")

                    if cand_path.exists():
                        cand_data = json.loads(cand_path.read_text(encoding="utf-8"))
                        st.success(f"Semantic Scholar 返回 {len(cand_data)} 条候选")

                        st.markdown("#### ② Crossref 核验 + 去重")
                        out_xlsx = EXPORTS / f"{cg_out_name.strip() or 'citation_results'}.xlsx"
                        code2, out2 = run_streaming(
                            [sys.executable, str(SCRIPTS / "scan_new_papers.py"),
                             "--candidates", str(cand_path), "--out", str(out_xlsx)],
                            timeout=600,
                            progress_total=len(cand_data),
                            progress_label="核验进度",
                            stop_flag="_oa_stop_requested")

                        summary_path = out_xlsx.with_suffix(".summary.txt")
                        if summary_path.exists():
                            st.markdown("### 结果摘要")
                            st.code(summary_path.read_text(encoding="utf-8"), language="markdown")
                        if out_xlsx.exists():
                            st.download_button("下载完整 xlsx", data=out_xlsx.read_bytes(),
                                                file_name=out_xlsx.name)

        else:  # WebSearch
            st.subheader("从一段搜索结果原始文字里识别候选论文并核验")
            raw_text = st.text_area("WebSearch 原始结果文字（一次或多次搜索拼在一起）", height=180)
            context = st.text_input("这批搜索的目标（帮助模型判断相关性）",
                                     placeholder="某材料体系, 2021-2026, top journals")
            if st.button("识别候选并核验", key="run_scan"):
                st.session_state["active_tab"] = TAB_LABELS["scan"]
                if not raw_text.strip():
                    st.error("先贴一段搜索结果文字")
                else:
                    with tempfile.TemporaryDirectory() as tmp:
                        raw_path = Path(tmp) / "raw.txt"
                        cand_path = Path(tmp) / "candidates.json"
                        out_path = Path(tmp) / "results.xlsx"
                        raw_path.write_text(raw_text, encoding="utf-8")
                        with st.spinner("LLM 抽取候选中..."):
                            code1, out1, err1 = run([sys.executable, str(SCRIPTS / "parse_search_results.py"),
                                                      "--raw-file", str(raw_path), "--context", context,
                                                      "--out", str(cand_path)], timeout=300)
                        st.code(out1 + err1, language="text")
                        if cand_path.exists():
                            with st.spinner("Crossref 核验 + 去重 + 分类中..."):
                                code2, out2, err2 = run([sys.executable, str(SCRIPTS / "scan_new_papers.py"),
                                                          "--candidates", str(cand_path), "--out", str(out_path)],
                                                         timeout=300)
                            st.code(out2 + err2, language="text")
                            summary_path = out_path.with_suffix(".summary.txt")
                            if summary_path.exists():
                                st.markdown("### 结果摘要")
                                st.code(summary_path.read_text(encoding="utf-8"), language="markdown")
                            if out_path.exists():
                                st.download_button("下载完整 xlsx", data=out_path.read_bytes(),
                                                    file_name="scan_results.xlsx")

    # ---------- Word 插入引用 ----------
    with tab_word:
        st.subheader("在已打开的 Word 文档光标处插入引用")
        st.info("仅 Windows + 已装 Word；目标文档必须**已经在 Word 里打开着**且光标停在目标位置，"
                "跑之前不要再点别的地方。")
        citekeys = st.text_input("citekey（逗号分隔，多篇一起插）")
        doc_name = st.text_input("文档名片段（Word 里同时开多个文档时用来指定，只开一个可留空）")
        style = st.selectbox("参考文献格式", ["numbered", "nature", "wiley", "gbt7714"])
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("插入引用", key="run_word_insert"):
                st.session_state["active_tab"] = TAB_LABELS["word"]
                if not citekeys.strip():
                    st.error("先填 citekey")
                else:
                    cmd = [sys.executable, str(SCRIPTS / "word_insert_citation.py"),
                           "--citekeys", citekeys.strip(), "--style", style]
                    if doc_name.strip():
                        cmd += ["--doc", doc_name.strip()]
                    code, out, err = run(cmd, timeout=60)
                    st.code(out + err, language="text")
        with col_b:
            if st.button("重新编号（--rebuild）", key="run_word_rebuild"):
                st.session_state["active_tab"] = TAB_LABELS["word"]
                cmd = [sys.executable, str(SCRIPTS / "word_insert_citation.py"), "--rebuild"]
                if doc_name.strip():
                    cmd += ["--doc", doc_name.strip()]
                code, out, err = run(cmd, timeout=60)
                st.code(out + err, language="text")

        st.divider()
        st.subheader("自动扫描全文并添加引用")
        doc_name2 = st.text_input("文档名片段（同上）", key="doc2")
        style2 = st.selectbox("参考文献格式 ", ["numbered", "nature", "wiley", "gbt7714"], key="style2")
        top_journals2 = st.checkbox("只从 top journals 里筛候选", key="tj2")
        apply_write = st.checkbox("确认后写入文档（不勾选只预览，不改文档）")
        if st.button("扫描并" + ("插入" if apply_write else "预览"), key="run_auto_cite"):
            st.session_state["active_tab"] = TAB_LABELS["word"]
            cmd = [sys.executable, str(SCRIPTS / "word_auto_cite.py"), "--style", style2]
            if doc_name2.strip():
                cmd += ["--doc", doc_name2.strip()]
            if top_journals2:
                cmd.append("--top-journals")
            if apply_write:
                cmd.append("--apply")
            with st.spinner("扫描全文、检索候选、判断方向中，视文档长度可能要一两分钟..."):
                code, out, err = run(cmd, timeout=600)
            st.code(out + err, language="text")

    # ---------- 命令 ----------
    with tab_cmd:
        st.subheader("直接运行命令")
        st.caption(f"在文献库根目录（`{ROOT}`）下执行，比如 `python scripts/check_library.sh` 或 "
                   f"`bash scripts/build_keyword_index.sh`。这是给熟悉命令行的人用的快捷入口，"
                   f"跑的就是你敲进去的原始命令，没有额外的安全过滤——别粘来路不明的命令。")
        raw_cmd = st.text_input("命令", key="raw_cmd_input",
                                 placeholder="python scripts/find_duplicate_titles.py")
        if st.button("运行", key="run_raw_cmd"):
            st.session_state["active_tab"] = TAB_LABELS["cmd"]
            if not raw_cmd.strip():
                st.error("先输入命令")
            else:
                with st.spinner("运行中..."):
                    try:
                        result = subprocess.run(raw_cmd, shell=True, cwd=ROOT, capture_output=True,
                                                 text=True, encoding="utf-8", errors="replace", timeout=600)
                        st.code((result.stdout or "") + (result.stderr or "") or "(无输出)", language="text")
                        if result.returncode != 0:
                            st.warning(f"退出码 {result.returncode}")
                    except subprocess.TimeoutExpired:
                        st.error("运行超时（超过 10 分钟）")

        st.divider()
        st.subheader("用自然语言下命令")
        st.caption("描述你想干什么，交给 LLM 翻译成一条命令——**先看它翻译得对不对，确认后再手动点运行**，"
                   "不会自己直接执行，避免翻译错了误操作。")
        nl_instruction = st.text_area("想做什么", key="nl_instruction",
                                       placeholder="帮我把 topics 目录下所有综述转成 docx")
        if st.button("翻译成命令", key="gen_cmd"):
            st.session_state["active_tab"] = TAB_LABELS["cmd"]
            if not nl_instruction.strip():
                st.error("先描述一下你想做什么")
            else:
                try:
                    sys.path.insert(0, str(SCRIPTS))
                    import ds  # noqa: E402
                    system = (
                        "你在协助把一句中文/英文指令翻译成一条可以在这个文献库根目录下直接执行的命令。"
                        "库里常用脚本(位于 scripts/ 下, 都可以用 `python scripts/<name>.py --help` 看参数): "
                        "check_library.sh/build_keyword_index.sh(Git Bash, 体检/关键词索引)、"
                        "find_duplicate_titles.py(全库标题查重)、build_topic_digest.py(主题综述摘要)、"
                        "md_to_docx.py(综述转docx)、find_citations.py(引文献)、export_for_endnote.py、"
                        "word_insert_citation.py/word_auto_cite.py(仅Windows,Word插入引用)、"
                        "scan_new_papers.py/parse_search_results.py/scan_state.py(扩充查新)、"
                        "export_referable_folder.py、resolve_duplicate.py、rename_journal_abbr.py。"
                        "只输出这一条命令本身(能直接在终端执行的样子), 不要解释、不要 markdown 代码块围栏。"
                        "如果没把握翻译对(比如需要的参数你不确定), 就输出一句注释形式的中文说明"
                        "(以 # 开头)讲清楚缺什么信息, 不要瞎编参数。"
                    )
                    with st.spinner("翻译中..."):
                        client = ds.get_client()
                        suggested = ds.call(client, "deepseek-v4-flash", system, nl_instruction,
                                             temperature=0, json_mode=False).strip()
                    st.session_state["suggested_cmd"] = suggested
                except Exception as e:
                    st.error(f"调用 LLM 失败: {e}（检查 DEEPSEEK_API_KEY 环境变量是否配置好）")

        if st.session_state.get("suggested_cmd"):
            edited_cmd = st.text_area("翻译出的命令（可以手动改）", value=st.session_state["suggested_cmd"],
                                       key="suggested_cmd_edit")
            if st.button("运行这条命令", key="run_suggested_cmd"):
                st.session_state["active_tab"] = TAB_LABELS["cmd"]
                with st.spinner("运行中..."):
                    try:
                        result = subprocess.run(edited_cmd, shell=True, cwd=ROOT, capture_output=True,
                                                 text=True, encoding="utf-8", errors="replace", timeout=600)
                        st.code((result.stdout or "") + (result.stderr or "") or "(无输出)", language="text")
                        if result.returncode != 0:
                            st.warning(f"退出码 {result.returncode}")
                    except subprocess.TimeoutExpired:
                        st.error("运行超时（超过 10 分钟）")

if reader_col is not None:
    with reader_col:
        st.write("")  # 顶部留白, 大致对齐左边标题高度
        render_reading_pane(ALL_PAPERS)


