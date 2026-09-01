import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

SHIPPING = 990
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9",
}

BASE_DIR = Path(__file__).parent
BOX_URLS: dict[str, str] = json.loads(
    (BASE_DIR / "box_urls.json").read_text(encoding="utf-8")
)
try:
    BOX_IMAGES: dict[str, str] = json.loads(
        (BASE_DIR / "box_images.json").read_text(encoding="utf-8")
    )
except FileNotFoundError:  # fetch_box_images.py を流せば作られる
    BOX_IMAGES = {}
SNAPSHOT_DIR = BASE_DIR / "snapshots"
SNAPSHOT_CACHE_FILE = BASE_DIR / "snapshots_cache.json"

WATCHLIST_ORDER = [
    "ストームエメラルダ",
    "アビスアイ", "ニンジャスピナー", "ムニキスゼロ", "メガドリーム", "インフェルノX",
    "メガブレイブ", "メガシンフォニア", "ブラックボルト", "ホワイトフレア", "ブラックボルトDX", "ホワイトフレアDX",
    "スペシャルボックストウホク", "スペシャルボックスヒロシマ", "スペシャルボックスフクオカ",
    "ロケット団の栄光アタッシュケース", "ロケット団の栄光", "熱風のアリーナ",
    "バトルパートナーズ", "テラスタルフェス", "超電ブレイカー",
    "楽園ドラゴーナ", "ステラミラクル", "ナイトワンダラー", "変幻の仮面",
    "クリムゾンヘイズ", "ワイルドフォース", "サイバージャッジ", "シャイニートレジャー",
    "古代の咆哮", "未来の一閃", "レイジングサーフ", "黒炎の支配者",
    "ポケモンカード151", "スノーハザード", "クレイバースト", "トリプレットビート",
    "バイオレット", "スカーレット", "VSTARユニバース", "パラダイムトリガー",
    "白熱のアルカナ", "ロストアビス", "Pokemon GO", "ダークファンタズマ",
    "スペースジャグラー", "タイムゲイザー", "バトルリージョン", "スターバース",
    "VMAXクライマックス", "25thアニバーサリーコレクション", "フュージョンアーツ",
    "蒼空ストリーム", "摩天パーフェクト", "イーブイヒーローズ", "漆黒のガイスト",
    "白銀のランス", "双璧のファイター", "連撃マスター", "一撃マスター",
    "シャイニースターV", "仰天のボルテッカー",
]


def _qty1_unit(entry: dict) -> int | None:
    """BOX1個出品の送料込単価（offers の quantity==1）。"""
    for o in entry.get("offers") or []:
        if o.get("quantity") == 1 and o.get("avg"):
            return round(o["avg"])
    return None


def _build_snapshot_cache() -> list[dict]:
    """Build a lightweight cache: extract only best_avg + total_boxes per item."""
    if not SNAPSHOT_DIR.exists():
        return []
    snapshots = []
    for f in sorted(SNAPSHOT_DIR.glob("2026-*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            items = data.get("items", data)
            light = {}
            for name, entry in items.items():
                if isinstance(entry, dict) and entry.get("best_avg"):
                    rec = {
                        "best_avg": entry["best_avg"],
                        "total_boxes": entry.get("total_boxes", 0),
                        "inventory": entry.get("inventory", 0),
                    }
                    q1 = _qty1_unit(entry)
                    if q1:
                        rec["q1"] = q1
                    light[name] = rec
            snapshots.append({"ts": f.stem, "items": light})
        except (json.JSONDecodeError, KeyError):
            continue
    return snapshots


def _load_snapshots() -> list[dict]:
    """Load snapshots from cache file, rebuild if missing."""
    if SNAPSHOT_CACHE_FILE.exists():
        try:
            return json.loads(SNAPSHOT_CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    snaps = _build_snapshot_cache()
    SNAPSHOT_CACHE_FILE.write_text(json.dumps(snaps, ensure_ascii=False), encoding="utf-8")
    return snaps


_snapshot_cache: list[dict] | None = None


def get_snapshots() -> list[dict]:
    global _snapshot_cache
    if _snapshot_cache is None:
        _snapshot_cache = _load_snapshots()
    return _snapshot_cache


def _build_summary() -> list[dict]:
    """Build summary for all boxes: current price, 1d/7d/30d change."""
    snaps = get_snapshots()
    if not snaps:
        return []

    latest = snaps[-1]
    prev_1d = snaps[-2] if len(snaps) >= 2 else None
    prev_7d = None
    prev_30d = None
    for s in reversed(snaps):
        days_ago = _days_between(s["ts"], latest["ts"])
        if days_ago >= 7 and prev_7d is None:
            prev_7d = s
        if days_ago >= 30 and prev_30d is None:
            prev_30d = s
            break

    results = []
    for name in WATCHLIST_ORDER:
        entry = latest["items"].get(name)
        if not entry or not entry.get("best_avg"):
            continue

        cur = round(entry["best_avg"])
        total_boxes = entry.get("total_boxes", 0)
        inv = entry.get("inventory", 0)

        d1 = _pct_change(prev_1d, name, cur) if prev_1d else None
        d7 = _pct_change(prev_7d, name, cur) if prev_7d else None
        d30 = _pct_change(prev_30d, name, cur) if prev_30d else None

        results.append({
            "name": name,
            "price": cur,
            "total_boxes": total_boxes,
            "inventory": inv,
            "d1": d1,
            "d7": d7,
            "d30": d30,
        })
    return results


def _days_between(ts1: str, ts2: str) -> int:
    d1 = ts1[:10]
    d2 = ts2[:10]
    try:
        return (datetime.strptime(d2, "%Y-%m-%d") - datetime.strptime(d1, "%Y-%m-%d")).days
    except ValueError:
        return 0


def _pct_change(snap: dict, name: str, cur: float) -> float | None:
    entry = snap["items"].get(name)
    if not entry or not entry.get("best_avg"):
        return None
    prev = entry["best_avg"]
    if prev == 0:
        return None
    return round((cur - prev) / prev * 100, 1)


def _build_chart_data(name: str) -> list[dict]:
    """Build daily aggregated chart data for a specific box."""
    snaps = get_snapshots()
    daily: dict[str, list] = {}
    for s in snaps:
        entry = s["items"].get(name)
        if not entry or not entry.get("best_avg"):
            continue
        day = s["ts"][:10]
        if day not in daily:
            daily[day] = {"prices": [], "boxes": [], "q1": []}
        daily[day]["prices"].append(entry["best_avg"])
        daily[day]["boxes"].append(entry.get("total_boxes", 0))
        if entry.get("q1"):
            daily[day]["q1"].append(entry["q1"])

    result = []
    for day in sorted(daily.keys()):
        prices = daily[day]["prices"]
        boxes = daily[day]["boxes"]
        q1 = daily[day]["q1"]
        result.append({
            "day": day,
            "date": day[5:],
            "avg": round(sum(prices) / len(prices)),
            "min": round(min(prices)),
            "max": round(max(prices)),
            "boxes": max(boxes),
            "q1": round(sum(q1) / len(q1)) if q1 else None,
            "q1_min": round(min(q1)) if q1 else None,
            "q1_max": round(max(q1)) if q1 else None,
        })
    return result


SNKRDUNK = "https://snkrdunk.com"
JST = timezone(timedelta(hours=9))
HISTORY_TTL = 1800  # 相場履歴のメモリキャッシュ 30分

_history_cache: dict[str, tuple[float, dict]] = {}
_product_cache: dict[str, tuple[int, int | None]] = {}


def _sd_json(path: str, params: dict | None = None):
    r = requests.get(SNKRDUNK + path, params=params, timeout=15,
                     headers={**HEADERS, "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def _resolve_product(name: str) -> tuple[int, int | None]:
    """BOX名 → (productCatalogId, 「1個」variant_id)。どちらも変わらないので永続キャッシュ。"""
    if name in _product_cache:
        return _product_cache[name]
    m = re.search(r"/apparels/(\d+)", BOX_URLS[name])
    if not m:
        raise ValueError(f"apparel id が取れません: {name}")
    pcid = _sd_json(f"/v1/apparels/{m.group(1)}").get("productCatalogId")
    if not pcid:
        raise ValueError(f"productCatalogId がありません: {name}")
    hist = _sd_json(f"/v3/products/{pcid}/trading-history", {"range": "all"})
    opts = ((hist.get("filters") or {}).get("variants") or {}).get("options") or []
    vid = next((o["id"] for o in opts if o.get("name") == "1個"), None)
    _product_cache[name] = (pcid, vid)
    return pcid, vid


def _box_history(name: str) -> dict:
    """BOX1個の相場推移（スニダン取引価格・最大3年）。
    取得できなければ手元スナップショットの出品最安単価にフォールバックする。"""
    now = time.time()
    hit = _history_cache.get(name)
    if hit and now - hit[0] < HISTORY_TTL:
        return hit[1]

    out = None
    try:
        pcid, vid = _resolve_product(name)
        params = {"range": "all"}
        if vid:
            params["variant_id"] = vid
        hist = _sd_json(f"/v3/products/{pcid}/trading-history", params)
        lines = (hist.get("chart") or {}).get("lines") or []
        raw = (lines[0].get("points") if lines else []) or []
        points = [
            {"d": datetime.fromtimestamp(pt["timestamp"] / 1000, JST).strftime("%Y-%m-%d"),
             "p": pt["price"]}
            for pt in raw if pt.get("price")
        ]
        if points:
            trades = [t for t in hist.get("trades") or [] if t.get("title") == "1個"]
            out = {
                "name": name,
                "source": "snkrdunk",
                "points": points,
                "last_trade": trades[0]["price"] if trades else None,
                "last_trade_at": trades[0]["soldAt"][:10] if trades else None,
            }
    except Exception:
        out = None

    if out is None:
        points = [{"d": p["day"], "p": p["q1"]} for p in _build_chart_data(name) if p.get("q1")]
        out = {"name": name, "source": "snapshot", "points": points,
               "last_trade": None, "last_trade_at": None}

    _history_cache[name] = (now, out)
    return out


# ─── Lookup page (existing) ───

LOOKUP_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ポケカBOX 最安値検索</title>
<style>
  :root {
    --bg: #eef1f5;
    --card: #fff;
    --ink: #1f2933;
    --sub: #5c6773;
    --muted: #98a2ae;
    --line: #e9edf2;
    --navy: #2c3e50;
    --red: #e74c3c;
    --green: #2e7d32;
    --blue: #378ADD;
    --shadow: 0 1px 2px rgba(16,24,40,.05), 0 6px 20px rgba(16,24,40,.06);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Hiragino Sans', sans-serif;
         background: var(--bg); color: var(--ink); padding-bottom: 48px;
         -webkit-font-smoothing: antialiased; }

  /* ── top bar ── */
  .topbar { position: sticky; top: 0; z-index: 30; background: var(--navy);
            padding: 10px 14px 8px; box-shadow: 0 2px 10px rgba(0,0,0,.12); }
  .brand { font-size: 15px; font-weight: 700; color: #fff; letter-spacing: .02em;
           margin-bottom: 8px; }
  .nav { display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none;
         /* 横スクロールできることが分かるよう右端をフェードさせる */
         mask-image: linear-gradient(to right, #000 86%, transparent);
         -webkit-mask-image: linear-gradient(to right, #000 86%, transparent); }
  .nav::-webkit-scrollbar { display: none; }
  .nav a { flex: none; padding: 6px 13px; border-radius: 999px; text-decoration: none;
           font-size: 12.5px; color: #cfd8e3; background: rgba(255,255,255,.09); }
  .nav a.active { background: var(--red); color: #fff; font-weight: 700; }

  .wrap { max-width: 720px; margin: 0 auto; padding: 14px; }
  .card { background: var(--card); border-radius: 14px; box-shadow: var(--shadow);
          margin-bottom: 14px; overflow: hidden; }
  .card-pad { padding: 14px; }

  /* ── search ── */
  .search-box { display: flex; gap: 8px; }
  input[type=text] { flex: 1; min-width: 0; padding: 12px 14px; font-size: 16px;
          border: 1px solid var(--line); border-radius: 10px; outline: none;
          background: #fafbfc; }
  input[type=text]:focus { border-color: var(--navy); background: #fff; }
  .btn-go { flex: none; padding: 12px 20px; background: var(--red); color: #fff;
            border: none; border-radius: 10px; font-size: 15px; font-weight: 700;
            cursor: pointer; }
  .btn-go:active { background: #c0392b; }
  .hint { font-size: 11.5px; color: var(--muted); margin-top: 9px; line-height: 1.6; }

  /* ── box gallery ── */
  .list-head { display: flex; align-items: baseline; justify-content: space-between;
               gap: 8px; padding: 13px 14px 10px; }
  .list-title { font-size: 13px; font-weight: 700; color: var(--sub); }
  .list-title span { color: var(--muted); font-weight: 500; margin-left: 4px; }
  .list-hint { font-size: 11px; color: var(--muted); }
  .list-body { padding: 0 12px 14px; border-top: 1px solid var(--line); }
  .list-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr));
               gap: 10px 8px; padding-top: 12px; }
  .tile { display: flex; flex-direction: column; align-items: center; gap: 6px;
          background: none; border: none; padding: 0; font-family: inherit;
          cursor: pointer; color: var(--sub); }
  .tile.hide { display: none; }
  .thumb { position: relative; width: 100%; aspect-ratio: 1; border-radius: 10px;
           background: #f4f6f9; border: 1px solid var(--line); overflow: hidden;
           display: flex; align-items: center; justify-content: center; font-size: 24px; }
  .thumb img { position: absolute; inset: 0; width: 100%; height: 100%;
               object-fit: contain; padding: 5px; }
  .tile:active .thumb { border-color: var(--navy); }
  .tile.on .thumb { border-color: var(--red); box-shadow: 0 0 0 2px rgba(231,76,60,.22); }
  .tile-name { font-size: 11px; line-height: 1.35; text-align: center; word-break: break-word; }
  .tile.on .tile-name { color: var(--red); font-weight: 700; }
  .tile-meta { font-size: 11.5px; font-weight: 700; color: var(--ink); line-height: 1.3;
               font-variant-numeric: tabular-nums; }
  .tile-chg { font-size: 10px; font-weight: 600; margin-left: 3px; }
  .tile-chg.up { color: var(--green); }
  .tile-chg.down { color: var(--red); }
  .tile-chg.flat { color: var(--muted); }
  .empty-chip { font-size: 12px; color: var(--muted); padding: 14px 2px 0; }

  /* ── result header ── */
  .res-head { padding: 14px; border-bottom: 1px solid var(--line); }
  .res-top { display: flex; gap: 12px; align-items: center; }
  .res-thumb { position: relative; flex: none; width: 56px; height: 56px; border-radius: 10px;
               overflow: hidden; background: #f4f6f9; border: 1px solid var(--line);
               display: flex; align-items: center; justify-content: center; font-size: 22px; }
  .res-thumb img { position: absolute; inset: 0; width: 100%; height: 100%;
                   object-fit: contain; padding: 3px; }
  .res-titles { min-width: 0; }
  .back-gallery { margin-top: 11px; width: 100%; padding: 9px; border: 1px solid var(--line);
                  background: #fafbfc; border-radius: 9px; font-size: 12px; font-weight: 700;
                  color: var(--navy); font-family: inherit; cursor: pointer; }
  .back-gallery:active { background: #f0f3f7; }
  .res-name { font-size: 17px; font-weight: 700; line-height: 1.35; }
  .res-meta { font-size: 11.5px; color: var(--muted); margin-top: 5px; }
  .res-meta b { color: var(--sub); font-weight: 600; }

  /* ── hero stats ── */
  .hero { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: var(--line); }
  .hero-cell { background: var(--card); padding: 13px 14px; }
  .hero-cell.wide { grid-column: 1 / -1; }
  .hero-label { font-size: 10.5px; color: var(--muted); letter-spacing: .02em; }
  .hero-value { font-size: 21px; font-weight: 700; margin-top: 3px; letter-spacing: -.02em; }
  .hero-value.accent { color: var(--red); }
  .hero-note { font-size: 11px; color: var(--sub); margin-top: 3px; }

  /* ── table ── */
  .sec-head { display: flex; align-items: center; justify-content: space-between;
              gap: 10px; padding: 13px 14px 9px; }
  .sec-head.stack { flex-direction: column; align-items: stretch; }
  .sec-head.stack .seg { width: 100%; }
  .sec-head.stack .seg button { flex: 1; }
  .sec-title { font-size: 12.5px; font-weight: 700; color: var(--sub); }
  .seg { display: flex; background: #f0f3f7; border-radius: 8px; padding: 2px; }
  .seg button { border: none; background: none; font-size: 11.5px; color: var(--sub);
                padding: 6px 10px; border-radius: 6px; cursor: pointer; font-family: inherit;
                white-space: nowrap; }
  .seg button.on { background: #fff; color: var(--navy); font-weight: 700;
                   box-shadow: 0 1px 2px rgba(0,0,0,.08); }
  .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { background: #f7f9fb; color: var(--muted); padding: 8px 10px; text-align: right;
       font-size: 10.5px; font-weight: 700; letter-spacing: .03em;
       border-bottom: 1px solid var(--line); white-space: nowrap; }
  th:first-child { text-align: left; padding-left: 14px; }
  th:last-child, td:last-child { padding-right: 14px; }
  td { padding: 9px 10px; border-bottom: 1px solid #f4f6f9; text-align: right;
       font-variant-numeric: tabular-nums; white-space: nowrap; }
  td:first-child { padding-left: 14px; }
  td:first-child { text-align: left; color: var(--sub); }
  tr.best td { background: #fff6f4; color: var(--red); font-weight: 700; }
  tr.q1 td:first-child { font-weight: 700; color: var(--navy); }
  td.dn { color: var(--green); font-weight: 600; }
  td.up { color: var(--red); }
  .tag { display: inline-block; font-size: 9.5px; font-weight: 700; padding: 1px 5px;
         border-radius: 4px; margin-left: 5px; vertical-align: 1px; }
  .tag.red { background: var(--red); color: #fff; }
  .tag.gray { background: #eaeef3; color: var(--sub); }
  .more { width: 100%; padding: 11px; border: none; background: #fafbfc; color: var(--navy);
          font-size: 12.5px; font-weight: 700; cursor: pointer; font-family: inherit;
          border-top: 1px solid var(--line); }
  .note { font-size: 11px; color: var(--muted); padding: 10px 14px 14px; line-height: 1.6; }

  /* ── chart ── */
  .chart-card .sec-head { border-top: 1px solid var(--line); }
  .chart-stats { display: flex; gap: 14px; padding: 0 14px 10px; flex-wrap: wrap; }
  .cs { font-size: 11px; color: var(--muted); }
  .cs b { display: block; font-size: 14px; color: var(--ink); margin-top: 1px;
          font-variant-numeric: tabular-nums; }
  .cs b.up { color: var(--green); }
  .cs b.down { color: var(--red); }
  .chart-container { position: relative; width: 100%; height: 220px; padding: 0 8px 12px; }
  .chart-empty { font-size: 12.5px; color: var(--muted); text-align: center; padding: 28px 14px; }
  .detail-link { display: block; text-align: center; font-size: 12px; color: var(--blue);
                 text-decoration: none; padding: 11px; border-top: 1px solid var(--line);
                 background: #fafbfc; font-weight: 600; }

  .state { text-align: center; color: var(--muted); font-size: 13.5px; padding: 26px 14px; }
  .error { background: #fff5f4; border: 1px solid #ffd7d1; border-radius: 12px;
           padding: 14px; color: #c0392b; font-size: 13.5px; margin-bottom: 14px; }
  .spin { display: inline-block; width: 15px; height: 15px; margin-right: 7px;
          border: 2px solid #dfe5ec; border-top-color: var(--red); border-radius: 50%;
          animation: sp .8s linear infinite; vertical-align: -3px; }
  @keyframes sp { to { transform: rotate(360deg); } }

  @media (max-width: 360px) {
    .hero-value { font-size: 19px; }
    table { font-size: 12.5px; }
    th, td { padding-left: 8px; padding-right: 8px; }
  }
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">🃏 ポケカBOX 最安値検索</div>
  <nav class="nav">
    <a href="/" class="active">検索</a>
    <a href="/dashboard">ダッシュボード</a>
    <a href="/ranking">下落ランキング</a>
    <a href="/portfolio">ポートフォリオ</a>
    <a href="/psa">PSA計算</a>
  </nav>
</header>

<main class="wrap">
  <section class="card card-pad">
    <div class="search-box">
      <input type="text" id="q" placeholder="BOX名を入力（例：メガドリーム）" autocomplete="off">
      <button class="btn-go" onclick="search()">検索</button>
    </div>
    <p class="hint">スニダンのリアルタイム価格を取得します（10〜20秒かかります）。単価はすべて送料 ¥990 込み。</p>
  </section>

  <section class="card" id="gallery">
    <div class="list-head">
      <div class="list-title">📦 BOXを選ぶ<span id="list-count"></span></div>
      <div class="list-hint">価格は前回取得時点の目安</div>
    </div>
    <div class="list-body">
      <div class="list-grid" id="box-list"></div>
      <div class="empty-chip" id="chip-empty" style="display:none">該当するBOXがありません</div>
    </div>
  </section>

  <div id="result"></div>
</main>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const BOX_LIST = {{ box_list | safe }};
const RANGES = [
  { key: '1m', label: '1ヶ月', days: 30 },
  { key: '3m', label: '3ヶ月', days: 90 },
  { key: '1y', label: '1年', days: 365 },
  { key: 'all', label: 'すべて', days: null },
];

let CURRENT = null;      // 直近の検索結果
let CHART_PTS = [];      // BOX1個の相場履歴 [{d, p}]
let CHART_SRC = '';      // snkrdunk（取引相場）か snapshot（出品最安単価）か
let chartObj = null;
let sortMode = 'qty';
let expanded = false;
let range = 'all';

function yen(n) { return '¥' + Math.round(n).toLocaleString(); }
function pct(n) { return (n > 0 ? '+' : '') + n.toFixed(1) + '%'; }

/* ── BOX一覧（画像＋名前＋直近価格のタイル。全件そのまま並べる） ── */
const IMG_OF = Object.fromEntries(BOX_LIST.map(b => [b.name, b.img]));

window.addEventListener('DOMContentLoaded', () => {
  const grid = document.getElementById('box-list');
  BOX_LIST.forEach(box => {
    const el = document.createElement('button');
    el.className = 'tile';
    el.type = 'button';
    el.dataset.name = box.name;
    const img = box.img
      ? '<img loading="lazy" src="' + box.img + '" alt="" onerror="this.remove()">'
      : '';
    el.innerHTML = '<div class="thumb">📦' + img + '</div>'
                 + '<div class="tile-name">' + box.name + '</div>'
                 + '<div class="tile-meta"></div>';
    el.onclick = () => { document.getElementById('q').value = box.name; search(); };
    grid.appendChild(el);
  });
  document.getElementById('list-count').textContent = '（' + BOX_LIST.length + '件）';
  document.getElementById('q').addEventListener('input', filterTiles);
  document.getElementById('q').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.target.blur(); search(); }
  });
  loadSummary();
});

/* タイルに前回スナップショットの最安単価と7日変動を入れる（一覧のまま比べられるように） */
async function loadSummary() {
  let items;
  try {
    items = (await (await fetch('/api/summary')).json()).items || [];
  } catch (e) { return; }
  const by = Object.fromEntries(items.map(i => [i.name, i]));
  document.querySelectorAll('.tile').forEach(t => {
    const it = by[t.dataset.name];
    if (!it) return;
    const d7 = it.d7;
    const cls = d7 === null || d7 === undefined ? 'flat' : d7 > 0.05 ? 'up' : d7 < -0.05 ? 'down' : 'flat';
    const txt = d7 === null || d7 === undefined ? '' : pct(d7);
    t.querySelector('.tile-meta').innerHTML =
      yen(it.price) + (txt ? '<span class="tile-chg ' + cls + '">' + txt + '</span>' : '');
  });
}

function filterTiles() {
  const q = document.getElementById('q').value.trim();
  let shown = 0;
  document.querySelectorAll('.tile').forEach(t => {
    const hit = !q || t.dataset.name.includes(q);
    t.classList.toggle('hide', !hit);
    if (hit) shown++;
  });
  document.getElementById('chip-empty').style.display = shown ? 'none' : 'block';
}

function markActive(name) {
  document.querySelectorAll('.tile').forEach(t => {
    t.classList.toggle('on', t.dataset.name === name);
  });
}

function scrollTo_(el) {
  const bar = document.querySelector('.topbar');
  const off = (bar ? bar.offsetHeight : 60) + 10;
  window.scrollTo(0, Math.max(0, el.offsetTop - off));
}
function scrollToResult() { scrollTo_(document.getElementById('result')); }
function scrollToGallery() { scrollTo_(document.getElementById('gallery')); }

/* ── 検索 ── */
async function search() {
  const q = document.getElementById('q').value.trim();
  if (!q) return;
  const res = document.getElementById('result');
  res.innerHTML = '<div class="card"><div class="state"><span class="spin"></span>スニダンからデータ取得中…</div></div>';
  scrollToResult();
  try {
    const resp = await fetch('/api/lookup?name=' + encodeURIComponent(q));
    const data = await resp.json();
    if (data.error) { res.innerHTML = '<div class="error">⚠️ ' + data.error + '</div>'; return; }
    CURRENT = data;
    expanded = false;
    renderResult();
    markActive(data.name);
    scrollToResult();
    loadChart(data.name);
  } catch (e) {
    res.innerHTML = '<div class="error">⚠️ 通信エラーが発生しました</div>';
  }
}

/* ── 結果の描画 ── */
function renderResult() {
  const d = CURRENT;
  const byUnit = [...d.offers].sort((a, b) => a.unit - b.unit);
  const best = byUnit[0];
  const one = d.offers.find(o => o.qty === 1);
  const baseUnit = one ? one.unit : best.unit;
  const gap = one ? (best.unit - one.unit) / one.unit * 100 : 0;

  const img = IMG_OF[d.name];
  const head =
    '<div class="res-head">'
    + '<div class="res-top">'
    + '<div class="res-thumb">📦' + (img ? '<img src="' + img + '" alt="" onerror="this.remove()">' : '') + '</div>'
    + '<div class="res-titles"><div class="res-name">' + d.name + '</div>'
    + '<div class="res-meta">' + d.fetched_at + ' 時点　/　市場 <b>' + d.total_boxes.toLocaleString() + '箱</b></div>'
    + '</div></div>'
    + '<button class="back-gallery" onclick="scrollToGallery()">📦 BOX一覧にもどる</button>'
    + '</div>';

  const hero =
    '<div class="hero">'
    + '<div class="hero-cell"><div class="hero-label">BOX1個 単価</div>'
    + '<div class="hero-value">' + (one ? yen(one.unit) : '—') + '</div>'
    + '<div class="hero-note">' + (one ? '出品 ' + yen(one.price) + ' + 送料' : '1個出品なし') + '</div></div>'
    + '<div class="hero-cell"><div class="hero-label">最安単価（まとめ買い）</div>'
    + '<div class="hero-value accent">' + yen(best.unit) + '</div>'
    + '<div class="hero-note">' + best.qty + '個 ' + yen(best.price)
    + (one && best.qty !== 1 ? '　<b>' + pct(gap) + '</b>' : '') + '</div></div>'
    + '</div>';

  const rows = renderRows(byUnit, best, baseUnit);
  const table =
    '<div class="sec-head"><div class="sec-title">個数別の単価</div>'
    + '<div class="seg">'
    + '<button class="' + (sortMode === 'qty' ? 'on' : '') + '" onclick="setSort(\'qty\')">個数順</button>'
    + '<button class="' + (sortMode === 'unit' ? 'on' : '') + '" onclick="setSort(\'unit\')">安い順</button>'
    + '</div></div>'
    + '<div class="table-wrap"><table><thead><tr><th>個数</th><th>出品最安値</th><th>送料込単価</th><th>1個比</th></tr></thead>'
    + '<tbody>' + rows.html + '</tbody></table></div>'
    + (rows.hidden > 0
        ? '<button class="more" onclick="toggleMore()">' + (expanded ? '折りたたむ' : 'すべて表示（あと' + rows.hidden + '件）') + '</button>'
        : '')
    + '<div class="note">送料込単価 =（出品最安値 + 送料¥990）÷ 個数。「1個比」はBOX1個で買った場合との差。</div>';

  document.getElementById('result').innerHTML =
    '<section class="card">' + head + hero + table + '</section>'
    + '<section class="card chart-card" id="chart-card">'
    + '<div class="sec-head stack"><div class="sec-title" id="chart-title">📈 BOX1個の相場推移</div>'
    + '<div class="seg" id="range-seg">'
    + RANGES.map(r => '<button class="' + (range === r.key ? 'on' : '') + '" onclick="setRange(\'' + r.key + '\')">' + r.label + '</button>').join('')
    + '</div></div>'
    + '<div id="chart-body"><div class="chart-empty"><span class="spin"></span>読み込み中…</div></div>'
    + '<a class="detail-link" href="/chart/' + encodeURIComponent(d.name) + '">まとめ買い最安値・出品数の詳細チャート →</a>'
    + '</section>';
}

function renderRows(byUnit, best, baseUnit) {
  const rank = {};
  byUnit.slice(0, 3).forEach((o, i) => { rank[o.qty] = i; });
  const list = sortMode === 'unit' ? byUnit : [...CURRENT.offers].sort((a, b) => a.qty - b.qty);
  const limit = expanded ? list.length : 10;
  const visible = list.slice(0, limit);

  const html = visible.map(o => {
    const diff = (o.unit - baseUnit) / baseUnit * 100;
    const cls = [o === best || o.qty === best.qty ? 'best' : '', o.qty === 1 ? 'q1' : ''].join(' ').trim();
    const medal = rank[o.qty] !== undefined ? ['🔴', '🟠', '🟡'][rank[o.qty]] + ' ' : '';
    const tag = o.qty === best.qty ? '<span class="tag red">最安</span>'
              : o.qty === 1 ? '<span class="tag gray">基準</span>' : '';
    const dcls = o.qty === 1 ? '' : diff < -0.05 ? 'dn' : diff > 0.05 ? 'up' : '';
    const dtxt = o.qty === 1 ? '—' : pct(diff);
    return '<tr class="' + cls + '"><td>' + medal + o.qty + '個' + tag + '</td>'
      + '<td>' + yen(o.price) + '</td><td>' + yen(o.unit) + '</td>'
      + '<td class="' + dcls + '">' + dtxt + '</td></tr>';
  }).join('');

  return { html: html, hidden: list.length - visible.length };
}

function setSort(mode) { sortMode = mode; renderResult(); drawChart(); }
function toggleMore() { expanded = !expanded; renderResult(); drawChart(); }
function setRange(key) { range = key; renderResult(); drawChart(); }

/* ── BOX1個の単価チャート ── */
async function loadChart(name) {
  CHART_PTS = []; CHART_SRC = '';
  try {
    const resp = await fetch('/api/box_history?name=' + encodeURIComponent(name));
    const data = await resp.json();
    CHART_PTS = data.points || [];
    CHART_SRC = data.source || '';
  } catch (e) { CHART_PTS = []; }
  drawChart();
}

function slicePoints() {
  const days = (RANGES.find(r => r.key === range) || {}).days;
  if (!days || !CHART_PTS.length) return CHART_PTS;
  const last = new Date(CHART_PTS[CHART_PTS.length - 1].d + 'T00:00:00');
  const from = new Date(last.getTime() - days * 86400000);
  const pts = CHART_PTS.filter(p => new Date(p.d + 'T00:00:00') >= from);
  return pts.length >= 2 ? pts : CHART_PTS.slice(-2);
}

function drawChart() {
  const body = document.getElementById('chart-body');
  if (!body) return;
  if (chartObj) { chartObj.destroy(); chartObj = null; }
  const isSnkr = CHART_SRC === 'snkrdunk';
  const title = document.getElementById('chart-title');
  if (title) title.textContent = isSnkr ? '📈 BOX1個の相場推移（取引価格）'
                                        : '📈 BOX1個の単価推移（出品最安・送料込）';
  if (!CHART_PTS.length) {
    body.innerHTML = '<div class="chart-empty">このBOXの履歴データがまだありません</div>';
    return;
  }
  const pts = slicePoints();
  const cur = pts[pts.length - 1].p;
  const first = pts[0].p;
  const hi = Math.max.apply(null, pts.map(p => p.p));
  const lo = Math.min.apply(null, pts.map(p => p.p));
  const chg = first ? (cur - first) / first * 100 : 0;
  const ccls = chg > 0.05 ? 'up' : chg < -0.05 ? 'down' : '';
  const span = CHART_PTS[0].d + ' 〜 ' + CHART_PTS[CHART_PTS.length - 1].d;

  body.innerHTML =
    '<div class="chart-stats">'
    + '<div class="cs">' + (isSnkr ? '直近相場' : '現在') + '<b>' + yen(cur) + '</b></div>'
    + '<div class="cs">期間最高<b>' + yen(hi) + '</b></div>'
    + '<div class="cs">期間最安<b>' + yen(lo) + '</b></div>'
    + '<div class="cs">期間変動<b class="' + ccls + '">' + pct(chg) + '</b></div>'
    + '</div>'
    + '<div class="chart-container"><canvas id="q1Chart"></canvas></div>'
    + '<div class="note">' + (isSnkr
        ? 'スニダンの「1個」取引価格（送料別）。上の表は販売中の出品最安値（送料込）なので水準は一致しません。'
        : 'スニダンの相場を取得できなかったため、手元スナップショットの出品最安単価（送料込）を表示しています。')
      + '<br>データ期間: ' + span + '（' + CHART_PTS.length + '日分）</div>';

  // ラベル書式は実際に表示される期間の長さで決める（「すべて」でも履歴が短いBOXがあるため）
  const spanDays = (new Date(pts[pts.length - 1].d) - new Date(pts[0].d)) / 86400000;
  const fmtLabel = d => spanDays > 300 ? d.slice(2, 7).replace('-', '/') : d.slice(5).replace('-', '/');

  chartObj = new Chart(document.getElementById('q1Chart'), {
    type: 'line',
    data: {
      labels: pts.map(p => fmtLabel(p.d)),
      datasets: [{
        data: pts.map(p => p.p),
        borderColor: '#e74c3c', borderWidth: 2.4,
        backgroundColor: 'rgba(231,76,60,0.10)', fill: true,
        pointRadius: 0, pointHoverRadius: 5, pointHoverBackgroundColor: '#e74c3c',
        tension: 0.25,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: items => pts[items[0].dataIndex].d,
            label: ctx => 'BOX1個 ' + yen(ctx.raw),
          },
        },
      },
      scales: {
        x: { grid: { display: false },
             ticks: { font: { size: 10 }, color: '#98a2ae', maxRotation: 0,
                      autoSkip: true, maxTicksLimit: 6 } },
        y: { grace: '6%',
             ticks: { font: { size: 10 }, color: '#98a2ae', maxTicksLimit: 6,
                      callback: v => '¥' + (v / 1000).toFixed(v % 1000 === 0 ? 0 : 1) + 'k' },
             grid: { color: 'rgba(0,0,0,0.05)' } },
      },
    },
  });
}
</script>
</body></html>"""

# ─── Dashboard page ───

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOXダッシュボード</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Hiragino Sans', sans-serif;
         background: #f5f5f5; padding: 12px; }
  h1 { font-size: 18px; color: #333; margin-bottom: 12px; }
  .nav { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
  .nav a { padding: 8px 14px; background: #2c3e50; color: white; border-radius: 8px;
           text-decoration: none; font-size: 13px; }
  .nav a.active { background: #e74c3c; }
  .sort-bar { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
  .sort-btn { padding: 6px 12px; background: white; border: 1px solid #ddd; border-radius: 6px;
              font-size: 12px; cursor: pointer; color: #555; }
  .sort-btn.active { background: #2c3e50; color: white; border-color: #2c3e50; }
  .grid { display: grid; gap: 8px; }
  .box-card { background: white; border-radius: 10px; padding: 12px 14px;
              box-shadow: 0 1px 4px rgba(0,0,0,.06); cursor: pointer;
              text-decoration: none; color: inherit; display: block; }
  .box-card:active { background: #f9f9f9; }
  .box-name { font-size: 14px; font-weight: 700; color: #222; margin-bottom: 4px; }
  .box-price { font-size: 20px; font-weight: 700; color: #222; }
  .box-row { display: flex; justify-content: space-between; align-items: baseline; }
  .box-meta { font-size: 11px; color: #888; margin-top: 4px; }
  .changes { display: flex; gap: 8px; margin-top: 6px; }
  .chg { font-size: 12px; font-weight: 600; padding: 2px 6px; border-radius: 4px; }
  .chg.up { background: #e8f5e9; color: #2e7d32; }
  .chg.down { background: #fce4ec; color: #c62828; }
  .chg.flat { background: #f5f5f5; color: #888; }
  .chg-label { font-size: 10px; color: #999; margin-right: 2px; }
  .updated { font-size: 11px; color: #aaa; text-align: center; margin-top: 12px; }
</style>
</head>
<body>
<h1>📊 BOXダッシュボード</h1>
<nav class="nav">
  <a href="/">検索</a>
  <a href="/dashboard" class="active">ダッシュボード</a>
  <a href="/ranking">下落ランキング</a>
  <a href="/portfolio">ポートフォリオ</a>
  <a href="/psa">PSA計算</a>
</nav>
<div class="sort-bar">
  <button class="sort-btn active" onclick="sortBy('default',this)">発売順</button>
  <button class="sort-btn" onclick="sortBy('price_asc',this)">安い順</button>
  <button class="sort-btn" onclick="sortBy('price_desc',this)">高い順</button>
  <button class="sort-btn" onclick="sortBy('d7_asc',this)">週間下落順</button>
  <button class="sort-btn" onclick="sortBy('d7_desc',this)">週間上昇順</button>
</div>
<div class="grid" id="grid"></div>
<div class="updated" id="updated"></div>
<script>
let DATA = [];
function yen(n) { return '¥' + n.toLocaleString(); }
function chgHtml(val, label) {
  if (val === null || val === undefined) return '<span class="chg flat"><span class="chg-label">'+label+'</span>—</span>';
  const cls = val > 0.05 ? 'up' : val < -0.05 ? 'down' : 'flat';
  const sign = val > 0 ? '+' : '';
  return '<span class="chg '+cls+'"><span class="chg-label">'+label+'</span>'+sign+val.toFixed(1)+'%</span>';
}
function render(data) {
  const grid = document.getElementById('grid');
  grid.innerHTML = data.map(d => {
    return '<a class="box-card" href="/chart/'+encodeURIComponent(d.name)+'">'
      +'<div class="box-row"><div class="box-name">'+d.name+'</div><div class="box-price">'+yen(d.price)+'</div></div>'
      +'<div class="changes">'+chgHtml(d.d1,'1D')+chgHtml(d.d7,'7D')+chgHtml(d.d30,'30D')+'</div>'
      +'<div class="box-meta">出品 '+d.total_boxes.toLocaleString()+' boxes</div>'
      +'</a>';
  }).join('');
}
function sortBy(key, el) {
  document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
  if (el) el.classList.add('active');
  let sorted = [...DATA];
  if (key === 'price_asc') sorted.sort((a,b) => a.price - b.price);
  else if (key === 'price_desc') sorted.sort((a,b) => b.price - a.price);
  else if (key === 'd7_asc') sorted.sort((a,b) => (a.d7 ?? 0) - (b.d7 ?? 0));
  else if (key === 'd7_desc') sorted.sort((a,b) => (b.d7 ?? 0) - (a.d7 ?? 0));
  render(sorted);
}
fetch('/api/summary').then(r => r.json()).then(data => {
  DATA = data.items;
  render(DATA);
  document.getElementById('updated').textContent = 'Last snapshot: ' + data.last_snapshot;
});
</script>
</body></html>"""

# ─── Chart page ───

CHART_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ name }} - 価格推移</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Hiragino Sans', sans-serif;
         background: #f5f5f5; padding: 12px; }
  .nav { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
  .nav a { padding: 8px 14px; background: #2c3e50; color: white; border-radius: 8px;
           text-decoration: none; font-size: 13px; }
  h1 { font-size: 18px; color: #333; margin-bottom: 12px; }
  .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 16px; }
  .stat { background: white; border-radius: 10px; padding: 10px 12px;
          box-shadow: 0 1px 4px rgba(0,0,0,.06); }
  .stat-label { font-size: 11px; color: #888; }
  .stat-value { font-size: 18px; font-weight: 700; color: #222; margin-top: 2px; }
  .stat-value.down { color: #c62828; }
  .stat-value.up { color: #2e7d32; }
  .chart-wrap { background: white; border-radius: 10px; padding: 12px;
                box-shadow: 0 1px 4px rgba(0,0,0,.06); margin-bottom: 12px; }
  .chart-title { font-size: 13px; color: #666; font-weight: 600; margin-bottom: 8px; }
  .chart-container { position: relative; width: 100%; height: 260px; }
  .chart-container2 { position: relative; width: 100%; height: 160px; }
  .back { display: inline-block; margin-bottom: 12px; font-size: 13px; color: #3498db;
          text-decoration: none; }
</style>
</head>
<body>
<nav class="nav">
  <a href="/">検索</a>
  <a href="/dashboard">ダッシュボード</a>
  <a href="/ranking">下落ランキング</a>
  <a href="/portfolio">ポートフォリオ</a>
  <a href="/psa">PSA計算</a>
</nav>
<a class="back" href="/dashboard">← ダッシュボードに戻る</a>
<h1>📈 {{ name }}</h1>
<div class="stats" id="stats"></div>
<div class="chart-wrap">
  <div class="chart-title">最安単価（送料込）</div>
  <div class="chart-container"><canvas id="priceChart"></canvas></div>
</div>
<div class="chart-wrap">
  <div class="chart-title">出品BOX数</div>
  <div class="chart-container2"><canvas id="boxChart"></canvas></div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
const NAME = {{ name_json | safe }};
function yen(n) { return '¥' + n.toLocaleString(); }
fetch('/api/chart?name=' + encodeURIComponent(NAME)).then(r => r.json()).then(data => {
  const pts = data.points;
  if (!pts.length) return;

  const cur = pts[pts.length - 1].avg;
  const hi = Math.max(...pts.map(p => p.max));
  const lo = Math.min(...pts.map(p => p.min));
  const first = pts[0].avg;
  const pct = ((cur - first) / first * 100).toFixed(1);
  const pctCls = pct > 0 ? 'up' : pct < 0 ? 'down' : '';

  document.getElementById('stats').innerHTML =
    '<div class="stat"><div class="stat-label">現在</div><div class="stat-value">'+yen(cur)+'</div></div>'
    +'<div class="stat"><div class="stat-label">期間最高</div><div class="stat-value">'+yen(hi)+'</div></div>'
    +'<div class="stat"><div class="stat-label">期間変動</div><div class="stat-value '+pctCls+'">'+(pct>0?'+':'')+pct+'%</div></div>';

  const labels = pts.map(p => p.date);
  const yMin = Math.floor(lo * 0.9 / 1000) * 1000;
  const yMax = Math.ceil(hi * 1.05 / 1000) * 1000;

  new Chart(document.getElementById('priceChart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'High', data: pts.map(p => p.max), borderColor: 'transparent',
          backgroundColor: 'rgba(55,138,221,0.12)', fill: '+1', pointRadius: 0, tension: 0.3 },
        { label: 'Low', data: pts.map(p => p.min), borderColor: 'transparent',
          backgroundColor: 'transparent', fill: false, pointRadius: 0, tension: 0.3 },
        { label: 'Avg', data: pts.map(p => p.avg), borderColor: '#378ADD',
          backgroundColor: 'transparent', borderWidth: 2.5, pointRadius: 0,
          pointHoverRadius: 5, pointHoverBackgroundColor: '#378ADD', tension: 0.3 }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          filter: item => item.datasetIndex === 2,
          callbacks: {
            label: ctx => {
              const i = ctx.dataIndex;
              return ['Avg: ¥'+pts[i].avg.toLocaleString(), 'High: ¥'+pts[i].max.toLocaleString(), 'Low: ¥'+pts[i].min.toLocaleString()];
            }
          }
        }
      },
      scales: {
        x: { grid: { display: false }, ticks: { font: { size: 10 }, maxRotation: 45, autoSkip: true, maxTicksLimit: 12 } },
        y: { min: yMin, max: yMax, ticks: { callback: v => '¥'+(v/1000).toFixed(0)+'k', font: { size: 10 } },
             grid: { color: 'rgba(0,0,0,0.06)' } }
      }
    }
  });

  new Chart(document.getElementById('boxChart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{ data: pts.map(p => p.boxes || null), backgroundColor: 'rgba(55,138,221,0.25)',
                    borderRadius: 2, barPercentage: 0.85 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => ctx.raw ? ctx.raw.toLocaleString() + ' boxes' : 'N/A' } } },
      scales: {
        x: { grid: { display: false }, ticks: { font: { size: 10 }, maxRotation: 45, autoSkip: true, maxTicksLimit: 12 } },
        y: { ticks: { callback: v => (v/1000).toFixed(1)+'k', font: { size: 10 } },
             grid: { color: 'rgba(0,0,0,0.06)' }, min: 0 }
      }
    }
  });
});
</script>
</body></html>"""

# ─── Ranking page ───

RANKING_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>下落ランキング</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Hiragino Sans', sans-serif;
         background: #f5f5f5; padding: 12px; }
  h1 { font-size: 18px; color: #333; margin-bottom: 12px; }
  .nav { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
  .nav a { padding: 8px 14px; background: #2c3e50; color: white; border-radius: 8px;
           text-decoration: none; font-size: 13px; }
  .nav a.active { background: #e74c3c; }
  .tabs { display: flex; gap: 6px; margin-bottom: 12px; }
  .tab { padding: 8px 14px; background: white; border: 1px solid #ddd; border-radius: 8px;
         font-size: 13px; cursor: pointer; color: #555; }
  .tab.active { background: #c62828; color: white; border-color: #c62828; }
  .rank-item { background: white; border-radius: 10px; padding: 10px 14px;
               box-shadow: 0 1px 4px rgba(0,0,0,.06); margin-bottom: 6px;
               display: flex; justify-content: space-between; align-items: center;
               text-decoration: none; color: inherit; }
  .rank-item:active { background: #f9f9f9; }
  .rank-left { display: flex; align-items: center; gap: 10px; }
  .rank-num { font-size: 16px; font-weight: 700; color: #ccc; width: 24px; text-align: center; }
  .rank-num.top3 { color: #c62828; }
  .rank-name { font-size: 14px; font-weight: 600; color: #222; }
  .rank-price { font-size: 12px; color: #888; }
  .rank-right { text-align: right; }
  .rank-pct { font-size: 16px; font-weight: 700; }
  .rank-pct.down { color: #c62828; }
  .rank-pct.up { color: #2e7d32; }
</style>
</head>
<body>
<h1>📉 下落/上昇ランキング</h1>
<nav class="nav">
  <a href="/">検索</a>
  <a href="/dashboard">ダッシュボード</a>
  <a href="/ranking" class="active">下落ランキング</a>
  <a href="/portfolio">ポートフォリオ</a>
  <a href="/psa">PSA計算</a>
</nav>
<div class="tabs">
  <button class="tab" onclick="showPeriod('d1',this)">1日</button>
  <button class="tab active" onclick="showPeriod('d7',this)">7日</button>
  <button class="tab" onclick="showPeriod('d30',this)">30日</button>
</div>
<div id="list"></div>
<script>
let DATA = [];
function yen(n) { return '¥' + n.toLocaleString(); }
function showPeriod(key, el) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  if (el) el.classList.add('active');
  const sorted = DATA.filter(d => d[key] !== null && d[key] !== undefined).sort((a,b) => a[key] - b[key]);
  const list = document.getElementById('list');
  list.innerHTML = sorted.map((d, i) => {
    const pct = d[key];
    const cls = pct < 0 ? 'down' : pct > 0 ? 'up' : '';
    const sign = pct > 0 ? '+' : '';
    const numCls = i < 3 ? 'top3' : '';
    return '<a class="rank-item" href="/chart/'+encodeURIComponent(d.name)+'">'
      +'<div class="rank-left"><div class="rank-num '+numCls+'">'+(i+1)+'</div>'
      +'<div><div class="rank-name">'+d.name+'</div><div class="rank-price">'+yen(d.price)+'</div></div></div>'
      +'<div class="rank-right"><div class="rank-pct '+cls+'">'+sign+pct.toFixed(1)+'%</div></div>'
      +'</a>';
  }).join('');
}
fetch('/api/summary').then(r => r.json()).then(data => {
  DATA = data.items;
  showPeriod('d7', document.querySelector('.tab.active'));
});
</script>
</body></html>"""

# ─── Portfolio page ───

PORTFOLIO_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ポートフォリオ</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Hiragino Sans', sans-serif;
         background: #f5f5f5; padding: 12px; }
  h1 { font-size: 18px; color: #333; margin-bottom: 12px; }
  .nav { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
  .nav a { padding: 8px 14px; background: #2c3e50; color: white; border-radius: 8px;
           text-decoration: none; font-size: 13px; }
  .nav a.active { background: #e74c3c; }
  .summary { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-bottom: 16px; }
  .sum-card { background: white; border-radius: 10px; padding: 12px;
              box-shadow: 0 1px 4px rgba(0,0,0,.06); }
  .sum-label { font-size: 11px; color: #888; }
  .sum-value { font-size: 20px; font-weight: 700; color: #222; margin-top: 2px; }
  .sum-value.up { color: #2e7d32; }
  .sum-value.down { color: #c62828; }
  .add-form { background: white; border-radius: 10px; padding: 14px;
              box-shadow: 0 1px 4px rgba(0,0,0,.06); margin-bottom: 12px; }
  .form-title { font-size: 13px; font-weight: 600; color: #444; margin-bottom: 10px; }
  .form-row { display: flex; gap: 8px; margin-bottom: 8px; }
  .form-row select, .form-row input { flex: 1; padding: 10px; font-size: 14px;
    border: 1px solid #ddd; border-radius: 8px; }
  .form-row input { width: 80px; flex: none; }
  .btn-add { width: 100%; padding: 10px; background: #2c3e50; color: white; border: none;
             border-radius: 8px; font-size: 14px; cursor: pointer; }
  .holdings { margin-top: 12px; }
  .hold-item { background: white; border-radius: 10px; padding: 12px 14px;
               box-shadow: 0 1px 4px rgba(0,0,0,.06); margin-bottom: 8px; }
  .hold-top { display: flex; justify-content: space-between; align-items: baseline; }
  .hold-name { font-size: 14px; font-weight: 700; color: #222; }
  .hold-pnl { font-size: 16px; font-weight: 700; }
  .hold-pnl.up { color: #2e7d32; }
  .hold-pnl.down { color: #c62828; }
  .hold-detail { font-size: 12px; color: #888; margin-top: 4px; line-height: 1.6; }
  .hold-del { font-size: 12px; color: #e74c3c; cursor: pointer; margin-top: 4px;
              display: inline-block; }
</style>
</head>
<body>
<h1>💼 ポートフォリオ</h1>
<nav class="nav">
  <a href="/">検索</a>
  <a href="/dashboard">ダッシュボード</a>
  <a href="/ranking">下落ランキング</a>
  <a href="/portfolio" class="active">ポートフォリオ</a>
  <a href="/psa">PSA計算</a>
</nav>
<div class="summary" id="summary"></div>
<div class="add-form">
  <div class="form-title">保有BOXを追加</div>
  <div class="form-row">
    <select id="sel-name"></select>
  </div>
  <div class="form-row">
    <input type="number" id="inp-qty" placeholder="数量" min="1">
    <input type="number" id="inp-cost" placeholder="取得単価">
  </div>
  <button class="btn-add" onclick="addHolding()">追加</button>
</div>
<div class="holdings" id="holdings"></div>
<script>
const BOX_NAMES = {{ box_list | safe }};
let PRICES = {};
let holdings = JSON.parse(localStorage.getItem('pokeca_holdings') || '[]');

function save() { localStorage.setItem('pokeca_holdings', JSON.stringify(holdings)); }
function yen(n) { return '¥' + Math.round(n).toLocaleString(); }

function addHolding() {
  const name = document.getElementById('sel-name').value;
  const qty = parseInt(document.getElementById('inp-qty').value);
  const cost = parseInt(document.getElementById('inp-cost').value);
  if (!name || !qty || !cost) return;
  holdings.push({ name, qty, cost });
  save();
  document.getElementById('inp-qty').value = '';
  document.getElementById('inp-cost').value = '';
  render();
}

function delHolding(i) {
  holdings.splice(i, 1);
  save();
  render();
}

function render() {
  let totalCost = 0, totalValue = 0;
  const hDiv = document.getElementById('holdings');
  let html = '';
  holdings.forEach((h, i) => {
    const cur = PRICES[h.name] || 0;
    const value = cur * h.qty;
    const cost = h.cost * h.qty;
    const pnl = value - cost;
    const pnlPct = cost > 0 ? (pnl / cost * 100) : 0;
    totalCost += cost;
    totalValue += value;
    const cls = pnl >= 0 ? 'up' : 'down';
    const sign = pnl >= 0 ? '+' : '';
    html += '<div class="hold-item">'
      +'<div class="hold-top"><div class="hold-name">'+h.name+' x'+h.qty+'</div>'
      +'<div class="hold-pnl '+cls+'">'+sign+yen(pnl)+'</div></div>'
      +'<div class="hold-detail">'
      +'取得: '+yen(h.cost)+'/個 → 現在: '+yen(cur)+'/個<br>'
      +'投資額: '+yen(cost)+' → 評価額: '+yen(value)+' ('+sign+pnlPct.toFixed(1)+'%)'
      +'</div>'
      +'<span class="hold-del" onclick="delHolding('+i+')">🗑 削除</span>'
      +'</div>';
  });
  hDiv.innerHTML = html;

  const totalPnl = totalValue - totalCost;
  const totalPct = totalCost > 0 ? (totalPnl / totalCost * 100) : 0;
  const cls = totalPnl >= 0 ? 'up' : 'down';
  const sign = totalPnl >= 0 ? '+' : '';
  document.getElementById('summary').innerHTML =
    '<div class="sum-card"><div class="sum-label">投資総額</div><div class="sum-value">'+yen(totalCost)+'</div></div>'
    +'<div class="sum-card"><div class="sum-label">評価総額</div><div class="sum-value">'+yen(totalValue)+'</div></div>'
    +'<div class="sum-card"><div class="sum-label">含み損益</div><div class="sum-value '+cls+'">'+sign+yen(totalPnl)+'</div></div>'
    +'<div class="sum-card"><div class="sum-label">損益率</div><div class="sum-value '+cls+'">'+sign+totalPct.toFixed(1)+'%</div></div>';
}

window.onload = () => {
  const sel = document.getElementById('sel-name');
  BOX_NAMES.forEach(n => { const o = document.createElement('option'); o.value = n; o.textContent = n; sel.appendChild(o); });
  fetch('/api/summary').then(r => r.json()).then(data => {
    data.items.forEach(d => { PRICES[d.name] = d.price; });
    render();
  });
};
</script>
</body></html>"""

# ─── PSA gross-profit calculator page ───

PSA_CALC_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PSA粗利計算</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Hiragino Sans', sans-serif;
         background: #f5f5f5; padding: 12px; }
  h1 { font-size: 18px; color: #333; margin-bottom: 12px; }
  .nav { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; }
  .nav a { padding: 8px 14px; background: #2c3e50; color: white; border-radius: 8px;
           text-decoration: none; font-size: 13px; }
  .nav a.active { background: #e74c3c; }
  .form-card { background: white; border-radius: 12px; padding: 16px;
               box-shadow: 0 2px 8px rgba(0,0,0,.08); margin-bottom: 12px; }
  .field { margin-bottom: 12px; }
  .field label { display: block; font-size: 12px; color: #666; font-weight: 600;
                 margin-bottom: 4px; }
  .field input, .field select { width: 100%; padding: 12px; font-size: 16px;
    border: 1px solid #ddd; border-radius: 8px; outline: none; background: white; }
  .field input:focus, .field select:focus { border-color: #2c3e50; }
  .check-row { display: flex; align-items: center; gap: 8px; font-size: 13px;
               color: #555; margin: 4px 0 14px; cursor: pointer; }
  .check-row input { width: 18px; height: 18px; }
  .btn-reset { width: 100%; padding: 12px; background: #95a5a6; color: white;
               border: none; border-radius: 8px; font-size: 14px; cursor: pointer; }
  .btn-reset:active { background: #7f8c8d; }
  .summary { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-bottom: 12px; }
  .sum-card { background: white; border-radius: 10px; padding: 12px;
              box-shadow: 0 1px 4px rgba(0,0,0,.06); }
  .sum-label { font-size: 11px; color: #888; }
  .sum-value { font-size: 18px; font-weight: 700; color: #222; margin-top: 2px; }
  .result-card { background: white; border-radius: 12px; padding: 16px;
                 box-shadow: 0 2px 8px rgba(0,0,0,.08); margin-bottom: 12px; }
  .result-title { font-size: 13px; color: #666; font-weight: 600; margin-bottom: 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { background: #2c3e50; color: white; padding: 8px 6px; text-align: right; font-size: 11px; }
  th:first-child { text-align: left; }
  td { padding: 8px 6px; border-bottom: 1px solid #f0f0f0; text-align: right; }
  td:first-child { text-align: left; font-weight: 600; color: #555; }
  td.profit-plus { color: #2e7d32; font-weight: 700; }
  td.profit-minus { color: #c62828; font-weight: 700; }
  .note { font-size: 11px; color: #999; margin-top: 8px; line-height: 1.6; }
  .placeholder { text-align: center; color: #aaa; padding: 24px 0; font-size: 13px; }
</style>
</head>
<body>
<h1>🧮 PSA粗利計算</h1>
<nav class="nav">
  <a href="/">検索</a>
  <a href="/dashboard">ダッシュボード</a>
  <a href="/ranking">下落ランキング</a>
  <a href="/portfolio">ポートフォリオ</a>
  <a href="/psa" class="active">PSA計算</a>
</nav>
<div class="form-card">
  <div class="field">
    <label for="inp-cost">仕入れ単価（円/枚）</label>
    <input type="number" id="inp-cost" inputmode="numeric" min="0" placeholder="例: 10000">
  </div>
  <div class="field">
    <label for="inp-qty">仕入れ枚数</label>
    <input type="number" id="inp-qty" inputmode="numeric" min="1" placeholder="例: 10">
  </div>
  <div class="field">
    <label for="inp-rate">予想PSA10取得率（%）</label>
    <input type="number" id="inp-rate" inputmode="decimal" min="0" max="100" placeholder="例: 50">
  </div>
  <div class="field">
    <label for="sel-plan">鑑定料金/枚</label>
    <select id="sel-plan">
      <option value="regular">レギュラー（11,980円）</option>
      <option value="express">エクスプレス（22,980円）</option>
    </select>
  </div>
  <div class="field">
    <label for="inp-sell">PSA10販売価格（円）</label>
    <input type="number" id="inp-sell" inputmode="numeric" min="0" placeholder="例: 88000">
  </div>
  <div class="field">
    <label for="inp-raw">素体販売価格（PSA10非該当分、円/枚）</label>
    <input type="number" id="inp-raw" inputmode="numeric" min="0" placeholder="例: 35850">
  </div>
  <label class="check-row"><input type="checkbox" id="chk-rawfee">素体販売にも販売手数料10%を適用</label>
  <button class="btn-reset" onclick="resetAll()">リセット</button>
</div>
<div class="summary" id="summary"></div>
<div class="result-card">
  <div class="result-title">PSA10販売価格シナリオ別 粗利・利益率（素体販売込み）</div>
  <div id="result"></div>
  <div class="note">
    合計粗利 = PSA10枚数 × PSA10販売価格 × 0.9 + 素体枚数 × 素体販売価格 − 総原価<br>
    PSA10枚数 = 枚数 × 取得率、素体枚数 = 枚数 × (1 − 取得率)<br>
    総原価 = (仕入れ単価 + 鑑定料) × 枚数（鑑定料は全枚数に発生）<br>
    利益率 = 合計粗利 ÷ 総投資額。満額/9割/8割/7割はPSA10販売価格が下振れした場合のシミュレーションです
  </div>
</div>
<script>
const PLAN_FEES = { regular: 11980, express: 22980 };
const SCENARIOS = [
  { label: '満額', ratio: 1.0 },
  { label: '9割',  ratio: 0.9 },
  { label: '8割',  ratio: 0.8 },
  { label: '7割',  ratio: 0.7 },
];
const INPUT_IDS = ['inp-cost', 'inp-qty', 'inp-rate', 'inp-sell', 'inp-raw'];

function yen(n) {
  const r = Math.round(n);
  return (r < 0 ? '-¥' : '¥') + Math.abs(r).toLocaleString();
}
function cnt(n) {
  const v = Math.round(n * 100) / 100;
  return Number.isInteger(v) ? v.toString() : v.toFixed(1);
}
function pct(n) {
  return (n >= 0 ? '+' : '') + n.toFixed(1) + '%';
}

function calc() {
  const cost = parseFloat(document.getElementById('inp-cost').value);
  const qty  = parseFloat(document.getElementById('inp-qty').value);
  const rate = parseFloat(document.getElementById('inp-rate').value);
  const sell = parseFloat(document.getElementById('inp-sell').value);
  const rawIn = parseFloat(document.getElementById('inp-raw').value);
  const raw  = isNaN(rawIn) ? 0 : rawIn;
  const fee  = PLAN_FEES[document.getElementById('sel-plan').value];
  const rawFee = document.getElementById('chk-rawfee').checked ? 0.9 : 1.0;

  const sumDiv = document.getElementById('summary');
  const resDiv = document.getElementById('result');

  if ([cost, qty, rate, sell].some(v => isNaN(v)) || qty <= 0 || rate < 0 || rate > 100) {
    sumDiv.innerHTML = '';
    resDiv.innerHTML = '<div class="placeholder">仕入れ単価・枚数・取得率・PSA10販売価格を入力すると自動計算されます</div>';
    return;
  }

  const r = rate / 100;
  const n10 = qty * r;
  const nRaw = qty * (1 - r);
  const invest = (cost + fee) * qty;
  const rawRevenue = nRaw * raw * rawFee;
  const breakeven = n10 > 0 ? (invest - rawRevenue) / (n10 * 0.9) : null;
  const beText = breakeven === null ? '—'
    : breakeven <= 0 ? '素体回収で黒字' : yen(breakeven);

  sumDiv.innerHTML =
    '<div class="sum-card"><div class="sum-label">総投資額（仕入れ+鑑定料）</div><div class="sum-value">'+yen(invest)+'</div></div>'
    +'<div class="sum-card"><div class="sum-label">枚数内訳（PSA10 / 素体）</div><div class="sum-value">'+cnt(n10)+' / '+cnt(nRaw)+'枚</div></div>'
    +'<div class="sum-card"><div class="sum-label">素体回収額</div><div class="sum-value">'+yen(rawRevenue)+'</div></div>'
    +'<div class="sum-card"><div class="sum-label">損益分岐PSA10価格</div><div class="sum-value">'+beText+'</div></div>';

  const rows = SCENARIOS.map(s => {
    const price = sell * s.ratio;
    const psaRevenue = n10 * price * 0.9;
    const total = psaRevenue + rawRevenue - invest;
    const roi = invest > 0 ? total / invest * 100 : 0;
    const cls = total >= 0 ? 'profit-plus' : 'profit-minus';
    const sign = total >= 0 ? '+' : '';
    return '<tr><td>'+s.label+'</td><td>'+yen(price)+'</td>'
      +'<td class="'+cls+'">'+sign+yen(total)+'</td>'
      +'<td class="'+cls+'">'+pct(roi)+'</td></tr>';
  }).join('');

  resDiv.innerHTML = '<table><thead><tr><th>シナリオ</th><th>PSA10販売価格</th>'
    +'<th>合計粗利</th><th>利益率</th></tr></thead><tbody>'+rows+'</tbody></table>';
}

function resetAll() {
  if (!confirm('リセットしますか？')) return;
  INPUT_IDS.forEach(id => { document.getElementById(id).value = ''; });
  document.getElementById('sel-plan').value = 'regular';
  document.getElementById('chk-rawfee').checked = false;
  calc();
}

INPUT_IDS.forEach(id => document.getElementById(id).addEventListener('input', calc));
document.getElementById('sel-plan').addEventListener('change', calc);
document.getElementById('chk-rawfee').addEventListener('change', calc);
calc();
</script>
</body></html>"""


# ─── Fetch helpers (existing) ───

def fetch_offers(url: str):
    """v1/apparels/{id}/sizes API から各サイズ最安値を取得。
    スニダンがJSON-LD構造化データを削除したためAPI方式に移行。"""
    m = re.search(r"/apparels/(\d+)", url)
    if not m:
        return "不明", []
    product_id  = m.group(1)
    api_url     = f"https://snkrdunk.com/v1/apparels/{product_id}/sizes"
    api_headers = {**HEADERS, "Accept": "application/json",
                   "Referer": f"https://snkrdunk.com/apparels/{product_id}"}

    resp = requests.get(api_url, headers=api_headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    # 商品名をページ <title> から取得（取得できなければ「不明」）
    product_name = "不明"
    try:
        page = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(page.text, "html.parser")
        title = soup.find("title")
        if title and title.string:
            raw = title.string.split("｜")[0]
            raw = re.split(r"(通販|買取|相場|のフィギュア)", raw)[0]
            product_name = raw.strip()
    except Exception:
        pass

    offers = []
    for sp in data.get("sizePrices", []):
        size_name = sp["size"]["localizedName"]   # "1個", "2個", ...
        qty_m = re.search(r"(\d+)", size_name)
        if not qty_m:
            continue
        qty   = int(qty_m.group(1))
        price = sp.get("minListingPrice", 0)
        if price <= 0 or qty <= 0:
            continue
        offers.append({"qty": qty, "price": price, "unit": (price + SHIPPING) / qty})

    offers.sort(key=lambda x: x["qty"])
    return product_name, offers


def fetch_total_boxes(url: str) -> int:
    m = re.search(r"/apparels/(\d+)", url)
    if not m:
        return 0
    product_id = m.group(1)
    api_url = f"https://snkrdunk.com/v1/apparels/{product_id}/sizes"
    try:
        resp = requests.get(api_url, headers={**HEADERS, "Accept": "application/json",
                            "Referer": url}, timeout=10)
        resp.raise_for_status()
        sizes = resp.json().get("sizePrices", [])
        total = 0
        for s in sizes:
            qty_m = re.search(r"(\d+)", s["size"]["localizedName"])
            if qty_m:
                total += int(qty_m.group(1)) * s.get("listingItemCount", 0)
        return total
    except Exception:
        return 0


# ─── Routes ───

@app.route("/")
def index():
    box_list = [{"name": k, "img": BOX_IMAGES.get(k, "")}
                for k in WATCHLIST_ORDER if k in BOX_URLS]
    return render_template_string(LOOKUP_HTML, box_list=json.dumps(box_list, ensure_ascii=False))


@app.route("/api/lookup")
def lookup():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "BOX名を入力してください"})
    if name in BOX_URLS:
        matched = {name: BOX_URLS[name]}
    else:
        matched = {k: v for k, v in BOX_URLS.items() if name in k}
    if not matched:
        candidates = [k for k in BOX_URLS if any(c in k for c in name)]
        msg = f"「{name}」が見つかりません"
        if candidates:
            msg += f"。候補: {', '.join(candidates[:5])}"
        return jsonify({"error": msg})
    box_name, url = next(iter(matched.items()))
    try:
        product_name, offers = fetch_offers(url)
        total_boxes = fetch_total_boxes(url)
    except Exception as e:
        return jsonify({"error": f"データ取得失敗: {e}"})
    if not offers:
        return jsonify({"error": f"「{box_name}」の出品データが取得できませんでした"})
    return jsonify({
        "name": box_name,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "offers": offers,
        "total_boxes": total_boxes,
    })


@app.route("/dashboard")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/summary")
def api_summary():
    items = _build_summary()
    snaps = get_snapshots()
    last = snaps[-1]["ts"] if snaps else "—"
    return jsonify({"items": items, "last_snapshot": last})


@app.route("/chart/<name>")
def chart_page(name: str):
    return render_template_string(CHART_HTML, name=name,
                                  name_json=json.dumps(name, ensure_ascii=False))


@app.route("/api/chart")
def api_chart():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"})
    points = _build_chart_data(name)
    return jsonify({"name": name, "points": points})


@app.route("/ranking")
def ranking():
    return render_template_string(RANKING_HTML)


@app.route("/portfolio")
def portfolio():
    box_list = [k for k in WATCHLIST_ORDER if k in BOX_URLS]
    return render_template_string(PORTFOLIO_HTML,
                                  box_list=json.dumps(box_list, ensure_ascii=False))


@app.route("/api/box_history")
def api_box_history():
    name = request.args.get("name", "").strip()
    if name not in BOX_URLS:
        return jsonify({"error": f"「{name}」は対応BOX一覧にありません"}), 404
    return jsonify(_box_history(name))


@app.route("/psa")
def psa_calc():
    return render_template_string(PSA_CALC_HTML)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
