#!/bin/bash
# 三方对账: papers/ PDF、notes/ 笔记、library.bib 条目应一一对应
# 另外核对 SI(补充材料)绑定: notes frontmatter 的 si_files 列出的文件必须存在于 papers/,
# papers/ 里形如 <citekey>_SI.* 的文件必须被某篇 notes 的 si_files 引用(否则是孤儿 SI)。
# 用法: bash scripts/check_library.sh (在 Claude_reference 目录下运行)
cd "$(dirname "$0")/.." || exit 1

# 主 PDF 列表要排除 *_SI.* 附件, 否则会被误判成"缺笔记/缺bib"的主文献
ls papers/*.pdf 2>/dev/null | xargs -n1 basename 2>/dev/null | grep -v '_SI\.[Pp][Dd][Ff]$' | sed 's/\.pdf$//' | sort > /tmp/_papers
ls notes/*.md  2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/\.md$//'  | sort > /tmp/_notes
grep -oE '@[a-zA-Z]+\{[^,[:space:]]+,' library.bib 2>/dev/null | sed 's/.*{//;s/,$//' | sort > /tmp/_bib

problems=0
diff_report() {
  local title="$1" a="$2" b="$3"
  local items
  items=$(comm -23 "$a" "$b")
  if [ -n "$items" ]; then
    echo "[!] $title:"
    echo "$items" | sed 's/^/    - /'
    problems=1
  fi
}

diff_report "有 PDF 但缺笔记"      /tmp/_papers /tmp/_notes
diff_report "有笔记但缺 PDF"       /tmp/_notes  /tmp/_papers
diff_report "有 PDF 但 bib 缺条目"  /tmp/_papers /tmp/_bib
diff_report "bib 有条目但缺 PDF"    /tmp/_bib    /tmp/_papers

ls notes-readable/*.md 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/\.md$//' | sort > /tmp/_notes_readable
diff_report "notes/ 有但 notes-readable/ 缺(需跑 render_readable_notes.py 同步)" /tmp/_notes /tmp/_notes_readable
diff_report "notes-readable/ 有但 notes/ 缺(孤儿生成物)" /tmp/_notes_readable /tmp/_notes

ls extracted-text/*.txt 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/\.txt$//' | sort > /tmp/_extracted
diff_report "notes/ 有但 extracted-text/ 缺(PDF文字缓存没存, 以后改字段要重新读PDF)" /tmp/_notes /tmp/_extracted

# 排除各种"DOI缺失"占位值(模型有时不遵守"填N/A"的要求, 会写unknown/none/null等), 否则这些论文会互相被误判重复
dup=$(grep -ioE 'doi *= *[{"][^}"]+' library.bib 2>/dev/null | sed 's/.*[{"]//' | tr 'A-Z' 'a-z' | grep -vE '^(n/a|na|unknown|none|null|not available|not found|not provided)$' | sort | uniq -d)
if [ -n "$dup" ]; then
  echo "[!] bib 中重复 DOI:"; echo "$dup" | sed 's/^/    - /'; problems=1
fi

# --- SI 绑定核对 ---
: > /tmp/_si_referenced
for f in notes/*.md; do
  [ -e "$f" ] || continue
  line=$(sed -n 's/^si_files:[[:space:]]*\[\(.*\)\].*/\1/p' "$f" | head -1)
  [ -z "$line" ] && continue
  echo "$line" | tr ',' '\n' | while IFS= read -r tok; do
    tok=$(echo "$tok" | sed 's/^[[:space:]"'\'']*//; s/[[:space:]"'\'']*$//')
    [ -n "$tok" ] && echo "$tok" >> /tmp/_si_referenced
  done
done
sort -u /tmp/_si_referenced -o /tmp/_si_referenced 2>/dev/null

ls papers/ 2>/dev/null | grep -E '_SI\.[A-Za-z0-9]+$' | sort > /tmp/_si_actual

missing_si=$(comm -23 /tmp/_si_referenced /tmp/_si_actual 2>/dev/null)
if [ -n "$missing_si" ]; then
  echo "[!] 笔记引用了 si_files 但 papers/ 里找不到对应文件:"
  echo "$missing_si" | sed 's/^/    - /'; problems=1
fi

orphan_si=$(comm -13 /tmp/_si_referenced /tmp/_si_actual 2>/dev/null)
if [ -n "$orphan_si" ]; then
  echo "[!] papers/ 里有 _SI 文件但没有笔记的 si_files 引用它(孤儿 SI, 可能配对丢失):"
  echo "$orphan_si" | sed 's/^/    - /'; problems=1
fi

# --- 全库标题相似度查重(独立于上面的 DOI 查重, 抓 DOI 缺失/出错时漏过的重复收录) ---
if command -v py >/dev/null 2>&1; then
  title_dup_out=$(py "$(dirname "$0")/find_duplicate_titles.py" 2>&1)
  title_dup_status=$?
  echo "$title_dup_out"
  [ "$title_dup_status" -ne 0 ] && problems=1
else
  echo "[i] 跳过标题查重(找不到 py 启动器)"
fi

n_inbox=$(ls inbox/*.pdf 2>/dev/null | wc -l)
[ "$n_inbox" -gt 0 ] && echo "[i] inbox 有 $n_inbox 个 PDF 待整理"

n_si=$(wc -l < /tmp/_si_actual 2>/dev/null | tr -d ' ')
if [ "$problems" -eq 0 ]; then
  echo "OK: $(wc -l < /tmp/_papers) 篇 PDF / $(wc -l < /tmp/_notes) 篇笔记(含 $(wc -l < /tmp/_notes_readable) 篇 notes-readable, $(wc -l < /tmp/_extracted) 篇 extracted-text) / $(wc -l < /tmp/_bib) 条 bib / $n_si 个 SI 附件, 三方一致"
fi
exit $problems
