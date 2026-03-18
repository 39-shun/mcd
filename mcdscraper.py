"""
McDonald's Japan Regional Pricing Scraper - 最終版
================================================
使い方:
  python3 mcd_scraper.py            # フルモード（新県 + リトライ）
  python3 mcd_scraper.py --mode=full   # 同上
  python3 mcd_scraper.py --mode=retry  # リトライのみ（18時実行用）

cron設定例（ラズパイ）:
  0 15 * * * /usr/bin/python3 /home/pi/mcd/mcd_scraper.py --mode=full
  0 18 * * * /usr/bin/python3 /home/pi/mcd/mcd_scraper.py --mode=retry

フロントエンド用注釈文（サイトフッター等に掲載推奨）:
  JA: "掲載価格は自動巡回プログラムにより定期的に収集しています。
       全国約3,000店舗を約24日かけて一巡し、毎月更新します。
       実際の価格と異なる場合があります。最新情報は店舗またはマクドナルド公式アプリでご確認ください。"
  EN: "Prices are collected automatically via a scheduled scraper that
       cycles through all ~3,000 locations nationwide approximately every 24 days.
       Actual prices may differ. Please verify with the store or McDonald's official app."
"""

import argparse
import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path

import requests

# ============================================================
# ★ ここを変えるだけで運用速度を調整できる
# ============================================================
BATCH_COUNT = 2   # 1日に処理する県数。急ぎたいときは5〜7に増やす。
                  # 47 ÷ BATCH_COUNT = 全国一周にかかる日数の目安
                  # 例: 2 → 約24日, 5 → 約10日, 7 → 約7日

# ============================================================
# パス設定
# ============================================================

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR  = BASE_DIR / "logs"
STATE_FILE  = BASE_DIR / "last_run.json"
FAILED_FILE = BASE_DIR / "failed_stores.json"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"scraper_{datetime.now():%Y%m%d}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ============================================================
# API設定
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json",
}

POI_URL    = "https://map.mcdonalds.co.jp/api/poi"
MENU_URL   = "https://map.mcdonalds.co.jp/api/order/{store_key}/menu.json"
BIG_MAC_CODE  = "1215"
MAX_RETRY_COUNT = 3

# ============================================================
# 設備フラグ（condition_values インデックスマッピング）
# ============================================================

CONDITION_KEYS = {
    0:  "is_24h",
    1:  "drive_thru",
    2:  "delivery",
    3:  "breakfast",
    4:  "mccafe",
    5:  "park_and_go",
    6:  "playland",
    7:  "table_service",
    8:  "hospitality_leader",
    9:  "mobile_order",
    10: "free_wifi",
    11: "parking",
    12: "contactless_delivery",
    13: "birthday_party",
    14: "toy_recycle",
    # 15〜19: 内部システム用（raw_conditionsに保持のみ）
}

# ============================================================
# 都道府県設定（JIS X 0401準拠）
# ============================================================

PREFECTURE_CONFIG = {
    "01": {"name": "Hokkaido",  "bounds": "41.35,139.30,45.55,145.85"},
    "02": {"name": "Aomori",    "bounds": "40.20,139.75,41.55,141.70"},
    "03": {"name": "Iwate",     "bounds": "38.75,140.65,40.45,142.10"},
    "04": {"name": "Miyagi",    "bounds": "37.75,140.25,39.00,141.70"},
    "05": {"name": "Akita",     "bounds": "39.00,139.70,40.50,141.20"},
    "06": {"name": "Yamagata",  "bounds": "37.75,139.55,39.00,140.80"},
    "07": {"name": "Fukushima", "bounds": "36.75,139.10,37.95,141.05"},
    "08": {"name": "Ibaraki",   "bounds": "35.70,139.65,36.80,140.85"},
    "09": {"name": "Tochigi",   "bounds": "36.20,139.35,37.15,140.30"},
    "10": {"name": "Gunma",     "bounds": "36.10,138.40,37.00,139.70"},
    "11": {"name": "Saitama",   "bounds": "35.75,138.70,36.25,139.90"},
    "12": {"name": "Chiba",     "bounds": "34.90,139.70,35.95,140.90"},
    "13": {"name": "Tokyo",     "bounds": "35.50,139.55,35.85,139.90"},
    "14": {"name": "Kanagawa",  "bounds": "35.15,139.10,35.60,139.75"},
    "15": {"name": "Niigata",   "bounds": "36.75,137.65,38.55,139.60"},
    "16": {"name": "Toyama",    "bounds": "36.40,136.80,37.00,137.70"},
    "17": {"name": "Ishikawa",  "bounds": "36.10,136.20,37.55,137.35"},
    "18": {"name": "Fukui",     "bounds": "35.45,135.65,36.30,136.80"},
    "19": {"name": "Yamanashi", "bounds": "35.25,138.35,35.95,139.25"},
    "20": {"name": "Nagano",    "bounds": "35.15,137.30,37.00,138.85"},
    "21": {"name": "Gifu",      "bounds": "35.10,136.20,36.45,137.70"},
    "22": {"name": "Shizuoka",  "bounds": "34.55,137.45,35.70,139.15"},
    "23": {"name": "Aichi",     "bounds": "34.75,136.70,35.35,137.20"},
    "24": {"name": "Mie",       "bounds": "33.75,135.85,35.00,136.90"},
    "25": {"name": "Shiga",     "bounds": "34.80,135.85,35.65,136.35"},
    "26": {"name": "Kyoto",     "bounds": "34.65,135.05,35.80,135.95"},
    "27": {"name": "Osaka",     "bounds": "34.55,135.35,34.85,135.65"},
    "28": {"name": "Hyogo",     "bounds": "34.15,134.25,35.70,135.55"},
    "29": {"name": "Nara",      "bounds": "33.85,135.60,34.75,136.25"},
    "30": {"name": "Wakayama",  "bounds": "33.40,135.05,34.30,135.95"},
    "31": {"name": "Tottori",   "bounds": "35.00,133.20,35.60,134.30"},
    "32": {"name": "Shimane",   "bounds": "34.20,131.65,35.55,133.45"},
    "33": {"name": "Okayama",   "bounds": "34.45,133.20,35.20,134.30"},
    "34": {"name": "Hiroshima", "bounds": "34.05,131.85,35.20,133.35"},
    "35": {"name": "Yamaguchi", "bounds": "33.70,130.85,34.75,132.25"},
    "36": {"name": "Tokushima", "bounds": "33.50,133.65,34.35,134.80"},
    "37": {"name": "Kagawa",    "bounds": "34.00,133.45,34.45,134.35"},
    "38": {"name": "Ehime",     "bounds": "32.85,132.15,34.20,133.70"},
    "39": {"name": "Kochi",     "bounds": "32.70,132.55,33.90,134.30"},
    "40": {"name": "Fukuoka",   "bounds": "33.00,130.10,34.25,131.20"},
    "41": {"name": "Saga",      "bounds": "33.00,129.65,33.60,130.55"},
    "42": {"name": "Nagasaki",  "bounds": "32.55,128.55,34.75,130.30"},
    "43": {"name": "Kumamoto",  "bounds": "32.05,130.05,33.20,131.35"},
    "44": {"name": "Oita",      "bounds": "32.75,130.90,33.70,132.10"},
    "45": {"name": "Miyazaki",  "bounds": "31.35,130.65,32.85,132.00"},
    "46": {"name": "Kagoshima", "bounds": "30.00,129.25,32.30,131.25"},
    "47": {"name": "Okinawa",   "bounds": "24.00,122.90,27.10,131.35"},
}

# ============================================================
# 失敗理由の定数
# ============================================================

class FailureReason:
    NOT_SUPPORTED = "not_supported"  # 404: モバイルオーダー非対応（永続スキップ）
    TEMP_ERROR    = "temp_error"     # 5xx/タイムアウト: 一時的エラー（後日リトライ）
    NO_BIGMAC     = "no_bigmac"      # 200だがビッグマックなし（永続スキップ）

# ============================================================
# 失敗店舗の管理
# ============================================================

def load_failed() -> dict:
    if FAILED_FILE.exists():
        return json.loads(FAILED_FILE.read_text())
    return {}


def save_failed(failed: dict):
    FAILED_FILE.write_text(json.dumps(failed, ensure_ascii=False, indent=2))


def record_failure(failed: dict, store_key: str, store_name: str, reason: str):
    entry = failed.get(store_key, {"reason": reason, "count": 0, "name": store_name})
    entry["count"] += 1
    entry["reason"] = reason
    entry["last_failed"] = datetime.now().isoformat()
    failed[store_key] = entry
    log.warning(f"    失敗記録: {store_name} ({store_key}) - {reason} (累計{entry['count']}回)")


def should_skip(failed: dict, store_key: str) -> bool:
    entry = failed.get(store_key)
    if not entry:
        return False
    if entry["reason"] in (FailureReason.NOT_SUPPORTED, FailureReason.NO_BIGMAC):
        return True
    if entry["reason"] == FailureReason.TEMP_ERROR and entry["count"] >= MAX_RETRY_COUNT:
        log.warning(f"    {store_key} は{MAX_RETRY_COUNT}回失敗済み。永続スキップします。")
        return True
    return False

# ============================================================
# 状態管理
# ============================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"completed": [], "last_run_date": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def get_next_prefectures(state: dict) -> list[str]:
    """
    未完了の都道府県から次のBATCH_COUNT件を返す。
    全47県完了したら自動リセットして北海道から再開。
    """
    all_codes = list(PREFECTURE_CONFIG.keys())
    completed = set(state.get("completed", []))
    pending = [c for c in all_codes if c not in completed]

    if not pending:
        days = round(47 / BATCH_COUNT)
        log.info(f"全47都道府県の調査完了。次のサイクルを開始します（約{days}日で一周）。")
        state["completed"] = []
        save_state(state)
        pending = all_codes

    return pending[:BATCH_COUNT]

# ============================================================
# APIクライアント
# ============================================================

def fetch_bigmac_price_with_reason(store_key: str) -> tuple[int | None, str | None]:
    url = MENU_URL.format(store_key=store_key)

    for attempt in range(1, MAX_RETRY_COUNT + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    price = data["products"][BIG_MAC_CODE]["priceList"][0]["price"]
                    return price, None
                except (KeyError, IndexError, TypeError):
                    return None, FailureReason.NO_BIGMAC

            elif resp.status_code == 404:
                return None, FailureReason.NOT_SUPPORTED

            else:
                log.warning(f"    HTTP {resp.status_code} (試行 {attempt}/{MAX_RETRY_COUNT})")

        except requests.Timeout:
            log.warning(f"    タイムアウト (試行 {attempt}/{MAX_RETRY_COUNT})")
        except requests.RequestException as e:
            log.warning(f"    接続エラー: {e} (試行 {attempt}/{MAX_RETRY_COUNT})")

        if attempt < MAX_RETRY_COUNT:
            wait = random.uniform(10, 20)
            log.info(f"    {wait:.1f}秒後リトライ...")
            time.sleep(wait)

    return None, FailureReason.TEMP_ERROR


def fetch_poi(bounds: str) -> list[dict]:
    for attempt in range(1, 4):
        try:
            resp = requests.get(POI_URL, headers=HEADERS, params={"bounds": bounds}, timeout=15)
            if resp.status_code == 200:
                return resp.json()
        except requests.RequestException as e:
            log.warning(f"POI取得失敗 (試行 {attempt}/3): {e}")
        time.sleep(random.uniform(10, 20))
    return []

# ============================================================
# データ変換
# ============================================================

def determine_price_tier(price: int | None) -> str | None:
    if price is None:
        return None
    PRICE_TIERS = {
        500: "standard",
        530: "semi_urban",
        560: "urban",
        590: "special",
    }
    tier = PRICE_TIERS.get(price)
    if tier is None:
        log.warning(f"    未知の価格帯: {price}円 → 価格改定の可能性あり")
        return f"special_{price}"
    return tier


def parse_conditions(values: list[int]) -> dict:
    flags = {}
    for idx, key in CONDITION_KEYS.items():
        flags[key] = bool(values[idx]) if idx < len(values) else False
    flags["raw_conditions"] = values
    return flags


def build_shop_record(poi: dict, pref_code: str, price: int | None, failure_reason: str | None) -> dict:
    conditions = parse_conditions(poi.get("condition_values", []))
    return {
        "id": poi["key"],
        "internal_id": poi["id"],
        "name": poi["name"],
        "coords": [poi["latitude"], poi["longitude"]],
        "prefecture_code": int(pref_code),
        "address": poi.get("address", ""),
        "bigmac_price": price,
        "price_tier": determine_price_tier(price),
        "price_fetch_status": failure_reason or "ok",
        "options": {
            "is_24h":       conditions.get("is_24h", False),
            "drive_thru":   conditions.get("drive_thru", False),
            "delivery":     conditions.get("delivery", False),
            "breakfast":    conditions.get("breakfast", False),
            "mccafe":       conditions.get("mccafe", False),
            "mobile_order": conditions.get("mobile_order", False),
            "parking":      conditions.get("parking", False),
            "free_wifi":    conditions.get("free_wifi", False),
        },
        "raw_conditions": conditions.get("raw_conditions", []),
        "scraped_at": datetime.now().isoformat(),  # フロントエンド用: いつ調べたか
    }

# ============================================================
# リトライ処理（18時実行 --mode=retry でも単独で呼ばれる）
# ============================================================

def retry_failed_stores(failed: dict):
    retry_targets = [
        (key, entry) for key, entry in list(failed.items())
        if entry["reason"] == FailureReason.TEMP_ERROR
        and entry["count"] < MAX_RETRY_COUNT
    ]
    if not retry_targets:
        log.info("リトライ対象なし。")
        return

    log.info(f"===== 失敗店舗リトライ: {len(retry_targets)}件 =====")

    for store_key, entry in retry_targets:
        store_name = entry.get("name", store_key)
        log.info(f"  リトライ: {store_name} ({store_key})")

        price, reason = fetch_bigmac_price_with_reason(store_key)

        if price is not None:
            log.info(f"    リトライ成功: {price}円")
            del failed[store_key]
            save_failed(failed)
            _update_shop_price(store_key, price)
        else:
            record_failure(failed, store_key, store_name, reason)
            save_failed(failed)

        time.sleep(random.uniform(5, 10))


def _update_shop_price(store_key: str, price: int):
    pref_code = store_key[:2]
    out_path = DATA_DIR / f"shops_{pref_code}.json"
    if not out_path.exists():
        return
    shops = json.loads(out_path.read_text())
    for shop in shops:
        if shop["id"] == store_key:
            shop["bigmac_price"] = price
            shop["price_tier"] = determine_price_tier(price)
            shop["price_fetch_status"] = "ok"
            shop["scraped_at"] = datetime.now().isoformat()
            break
    out_path.write_text(json.dumps(shops, ensure_ascii=False, indent=2))

# ============================================================
# 県スクレイプ処理
# ============================================================

def scrape_prefecture(pref_code: str, failed: dict) -> bool:
    config = PREFECTURE_CONFIG[pref_code]
    name = config["name"]
    log.info(f"===== {pref_code}: {name} 開始 =====")

    poi_list = fetch_poi(config["bounds"])
    if not poi_list:
        log.error(f"{name}: POI取得失敗")
        return False
    log.info(f"{name}: {len(poi_list)}店舗検出")

    out_path = DATA_DIR / f"shops_{pref_code}.json"
    existing = {}
    if out_path.exists():
        for shop in json.loads(out_path.read_text()):
            existing[shop["id"]] = shop

    # ★ 優先度付きキュー: temp_errorの既知失敗店舗を先頭に並べる
    retry_first = [p for p in poi_list if p.get("key") in failed
                   and failed[p["key"]]["reason"] == FailureReason.TEMP_ERROR]
    normal      = [p for p in poi_list if p.get("key") not in {p2["key"] for p2 in retry_first}]
    ordered_poi = retry_first + normal

    if retry_first:
        log.info(f"  前回エラー店舗を優先処理: {len(retry_first)}件")

    shops_map = {}
    skipped = 0

    for i, poi in enumerate(ordered_poi, 1):
        store_key  = poi.get("key", "")
        store_name = poi.get("name", "")
        log.info(f"  [{i}/{len(ordered_poi)}] {store_name} ({store_key})")

        if should_skip(failed, store_key):
            log.info(f"    → スキップ（過去の失敗記録あり）")
            skipped += 1
            reason = failed[store_key]["reason"]
            shops_map[store_key] = existing.get(store_key) or build_shop_record(poi, pref_code, None, reason)
            continue

        price, reason = fetch_bigmac_price_with_reason(store_key)

        if price is not None:
            log.info(f"    ビッグマック: {price}円 → {determine_price_tier(price)}")
            # リトライ成功なら失敗記録を削除
            if store_key in failed:
                del failed[store_key]
                save_failed(failed)
        else:
            record_failure(failed, store_key, store_name, reason)
            save_failed(failed)

        shops_map[store_key] = build_shop_record(poi, pref_code, price, reason)
        time.sleep(random.uniform(5, 10))

    # POIの順序を維持して保存
    shops = [shops_map[p["key"]] for p in poi_list if p["key"] in shops_map]
    out_path.write_text(json.dumps(shops, ensure_ascii=False, indent=2))
    log.info(f"{name}: {len(shops)}件保存（スキップ {skipped}件） → {out_path}")
    return True

# ============================================================
# エントリーポイント
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["full", "retry"],
        default="full",
        help="full: 新県調査＋リトライ / retry: リトライのみ（18時実行用）"
    )
    args = parser.parse_args()

    state  = load_state()
    failed = load_failed()
    today  = datetime.now().strftime("%Y-%m-%d")

    if args.mode == "retry":
        log.info("===== モード: リトライのみ =====")
        retry_failed_stores(failed)
        generate_summary()
        log.info("リトライ完了。")
        return

    # --- フルモード ---
    log.info("===== モード: フル（新県調査 → リトライ） =====")
    targets = get_next_prefectures(state)
    log.info(f"本日の対象 ({BATCH_COUNT}県): {[PREFECTURE_CONFIG[c]['name'] for c in targets]}")

    for pref_code in targets:
        success = scrape_prefecture(pref_code, failed)
        if success:
            state["completed"].append(pref_code)
            state["last_run_date"] = today
            save_state(state)
        else:
            log.error(f"{pref_code} 失敗。次回リトライします。")

        if pref_code != targets[-1]:
            wait = random.uniform(30, 60)
            log.info(f"次の県まで{wait:.0f}秒待機...")
            time.sleep(wait)

    # フルモードの末尾でもリトライを実行
    retry_failed_stores(failed)
    generate_summary()
    log.info("本日分完了。")


if __name__ == "__main__":
    main()


# ============================================================
# サマリーJSON生成（フロントエンド初期表示用）
# ============================================================

def generate_summary():
    """
    全都道府県のJSONから軽量サマリーを生成する。
    抽出項目: id, coords, prefecture_code, price_tier のみ。
    出力: data/all_summary.json
    フロントエンドの初期ロードで全国分布を表示するために使用。
    """
    summary = []
    missing = []

    for code in PREFECTURE_CONFIG:
        path = DATA_DIR / f"shops_{code}.json"
        if not path.exists():
            missing.append(code)
            continue
        shops = json.loads(path.read_text())
        for s in shops:
            if not s.get("coords"):
                continue
            summary.append({
                "id":              s["id"],
                "coords":          s["coords"],
                "prefecture_code": s.get("prefecture_code"),
                "price_tier":      s.get("price_tier"),
            })

    out_path = DATA_DIR / "all_summary.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, separators=(',', ':')))

    size_kb = out_path.stat().st_size / 1024
    log.info(f"サマリー生成完了: {len(summary)}店舗 / {size_kb:.1f}KB → {out_path}")
    if missing:
        log.info(f"未取得県（サマリー未収録）: {[PREFECTURE_CONFIG[c]['name'] for c in missing]}")