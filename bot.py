#!/usr/bin/env python3
"""
Supply & Demand Signal Bot (Binance Futures -> Telegram)
Strategy: "The 3 Step A+ Supply & Demand Strategy" (Trade with Pat)

  Step 1  Demand / Supply zone  = base candle before a big impulsive move (3+ candles)
          + Fair Value Gap inside the move
  Step 2  Trend confirmation    = EMA50 slope on entry TF + price vs EMA50 on higher TF
  Step 3  Entry                 = first tap into the zone with slow momentum,
                                  no close beyond the zone, then a confirmation candle
  6 Keys  fresh zone, close/wick, confluence stack, lowest demand,
          discounted price (fib 50%), break of structure  -> score out of 6

NO AUTO TRADING. The bot only sends signals to Telegram. Not financial advice.

Usage:
  python bot.py              # scan once, send new signals (GitHub Actions runs this every 5 min)
  python bot.py --test       # send a test message to Telegram
  python bot.py --backtest   # backtest the strategy on recent history
"""
import argparse
import bisect
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ============================ SETTINGS ============================
# Coins to scan: the TOP_N USDT coins by 24h trading volume are picked automatically
# on every run. To use your own fixed list instead, set a repo variable
# SYMBOLS="BTCUSDT,ETHUSDT,..." (GitHub -> Settings -> Secrets and variables -> Variables).
TOP_N = 30
# Used only if the automatic top-volume list cannot be fetched.
FALLBACK_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "LTCUSDT", "TRXUSDT", "DOTUSDT", "BCHUSDT", "NEARUSDT", "SUIUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT", "UNIUSDT", "AAVEUSDT", "ATOMUSDT", "FILUSDT", "ETCUSDT", "INJUSDT",
    "TIAUSDT", "SEIUSDT", "WLDUSDT", "PEPEUSDT", "SHIBUSDT", "TONUSDT",
]
# Stablecoins / pegged tokens that should never be traded as S&D setups
EXCLUDE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDE", "USD1", "RLUSD",
                 "EUR", "EURI", "AEUR", "XUSD", "PAXG", "WBTC", "WBETH", "BFUSD", "USDS"}
SYMBOLS = []               # filled at runtime by resolve_symbols()
TIMEFRAME = "15m"          # entry timeframe
HTF = "1h"                 # higher timeframe for trend confirmation (step 2)
CANDLES = 500              # candles fetched per scan

EMA_LEN = 50
ATR_LEN = 14
PIVOT_LEN = 3              # swing high/low detection (candles on each side)

IMPULSE_MIN_CANDLES = 3    # "1 2 3 big green candles in a row"
IMPULSE_ATR_MULT = 2.0     # impulse must move >= 2 x ATR (institutional move)
BIG_BASE_ATR = 0.5         # base body >= 0.5 ATR -> body zone, else wick-to-wick
ZONE_MAX_AGE = 200         # zone older than this many candles is ignored
MAX_TAP_BODY_ATR = 1.5     # slow momentum: no huge candle crashing into the zone
SIGNAL_LOOKBACK = 3        # only signal if confirmation candle is among last 3 closed candles
MIN_SCORE = 5              # 6 keys score needed to send a signal (6 = A+, 5 = A)
SL_BUFFER_ATR = 0.1        # stop loss buffer below/above the zone
CONFLUENCE_PAD_ATR = 0.25  # how close EMA / old S-R must be to the zone

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
IST = timezone(timedelta(hours=5, minutes=30))
HEARTBEAT_HOUR_IST = 9     # daily "bot alive" message after 9 AM IST

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHATS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
MANUAL_SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "").split(",") if s.strip()]
if os.getenv("TOP_N", "").strip().isdigit():
    TOP_N = int(os.getenv("TOP_N"))

# GitHub's servers are in the US, where fapi.binance.com is blocked (HTTP 451).
# So we try Futures first, then fall back to Binance public spot data for the same pair.
SOURCES = [
    ("futures", "https://fapi.binance.com/fapi/v1/klines", 1500),
    ("spot-vision", "https://data-api.binance.vision/api/v3/klines", 1000),
    ("spot", "https://api.binance.com/api/v3/klines", 1000),
]
SOURCE_LABEL = {"futures": "Binance Futures", "spot-vision": "Binance Spot (same pair)",
                "spot": "Binance Spot (same pair)"}

# ============================ DATA ============================

def fetch_klines(symbol, interval, limit, end_time=None, source=None):
    """Returns (closed_candles, source_name)."""
    order = SOURCES
    if source:
        order = [s for s in SOURCES if s[0] == source] + [s for s in SOURCES if s[0] != source]
    last_err = "no source"
    for name, url, max_limit in order:
        params = {"symbol": symbol, "interval": interval, "limit": min(limit, max_limit)}
        if end_time:
            params["endTime"] = end_time
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code != 200:
                last_err = f"{name} HTTP {r.status_code}"
                continue
            now_ms = int(time.time() * 1000)
            candles = [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
                        "c": float(k[4]), "ct": int(k[6])} for k in r.json()]
            candles = [x for x in candles if x["ct"] < now_ms]  # drop the running candle
            if not candles:
                last_err = f"{name} empty"
                continue
            return candles, name
        except Exception as e:  # network error -> try next source
            last_err = f"{name} {type(e).__name__}"
    raise RuntimeError(f"{symbol} {interval}: data fetch failed ({last_err})")


def fetch_history(symbol, interval, total, source=None):
    out, end = [], None
    while len(out) < total:
        batch, source = fetch_klines(symbol, interval, 1000, end_time=end, source=source)
        seen = {x["t"] for x in out}
        batch = [x for x in batch if x["t"] not in seen]
        if not batch:
            break
        out = batch + out
        end = batch[0]["t"] - 1
        time.sleep(0.2)
    return out[-total:], source


TICKER_SOURCES = [
    ("futures", "https://fapi.binance.com/fapi/v1/ticker/24hr"),
    ("spot-vision", "https://data-api.binance.vision/api/v3/ticker/24hr"),
    ("spot", "https://api.binance.com/api/v3/ticker/24hr"),
]


def top_volume_symbols(n):
    """Top-n USDT pairs by 24h quote volume (stablecoins and leveraged tokens removed)."""
    for name, url in TICKER_SOURCES:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                continue
            rows = []
            for x in r.json():
                sym = x.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                base = sym[:-4]
                if (not base or base in EXCLUDE_BASES
                        or base.endswith(("UP", "DOWN", "BULL", "BEAR"))):
                    continue
                try:
                    qv = float(x.get("quoteVolume", 0))
                    if float(x.get("lastPrice", 0)) <= 0 or int(x.get("count", 1)) == 0:
                        continue
                except (TypeError, ValueError):
                    continue
                rows.append((qv, sym))
            rows.sort(reverse=True)
            if len(rows) >= n:
                return [s for _, s in rows[:n]], name
        except Exception as e:
            print(f"top list via {name} failed: {type(e).__name__}")
    return None, None


def resolve_symbols():
    """Fill SYMBOLS: manual repo variable > live top-volume list > fallback list."""
    global SYMBOLS
    if MANUAL_SYMBOLS:
        SYMBOLS = MANUAL_SYMBOLS
        print(f"Using {len(SYMBOLS)} coins from SYMBOLS variable")
        return
    syms, src = top_volume_symbols(TOP_N)
    if syms:
        SYMBOLS = syms
        print(f"Top {TOP_N} by 24h volume ({src}): {', '.join(SYMBOLS)}")
    else:
        SYMBOLS = FALLBACK_SYMBOLS[:TOP_N]
        print(f"Top list unavailable, using fallback list of {len(SYMBOLS)} coins")

# ============================ INDICATORS ============================

def ema(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2 / (n + 1)
    s = sum(vals[:n]) / n
    out[n - 1] = s
    for i in range(n, len(vals)):
        s = vals[i] * k + s * (1 - k)
        out[i] = s
    return out


def atr(h, l, c, n):
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                          for i in range(1, len(c))]
    out = [None] * len(c)
    if len(c) < n:
        return out
    s = sum(tr[:n]) / n
    out[n - 1] = s
    for i in range(n, len(c)):
        s = (s * (n - 1) + tr[i]) / n
        out[i] = s
    return out


def pivots(vals, p, is_high):
    res = []
    for i in range(p, len(vals) - p):
        win = vals[i - p:i + p + 1]
        if (is_high and vals[i] == max(win)) or (not is_high and vals[i] == min(win)):
            res.append((i, vals[i]))
    return res


def flip(candles):
    """Mirror prices so SHORT (supply) setups can reuse the LONG (demand) logic."""
    return [{"t": x["t"], "ct": x["ct"], "o": -x["o"], "c": -x["c"], "h": -x["l"], "l": -x["h"]}
            for x in candles]

# ============================ STRATEGY ============================

def find_setups(C, H):
    """Find demand-zone long setups in candles C (use flip() for supply/short)."""
    n = len(C)
    o = [x["o"] for x in C]; h = [x["h"] for x in C]
    l = [x["l"] for x in C]; cl = [x["c"] for x in C]
    E = ema(cl, EMA_LEN)
    A = atr(h, l, cl, ATR_LEN)
    ph = pivots(h, PIVOT_LEN, True)
    pl = pivots(l, PIVOT_LEN, False)
    ph_idx = [p[0] for p in ph]

    hc = [x["c"] for x in H]
    he = ema(hc, EMA_LEN)
    hct = [x["ct"] for x in H]

    setups = []
    i = max(EMA_LEN + 10, ATR_LEN + 1, PIVOT_LEN * 2 + 2)
    while i < n - 1:
        if cl[i] <= o[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and cl[j + 1] > o[j + 1]:
            j += 1
        base = i - 1
        a = A[base]
        if a and (j - i + 1) >= IMPULSE_MIN_CANDLES and (cl[j] - o[i]) >= IMPULSE_ATR_MULT * a:
            s = _evaluate(C, o, h, l, cl, E, A, ph, pl, ph_idx, hc, he, hct, base, i, j)
            if s:
                setups.append(s)
        i = j + 1
    return setups


def _evaluate(C, o, h, l, cl, E, A, ph, pl, ph_idx, hc, he, hct, base, i, j):
    n = len(C)
    a = A[base]

    # --- Step 1: zone on the base candle (body for big candle, wick-to-wick for small)
    if abs(cl[base] - o[base]) >= BIG_BASE_ATR * a:
        top, bot, ztype = max(o[base], cl[base]), min(o[base], cl[base]), "body"
    else:
        top, bot, ztype = h[base], l[base], "wick"

    # --- Step 1: fair value gap inside the impulse
    if not any(l[k + 1] > h[k - 1] for k in range(i, j + 1) if k + 1 < n):
        return None

    # --- first tap into the zone (so the zone is fresh / untested until now)
    t = next((k for k in range(j + 1, min(n, j + 1 + ZONE_MAX_AGE)) if l[k] <= top), None)
    if t is None:
        return None
    if cl[t] < bot:                      # closed beyond the zone -> invalid
        return None

    # --- Step 3: confirmation candle (tap candle itself or the next one)
    if cl[t] > o[t]:
        e = t
    elif t + 1 < n and cl[t + 1] > o[t + 1] and cl[t + 1] >= bot:
        e = t + 1
    else:
        return None

    # --- Step 3: slow momentum into the zone (no big candle crashing in)
    bears = [o[k] - cl[k] for k in range(max(j + 1, t - 2), t + 1) if cl[k] < o[k]]
    if bears and max(bears) > MAX_TAP_BODY_ATR * (A[t] or a):
        return None

    # --- Step 2: trend confirmation (entry TF EMA rising + higher TF above EMA)
    if E[e] is None or E[e - 10] is None or E[e] <= E[e - 10]:
        return None
    k = bisect.bisect_right(hct, C[e]["ct"]) - 1
    if k < 0 or he[k] is None or hc[k] <= he[k]:
        return None

    entry = cl[e]
    pad = CONFLUENCE_PAD_ATR * a

    # --- 6 keys
    fresh = True                          # guaranteed by first-tap logic
    close_wick = True                     # guaranteed by the close check above
    ema_conf = E[t] is not None and bot - pad <= E[t] <= top + pad
    sr_conf = any(bot - pad <= v <= top + pad for idx, v in ph + pl if base - 150 <= idx < base - PIVOT_LEN)
    confluence = ema_conf or sr_conf
    lowest = l[base] <= min(l[max(0, base - 10):base] or [l[base]])
    swing_low = min(l[base:t + 1])
    swing_high = max(h[i:t + 1])
    discount = entry <= (swing_low + swing_high) / 2
    pidx = bisect.bisect_left(ph_idx, i - PIVOT_LEN) - 1
    bos = pidx >= 0 and swing_high > ph[pidx][1]

    checks = {"Fresh zone": fresh, "Close/wick in zone": close_wick,
              "Confluence stack": confluence, "Lowest demand": lowest,
              "Discount (<50% fib)": discount, "Break of structure": bos}
    score = sum(checks.values())

    sl = min(bot, l[t], l[e]) - SL_BUFFER_ATR * a
    risk = entry - sl
    if risk <= 0:
        return None
    tp1, tp2 = entry + risk, entry + 1.5 * risk
    tp3 = swing_high if swing_high > tp2 else entry + 2 * risk

    return {"base": base, "tap": t, "e": e, "zone_time": C[base]["t"], "time": C[e]["ct"],
            "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "top": top, "bot": bot, "ztype": ztype, "checks": checks, "score": score,
            "ema_conf": ema_conf, "sr_conf": sr_conf}


def unflip(s):
    s = dict(s)
    for key in ("entry", "sl", "tp1", "tp2", "tp3"):
        s[key] = -s[key]
    s["top"], s["bot"] = -s["bot"], -s["top"]
    return s


def setups_for(C, H, side):
    if side == "LONG":
        return find_setups(C, H)
    return [unflip(s) for s in find_setups(flip(C), flip(H))]


def live_setups(C, H, side):
    n = len(C)
    out = []
    for s in setups_for(C, H, side):
        if s["e"] < n - SIGNAL_LOOKBACK:
            continue
        after = C[s["e"] + 1:]
        if side == "LONG":
            stale = any(x["l"] <= s["sl"] or x["h"] >= s["tp1"] for x in after)
        else:
            stale = any(x["h"] >= s["sl"] or x["l"] <= s["tp1"] for x in after)
        if not stale:                      # skip if SL/TP1 already hit before we saw it
            out.append(s)
    return out

# ============================ TELEGRAM ============================

def tg(text):
    if not TG_TOKEN or not TG_CHATS:
        print("[telegram not configured]\n" + text)
        return False
    ok = True
    for chat in TG_CHATS:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=15)
            if r.status_code != 200:
                print("Telegram error:", r.status_code, r.text[:200])
                ok = False
        except Exception as e:
            print("Telegram error:", e)
            ok = False
    return ok


def fp(x):
    ax = abs(x)
    d = 2 if ax >= 100 else 3 if ax >= 10 else 4 if ax >= 1 else 5 if ax >= 0.1 else 6 if ax >= 0.01 else 8
    return f"{x:.{d}f}"


def pct(a, b):
    return f"{abs(b - a) / a * 100:.2f}%"


def signal_message(sym, side, s, source):
    grade = "A+" if s["score"] == 6 else "A" if s["score"] == 5 else "B"
    icon = "🟢" if side == "LONG" else "🔴"
    zone = "Demand" if side == "LONG" else "Supply"
    t = datetime.fromtimestamp(s["time"] / 1000, IST).strftime("%d %b %I:%M %p IST")
    rr3 = abs(s["tp3"] - s["entry"]) / abs(s["entry"] - s["sl"])
    checks = "\n".join(f"{'✅' if v else '❌'} {k}" for k, v in s["checks"].items())
    return (
        f"{icon} <b>{side} {sym}</b>  ({TIMEFRAME})\n"
        f"Grade: <b>{grade}</b> ({s['score']}/6)\n\n"
        f"Entry: <code>{fp(s['entry'])}</code>\n"
        f"Stop Loss: <code>{fp(s['sl'])}</code> ({pct(s['entry'], s['sl'])})\n"
        f"TP1 (1R): <code>{fp(s['tp1'])}</code>\n"
        f"TP2 (1.5R): <code>{fp(s['tp2'])}</code>\n"
        f"TP3 ({rr3:.1f}R): <code>{fp(s['tp3'])}</code>\n\n"
        f"{zone} zone: {fp(s['bot'])} – {fp(s['top'])} ({s['ztype']})\n"
        f"{checks}\n"
        f"✅ Trend confirmed (EMA{EMA_LEN} {TIMEFRAME} + {HTF})\n\n"
        f"🕒 {t}\n📊 {SOURCE_LABEL.get(source, source)}\n"
        f"<i>Manual trade only. Check the chart before entering. Not financial advice.</i>"
    )

# ============================ STATE ============================

def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("sent", {})
    st.setdefault("last_heartbeat", "")
    st.setdefault("last_error_alert", 0)
    return st


def save_state(st):
    cutoff = time.time() - 14 * 86400
    st["sent"] = {k: v for k, v in st["sent"].items() if v >= cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)

# ============================ MODES ============================

def run_scan():
    st = load_state()
    sent, errors = 0, []
    for sym in SYMBOLS:
        try:
            C, src = fetch_klines(sym, TIMEFRAME, CANDLES)
            H, _ = fetch_klines(sym, HTF, CANDLES, source=src)
            for side in ("LONG", "SHORT"):
                for s in live_setups(C, H, side):
                    if s["score"] < MIN_SCORE:
                        continue
                    key = f"{sym}|{side}|{TIMEFRAME}|{s['zone_time']}"
                    if key in st["sent"]:
                        continue
                    if tg(signal_message(sym, side, s, src)):
                        st["sent"][key] = int(time.time())
                        sent += 1
            print(f"{sym}: ok ({src}, {len(C)} candles)")
        except Exception as e:
            errors.append(str(e))
            print("ERROR", e)
        time.sleep(0.15)

    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    if now.hour >= HEARTBEAT_HOUR_IST and st["last_heartbeat"] != today:
        msg = (f"✅ <b>S&D bot running</b>\nScanning {len(SYMBOLS)} coins on {TIMEFRAME} (+{HTF} trend)\n"
               f"Signals in last 24h: {sum(1 for v in st['sent'].values() if v > time.time() - 86400)}")
        if errors:
            msg += f"\n⚠️ Errors this run: {len(errors)}\n" + "\n".join(errors[:3])
        if tg(msg):
            st["last_heartbeat"] = today
    if errors and len(errors) == len(SYMBOLS) and time.time() - st["last_error_alert"] > 6 * 3600:
        if tg("⚠️ <b>S&D bot:</b> could not fetch data for any coin.\n" + errors[0]):
            st["last_error_alert"] = int(time.time())

    save_state(st)
    print(f"Done. New signals: {sent}. Errors: {len(errors)}")


def simulate(C, s, side, r_target):
    risk = abs(s["entry"] - s["sl"])
    tp = s["entry"] + r_target * risk if side == "LONG" else s["entry"] - r_target * risk
    for x in C[s["e"] + 1:]:
        hit_sl = x["l"] <= s["sl"] if side == "LONG" else x["h"] >= s["sl"]
        hit_tp = x["h"] >= tp if side == "LONG" else x["l"] <= tp
        if hit_sl:
            return -1.0          # if SL and TP in the same candle, count it as a loss
        if hit_tp:
            return r_target
    return None                  # still open


def run_backtest(total):
    rows, all_res = [], {1.0: [], 1.5: []}
    for sym in SYMBOLS:
        try:
            C, src = fetch_history(sym, TIMEFRAME, total)
            H, _ = fetch_history(sym, HTF, max(total // 4 + EMA_LEN + 50, 300), source=src)
        except Exception as e:
            print("ERROR", e)
            continue
        res = {1.0: [], 1.5: []}
        count = 0
        for side in ("LONG", "SHORT"):
            for s in setups_for(C, H, side):
                if s["score"] < MIN_SCORE:
                    continue
                count += 1
                for r in res:
                    out = simulate(C, s, side, r)
                    if out is not None:
                        res[r].append(out)
        for r in res:
            all_res[r] += res[r]
        days = (C[-1]["ct"] - C[0]["t"]) / 86400000
        w1 = sum(1 for x in res[1.0] if x > 0)
        w15 = sum(1 for x in res[1.5] if x > 0)
        rows.append(f"| {sym} | {days:.0f}d | {count} | "
                    f"{(w1 / len(res[1.0]) * 100 if res[1.0] else 0):.0f}% ({sum(res[1.0]):+.1f}R) | "
                    f"{(w15 / len(res[1.5]) * 100 if res[1.5] else 0):.0f}% ({sum(res[1.5]):+.1f}R) |")
        print(rows[-1])

    def summ(lst):
        if not lst:
            return "no closed trades"
        w = sum(1 for x in lst if x > 0)
        return f"{len(lst)} trades, win rate {w / len(lst) * 100:.0f}%, total {sum(lst):+.1f}R"

    report = (f"## Backtest: {TIMEFRAME}, min score {MIN_SCORE}/6, ~{total} candles per coin\n\n"
              "| Coin | Period | Signals | TP 1R win% | TP 1.5R win% |\n|---|---|---|---|---|\n"
              + "\n".join(rows) +
              f"\n\n**All coins, TP 1R:** {summ(all_res[1.0])}\n\n**All coins, TP 1.5R:** {summ(all_res[1.5])}\n\n"
              "_Fees, slippage and funding are not included. Past results do not guarantee future results._\n")
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write(report)
    tg(f"📈 <b>Backtest done</b> ({TIMEFRAME}, score ≥ {MIN_SCORE})\n"
       f"TP 1R: {summ(all_res[1.0])}\nTP 1.5R: {summ(all_res[1.5])}\n"
       f"Full table: GitHub → Actions → this run's summary.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send a Telegram test message")
    ap.add_argument("--backtest", action="store_true", help="backtest on history")
    ap.add_argument("--candles", type=int, default=5000, help="candles per coin for backtest")
    args = ap.parse_args()
    resolve_symbols()
    if args.test:
        ok = tg(f"👋 <b>S&D bot connected!</b>\nCoins: {', '.join(SYMBOLS)}\n"
                f"Timeframe: {TIMEFRAME} (trend: {HTF}), min grade: {MIN_SCORE}/6")
        sys.exit(0 if ok else 1)
    if args.backtest:
        run_backtest(args.candles)
        return
    run_scan()


if __name__ == "__main__":
    main()
