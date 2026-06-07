"""台指期左側加碼階梯 — 雲端到價盯盤 (GitHub Actions 版)

每 5 分鐘由 GitHub Actions 排程執行:
  1. 抓期交所行情網 (mis.taifex.com.tw) 台指期近月報價 — 日盤+夜盤,取最新
  2. 跟 state.json 的階梯點位比對
  3. 跌到武裝中的階 → 發 Telegram(跳空跨多階合併成一則)
  4. 觸發後該階靜音;價格回升超過 階梯點×(1+rearm_pct%) 自動重新武裝
  5. 狀態寫回 state.json,由 workflow commit

只用 Python 標準庫,不需要 pip install。
環境變數:TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID(放 GitHub Actions Secrets)

本機測試:
  TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python3 watch.py --force-price 40950
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state.json"
MIS_URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
_MONTHLY_RE = re.compile(r"^TXF[A-L]\d-[FM]$")
TPE = timezone(timedelta(hours=8))


# ============================================================================
# 期交所報價(同 MarketDashboard src/market/taifex_mis.py 的邏輯)
# ============================================================================

def _fetch_market(market_type: str) -> list[dict]:
    body = {
        "MarketType": market_type, "SymbolType": "F", "KindID": "1",
        "CID": "TXF", "ExpireMonth": "", "RowSize": "全部",
        "PageNo": "", "SortColumn": "", "AscDesc": "A",
    }
    req = urllib.request.Request(
        MIS_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    if str(data.get("RtCode")) != "0":
        return []
    return data.get("RtData", {}).get("QuoteList", []) or []


def _parse_near_month(quotes: list[dict], session: str) -> dict | None:
    for q in quotes:
        sid = q.get("SymbolID") or ""
        if not _MONTHLY_RE.match(sid):
            continue
        raw = (q.get("CLastPrice") or "").strip()
        if not raw:
            return None
        try:
            price = float(raw)
        except ValueError:
            return None
        quoted_at = None
        try:
            d, t = (q.get("CDate") or "").strip(), (q.get("CTime") or "").strip()
            if d and t:
                quoted_at = datetime.strptime(d + t.zfill(6), "%Y%m%d%H%M%S")
                # 夜盤跨午夜:CDate 是開盤日,凌晨段實際是隔天清晨
                if session == "night" and quoted_at.hour < 6:
                    quoted_at += timedelta(days=1)
        except ValueError:
            pass
        return {"price": price, "session": session, "symbol": sid,
                "quoted_at": quoted_at.isoformat() if quoted_at else None,
                "_sort": quoted_at or datetime.min}
    return None


def fetch_txf_quote() -> dict | None:
    candidates = []
    for market_type, session in (("0", "day"), ("1", "night")):
        try:
            q = _parse_near_month(_fetch_market(market_type), session)
            if q:
                candidates.append(q)
        except Exception as e:
            print(f"WARN: {session} 盤抓取失敗: {e}", file=sys.stderr)
    if not candidates:
        return None
    candidates.sort(key=lambda c: c["_sort"], reverse=True)
    best = candidates[0]
    best.pop("_sort", None)
    return best


# ============================================================================
# 到價判斷(同 MarketDashboard src/alert/ladder_watch.py 的純邏輯)
# ============================================================================

def evaluate_levels(state: dict, current_price: float) -> dict:
    armed = dict(state.get("armed", {}))
    rearm_pct = state.get("rearm_pct", 1.5) / 100.0
    fired, rearmed = [], []
    for lv in state.get("levels", []):
        key = str(int(lv["price"]))
        is_armed = armed.get(key, True)
        if is_armed and current_price <= lv["price"]:
            fired.append(lv)
            armed[key] = False
        elif not is_armed and current_price >= lv["price"] * (1 + rearm_pct):
            rearmed.append(lv["price"])
            armed[key] = True
    return {"fired": fired, "rearmed": rearmed, "armed": armed}


def build_message(fired: list[dict], price: float, session: str, start_price: float | None) -> str:
    sess = "夜盤" if session == "night" else "日盤"
    if len(fired) == 1:
        head = f"📉 <b>階梯到價:{int(fired[0]['price']):,}</b>"
    else:
        head = f"📉 <b>階梯到價:一次跨 {len(fired)} 階(跳空)</b>"
    lines = [head]
    for lv in fired:
        drop = f"(-{(start_price - lv['price']) / start_price * 100:.1f}%)" if start_price else ""
        note = f" → {lv['note']}" if lv.get("note") else ""
        lines.append(f"・{int(lv['price']):,} {drop}{note}")
    if len(fired) > 1:
        lines.append("⚠ 跳空跨多階:依紀律只執行最近一階的量,不補階!")
    lines.append(f"現價 {price:,.0f}({sess})")
    lines.append("加碼前:保證金使用率 &lt; 70%?LJI 進恐慌區了嗎?")
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("WARN: 缺 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        return False
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception as e:
        print(f"ERROR: Telegram 發送失敗: {e}", file=sys.stderr)
        return False


# ============================================================================
# 主流程
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-price", type=float, default=None, help="測試用:跳過抓價直接給價格")
    args = parser.parse_args()

    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if not state.get("enabled") or not state.get("levels"):
        print("disabled 或沒有點位,跳過")
        return 0

    if args.force_price is not None:
        price, session = args.force_price, "day"
        quoted_at = None
    else:
        quote = fetch_txf_quote()
        if quote is None:
            print("抓不到報價,本輪跳過")
            return 0
        price, session, quoted_at = quote["price"], quote["session"], quote["quoted_at"]

    result = evaluate_levels(state, price)
    state["armed"] = result["armed"]
    state["last_check"] = {
        "ts": datetime.now(TPE).isoformat(timespec="seconds"),
        "price": price, "session": session, "quoted_at": quoted_at,
    }

    if result["fired"]:
        msg = build_message(result["fired"], price, session, state.get("start_price"))
        sent = send_telegram(msg)
        state.setdefault("history", []).append({
            "ts": state["last_check"]["ts"], "price": price,
            "levels": [lv["price"] for lv in result["fired"]], "telegram_sent": sent,
        })
        state["history"] = state["history"][-30:]
        print(f"觸發 {[lv['price'] for lv in result['fired']]} @ {price},Telegram={'OK' if sent else 'FAIL'}")
    else:
        print(f"未觸發。現價 {price}({session}),武裝中 "
              f"{sum(1 for v in state['armed'].values() if v)}/{len(state.get('levels', []))} 階")

    if result["rearmed"]:
        print(f"重新武裝: {result['rearmed']}")

    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
