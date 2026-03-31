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
import os
import random
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests

# ============================================================
# ★ ここを変えるだけで運用速度を調整できる
# ============================================================
BATCH_COUNT = 7   # 1日に処理する県数。急ぎたいときは5〜7に増やす。
                  # 47 ÷ BATCH_COUNT = 全国一周にかかる日数の目安
                  # 例: 2 → 約24日, 5 → 約10日, 7 → 約7日

# ============================================================
# パス設定
# ============================================================

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = DATA_DIR / "backup"
LOG_DIR  = BASE_DIR / "logs"
STATE_FILE  = BASE_DIR / "last_run.json"

DATA_DIR.mkdir(exist_ok=True)
BACKUP_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ============================================================
# Discord Webhook 通知
# .env ファイルから読み込む（gitには含めない）
# .env の書き方:
#   DISCORD_DIFF=https://discord.com/api/webhooks/xxx/yyy
#   DISCORD_ERROR=https://discord.com/api/webhooks/xxx/zzz
#   DISCORD_LOG=https://discord.com/api/webhooks/xxx/www
# ============================================================

def _load_env():
    """`.env` を手動パース（python-dotenv不要）"""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('\'"'))

_load_env()

WEBHOOK_DIFF  = os.environ.get("DISCORD_DIFF", "")   # #差分・速報
WEBHOOK_ERROR = os.environ.get("DISCORD_ERROR", "")  # #エラー・警告
WEBHOOK_LOG   = os.environ.get("DISCORD_LOG", "")    # #定期実行ログ


def _discord_post(webhook_url: str, content: str):
    """Discord Webhookにメッセージを送る。失敗してもスクレイパーを止めない。"""
    if not webhook_url:
        return
    try:
        content = content[:1990] + "…" if len(content) > 1990 else content
        resp = requests.post(webhook_url, json={"content": content}, timeout=10)
        if resp.status_code >= 400:
            log.warning(f"Discord通知失敗 HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log.warning(f"Discord通知の送信に失敗しました（処理は継続します）: {e}")


def notify_diff(diffs: list):
    """差分をDiscordの #差分・速報 に投稿"""
    if not diffs or not WEBHOOK_DIFF:
        return
    lines = ["📢 **差分検知レポート**"]
    for d in diffs[:20]:
        if d["type"] == "new_open":
            lines.append(f"🆕 新店: **{d['name']}** （{d.get('address','')}）")
        elif d["type"] == "closed":
            lines.append(f"🚫 閉店: **{d['name']}** （{d.get('address','')}）")
        elif d["type"] == "price_change":
            lines.append(f"💰 価格変動: **{d['name']}** {d['old_tier']} → {d['new_tier']} （¥{d['old_price']}→¥{d['new_price']}）")
        elif d["type"] == "facility_change":
            label = {"drive_thru":"ドライブスルー","mccafe":"マックカフェ","mobile_order":"モバイルオーダー"}.get(d["item"], d["item"])
            state_str = "追加✅" if d["new"] else "廃止❌"
            lines.append(f"🏪 設備変更: **{d['name']}** {label}{state_str}")
    if len(diffs) > 20:
        lines.append(f"…他 {len(diffs)-20} 件")
    _discord_post(WEBHOOK_DIFF, "\n".join(lines))


def notify_error(context: str, exc: Exception = None):
    """エラーをDiscordの #エラー・警告 に投稿"""
    if not WEBHOOK_ERROR:
        return
    lines = [f"🚨 **エラー発生** — {context}"]
    if exc:
        lines.append(f"```{traceback.format_exc()[-1200:]}```")
    _discord_post(WEBHOOK_ERROR, "\n".join(lines))


def notify_log(message: str):
    """定期ログをDiscordの #定期実行ログ に投稿"""
    if not WEBHOOK_LOG:
        return
    _discord_post(WEBHOOK_LOG, message)


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

POI_URL       = "https://map.mcdonalds.co.jp/api/poi"
STORE_URL     = "https://data.cat.group-{group}.prod.mop.mcd.qorcommerce.com/{store_key}.json"
MENU_URL      = "https://data.cat.group-{group}.prod.mop.mcd.qorcommerce.com/{store_key}/menu.json"
BIG_MAC_CODE  = "1210"
GROUP_CACHE_FILE = BASE_DIR / "store_groups.json"
MAX_RETRY_COUNT = 3

# ============================================================
# 失敗理由
# ============================================================
class FailureReason:
    NOT_SUPPORTED = "not_supported"
    NO_BIGMAC     = "no_bigmac"
    TEMP_ERROR    = "temp_error"

# ============================================================
# 設備フラグ
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
    18: "special_location",
    19: "specific_store",
}

# ============================================================
# 都道府県設定
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

# 価格帯ごとの代表価格（diff通知用）
TIER_PRICES = {
    "standard":     500,
    "semi_urban_b": 520,
    "semi_urban_a": 530,
    "urban":        550,
    "special":      600,
}


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"completed": [], "last_run_date": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def load_group_cache() -> dict:
    if not GROUP_CACHE_FILE.exists():
        log.warning("store_groups.json が見つかりません。build_group_cache.py を先に実行してください。")
        return {}
    data = json.loads(GROUP_CACHE_FILE.read_text())
    found = sum(1 for v in data.values() if v)
    log.info(f"グループキャッシュ: {found}/{len(data)}件")
    return data


def get_next_prefectures(state: dict) -> list[str]:
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

def fetch_poi(bounds: str) -> list[dict]:
    for attempt in range(1, 4):
        try:
            resp = requests.get(POI_URL, headers=HEADERS, params={"bounds": bounds}, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            else:
                log.warning(f"POI取得エラー [HTTP {resp.status_code}]: {resp.text[:200]}")
        except requests.RequestException as e:
            log.warning(f"POI取得失敗 (試行 {attempt}/3): {e}")
        time.sleep(random.uniform(10, 20))
    return []


# ============================================================
# データ変換
# ============================================================

def determine_price_tier(price: int | None) -> str | None:
    """価格（円）から価格帯を判定する。完全一致。"""
    if price is None:
        return None
    if price <= 500:
        return "standard"
    elif price <= 520:
        return "semi_urban_b"
    elif price <= 530:
        return "semi_urban_a"
    elif price <= 550:
        return "urban"
    else:
        return "special"


def fetch_store_detail(store_key: str, group: str) -> dict | None:
    url = STORE_URL.format(group=group, store_key=store_key)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            store = data.get("store", {})
            details = store.get("details", {})

            svc_hours = details.get("restaurantServiceHours", {})
            def mins_to_hhmm(m):
                if m is None: return None
                h, mn = divmod(int(m), 60)
                return f"{h:02d}:{mn:02d}"

            hours = {}
            for day_key, js_key in [("weekday","weekdays"),("saturday","saturday"),("holiday","holiday")]:
                entry = svc_hours.get(js_key)
                if entry:
                    hours[day_key] = {
                        "open":  mins_to_hhmm(entry.get("start")),
                        "close": mins_to_hhmm(entry.get("end")),
                    }

            features = details.get("features", {})
            is_24h = bool(features.get("open24Hours", False))
            price_tier_raw = details.get("priceTier")

            return {
                "hours": hours,
                "price_tier_raw": price_tier_raw,
                "is_24h": is_24h,
            }
        elif resp.status_code == 404:
            return None
        else:
            log.warning(f"    store.json HTTP {resp.status_code}: {store_key}")
            return None
    except Exception as e:
        log.warning(f"    store.json 取得失敗: {store_key} - {e}")
        return None


def fetch_bigmac_price(store_key: str, group: str) -> tuple[int | None, str | None]:
    BIGMAC_CODE = "1215"
    url = f"https://data.cat.group-{group}.prod.mop.mcd.qorcommerce.com/{store_key}/menu.json"

    for attempt in range(1, MAX_RETRY_COUNT + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                item = data.get("products", {}).get(BIGMAC_CODE)
                if item is None:
                    log.warning(f"    [{store_key}] 商品コード {BIGMAC_CODE} が見つかりません")
                    return None, "no_bigmac"
                for p in item.get("priceList", []):
                    if p.get("priceCode") == "EATIN":
                        return int(p["price"]), None
                price = item.get("price", {}).get("price")
                if price:
                    return int(price), None
                return None, "no_bigmac"
            elif resp.status_code == 404:
                return None, "not_supported"
            else:
                log.warning(f"    HTTP {resp.status_code} (試行 {attempt}/{MAX_RETRY_COUNT}) 内容: {resp.text[:100]}")
        except requests.Timeout:
            log.warning(f"    タイムアウト (試行 {attempt}/{MAX_RETRY_COUNT})")
        except Exception as e:
            log.warning(f"    エラー: {e} (試行 {attempt}/{MAX_RETRY_COUNT})")
        if attempt < MAX_RETRY_COUNT:
            time.sleep(random.uniform(5, 15))
    return None, "temp_error"


def parse_conditions(values: list[int]) -> dict:
    flags = {}
    for idx, key in CONDITION_KEYS.items():
        flags[key] = bool(values[idx]) if idx < len(values) else False
    flags["raw_conditions"] = values
    return flags


def build_shop_record(poi: dict, pref_code: str, price: int | None, failure_reason: str | None, detail: dict | None = None) -> dict:
    conditions = parse_conditions(poi.get("condition_values", []))

    hours = {}
    is_24h = conditions.get("is_24h", False)
    if detail:
        hours = detail.get("hours", {})
        is_24h = detail.get("is_24h", is_24h)

    return {
        "id":               poi["key"],
        "internal_id":      poi["id"],
        "name":             poi["name"],
        "coords":           [poi["latitude"], poi["longitude"]],
        "prefecture_code":  int(pref_code),
        "address":          poi.get("address", ""),
        "bigmac_price":     price,
        "price_tier":       determine_price_tier(price),
        "price_tier_raw":   detail.get("price_tier_raw") if detail else None,
        "price_fetch_status": failure_reason or "ok",
        "hours":            hours,
        "options": {
            "is_24h":           is_24h,
            "drive_thru":       conditions.get("drive_thru", False),
            "delivery":         conditions.get("delivery", False),
            "breakfast":        conditions.get("breakfast", False),
            "mccafe":           conditions.get("mccafe", False),
            "mobile_order":     conditions.get("mobile_order", False),
            "parking":          conditions.get("parking", False),
            "free_wifi":        conditions.get("free_wifi", False),
            "special_location": conditions.get("special_location", False),
            "specific_store":   conditions.get("specific_store", False),
        },
        "raw_conditions": conditions.get("raw_conditions", []),
        "scraped_at":     datetime.now().isoformat(),
    }


# ============================================================
# リトライ処理
# ============================================================

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
# サマリー生成
# ============================================================

def generate_summary():
    """全shops_*.jsonからall_summary.jsonを生成する。"""
    all_shops = []
    for p in sorted(DATA_DIR.glob("shops_??.json")):
        try:
            shops = json.loads(p.read_text())
            for s in shops:
                all_shops.append({
                    "id":              s["id"],
                    "name":            s["name"],
                    "coords":          s["coords"],
                    "prefecture_code": s["prefecture_code"],
                    "price_tier":      s.get("price_tier"),
                    "options":         s.get("options", {}),
                    "scraped_at":      s.get("scraped_at"),
                })
        except Exception as e:
            log.warning(f"summary生成エラー {p}: {e}")
    out = DATA_DIR / "all_summary.json"
    out.write_text(json.dumps(all_shops, ensure_ascii=False))
    log.info(f"all_summary.json 生成完了: {len(all_shops)}件")


# ============================================================
# 差分生成
# ============================================================

def generate_diff() -> list:
    """今回のshops_*.jsonとbackupを比較して差分リストを返す。バックアップも更新する。"""
    diffs = []

    for p in sorted(DATA_DIR.glob("shops_??.json")):
        backup_path = BACKUP_DIR / p.name
        try:
            current_shops = {s["id"]: s for s in json.loads(p.read_text())}
        except Exception as e:
            log.warning(f"diff読み込みエラー {p}: {e}")
            continue

        if not backup_path.exists():
            # 初回はバックアップだけ作って差分なし
            backup_path.write_text(json.dumps(list(current_shops.values()), ensure_ascii=False))
            log.info(f"初回バックアップ作成: {backup_path.name}")
            continue

        try:
            prev_shops = {s["id"]: s for s in json.loads(backup_path.read_text())}
        except Exception as e:
            log.warning(f"backup読み込みエラー {backup_path}: {e}")
            prev_shops = {}

        all_ids = set(current_shops) | set(prev_shops)
        for sid in all_ids:
            cur = current_shops.get(sid)
            prv = prev_shops.get(sid)
            if cur and not prv:
                diffs.append({"type": "new_open", "name": cur["name"], "address": cur.get("address", "")})
            elif prv and not cur:
                diffs.append({"type": "closed", "name": prv["name"], "address": prv.get("address", "")})
            elif cur and prv:
                ct = cur.get("price_tier")
                pt = prv.get("price_tier")
                if ct != pt and ct and pt:
                    diffs.append({
                        "type":      "price_change",
                        "name":      cur["name"],
                        "old_tier":  pt,
                        "new_tier":  ct,
                        "old_price": TIER_PRICES.get(pt, "?"),
                        "new_price": TIER_PRICES.get(ct, "?"),
                    })
                for opt in ("drive_thru", "mccafe", "mobile_order"):
                    co = cur.get("options", {}).get(opt)
                    po = prv.get("options", {}).get(opt)
                    if co != po:
                        diffs.append({"type": "facility_change", "name": cur["name"], "item": opt, "new": co})

        # バックアップ更新
        backup_path.write_text(json.dumps(list(current_shops.values()), ensure_ascii=False))

    log.info(f"差分検出: {len(diffs)}件")
    return diffs


# ============================================================
# 県スクレイプ処理
# ============================================================

def scrape_prefecture(pref_code: str, group_cache: dict) -> bool:
    config = PREFECTURE_CONFIG[pref_code]
    name   = config["name"]
    log.info(f"===== {pref_code}: {name} 開始 =====")

    poi_list = fetch_poi(config["bounds"])
    if not poi_list:
        log.error(f"{name}: POI取得失敗")
        notify_error(f"{name}（{pref_code}）のPOI取得失敗")
        return False
    log.info(f"{name}: {len(poi_list)}店舗検出")

    out_path = DATA_DIR / f"shops_{pref_code}.json"
    existing = {}
    if out_path.exists():
        for shop in json.loads(out_path.read_text()):
            existing[shop["id"]] = shop

    shops_map   = {}
    success_cnt = skip_cnt = no_group_cnt = 0

    for i, poi in enumerate(poi_list, 1):
        store_key  = poi.get("key", "")
        store_name = poi.get("name", "")
        if not store_key.startswith(pref_code):
            log.debug(f"  スキップ（他県店舗）: {store_name} ({store_key})")
            continue
        log.info(f"  [{i}/{len(poi_list)}] {store_name} ({store_key})")

        group = group_cache.get(store_key)
        if not group:
            log.warning(f"    グループ未特定: {store_key} → build_group_cache.py を実行してください")
            no_group_cnt += 1
            shops_map[store_key] = existing.get(store_key) or build_shop_record(poi, pref_code, None, "no_group", None)
            continue

        cached = existing.get(store_key, {})
        status = cached.get("price_fetch_status", "")
        if status in ("not_supported", "no_bigmac"):
            log.info(f"    → スキップ（{status}）")
            shops_map[store_key] = cached
            skip_cnt += 1
            continue

        detail = fetch_store_detail(store_key, group)
        time.sleep(random.uniform(1.0, 2.0))

        price, reason = fetch_bigmac_price(store_key, group)
        time.sleep(random.uniform(1.0, 2.0))

        if price is not None:
            tier = determine_price_tier(price)
            log.info(f"    価格: {price}円 → {tier} / priceTier_raw: {detail.get('price_tier_raw') if detail else '?'}")
            success_cnt += 1
        else:
            log.warning(f"    失敗: {reason}")

        shops_map[store_key] = build_shop_record(poi, pref_code, price, reason, detail=detail)

    shops = [shops_map[p["key"]] for p in poi_list if p["key"] in shops_map]
    out_path.write_text(json.dumps(shops, ensure_ascii=False, indent=2))
    log.info(f"{name}: {len(shops)}件保存（取得{success_cnt} スキップ{skip_cnt} グループ未特定{no_group_cnt}）")
    return True


# ============================================================
# メイン
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["full", "retry"],
        default="full",
        help="full: 新県調査 / retry: 差分確認＋デプロイのみ"
    )
    args = parser.parse_args()

    state = load_state()
    group_cache = load_group_cache()
    today = datetime.now().strftime("%Y-%m-%d")

    if args.mode == "retry":
        log.info("===== モード: 差分確認のみ =====")
        try:
            diffs = generate_diff()
            generate_summary()
            if diffs:
                notify_diff(diffs)
            notify_log(f"🔄 **差分確認完了** {today} 差分: {len(diffs)}件")
        except Exception as e:
            notify_error("差分確認モードでエラー発生", e)
            raise
        return

    # --- フルモード ---
    log.info("===== モード: フル =====")
    targets = get_next_prefectures(state)
    log.info(f"本日の対象 ({BATCH_COUNT}県): {[PREFECTURE_CONFIG[c]['name'] for c in targets]}")

    for pref_code in targets:
        success = scrape_prefecture(pref_code, group_cache)
        if success:
            state["completed"].append(pref_code)
            state["last_run_date"] = today
            save_state(state)
        else:
            notify_error(f"{PREFECTURE_CONFIG[pref_code]['name']}のPOI取得失敗")

        if pref_code != targets[-1]:
            wait = random.uniform(30, 60)
            log.info(f"次の県まで{wait:.0f}秒待機...")
            time.sleep(wait)

    try:
        diffs = generate_diff()
        generate_summary()
        if diffs:
            notify_diff(diffs)
        completed_names = [PREFECTURE_CONFIG[c]["name"] for c in targets if c in state.get("completed", [])]
        notify_log("✅ **本日の処理完了** " + today + " 処理県: " + ", ".join(completed_names) + " 差分: " + str(len(diffs)) + "件")
    except Exception as e:
        notify_error("サマリー/diff生成でエラー発生", e)
        raise

    log.info("本日分完了。")


if __name__ == "__main__":
    main()