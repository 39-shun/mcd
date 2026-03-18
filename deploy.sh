#!/bin/bash
# ============================================================
# deploy.sh - ラズパイ用 データ自動デプロイスクリプト
# 使い方: bash deploy.sh
# cron例: 30 15 * * * bash /home/pi/mcd/deploy.sh >> /home/pi/mcd/logs/deploy.log 2>&1
# ============================================================

set -euo pipefail

REPO_DIR="/home/pi/mcd"          # ★ リポジトリのパスに変更
DATA_DIR="${REPO_DIR}/data"
LOG_PREFIX="[deploy.sh]"

echo "${LOG_PREFIX} $(date '+%Y-%m-%d %H:%M:%S') 開始"

# リポジトリに移動
cd "${REPO_DIR}"

# dataディレクトリに変更がなければ終了
if git diff --quiet HEAD -- data/ && git ls-files --others --exclude-standard data/ | grep -q .; then
  : # 未追跡ファイルあり → 続行
elif git diff --quiet HEAD -- data/; then
  echo "${LOG_PREFIX} data/ に変更なし。スキップします。"
  exit 0
fi

# git add（dataディレクトリのみ。コードの誤プッシュを防ぐ）
git add data/

# 変更があるか再確認
if git diff --cached --quiet; then
  echo "${LOG_PREFIX} ステージングに変更なし。スキップします。"
  exit 0
fi

# コミット（何県更新したかをメッセージに含める）
CHANGED_FILES=$(git diff --cached --name-only | wc -l)
COMMIT_MSG="data: update ${CHANGED_FILES} file(s) at $(date '+%Y-%m-%d %H:%M')"
git commit -m "${COMMIT_MSG}"

# プッシュ（リトライ付き）
for i in 1 2 3; do
  if git push origin main; then
    echo "${LOG_PREFIX} プッシュ成功: ${COMMIT_MSG}"
    exit 0
  fi
  echo "${LOG_PREFIX} プッシュ失敗 (試行 ${i}/3)。30秒後リトライ..."
  sleep 30
done

echo "${LOG_PREFIX} ERROR: プッシュが3回失敗しました。手動確認が必要です。"
exit 1
