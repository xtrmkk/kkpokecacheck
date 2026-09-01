#!/usr/bin/env python3
"""box_urls.json の各BOXのサムネイル画像URLをスニダンから取得して box_images.json に保存する。

画像URLは滅多に変わらないので、実行時に毎回62回叩かずこのファイルを配信に使う。
BOXを box_urls.json に足したら再実行するだけでよい。
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "ja,en-US;q=0.9",
}


def main() -> int:
    box_urls = json.loads((BASE_DIR / "box_urls.json").read_text(encoding="utf-8"))
    out_file = BASE_DIR / "box_images.json"
    images = {}
    if out_file.exists():
        images = json.loads(out_file.read_text(encoding="utf-8"))

    missing = []
    for name, url in box_urls.items():
        if images.get(name):
            continue
        m = re.search(r"/apparels/(\d+)", url)
        if not m:
            missing.append(name)
            continue
        try:
            r = requests.get(f"https://snkrdunk.com/v1/apparels/{m.group(1)}",
                             headers=HEADERS, timeout=15)
            r.raise_for_status()
            img = (r.json().get("primaryMedia") or {}).get("imageUrl")
        except Exception as e:
            print(f"  ! {name}: {e}", file=sys.stderr)
            img = None
        if img:
            images[name] = img
            print(f"  + {name}")
        else:
            missing.append(name)
        time.sleep(0.2)

    images = {k: images[k] for k in box_urls if k in images}
    out_file.write_text(json.dumps(images, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(images)}/{len(box_urls)} 件を {out_file.name} に保存")
    if missing:
        print("画像なし:", ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
