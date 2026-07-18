#!/bin/bash
# 从 notes/*.md 的 frontmatter 汇总 tags/keywords/表征方法, 重建 KEYWORDS.md
# 要求 frontmatter 用行内数组写法: tags: [a, b]  /  keywords: [x, y]  /  表征方法: [x, y]
# 用法: bash scripts/build_keyword_index.sh
cd "$(dirname "$0")/.." || exit 1

TMP_TAG=$(mktemp); TMP_KW=$(mktemp); TMP_CHAR=$(mktemp)

extract() {  # $1=field  $2=outfile
  local field="$1" out="$2"
  for f in notes/*.md; do
    [ -e "$f" ] || continue
    ck=$(basename "$f" .md)
    # 取 frontmatter 第一处 field 行, 抽 [ ] 内内容
    line=$(sed -n "s/^${field}:[[:space:]]*\[\(.*\)\].*/\1/p" "$f" | head -1)
    [ -z "$line" ] && continue
    echo "$line" | tr ',' '\n' | while IFS= read -r tok; do
      tok=$(echo "$tok" | sed 's/^[[:space:]"'\'']*//; s/[[:space:]"'\'']*$//')
      [ -n "$tok" ] && printf '%s\t%s\n' "$tok" "$ck" >> "$out"
    done
  done
}

extract tags "$TMP_TAG"
extract keywords "$TMP_KW"
extract 表征方法 "$TMP_CHAR"

render() {  # $1=infile -> markdown 列表, 按次数降序
  awk -F'\t' '{cnt[$1]++; if(ck[$1]=="") ck[$1]=$2; else ck[$1]=ck[$1]", "$2}
    END{ for(k in cnt) printf "%d\t%s\t%s\n", cnt[k], k, ck[k] }' "$1" \
  | sort -rn -k1,1 \
  | awk -F'\t' '{printf "- **%s** (%d) — %s\n", $2, $1, $3}'
}

n_tag=$(cut -f1 "$TMP_TAG" | sort -u | wc -l | tr -d ' ')
n_kw=$(cut -f1 "$TMP_KW" | sort -u | wc -l | tr -d ' ')
n_char=$(cut -f1 "$TMP_CHAR" | sort -u | wc -l | tr -d ' ')
today=$(date +%Y-%m-%d)

{
  echo "# 关键词索引"
  echo
  echo "自动由 \`scripts/build_keyword_index.sh\` 从 \`notes/\` 的 frontmatter 生成，**请勿手工编辑**。"
  echo "DeepSeek/Agent 写笔记时应先查此表复用已有词，避免近义词泛滥。"
  echo
  echo "更新于 $today | tags $n_tag 个 · keywords $n_kw 个 · 表征方法 $n_char 个"
  echo
  echo "## 受控标签 tags（次数）"
  echo
  body=$(render "$TMP_TAG"); [ -n "$body" ] && echo "$body" || echo "_（暂无）_"
  echo
  echo "## 自由关键词 keywords（次数）"
  echo
  body=$(render "$TMP_KW"); [ -n "$body" ] && echo "$body" || echo "_（暂无）_"
  echo
  echo "## 表征方法（次数）"
  echo
  body=$(render "$TMP_CHAR"); [ -n "$body" ] && echo "$body" || echo "_（暂无）_"
} > KEYWORDS.md

rm -f "$TMP_TAG" "$TMP_KW" "$TMP_CHAR"
echo "已重建 KEYWORDS.md: tags $n_tag 个, keywords $n_kw 个, 表征方法 $n_char 个"
