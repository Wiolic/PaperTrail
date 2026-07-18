#!/usr/bin/env python3
r"""通用 DeepSeek API 调用工具 —— 供 Claude(我)在会话里直接驱动 DeepSeek 做子任务用。

这不是无人值守批处理脚本(那是 batch_ingest.py, 全自动跑完就写盘)。这个工具是给"Claude
驱动 DeepSeek"工作流用的原语: 我读文件/做判断/写最终文件, 把可以外包的重体力活(读PDF抽
元数据、起草摘要、建议关键词等)丢给 DeepSeek 做, 拿到结果后自己审核再落盘, 而不是让
DeepSeek 的输出无人监督地直接写进 papers/notes/bib。

子命令:
  chat       任意 system+user prompt, 返回纯文本
  json       同上, 但用 JSON 模式, 返回结构化 JSON 文本
  pdf-meta   项目专用: 给一个 PDF 路径, 抽首页文字 + 调 DeepSeek 抽元数据(复用
             batch_ingest.py 里已测试过的 EXTRACT_SYSTEM_PROMPT/字段定义), 返回 JSON

用法:
  python scripts/ds.py chat --system "..." --user "..." [--model deepseek-v4-flash]
  python scripts/ds.py json --system "..." --user "..." [--out result.json]
  python scripts/ds.py pdf-meta "D:\...\paper.pdf" [--out meta.json]
  echo "文本" | python scripts/ds.py chat --system "..."   # 不给 --user 则从 stdin 读

输出默认写到 UTF-8 文件(--out)或按 UTF-8 重配置后打印到 stdout, 避免 Windows 控制台
GBK 代码页把中文/特殊字符打乱或导致 UnicodeEncodeError 崩溃(踩过的坑, 见 batch_ingest.py
同类问题)。
"""
import argparse
import io
import json
import os
import sys
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    sys.exit("缺少依赖: pip install openai")

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_MODEL = "deepseek-v4-flash"
BASE_URL = "https://api.deepseek.com"


def get_client() -> OpenAI:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        sys.exit("缺少环境变量 DEEPSEEK_API_KEY (setx DEEPSEEK_API_KEY \"你的key\" 后开新终端)")
    return OpenAI(api_key=key, base_url=BASE_URL)


def emit(text: str, out_path: str | None):
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)


def call(client, model, system, user, temperature, json_mode, reasoning_effort="low"):
    # 注: 据 DeepSeek 文档(2026-07-17查), 目前 low/medium 会向后兼容映射成 high, 只有 high/max 真正生效
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    kwargs = dict(model=model, messages=messages, temperature=temperature, stream=False,
                  reasoning_effort=reasoning_effort)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content


def cmd_chat(args):
    user = args.user if args.user is not None else sys.stdin.read()
    client = get_client()
    text = call(client, args.model, args.system, user, args.temperature, json_mode=False,
                reasoning_effort=args.reasoning_effort)
    emit(text, args.out)


def cmd_json(args):
    user = args.user if args.user is not None else sys.stdin.read()
    client = get_client()
    text = call(client, args.model, args.system, user, args.temperature, json_mode=True,
                reasoning_effort=args.reasoning_effort)
    json.loads(text)  # 校验确实是合法 JSON, 不合法就在这里报错而不是静默写坏文件
    emit(text, args.out)


def cmd_pdf_meta(args):
    import batch_ingest as b  # 复用已测试过的抽取逻辑与 prompt

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        sys.exit(f"文件不存在: {pdf_path}")
    text = b.extract_pdf_text(pdf_path, max_chars=args.max_chars)
    if len(text.strip()) < 50:
        sys.exit("PDF 抽不出文字(可能是扫描件), 无法抽取元数据")
    client = get_client()
    result = call(client, args.model, b.EXTRACT_SYSTEM_PROMPT, text, temperature=0, json_mode=True,
                  reasoning_effort=args.reasoning_effort)
    json.loads(result)
    emit(result, args.out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("chat", cmd_chat), ("json", cmd_json)):
        p = sub.add_parser(name)
        p.add_argument("--system", default="")
        p.add_argument("--user", default=None, help="不给则从 stdin 读取")
        p.add_argument("--model", default=DEFAULT_MODEL)
        p.add_argument("--temperature", type=float, default=0)
        p.add_argument("--reasoning-effort", dest="reasoning_effort", default="low")
        p.add_argument("--out", default=None, help="写入这个文件(UTF-8), 不给则打印到 stdout")
        p.set_defaults(func=fn)

    p = sub.add_parser("pdf-meta")
    p.add_argument("pdf")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--reasoning-effort", dest="reasoning_effort", default="low")
    p.add_argument("--max-chars", dest="max_chars", type=int, default=6000)
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_pdf_meta)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
