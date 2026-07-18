#!/usr/bin/env python3
"""数值层: 从已入库论文里抽取带条件的性能数据行 -> data/performance.csv。
每行是"一组测试条件"而不是"一篇论文"——同一篇论文报了半电池和 MEA 两套数据就是两行,
条件(电解液/温度/电流密度基准/测试形式)必须和数值绑在一起, 不可比的数据不能塞进同一列
(见 CLAUDE.md 反馈: 半电池 1 M HClO4 的 240 mV 和 MEA 80C 的数据不能直接比较)。

数据读取/结构化抽取全部走 DeepSeek(复用 extracted-text/ 缓存, 不重新读PDF), 不做本地正则解析——
性能数据藏在图表/正文各处, 规则解析不可靠, 只有 LLM 读全文语境才靠谱。

用法:
    python scripts/extract_performance.py --limit 20
    python scripts/extract_performance.py --until-done
    python scripts/extract_performance.py --citekey 2024-AdvMater-Xxx   # 单篇(强制重跑, 忽略 state)

断点续跑: 已处理过的 citekey 记录在 scripts/.performance_state.json, 重跑自动跳过
(除非用 --citekey 指定单篇强制重跑)。只追加 data/performance.csv, 不改动 papers/notes/bib/INDEX。
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    sys.exit("缺少依赖: pip install openai")

ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "notes"
EXTRACTED_TEXT = ROOT / "extracted-text"
DATA_DIR = ROOT / "data"
PERF_CSV = DATA_DIR / "performance.csv"
STATE_FILE = Path(__file__).resolve().parent / ".performance_state.json"

FIELDS = [
    "citekey", "row_id", "test_type", "reaction", "electrolyte", "temperature_C",
    "catalyst_loading_mg_cm2", "current_density_ref_mA_cm2",
    "overpotential_mV", "cell_voltage_V", "tafel_slope_mV_dec",
    "stability_duration_h", "degradation_rate", "other_metrics", "source_location",
]

# test_type 受控词表: 半电池测试和器件(MEA/全电池)测试的数值不可比, 必须显式标注,
# 不能靠"看起来像"猜——DeepSeek 抽取时如果论文没写清楚, 宁可留空也不要瞎猜。
# 2026-07-18 起: "device" 并入 "MEA"(同一回事的两种叫法, 之前分开写导致同类数据被割裂成两种
# test_type, 统计/筛选时要多写一次 OR 条件——历史数据已用 resolve_duplicate 同款脚本合并, 见 AGENTS.md)。
SYSTEM_PROMPT = """你是电催化/材料领域文献数据抽取助手。给定一篇论文的正文(可能不含全文,只有前几页),
找出论文中报告的所有"带完整测试条件的性能数据组"(通常是 OER/HER/ORR/PEMWE/AEMWE/PEMFC 等电催化性能
指标), 每一组独立测试条件(比如半电池 vs 全电池/MEA、不同电解液、不同温度)算一行, 不要把不同条件下的
数据混进同一行。

返回 JSON: {"rows": [ {...}, ... ]}, 数组可以为空(论文没有可提取的性能数据时)。每行字段:
- test_type: "half-cell" 或 "MEA" 或 "full-cell" 或 "unknown"(论文没说清楚就填这个,不要猜；
  器件级测试统一填 "MEA", 不要用 "device")
- reaction: 反应类型, 如 "OER"/"HER"/"ORR"/"PEMWE"/"AEMWE"/"PEMFC" 等, 没有就填 "unknown"
- electrolyte: 电解液/膜类型描述, 如 "0.5 M H2SO4"/"1 M KOH"/"Nafion 117 PEM" 等, 没有填 "unknown"
- temperature_C: 数字(摄氏度), 没提到就填 null, 不要假设室温
- catalyst_loading_mg_cm2: 数字, 没有填 null
- current_density_ref_mA_cm2: 报告过电位/电压时对应的电流密度基准(如 10 mA/cm2 是 OER 惯例), 没有填 null
- overpotential_mV: 在上面基准电流密度下的过电位(mV), 没有填 null
- cell_voltage_V: 器件测试下在某电流密度下的电池电压(V, 常见于 MEA/PEMWE), 没有填 null
- tafel_slope_mV_dec: Tafel 斜率(mV/dec), 没有填 null
- stability_duration_h: 稳定性测试时长(小时), 没有填 null
- degradation_rate: 衰减率, 保留原文单位描述(如 "0.02 mV/h" 或 "5% after 100h"), 没有填 ""
- other_metrics: 上面字段覆盖不到但论文强调的其他指标(如法拉第效率/TOF/质量活性), 自由文本, 没有填 ""
- source_location: 这组数据来自论文的哪个图/表/段落(如 "Figure 3a"/"Table 1"), 帮助人工回查原文, 没有填 ""

严禁编造数值——只抽取论文正文/摘要里明确写出的数字, 任何字段没有把握就填 null/"unknown"/空字符串,
宁可漏抽不可编造。如果论文根本不是电催化性能相关的(如纯理论/表征方法综述), rows 返回空数组即可。"""


def call_deepseek(client: OpenAI, model: str, text: str) -> list[dict]:
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    result = json.loads(resp.choices[0].message.content)
    return result.get("rows") or []


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def ensure_csv():
    DATA_DIR.mkdir(exist_ok=True)
    if not PERF_CSV.exists():
        with PERF_CSV.open("w", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()


def append_rows(rows: list[dict]):
    with PERF_CSV.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        for row in rows:
            writer.writerow({k: row.get(k, "") if row.get(k) is not None else "" for k in FIELDS})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--until-done", action="store_true")
    ap.add_argument("--citekey", help="只处理单篇, 强制重跑(忽略 state)")
    ap.add_argument("--model", default="deepseek-chat")
    args = ap.parse_args()

    import os
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("缺少环境变量 DEEPSEEK_API_KEY")
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")

    ensure_csv()
    state = load_state()

    if args.citekey:
        _run_batch(client, args, state, [args.citekey])
        return

    while True:
        all_citekeys = sorted(p.stem for p in EXTRACTED_TEXT.glob("*.txt"))
        todo = [k for k in all_citekeys if k not in state][: args.limit]
        if not todo:
            print("没有待处理的新论文(全部已在 .performance_state.json 记录里)。")
            return
        print(f"库里共 {len(all_citekeys)} 篇已抽文字缓存, 已处理 {len(state)} 篇, 本批处理 {len(todo)} 篇。")
        _run_batch(client, args, state, todo)
        if not args.until_done:
            return


def _run_batch(client, args, state, todo):
    total_rows = 0
    for citekey in todo:
        txt_path = EXTRACTED_TEXT / f"{citekey}.txt"
        if not txt_path.exists():
            print(f"[跳过] {citekey}: 没有 extracted-text 缓存")
            continue
        text = txt_path.read_text(encoding="utf-8")
        try:
            rows = call_deepseek(client, args.model, text)
        except Exception as e:
            print(f"[出错] {citekey}: {e}")
            if not args.citekey:
                state[citekey] = {"status": "error", "error": str(e)}
            continue

        for i, row in enumerate(rows, 1):
            row["citekey"] = citekey
            row["row_id"] = f"{citekey}#{i}"
        append_rows(rows)
        total_rows += len(rows)
        state[citekey] = {"rows": len(rows)}
        print(f"[完成] {citekey}: {len(rows)} 行性能数据")

    if not args.citekey:
        save_state(state)
    print(f"\n本批完成: {len(todo)} 篇论文, 新增 {total_rows} 行性能数据。")


if __name__ == "__main__":
    main()
