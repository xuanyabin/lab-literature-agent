#!/bin/bash
# 从孤儿 state 分支还原运行状态到工作区（GitHub Actions 流水线第一步）。
# 还原对象：literature_agent.db（SQLite 主库）、config/users/auto_terms/（整个目录，
# LLM 扩展词与聚类缓存）、logs/.llm_usage.json（LLM 用量）。
# state 分支不存在（首次运行或无远程）时打印提示并退出 0，不视为错误；
# 单个文件在 state 分支上不存在时跳过，不报错。
set -euo pipefail
cd "$(dirname "$0")/.."

if ! git fetch origin state 2>/dev/null; then
  echo "[pull_state] 远端无 state 分支（首次运行或无远程），跳过状态还原"
  exit 0
fi

restored=0

restore_file() {
  local path="$1"
  mkdir -p "$(dirname "$path")"
  # cat-file blob 输出原始字节，不经任何属性过滤，对 SQLite 二进制安全
  git cat-file blob "origin/state:$path" > "$path"
  echo "[pull_state] 还原 $path"
  restored=$((restored + 1))
}

# 单文件还原
for path in literature_agent.db logs/.llm_usage.json; do
  if git cat-file -e "origin/state:$path" 2>/dev/null; then
    restore_file "$path"
  else
    echo "[pull_state] state 分支上无 $path，跳过"
  fi
done

# 目录还原：列出 state 分支上 config/users/auto_terms/ 全部文件逐一还原
# （覆盖式还原，不删除本地多余文件——CI 检出是干净的，本地多余缓存文件无害）
dir_found=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  dir_found=1
  restore_file "$f"
done < <(git ls-tree -r --name-only origin/state -- config/users/auto_terms)
if [ "$dir_found" = "0" ]; then
  echo "[pull_state] state 分支上无 config/users/auto_terms/，跳过"
fi

echo "[pull_state] 完成，共还原 $restored 个文件"
