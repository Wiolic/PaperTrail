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
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
NOTES_DIR = ROOT / "notes"
PAPERS_DIR = ROOT / "papers"

TAB_LABELS = {
    "recent": "📊 总览", "lib": "📚 文献库", "check": "🩺 体检", "cite": "📝 引文献",
    "scan": "🔎 扩充/查新", "word": "📄 Word 插入引用", "cmd": "⚙️ 命令",
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
div[class*="st-key-main_pane"], div[class*="st-key-reader_pane"] {
    height: calc(100vh - 130px);
    overflow-y: auto;
    padding-right: 8px;
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
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", bufsize=1)
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


@st.cache_data(show_spinner=False)
def load_all_papers(_cache_bust: float = 0.0) -> dict:
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
        }
    return papers


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

    2026-07-20新增: 只保存/恢复 main_pane(标签页那一栏)的滚动位置, 不再是全局
    stMain——现在main_pane和reader_pane是各自独立的滚动容器(见CSS), 阅读笔记时
    不应该让"关闭窗格"这类操作把左边列表也带得乱跳; reader_pane自己的滚动位置
    由 inject_reader_scroll_top() 单独处理(每次打开笔记都强制回到顶部, 不是恢复
    历史位置)。"""
    nonce = time.time()
    script = """
        <script>
        (function() {
            const doc = window.parent.document;
            const win = window.parent;
            // 真正滚动的是 main_pane 这个keyed容器自己的滚动条(overflow-y:auto),
            // 不再是整个 stMain。
            function scroller() { return doc.querySelector('div[class*="st-key-main_pane"]'); }

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


def inject_reader_scroll_top():
    """阅读窗格每次打开(或切换到另一篇笔记)都应该从头展示, 不应该继承左边列表或者
    上一篇笔记残留的滚动位置——和 inject_scroll_preserver() 对 main_pane 的"记住并
    恢复"策略刻意相反, 这里是每次都强制归零。同样用nonce强制组件重新加载, 否则
    components.html内容不变时iframe不会重新执行脚本。"""
    nonce = time.time()
    script = f"""
        <!-- nonce:{nonce} -->
        <script>
        (function() {{
            const doc = window.parent.document;
            function scroller() {{ return doc.querySelector('div[class*="st-key-reader_pane"]'); }}
            function resetScroll() {{
                const el = scroller();
                if (el) el.scrollTop = 0;
            }}
            resetScroll();
            setTimeout(resetScroll, 60);
            setTimeout(resetScroll, 200);
        }})();
        </script>
        """
    components.html(script, height=0)


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
    if st.session_state.get("_last_reading_citekey") != citekey:
        inject_reader_scroll_top()
        st.session_state["_last_reading_citekey"] = citekey
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
            col_pdf, col_edit = st.columns(2)
            with col_pdf:
                if (PAPERS_DIR / f"{citekey}.pdf").exists():
                    if st.button("📄 打开对应 PDF", key="reading_pdf", use_container_width=True):
                        open_pdf(citekey)
            with col_edit:
                if st.button("✏️ 编辑笔记", key="start_edit", use_container_width=True):
                    st.session_state["editing_citekey"] = citekey
                    st.rerun()

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
                        st.session_state["papers_cache_bust"] = time.time()
                        st.success("已保存到 notes/，notes-readable/ 也同步更新了。")
                        st.rerun()
                    except Exception as e:
                        st.error(f"保存失败：{e}")
            with col_cancel:
                if st.button("取消", key="cancel_edit", use_container_width=True):
                    st.session_state["editing_citekey"] = None
                    st.rerun()


ALL_PAPERS = load_all_papers(st.session_state.get("papers_cache_bust", 0.0))
inject_scroll_preserver()

# 顶层布局: 有笔记在阅读时, 右侧留一栏当"阅读窗格"(所有标签页共用同一个阅读窗格,
# 不管从总览的最近新增点进去还是从文献库浏览页点进去, 效果一样)。
if st.session_state.get("reading_citekey"):
    col_l, col_r = st.columns([3, 2])
    main_col = col_l.container(key="main_pane")
    reader_col = col_r
else:
    main_col = st.container(key="main_pane")
    reader_col = None

with main_col:
    st.title("📘 PaperTrail")

    tab_overview, tab_library, tab_check, tab_cite, tab_scan, tab_word, tab_cmd = st.tabs(
        ["📊 总览", "📚 文献库", "🩺 体检", "📝 引文献", "🔎 扩充/查新", "📄 Word 插入引用", "⚙️ 命令"])
    restore_active_tab()

    # ---------- 总览 ----------
    with tab_overview:
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
        def do_batch_ingest():
            """真正跑 batch_ingest.py 全自动入库 + 事后清理 inbox 里已处理的文件。抽成函数
            是因为"检测到没有 Agent CLI"和"用户确认强制全自动"两条路径都要跑同一套逻辑,
            不想复制一遍。"""
            before_pdfs = sorted(p.name for p in inbox_dir.glob("*.pdf"))
            with st.spinner(f"正在批量入库 inbox/ 里的 {n_inbox} 篇 PDF，视数量可能要几分钟..."):
                code, out, err = run([sys.executable, str(SCRIPTS / "batch_ingest.py"),
                                       "--source", str(inbox_dir), "--until-done"], timeout=1800)
            st.code((out or "") + (err or "") or "(无输出)", language="text")

            # batch_ingest.py 出于"绝不删除只读源目录里的文件"这条红线, 设计上永远不会
            # 删除 --source 里的文件——这对它原本的用途(--source 指向外部只读EndNote库)
            # 是对的, 但对着 inbox/ 这种"消费完就该清空"的临时文件夹用就成了坑: 已经成功
            # 入库、或者被判定为库里已有的重复文献, PDF 会永远留在 inbox/ 里, 导致每次点
            # "入库"都显示"没有待处理的新主文献", 看起来像"完全没反应", 实际上是已经处理过
            # 了(要么真正写进了notes/, 要么是确认重复), 只是没人清理源文件。这里读一遍
            # scripts/.ingest_state.json, 把状态是 done/skipped_duplicate 的对应 PDF
            # 从 inbox/ 里删掉, 让重复点"入库"这件事最终会收敛(inbox 清空), 而不是卡死不动。
            state_path = SCRIPTS / ".ingest_state.json"
            cleaned, new_citekeys = [], []
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
                        if entry and entry.get("status") in ("done", "skipped_duplicate"):
                            if entry.get("status") == "done" and entry.get("citekey"):
                                new_citekeys.append(entry["citekey"])
                            pdf_path.unlink()
                            cleaned.append(name)
                            break

            if code == 0:
                if cleaned:
                    msg = f"入库跑完了，已从 inbox/ 清掉 {len(cleaned)} 篇已处理的 PDF：{', '.join(cleaned)}。"
                    if new_citekeys:
                        msg += f" 新增 citekey：{', '.join(new_citekeys)}。"
                    else:
                        msg += "（这些是确认库里已有的重复文献，没有新增笔记，这是正常的去重行为，不是 bug。）"
                    st.success(msg + " 点上面「🔄 刷新库列表」看最新结果。")
                else:
                    st.success("入库跑完了，点上面「🔄 刷新库列表」看最新结果。")
            else:
                st.warning(f"退出码 {code}，看上面输出排查。")

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
                            do_batch_ingest()
                    with col_cancel:
                        if st.button("取消", key="cancel_api_ingest", use_container_width=True):
                            st.session_state["ingest_pending"] = False
                            st.session_state["active_tab"] = TAB_LABELS["recent"]
                            st.rerun()
                else:
                    st.session_state["ingest_pending"] = False
                    do_batch_ingest()

        if st.button("🔄 刷新库列表", help="入库了新文献之后点这个重新扫描 notes/"):
            st.session_state["active_tab"] = TAB_LABELS["recent"]
            st.session_state["papers_cache_bust"] = time.time()
            st.rerun()

        st.divider()
        st.subheader("最近新增")
        recent = sorted(ALL_PAPERS.items(), key=lambda kv: kv[1]["added"], reverse=True)[:5]
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

            f1, f2 = st.columns([2, 3])
            with f1:
                keyword_search = st.text_input("搜标题/关键词", placeholder="输入关键词过滤",
                                                on_change=_stay_on_library_tab)
            with f2:
                tag_filter = st.multiselect("按标签筛选", all_tags, on_change=_stay_on_library_tab)

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
                    haystack = (meta["title"] + " " + " ".join(meta["keywords"])).lower()
                    if keyword_search.lower() not in haystack:
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
