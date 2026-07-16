import json
import re
from datetime import datetime
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
SNAPSHOT_DIR = BASE_DIR / "snapshots"
SNAPSHOT_CACHE_FILE = BASE_DIR / "snapshots_cache.json"

WATCHLIST_ORDER = [
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
                    light[name] = {
                        "best_avg": entry["best_avg"],
                        "total_boxes": entry.get("total_boxes", 0),
                        "inventory": entry.get("inventory", 0),
                    }
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
            daily[day] = {"prices": [], "boxes": []}
        daily[day]["prices"].append(entry["best_avg"])
        daily[day]["boxes"].append(entry.get("total_boxes", 0))

    result = []
    for day in sorted(daily.keys()):
        prices = daily[day]["prices"]
        boxes = daily[day]["boxes"]
        result.append({
            "date": day[5:],
            "avg": round(sum(prices) / len(prices)),
            "min": round(min(prices)),
            "max": round(max(prices)),
            "boxes": max(boxes),
        })
    return result


# ─── Lookup page (existing) ───

LOOKUP_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ポケカBOX 最安値検索</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Hiragino Sans', sans-serif;
         background: #f5f5f5; padding: 16px; }
  h1 { font-size: 18px; color: #333; margin-bottom: 16px; }
  .nav { display: flex; gap: 6px; margin-bottom: 16px; flex-wrap: wrap; }
  .nav a { padding: 8px 14px; background: #2c3e50; color: white; border-radius: 8px;
           text-decoration: none; font-size: 13px; }
  .nav a.active { background: #e74c3c; }
  .search-box { display: flex; gap: 8px; margin-bottom: 12px; }
  input { flex: 1; padding: 12px; font-size: 16px; border: 1px solid #ddd;
          border-radius: 8px; outline: none; }
  button { padding: 12px 20px; background: #e74c3c; color: white;
           border: none; border-radius: 8px; font-size: 16px; cursor: pointer; }
  .hint { font-size: 12px; color: #888; margin-bottom: 16px; line-height: 1.6; }
  .card { background: white; border-radius: 12px; padding: 16px;
          box-shadow: 0 2px 8px rgba(0,0,0,.08); margin-bottom: 16px; }
  .card-title { font-size: 16px; font-weight: 700; color: #222; margin-bottom: 4px; }
  .card-meta { font-size: 12px; color: #888; margin-bottom: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { background: #2c3e50; color: white; padding: 8px 6px; text-align: right; font-size: 12px; }
  th:first-child { text-align: left; }
  td { padding: 7px 6px; border-bottom: 1px solid #f0f0f0; text-align: right; }
  td:first-child { text-align: left; }
  tr.best td { color: #e74c3c; font-weight: 700; }
  tr.second td { color: #e67e22; font-weight: 600; }
  tr.third td { color: #f1c40f; font-weight: 600; }
  .legend { font-size: 11px; color: #888; margin-top: 8px; }
  .total { font-size: 12px; color: #555; margin-top: 6px; }
  .error { background: #fff3f3; border: 1px solid #ffccc7; border-radius: 8px;
           padding: 12px; color: #c0392b; font-size: 14px; }
  .loading { text-align: center; color: #888; padding: 20px; }
  .list-section { margin-bottom: 20px; }
  .list-title { font-size: 13px; color: #666; font-weight: 600; margin-bottom: 8px; }
  .list-grid { display: flex; flex-wrap: wrap; gap: 6px; }
  .list-item { background: white; border: 1px solid #ddd; border-radius: 6px;
               padding: 6px 10px; font-size: 13px; cursor: pointer; color: #333; }
  .list-item:hover { background: #f9f9f9; }
</style>
</head>
<body>
<h1>🃏 ポケカBOX 最安値検索</h1>
<nav class="nav">
  <a href="/" class="active">検索</a>
  <a href="/dashboard">ダッシュボード</a>
  <a href="/ranking">下落ランキング</a>
  <a href="/portfolio">ポートフォリオ</a>
  <a href="/psa">PSA計算</a>
</nav>
<div class="search-box">
  <input type="text" id="q" placeholder="BOX名を入力（例：メガドリーム）" autocomplete="off">
  <button onclick="search()">検索</button>
</div>
<p class="hint">スニダンのリアルタイム価格を取得します（10〜20秒かかります）</p>
<div class="list-section">
  <div class="list-title">📦 対応BOX一覧</div>
  <div class="list-grid" id="box-list"></div>
</div>
<div id="result"></div>
<script>
const BOX_LIST = {{ box_list | safe }};
window.onload = () => {
  const grid = document.getElementById('box-list');
  BOX_LIST.forEach(name => {
    const el = document.createElement('div');
    el.className = 'list-item';
    el.textContent = name;
    el.onclick = () => { document.getElementById('q').value = name; search(); };
    grid.appendChild(el);
  });
};
async function search() {
  const q = document.getElementById('q').value.trim();
  if (!q) return;
  const res = document.getElementById('result');
  res.innerHTML = '<div class="loading">🔍 スニダンからデータ取得中...</div>';
  try {
    const resp = await fetch('/api/lookup?name=' + encodeURIComponent(q));
    const data = await resp.json();
    if (data.error) { res.innerHTML = '<div class="error">⚠️ ' + data.error + '</div>'; return; }
    res.innerHTML = renderCard(data);
  } catch(e) { res.innerHTML = '<div class="error">⚠️ 通信エラーが発生しました</div>'; }
}
function yen(n) { return '¥' + n.toLocaleString(); }
function renderCard(d) {
  const sorted = [...d.offers].sort((a,b) => a.unit - b.unit);
  const ranks = {};
  sorted.slice(0,3).forEach((o,i) => ranks[o.qty] = ['🔴','🟠','🟡'][i]);
  const bestQty = sorted[0]?.qty;
  let rows = d.offers.map(o => {
    const medal = ranks[o.qty] || '';
    const cls = o.qty === sorted[0]?.qty ? 'best' : o.qty === sorted[1]?.qty ? 'second' : o.qty === sorted[2]?.qty ? 'third' : '';
    const marker = o.qty === bestQty ? ' ← 最安' : '';
    return '<tr class="'+cls+'"><td>'+medal+' '+o.qty+'個'+marker+'</td><td>'+yen(o.price)+'</td><td>'+yen(Math.round(o.unit))+'</td></tr>';
  }).join('');
  return '<div class="card"><div class="card-title">'+d.name+'</div><div class="card-meta">取得時刻: '+d.fetched_at+'</div><table><thead><tr><th>個数</th><th>出品最安値</th><th>送料込単価</th></tr></thead><tbody>'+rows+'</tbody></table><div class="legend">🔴 最安 🟠 2番目 🟡 3番目</div><div class="total">市場総箱数: '+d.total_boxes.toLocaleString()+'箱</div></div>';
}
document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') search(); });
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
    box_list = [k for k in WATCHLIST_ORDER if k in BOX_URLS]
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


@app.route("/psa")
def psa_calc():
    return render_template_string(PSA_CALC_HTML)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
