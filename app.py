import json
import re
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

BOX_URLS: dict[str, str] = json.loads(
    (Path(__file__).parent / "box_urls.json").read_text(encoding="utf-8")
)

HTML = """<!DOCTYPE html>
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
    if (data.error) {
      res.innerHTML = '<div class="error">⚠️ ' + data.error + '</div>';
      return;
    }
    res.innerHTML = renderCard(data);
  } catch(e) {
    res.innerHTML = '<div class="error">⚠️ 通信エラーが発生しました</div>';
  }
}

function yen(n) { return '¥' + n.toLocaleString(); }

function renderCard(d) {
  const sorted = [...d.offers].sort((a,b) => a.unit - b.unit);
  const ranks = {};
  sorted.slice(0,3).forEach((o,i) => ranks[o.qty] = ['🔴','🟠','🟡'][i]);
  const bestQty = sorted[0]?.qty;

  let rows = d.offers.map(o => {
    const medal = ranks[o.qty] || '';
    const cls = o.qty === sorted[0]?.qty ? 'best'
              : o.qty === sorted[1]?.qty ? 'second'
              : o.qty === sorted[2]?.qty ? 'third' : '';
    const marker = o.qty === bestQty ? ' ← 最安' : '';
    return `<tr class="${cls}">
      <td>${medal} ${o.qty}個${marker}</td>
      <td>${yen(o.price)}</td>
      <td>${yen(Math.round(o.unit))}</td>
    </tr>`;
  }).join('');

  return `<div class="card">
    <div class="card-title">${d.name}</div>
    <div class="card-meta">取得時刻: ${d.fetched_at}</div>
    <table>
      <thead><tr><th>個数</th><th>出品最安値</th><th>送料込単価</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="legend">🔴 最安 🟠 2番目 🟡 3番目</div>
    <div class="total">市場総箱数: ${d.total_boxes.toLocaleString()}箱</div>
  </div>`;
}

document.getElementById('q').addEventListener('keydown', e => {
  if (e.key === 'Enter') search();
});
</script>
</body>
</html>"""


def fetch_offers(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    offers = []
    product_name = "不明"

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        if isinstance(ld, dict) and "@graph" in ld:
            ld = ld["@graph"]
        if isinstance(ld, dict):
            ld = [ld]
        for item in ld:
            if item.get("@type") == "Product":
                product_name = item.get("name", product_name)
            raw_offers = item.get("offers", [])
            if isinstance(raw_offers, dict):
                inner = raw_offers.get("offers", [])
                raw_offers = inner if inner else [raw_offers]
            for offer in raw_offers:
                price = offer.get("price")
                qty = None
                eq = offer.get("eligibleQuantity", {})
                if isinstance(eq, dict):
                    qty = eq.get("value")
                if qty is None:
                    for key in ("name", "size", "sku", "description"):
                        m = re.search(r"(\d+)\s*個", str(offer.get(key, "")))
                        if m:
                            qty = m.group(1)
                            break
                if price is None or qty is None:
                    continue
                try:
                    price, qty = int(float(price)), int(float(qty))
                except (ValueError, TypeError):
                    continue
                if qty > 0:
                    offers.append({
                        "qty": qty,
                        "price": price,
                        "unit": (price + SHIPPING) / qty,
                    })
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


@app.route("/")
def index():
    box_list = [k for k in WATCHLIST_ORDER if k in BOX_URLS]
    return render_template_string(HTML, box_list=json.dumps(box_list, ensure_ascii=False))


@app.route("/api/lookup")
def lookup():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "BOX名を入力してください"})

    # 完全一致を優先、なければ部分一致
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

    from datetime import datetime
    return jsonify({
        "name": box_name,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "offers": offers,
        "total_boxes": total_boxes,
    })


if __name__ == "__main__":
    app.run(debug=True)
