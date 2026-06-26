#!/bin/bash
# スナップショットキャッシュを最新化してGitにpush
# box_average/snapshot.py の実行後に呼ぶ想定

set -e
cd "$(dirname "$0")"

PYTHON=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
SNAP_SRC=~/pokemon_monitors/box_average/snapshots

# 1. ローカルsnapshotsを最新化
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
