#!/usr/bin/env python3
"""
"扩充"/"查新"工作流提速 —— 2026-07-20 加: 把"从一堆WebSearch原始结果里挑出真正的候选论文"
这一步交给DeepSeek, 不用Claude自己逐条读搜索结果判断相关性/摘抄标题。

背景: 之前的流程是Claude跑WebSearch → Claude自己读全部搜索结果文字 → Claude手动挑出候选、
手动确认DOI(经常还要逐个curl查Crossref核对)→ 手动拼JSON喂给scan_new_papers.py。中间"读一堆
搜索结果+判断哪些是真候选+摘出标题"这一步是可以外包的体力活, 不需要Claude的判断力, 而"手动
核对DOI"这一步其实scan_new_papers.py的Crossref查询已经能做得更准(比LLM复述准, 不会编造)。

新流程: Claude跑WebSearch → 把原始结果原样存成文本 → 这个脚本调DeepSeek(默认便宜的v4-flash,
这是纯结构化抽取不需要强推理)从文本里抽出"看起来像候选论文"的条目(标题+DOI线索+期刊/年份线索+
粗分类) → 直接产出 scan_new_papers.py 能吃的 --candidates JSON → 交给 scan_new_papers.py 做
Crossref核验+去重+分类(这步不需要LLM, 纯HTTP查询更可靠)。Claude全程只需要读这个脚本打印的
一行统计, 不用读原始搜索结果全文, 也不用逐个手动查DOI。

用法:
  1. 把一次或多次WebSearch的原始输出(文字/JSON都行)存成一个文本文件, 可以是多次搜索结果拼在一起
  2. python scripts/parse_search_results.py --raw-file raw_search_dump.txt \\
       --context "Ru基PEM水电解酸性OER催化剂, 2021-2026, top journals" \\
       --out candidates.json
  3. python scripts/scan_new_papers.py --candidates candidates.json --out exports/xxx.xlsx

不做什么: 不判断"这篇论文到底重不重要"这种需要专业知识的相关性判断——那是Claude在最终看到
xlsx汇总(不是原始搜索结果)时做的事; 这个脚本只做"从一堆文字里把候选论文列表抽出来"这个纯体力活。
"""

import argparse
import json
import sys
if sys.platform == "win32":
    try:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", write_through=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8",
                                      errors="replace", write_through=True)
    except Exception:
        pass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SYSTEM_PROMPT = """你在协助从一段网页搜索结果原始文字里提取候选学术论文列表。这段文字可能包含
多次搜索的结果拼接、搜索引擎自己生成的摘要、不相关的链接等噪音。

只输出一个 JSON 对象: {"candidates": [...]}, 数组每个元素是一篇被提到的、看起来像真实学术论文的条目:
{
  "title": "论文标题(尽量原样摘录, 不要翻译/编造/截断)",
  "doi": "如果文字里明确出现了DOI就填, 没有就填 null(不要猜测编造DOI, 猜错比没有更糟)",
  "journal": "期刊名, 没提到就 null",
  "year": 年份数字, 没提到就 null,
  "category_hint": "根据标题内容判断的粗分类, 从这几个里选: Ir-based / Ru-based / Ir-Ru-mixed / Non-PGM / Unclear"
}

规则:
1. 同一篇论文如果在文字里出现多次(比如摘要和链接列表各出现一次), 只输出一次, 不要重复。
2. 明显不是学术论文的内容(公司主页、专利、新闻稿、无关的产品页面)不要输出。
3. 综述/perspective类文章也算, 照实输出, 不用额外标注(下游会按需要处理)。
4. 不确定的字段宁可留 null, 绝不编造。
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-file", required=True, help="WebSearch原始输出存成的文本文件路径")
    ap.add_argument("--context", default="", help="这批搜索是为了找什么(帮助模型判断哪些是真正相关的候选)")
    ap.add_argument("--out", required=True, help="输出JSON路径, 直接喂给 scan_new_papers.py --candidates")
    ap.add_argument("--model", default="deepseek-v4-flash", help="纯结构化抽取任务, 默认用便宜的flash模型")
    args = ap.parse_args()

    raw_text = Path(args.raw_file).read_text(encoding="utf-8")
    if args.context:
        user_text = f"这批搜索的目标: {args.context}\n\n搜索结果原文:\n{raw_text}"
    else:
        user_text = raw_text

    import ds

    client = ds.get_client()
    result = ds.call(client, args.model, SYSTEM_PROMPT, user_text, temperature=0, json_mode=True)
    data = json.loads(result)
    candidates = data.get("candidates", [])

    # 转换成 scan_new_papers.py 期待的格式: 有DOI的走 {"doi":...}, 没DOI的走标题(scan_new_papers.py
    # 会用Crossref按标题反查, 比这里再调一次LLM猜DOI更可靠)
    out_candidates = []
    for c in candidates:
        entry = {"category": c.get("category_hint", "Unclear")}
        if c.get("doi"):
            entry["doi"] = c["doi"]
        else:
            entry["title"] = c.get("title", "")
            entry["journal_hint"] = c.get("journal")
            entry["year_hint"] = c.get("year")
        out_candidates.append(entry)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    n_with_doi = sum(1 for c in out_candidates if "doi" in c)
    print(f"已写入 {out_path}: 共 {len(out_candidates)} 条候选({n_with_doi} 条有DOI, "
          f"{len(out_candidates) - n_with_doi} 条只有标题需要 scan_new_papers.py 用Crossref反查)")


if __name__ == "__main__":
    main()
