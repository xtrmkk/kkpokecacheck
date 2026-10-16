#!/bin/bash
# スナップショットキャッシュを最新化してGitにpush
# box_average/snapshot.py の実行後に呼ぶ想定

set -e
cd "$(dirname "$0")"

PYTHON=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
SNAP_SRC=~/pokemon_monitors/box_average/snapshots

# 1. ローカルsnapshotsを最新化
#    既存 ./snapshots を消してからコピー。残すと cp -r が入れ子
#    (./snapshots/snapshots) を作り、古いファイルが残ってキャッシュが更新されない。
rm -rf ./snapshots
cp -r "$SNAP_SRC" ./snapshots

# 2. キャッシュ再生成
$PYTHON -c "
from app import _build_snapshot_cache
import json
snaps = _build_snapshot_cache()
with open('snapshots_cache.json', 'w') as f:
    json.dump(snaps, f, ensure_ascii=False)
print(f'Cache rebuilt: {len(snaps)} snapshots')
"

# 3. Git push
git add snapshots_cache.json
if git diff --cached --quiet; then
    echo "No changes to push"
else
    git commit -m "Update snapshot cache $(date +%Y-%m-%d_%H:%M)"
    git push origin main
    echo "Pushed to GitHub → Render will auto-deploy"
fi

# 4. kkonly（統合ダッシュボード）を更新してデプロイ。
#    ここで失敗しても上のRender更新は済んでいるので、ログに出すだけで終了コードは汚さない。
if ! ~/pokemon_monitors/kkonly/sync_and_deploy.sh; then
    echo "[warn] kkonly のデプロイに失敗しました（Renderの更新は完了しています）" >&2
fi

# 5. Cloudflare 版 BOX検索（https://box.kk1117.workers.dev）にも同じキャッシュを載せてデプロイ。
#    ここを通さないと CF 版のスナップショットが更新されず、タイルの価格が古いまま残る。
#    node は nvm 配下で launchd の PATH に無いので、kkonly と同じ手順で通す。
CF_DIR=~/pokemon_monitors/pokemon_lookup-cf
NODE_BIN=$(dirname "$(ls -1d "$HOME"/.nvm/versions/node/*/bin/npx 2>/dev/null | sort -V | tail -1)")
if cp snapshots_cache.json "$CF_DIR/data/snapshots_cache.json" \
   && (cd "$CF_DIR" && PATH="$NODE_BIN:$PATH" npx wrangler deploy); then
    echo "Deployed → https://box.kk1117.workers.dev"
else
    echo "[warn] box (Cloudflare) のデプロイに失敗しました" >&2
fi
