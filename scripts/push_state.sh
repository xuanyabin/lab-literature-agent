#!/bin/bash
# 把运行状态打包成孤儿 commit 强推到远端 state 分支（单 commit，历史不累积）。
# 托管对象：literature_agent.db（SQLite 主库）、config/users/auto_terms/（整个目录，
# LLM 扩展词与聚类缓存）、logs/.llm_usage.json（LLM 用量）。
#
# 实现说明：用临时 GIT_INDEX_FILE + git commit-tree 构建只含状态文件的孤儿提交，
# 全程不切换分支、不触碰工作区与主 index。不能用 git switch --orphan：
# git 2.55 下它会把跟踪文件从工作区移除，且已提交到孤儿分支的状态文件
# （literature_agent.db 等）会在切回 main 时被删除，造成数据丢失。
set -euo pipefail
cd "$(dirname "$0")/.."

# 收集当前存在的状态文件
files=()
[ -f literature_agent.db ] && files+=("literature_agent.db")
if [ -d config/users/auto_terms ]; then
  while IFS= read -r f; do
    files+=("$f")
  done < <(find config/users/auto_terms -type f | sort)
fi
[ -f logs/.llm_usage.json ] && files+=("logs/.llm_usage.json")

if [ "${#files[@]}" -eq 0 ]; then
  echo "[push_state] 本地无状态文件，跳过推送"
  exit 0
fi

# 在临时 index 中只加入状态文件（-f：这些路径在 main 分支被 gitignore）。
# mktemp 创建的空文件不是合法 index，先删掉让 git 从零创建。
TMP_INDEX=$(mktemp "${TMPDIR:-/tmp}/push-state-index.XXXXXX")
trap 'rm -f "$TMP_INDEX"' EXIT
rm -f "$TMP_INDEX"

for f in "${files[@]}"; do
  GIT_INDEX_FILE="$TMP_INDEX" git add -f "$f"
done
TREE=$(GIT_INDEX_FILE="$TMP_INDEX" git write-tree)

# 与远端 state 分支现有 tree 比较，无变化则跳过提交与推送
REMOTE_TREE=""
if git fetch origin state 2>/dev/null; then
  REMOTE_TREE=$(git rev-parse -q --verify "origin/state^{tree}" 2>/dev/null || true)
fi
if [ -n "$REMOTE_TREE" ] && [ "$TREE" = "$REMOTE_TREE" ]; then
  echo "[push_state] 状态与远端 state 分支一致，跳过推送"
  exit 0
fi

# 身份信息经环境变量传入，不改动仓库 git config（本地手动跑也不留痕迹）
COMMIT=$(
  GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-github-actions[bot]}" \
  GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}" \
  GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-github-actions[bot]}" \
  GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}" \
  git commit-tree "$TREE" -m "chore(state): 运行状态快照 $(date -u '+%F %T') UTC"
)

git push -f origin "$COMMIT:refs/heads/state"
echo "[push_state] 已推送 state 分支（${#files[@]} 个文件，commit ${COMMIT:0:7}）"
