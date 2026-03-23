"""
build_group_cache.py
====================
全店舗のグループ名（a〜z）を特定し store_groups.json に保存する。
使い捨てスクリプト。mcd_scraper.py の初回実行前に一度だけ実行する。

仕組み:
  1. POI APIで全都道府県の店舗リストを取得
  2. 各店舗に対して a〜z を順番に試行
  3. 200 OK が返った時点でレスポンス内の catRootUrl からグループ名を抽出
  4. store_groups.json に { "store_key": "group_letter" } 形式で保存

実行方法:
  python3 build_group_cache.py

所要時間の目安:
  3000店舗 × 平均3試行 × 2秒 = 約5時間（一晩放置推奨）
  途中で止めても次回実行時に resume する（既存エントリはスキップ）
"""

import json
import logging
import random
import string
import time
from datetime import datetime
from pathlib import Path

import requests

# ============================================================
# 設定
# ============================================================
BASE_DIR    = Path(__file__).parent
OUTPUT_FILE = BASE_DIR / "store_groups.json"
LOG_DIR     = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"build_group_{datetime.now():%Y%m%d}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

POI_URL      = "https://map.mcdonalds.co.jp/api/poi"
STORE_URL    = "https://data.cat.group-{group}.prod.mop.mcd.qorcommerce.com/{store_key}.json"
SLEEP_PER_REQ = 2.0   # 1リクエストあたりのスリープ秒数

# 頻出グループ (f, g, h, i, j) を先頭に配置。ただし全26文字を必ず最後まで試行する。
# 打ち切りロジックなし：存在するかもしれないグループを見落とすリスクを排除。
# 完了後に store_groups.json を集計してグループ分けの法則性を分析できる。
GROUPS = ['f', 'g', 'h', 'i', 'j', 'k', 'e', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z', 'a', 'b', 'c', 'd']

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json",
}

PREFECTURE_CONFIG = {
    "01": "41.35,139.30,45.55,145.85", "02": "40.20,139.75,41.55,141.70",
    "03": "38.75,140.65,40.45,142.10", "04": "37.75,140.25,39.00,141.70",
    "05": "39.00,139.70,40.50,141.20", "06": "37.75,139.55,39.00,140.80",
    "07": "36.75,139.10,37.95,141.05", "08": "35.70,139.65,36.80,140.85",
    "09": "36.20,139.35,37.15,140.30", "10": "36.10,138.40,37.00,139.70",
    "11": "35.75,138.70,36.25,139.90", "12": "34.90,139.70,35.95,140.90",
    "13": "35.50,139.55,35.85,139.90", "14": "35.15,139.10,35.60,139.75",
    "15": "36.75,137.65,38.55,139.60", "16": "36.40,136.80,37.00,137.70",
    "17": "36.10,136.20,37.55,137.35", "18": "35.45,135.65,36.30,136.80",
    "19": "35.25,138.35,35.95,139.25", "20": "35.15,137.30,37.00,138.85",
    "21": "35.10,136.20,36.45,137.70", "22": "34.55,137.45,35.70,139.15",
    "23": "34.75,136.70,35.35,137.20", "24": "33.75,135.85,35.00,136.90",
    "25": "34.80,135.85,35.65,136.35", "26": "34.65,135.05,35.80,135.95",
    "27": "34.55,135.35,34.85,135.65", "28": "34.15,134.25,35.70,135.55",
    "29": "33.85,135.60,34.75,136.25", "30": "33.40,135.05,34.30,135.95",
    "31": "35.00,133.20,35.60,134.30", "32": "34.20,131.65,35.55,133.45",
    "33": "34.45,133.20,35.20,134.30", "34": "34.05,131.85,35.20,133.35",
    "35": "33.70,130.85,34.75,132.25", "36": "33.50,133.65,34.35,134.80",
    "37": "34.00,133.45,34.45,134.35", "38": "32.85,132.15,34.20,133.70",
    "39": "32.70,132.55,33.90,134.30", "40": "33.00,130.10,34.25,131.20",
    "41": "33.00,129.65,33.60,130.55", "42": "32.55,128.55,34.75,130.30",
    "43": "32.05,130.05,33.20,131.35", "44": "32.75,130.90,33.70,132.10",
    "45": "31.35,130.65,32.85,132.00", "46": "30.00,129.25,32.30,131.25",
    "47": "24.00,122.90,27.10,131.35",
}

# ============================================================
# POI取得
# ============================================================
def fetch_all_store_keys() -> list[str]:
    """全都道府県のPOIからstore_keyを重複なく取得する。"""
    keys = set()
    for code, bounds in PREFECTURE_CONFIG.items():
        try:
            resp = requests.get(POI_URL, headers=HEADERS, params={"bounds": bounds}, timeout=15)
            if resp.status_code == 200:
                pois = resp.json()
                for poi in pois:
                    k = poi.get("key")
                    if k:
                        keys.add(k)
                log.info(f"  {code}: {len(pois)}店舗")
            else:
                log.warning(f"  {code}: HTTP {resp.status_code}")
        except Exception as e:
            log.warning(f"  {code}: エラー {e}")
        time.sleep(1.0)

    log.info(f"合計 {len(keys)} 店舗のstore_keyを取得")
    return sorted(keys)

# ============================================================
# グループ特定
# ============================================================
def find_group(store_key: str) -> tuple[str | None, dict | None]:
    """
    a〜zを順番に試して200が返ったグループを特定する。
    レスポンス内のcatRootUrlからグループ名を確認して返す。
    戻り値: (group_letter, store_data) or (None, None)
    """
    for group in GROUPS:
        url = STORE_URL.format(group=group, store_key=store_key)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                # レスポンス内のcatRootUrlからグループ名を確認
                cat_url = data.get("store", {}).get("api", {}).get("catRootUrl", "")
                confirmed = None
                if "group-" in cat_url:
                    import re
                    m = re.search(r"group-([a-z])\.", cat_url)
                    if m:
                        confirmed = m.group(1)
                result_group = confirmed or group
                log.info(f"    {store_key}: group-{result_group} (試行: {group})")
                return result_group, data
            elif resp.status_code == 404:
                pass  # 次のグループを試す
            elif resp.status_code == 429:
                log.warning(f"    {store_key}: 429 Too Many Requests - 60秒待機")
                time.sleep(60)
            else:
                log.warning(f"    {store_key}: HTTP {resp.status_code} (group-{group})")
        except requests.Timeout:
            pass
        except Exception as e:
            log.warning(f"    {store_key}: 例外 {e} (group-{group})")

        time.sleep(SLEEP_PER_REQ)

    log.warning(f"    {store_key}: グループ特定失敗（a〜z全滅 / モバイルオーダー非対応の可能性）")
    return None, None

# ============================================================
# メイン
# ============================================================
def main():
    log.info("===== build_group_cache.py 開始 =====")

    # 既存キャッシュ読み込み（resume対応）
    cache: dict = {}
    if OUTPUT_FILE.exists():
        cache = json.loads(OUTPUT_FILE.read_text())
        log.info(f"既存キャッシュ: {len(cache)}件")

    # POIから全store_key取得
    log.info("全都道府県のPOIを取得中...")
    all_keys = fetch_all_store_keys()

    # 未処理の店舗のみ対象
    pending = [k for k in all_keys if k not in cache]
    log.info(f"未処理: {len(pending)}件 / 全{len(all_keys)}件")

    for i, store_key in enumerate(pending, 1):
        log.info(f"[{i}/{len(pending)}] {store_key} のグループを探索中...")
        group, data = find_group(store_key)

        if group:
            cache[store_key] = group
        else:
            cache[store_key] = None  # 失敗もNoneとして記録（再試行しない）

        # 100件ごとに中間保存
        if i % 100 == 0:
            OUTPUT_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
            log.info(f"中間保存: {i}件処理済み")

    OUTPUT_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    found = sum(1 for v in cache.values() if v is not None)
    log.info(f"完了: {found}/{len(cache)}件のグループを特定 → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()