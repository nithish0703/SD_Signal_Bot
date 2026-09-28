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
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ============================ SETTINGS ============================
# Coins to scan: the TOP_N USDT coins by 24h trading volume are picked automatically
# on every run. To use your own fixed list instead, set a repo variable
# SYMBOLS="BTCUSDT,ETHUSDT,..." (GitHub -> Settings -> Secrets and variables -> Variables).
TOP_N = 50                 # only coins that are listed on Binance USDT-M Futures
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
TIMEFRAME = "1h"           # entry timeframe (1h beat 15m in backtest: fees hurt less)
HTF = "4h"                 # higher timeframe for trend confirmation (step 2)
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
MIN_SL_PCT = 1.0           # skip trades whose stop is closer than 1% (fees would eat the profit)
TRAIL_ATR = 3.0            # exit plan: trailing stop = 3 x ATR (best exit in the 1h backtest)
SIMPLE_TP_R = 2.0          # simple alternative exit: fixed 2R target
MAX_TRADE_DAYS = 30        # tracked trades still open after this are closed at market
MARGIN_OPTIONS = [5.0, 8.0]  # margins you use per trade (Isolated); leverage is shown for each
RISK_USD = 1.0             # max loss if SL is hit; leverage is picked per trade to match this
MAX_LEVERAGE = 20          # never suggest more than this
MMR_PCT = 0.5              # approx. maintenance margin %, for the liquidation estimate
FEE_PCT = 0.10             # round-trip fee in % (Binance taker 0.05% in + 0.05% out).
                           # With limit (maker) entry + exit it is ~0.04%.
CONFLUENCE_PAD_ATR = 0.25  # how close EMA / old S-R must be to the zone

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
IST = timezone(timedelta(hours=5, minutes=30))
HEARTBEAT_HOUR_IST = 9     # daily "bot alive" message after 9 AM IST

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHATS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
MANUAL_SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "").split(",") if s.strip()]
if os.getenv("TOP_N", "").strip().isdigit():
    TOP_N = int(os.getenv("TOP_N"))


def _env_num(name, default, cast=float):
    """Read an optional GitHub repo variable (empty or invalid -> keep default)."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        print(f"Ignoring invalid {name}={raw!r}")
        return default


MIN_SCORE = max(1, min(6, _env_num("MIN_SCORE", MIN_SCORE, int)))
MIN_SL_PCT = _env_num("MIN_SL_PCT", MIN_SL_PCT)
FEE_PCT = _env_num("FEE_PCT", FEE_PCT)
TRAIL_ATR = _env_num("TRAIL_ATR", TRAIL_ATR)
_m = [x.strip() for x in os.getenv("MARGIN_USD", "").split(",") if x.strip()]
try:
    MARGIN_OPTIONS = [float(x) for x in _m] or MARGIN_OPTIONS
except ValueError:
    print(f"Ignoring invalid MARGIN_USD={os.getenv('MARGIN_USD')!r}")
MARGIN_USD = MARGIN_OPTIONS[0]
RISK_USD = _env_num("RISK_USD", RISK_USD)
MAX_LEVERAGE = max(1, _env_num("MAX_LEVERAGE", MAX_LEVERAGE, int))
TIMEFRAME = os.getenv("TIMEFRAME", "").strip() or TIMEFRAME
HTF = os.getenv("HTF", "").strip() or HTF

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
        try:
            batch, source = fetch_klines(symbol, interval, 1000, end_time=end, source=source)
        except RuntimeError:
            if out:          # reached the coin's listing date: use the history we have
                break
            raise
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


def top_volume_ranked():
    """All USDT pairs ranked by 24h quote volume -> (symbols, source).
    Stablecoins, leveraged tokens and odd symbols are removed."""
    for name, url in TICKER_SOURCES:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                continue
            rows = []
            for x in r.json():
                sym = x.get("symbol", "")
                if not sym.endswith("USDT") or not re.fullmatch(r"[A-Z0-9]{2,20}USDT", sym):
                    continue                     # skips odd symbols (e.g. non-English meme tokens)
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
            if rows:
                return [s for _, s in rows], name
        except Exception as e:
            print(f"top list via {name} failed: {type(e).__name__}")
    return None, None


def top_volume_symbols(n):
    syms, src = top_volume_ranked()
    return (syms[:n], src) if syms and len(syms) >= n else (None, None)


# Futures name when it differs from spot (e.g. PEPEUSDT on spot = 1000PEPEUSDT on futures)
FUTURES_NAME = {}
FUT_CHECK_URL = "https://data.binance.vision/data/futures/um/daily/klines/{s}/1d/{s}-1d-{d}.zip"


def _futures_name(sym):
    """Futures symbol for a spot pair if it is trading on USDT-M futures, else None.
    Checks Binance's public daily futures files (reachable from GitHub, unlike fapi)."""
    base = sym[:-4]
    today = datetime.now(timezone.utc).date()
    for fut in (sym, f"1000{base}USDT"):
        for lag in (2, 3):                        # files are published with ~1 day delay
            d = (today - timedelta(days=lag)).isoformat()
            try:
                r = requests.head(FUT_CHECK_URL.format(s=fut, d=d), timeout=10)
                if r.status_code == 200:
                    return fut
                if r.status_code not in (403, 404):
                    raise RuntimeError(f"HTTP {r.status_code}")
            except requests.RequestException as e:
                raise RuntimeError(type(e).__name__)
    return None


def futures_only(candidates, n):
    """Keep the first n candidates that are listed on Binance Futures."""
    from concurrent.futures import ThreadPoolExecutor
    found, errors = [], 0
    for i in range(0, min(len(candidates), 200), 25):
        chunk = candidates[i:i + 25]
        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(lambda c: _safe_fut(c), chunk))
        for sym, res in zip(chunk, results):
            if res == "error":
                errors += 1
            elif res:
                found.append(sym)
                if res != sym:
                    FUTURES_NAME[sym] = res
        if len(found) >= n:
            break
    return found[:n], errors


def _safe_fut(sym):
    try:
        return _futures_name(sym)
    except RuntimeError:
        return "error"


def resolve_symbols():
    """Fill SYMBOLS: manual repo variable > top-volume futures coins > fallback list."""
    global SYMBOLS
    if MANUAL_SYMBOLS:
        SYMBOLS = MANUAL_SYMBOLS
        print(f"Using {len(SYMBOLS)} coins from SYMBOLS variable")
        return
    ranked, src = top_volume_ranked()
    if ranked and src == "futures":               # futures API reachable: list is futures-only
        SYMBOLS = ranked[:TOP_N]
    elif ranked:
        syms, errors = futures_only(ranked, TOP_N)
        if len(syms) >= min(TOP_N, 10):
            SYMBOLS = syms
        else:                                     # futures check not reachable
            print(f"Futures check failed ({errors} errors); using spot list without the check")
            SYMBOLS = ranked[:TOP_N]
            print(f"Top {len(SYMBOLS)} coins by 24h volume, NOT futures-checked: {', '.join(SYMBOLS)}")
            return
    else:
        SYMBOLS = FALLBACK_SYMBOLS[:TOP_N]
        print(f"Top list unavailable, using fallback list of {len(SYMBOLS)} coins")
        return
    print(f"Top {len(SYMBOLS)} futures coins by 24h volume ({src}): {', '.join(SYMBOLS)}")


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
            "ema_conf": ema_conf, "sr_conf": sr_conf, "atr": A[e] or a}


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


def sl_pct(s):
    return abs(s["entry"] - s["sl"]) / abs(s["entry"]) * 100


def fee_r(s):
    """Round-trip fee expressed in R (fraction of the risk)."""
    return FEE_PCT / sl_pct(s)


def live_setups(C, H, side):
    n = len(C)
    out = []
    for s in setups_for(C, H, side):
        if s["e"] < n - SIGNAL_LOOKBACK or sl_pct(s) < MIN_SL_PCT:
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


def sizing(s, side, margin=None):
    """Leverage for a fixed margin so that an SL hit loses about RISK_USD (never more).
    Also capped so liquidation stays at least 2x further away than the SL."""
    margin = margin or MARGIN_USD
    slp = sl_pct(s)
    want = RISK_USD / (margin * slp / 100)
    safe = 100 / (2 * slp)                         # liquidation >= 2 x SL distance
    lev = int(max(1, min(want, safe, MAX_LEVERAGE)))
    pos = margin * lev
    loss = pos * slp / 100
    liq_dist = max(0.0, 100 / lev - MMR_PCT)       # % move to liquidation (isolated, approx.)
    liq = s["entry"] * (1 - liq_dist / 100) if side == "LONG" else s["entry"] * (1 + liq_dist / 100)
    return {"margin": margin, "lev": lev, "pos": pos, "loss": loss, "fee": pos * FEE_PCT / 100,
            "liq": liq, "liq_dist": liq_dist, "capped": want > lev + 0.999}


def signal_message(sym, side, s, source):
    grade = "A+" if s["score"] == 6 else "A" if s["score"] == 5 else "B"
    icon = "🟢" if side == "LONG" else "🔴"
    zone = "Demand" if side == "LONG" else "Supply"
    t = datetime.fromtimestamp(s["time"] / 1000, IST).strftime("%d %b %I:%M %p IST")
    risk = abs(s["entry"] - s["sl"])
    tp_simple = s["entry"] + SIMPLE_TP_R * risk if side == "LONG" else s["entry"] - SIMPLE_TP_R * risk
    trail = TRAIL_ATR * s["atr"]
    best_word = "highest high" if side == "LONG" else "lowest low"
    move = "−" if side == "LONG" else "+"
    checks = "\n".join(f"{'✅' if v else '❌'} {k}" for k, v in s["checks"].items())
    opts = [sizing(s, side, m) for m in MARGIN_OPTIONS]
    z = opts[0]
    return (
        f"{icon} <b>{side} {sym}</b>  ({TIMEFRAME})\n"
        + (f"⚠️ On Futures this is <b>{FUTURES_NAME[sym]}</b> (price ×1000)\n" if sym in FUTURES_NAME else "")
        + f"Grade: <b>{grade}</b> ({s['score']}/6)\n\n"
        f"Entry: <code>{fp(s['entry'])}</code>\n"
        f"Stop Loss: <code>{fp(s['sl'])}</code> ({pct(s['entry'], s['sl'])})\n\n"
        f"🎯 <b>Exit plan: trailing stop {TRAIL_ATR:g}×ATR</b>\n"
        f"Trail distance: <code>{fp(trail)}</code>\n"
        f"After each {TIMEFRAME} candle close: SL = {best_word} {move} {fp(trail)} (never move it back).\n"
        f"🤖 I'll send you SL updates and the exit here.\n"
        f"Simple option: TP {SIMPLE_TP_R:g}R <code>{fp(tp_simple)}</code>\n\n"
        f"💰 <b>Your trade (Isolated)</b>\n"
        + "".join(f"${o['margin']:g} margin → <b>{o['lev']}x</b> (position ${o['pos']:.0f}, "
                  f"SL loss −${o['loss']:.2f}, liq ≈ <code>{fp(o['liq'])}</code>)\n" for o in opts)
        + f"Profit at {SIMPLE_TP_R:g}R ≈ +${z['loss'] * SIMPLE_TP_R:.2f} | SL is {sl_pct(s):.1f}% away\n"
        + (f"ℹ️ Leverage capped for safety, so loss is below ${RISK_USD:g}.\n" if z["capped"] else "")
        + (f"⚠️ SL is wide: even at 1x the loss is above ${RISK_USD:g}. Use less margin or skip.\n"
           if z["loss"] > RISK_USD * 1.01 else "")
        + f"💸 Fees ~${z['fee']:.2f} ({fee_r(s):.2f}R). Limit orders cut this.\n\n"
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
    st.setdefault("open", {})      # signals being tracked (trailing stop)
    st.setdefault("closed", [])    # finished paper trades
    return st


def save_state(st):
    cutoff = time.time() - 14 * 86400
    st["sent"] = {k: v for k, v in st["sent"].items() if v >= cutoff}
    st["closed"] = st["closed"][-300:]
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)

# ============================ MODES ============================

# ============================ TRADE TRACKING ============================

def _px(v, side):
    """Mirror a price for SHORT so one piece of code handles both sides."""
    return v if side == "LONG" else -v


def open_trade(st, key, sym, side, s, source):
    risk = abs(s["entry"] - s["sl"])
    st["open"][key] = {
        "sym": sym, "side": side, "tf": TIMEFRAME, "source": source,
        "entry": s["entry"], "sl": s["sl"], "stop": s["sl"], "best": s["entry"],
        "risk": risk, "trail": TRAIL_ATR * s["atr"], "slp": sl_pct(s), "fee_r": fee_r(s),
        "tp_simple": (s["entry"] + SIMPLE_TP_R * risk) if side == "LONG" else (s["entry"] - SIMPLE_TP_R * risk),
        "simple_r": None, "opened": s["time"], "last_ct": s["time"], "notified_stop": s["sl"],
        "usd_r": sizing(s, side)["loss"],           # $ per 1R with the suggested leverage
    }


def update_trade(tr, candles):
    """Walk new closed candles. Returns ('closed', exit_price, R) or ('open', None, None)."""
    side = tr["side"]
    E, SL, TP = _px(tr["entry"], side), _px(tr["sl"], side), _px(tr["tp_simple"], side)
    stop, best = _px(tr["stop"], side), _px(tr["best"], side)
    for x in candles:
        if x["ct"] <= tr["last_ct"]:
            continue
        if side == "LONG":
            hi, lo, op = x["h"], x["l"], x["o"]
        else:
            hi, lo, op = -x["l"], -x["h"], -x["o"]
        if tr["simple_r"] is None:                    # the simple fixed-2R version
            if lo <= SL:
                tr["simple_r"] = -1.0
            elif hi >= TP:
                tr["simple_r"] = SIMPLE_TP_R
        tr["last_ct"] = x["ct"]
        if lo <= stop:                                # trailing stop hit
            exit_px = min(op, stop)
            tr["stop"], tr["best"] = _px(stop, side), _px(best, side)
            return "closed", _px(exit_px, side), (exit_px - E) / tr["risk"]
        best = max(best, hi)
        stop = max(stop, best - tr["trail"])
        tr["stop"], tr["best"] = _px(stop, side), _px(best, side)
    if candles and time.time() * 1000 - tr["opened"] > MAX_TRADE_DAYS * 86400000:
        last = _px(candles[-1]["c"], side)
        return "closed", candles[-1]["c"], (last - E) / tr["risk"]
    return "open", None, None


def locked_r(tr):
    return (_px(tr["stop"], tr["side"]) - _px(tr["entry"], tr["side"])) / tr["risk"]


def manage_trades(st, cache):
    """Send SL updates / exits for tracked signals and record paper results."""
    for key, tr in list(st["open"].items()):
        ck = (tr["sym"], tr["tf"])
        try:
            if ck not in cache:
                cache[ck] = fetch_klines(tr["sym"], tr["tf"], CANDLES, source=tr.get("source"))[0]
            status, exit_px, r = update_trade(tr, cache[ck])
        except Exception as e:
            print("track error", tr["sym"], e)
            continue
        icon = "🟢" if tr["side"] == "LONG" else "🔴"
        name = f"{icon} <b>{tr['side']} {tr['sym']}</b> ({tr['tf']})"
        if status == "closed":
            simple = tr["simple_r"] if tr["simple_r"] is not None else max(-1.0, min(SIMPLE_TP_R, r))
            net = r - tr["fee_r"]
            usd = net * tr.get("usd_r", RISK_USD)
            tg(f"🏁 {name} closed at <code>{fp(exit_px)}</code>\n"
               f"Trailing result: <b>{r:+.2f}R</b> (after fees ~{net:+.2f}R ≈ <b>{'+' if usd >= 0 else '−'}${abs(usd):.2f}</b>)\n"
               f"Simple {SIMPLE_TP_R:g}R plan would be: {simple:+.2f}R")
            st["closed"].append({"sym": tr["sym"], "side": tr["side"], "tf": tr["tf"], "r": round(r, 3),
                                 "net": round(net, 3), "simple": round(simple - tr["fee_r"], 3),
                                 "usd": round(net * tr.get("usd_r", RISK_USD), 2),
                                 "closed": int(time.time())})
            del st["open"][key]
            continue
        moved = (_px(tr["stop"], tr["side"]) - _px(tr["notified_stop"], tr["side"])) / tr["risk"]
        if moved >= 0.25:                             # only ping for meaningful moves
            lr = locked_r(tr)
            if abs(lr) < 0.01:
                lock = "breakeven, no risk left"
            elif lr > 0:
                lock = f"locks {lr:+.2f}R profit"
            else:
                lock = f"risk now {-lr:.2f}R"
            if tg(f"🔁 {name}: move SL to <code>{fp(tr['stop'])}</code> ({lock})"):
                tr["notified_stop"] = tr["stop"]


def paper_stats(st, days=None):
    rows = st["closed"]
    if days:
        rows = [c for c in rows if c["closed"] > time.time() - days * 86400]
    if not rows:
        return "no closed trades yet"
    w = sum(1 for c in rows if c["net"] > 0)
    usd = sum(c.get("usd", c["net"] * RISK_USD) for c in rows)
    return (f"{len(rows)} trades, win {w / len(rows) * 100:.0f}%, trailing {sum(c['net'] for c in rows):+.1f}R "
            f"({'+' if usd >= 0 else '−'}${abs(usd):.2f}), "
            f"simple {SIMPLE_TP_R:g}R {sum(c['simple'] for c in rows):+.1f}R (after fees)")


def run_scan():
    st = load_state()
    sent, errors, cache = 0, [], {}
    for sym in SYMBOLS:
        try:
            C, src = fetch_klines(sym, TIMEFRAME, CANDLES)
            cache[(sym, TIMEFRAME)] = C
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
                        open_trade(st, key, sym, side, s, src)
                        sent += 1
            print(f"{sym}: ok ({src}, {len(C)} candles)")
        except Exception as e:
            errors.append(str(e))
            print("ERROR", e)
        time.sleep(0.15)

    manage_trades(st, cache)

    now = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    if now.hour >= HEARTBEAT_HOUR_IST and st["last_heartbeat"] != today:
        msg = (f"✅ <b>S&D bot running</b>\nScanning {len(SYMBOLS)} coins on {TIMEFRAME} (+{HTF} trend)\n"
               f"Signals in last 24h: {sum(1 for v in st['sent'].values() if v > time.time() - 86400)}\n"
               f"Open tracked trades: {len(st['open'])}\n\n"
               f"📒 <b>Paper results</b>\nLast 30 days: {paper_stats(st, 30)}\nAll time: {paper_stats(st)}")
        if errors:
            msg += f"\n\n⚠️ Errors this run: {len(errors)}\n" + "\n".join(errors[:3])
        if tg(msg):
            st["last_heartbeat"] = today
    if errors and len(errors) == len(SYMBOLS) and time.time() - st["last_error_alert"] > 6 * 3600:
        if tg("⚠️ <b>S&D bot:</b> could not fetch data for any coin.\n" + errors[0]):
            st["last_error_alert"] = int(time.time())

    save_state(st)
    print(f"Done. New signals: {sent}. Open trades: {len(st['open'])}. Errors: {len(errors)}")


def simulate(C, s, side, r_target):
    """Gross result in R for a fixed R-multiple target (fees handled separately)."""
    risk = abs(s["entry"] - s["sl"])
    return _simulate_tp(C, s, side, r_target * risk)


def _simulate_tp(C, s, side, tp_dist):
    risk = abs(s["entry"] - s["sl"])
    tp = s["entry"] + tp_dist if side == "LONG" else s["entry"] - tp_dist
    for x in C[s["e"] + 1:]:
        hit_sl = x["l"] <= s["sl"] if side == "LONG" else x["h"] >= s["sl"]
        hit_tp = x["h"] >= tp if side == "LONG" else x["l"] <= tp
        if hit_sl:
            return -1.0          # if SL and TP in the same candle, count it as a loss
        if hit_tp:
            return tp_dist / risk
    return None                  # still open


def _simulate_trail(C, s, side, k):
    """ATR trailing stop: stop follows the best price by k x ATR, never moves back."""
    risk = abs(s["entry"] - s["sl"])
    dist = k * s["atr"]
    stop = s["sl"]
    for x in C[s["e"] + 1:]:
        if side == "LONG":
            if x["l"] <= stop:                   # check the stop set by earlier candles first
                return (min(x["o"], stop) - s["entry"]) / risk
            stop = max(stop, x["h"] - dist)
        else:
            if x["h"] >= stop:
                return (s["entry"] - max(x["o"], stop)) / risk
            stop = min(stop, x["l"] + dist)
    return None


# (label, kind, value). "atr" TP is never closer than 1R.
EXITS = [
    ("Fixed 1R", "fixed", 1.0), ("Fixed 1.5R", "fixed", 1.5), ("Fixed 2R", "fixed", 2.0),
    ("TP 1.5 ATR", "atr", 1.5), ("TP 2 ATR", "atr", 2.0), ("TP 3 ATR", "atr", 3.0),
    ("Trail 2 ATR", "trail", 2.0), ("Trail 3 ATR", "trail", 3.0),
]
MAKER_FEE_PCT = 0.04


def run_exit(C, s, side, kind, v):
    if kind == "fixed":
        return simulate(C, s, side, v)
    if kind == "atr":
        risk = abs(s["entry"] - s["sl"])
        return _simulate_tp(C, s, side, max(v * s["atr"], risk))
    return _simulate_trail(C, s, side, v)


def _stats(trades, key, fee_pct):
    """-> (closed trades, win %, net R per trade, net total R, avg win R, max losing streak)."""
    closed = [t for t in trades if t["res"][key] is not None]
    if not closed:
        return 0, 0.0, 0.0, 0.0, 0.0, 0
    closed.sort(key=lambda t: t["time"])
    rs = [t["res"][key] - fee_pct / t["slp"] for t in closed]
    wins = [t["res"][key] for t in closed if t["res"][key] > 0]
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r < 0 else 0
        worst = max(worst, streak)
    return (len(rs), len(wins) / len(rs) * 100, sum(rs) / len(rs), sum(rs),
            (sum(wins) / len(wins)) if wins else 0.0, worst)


def run_backtest(total):
    trades, per_coin = [], []
    for sym in SYMBOLS:
        try:
            C, src = fetch_history(sym, TIMEFRAME, total)
            H, _ = fetch_history(sym, HTF, max(total // 4 + EMA_LEN + 50, 300), source=src)
        except Exception as e:
            print("ERROR", e)
            continue
        coin = []
        for side in ("LONG", "SHORT"):
            for s in setups_for(C, H, side):
                if s["score"] < 5 or sl_pct(s) < MIN_SL_PCT:
                    continue
                coin.append({"sym": sym, "score": s["score"], "slp": sl_pct(s), "time": s["time"],
                             "res": {lbl: run_exit(C, s, side, k, v) for lbl, k, v in EXITS}})
        trades += coin
        days = (C[-1]["ct"] - C[0]["t"]) / 86400000
        mine = [t for t in coin if t["score"] >= MIN_SCORE]
        n, w, _, tot, _, _ = _stats(mine, "Fixed 1R", FEE_PCT)
        per_coin.append(f"| {sym} | {days:.0f}d | {n} | {w:.0f}% | {tot:+.1f}R |")
        print(per_coin[-1])

    groups = [("Score ≥5", [t for t in trades if t["score"] >= 5]),
              ("Score 6", [t for t in trades if t["score"] >= 6])]
    head = ("| Exit | Trades | Win% | Avg win | Net/trade (market) | Net/trade (limit) | Total (market) | Max loss streak |\n"
            "|---|---|---|---|---|---|---|---|\n")
    sections, best = [], {}
    for gname, sel in groups:
        rows = []
        for lbl, _, _ in EXITS:
            n, w, avg, tot, aw, ls = _stats(sel, lbl, FEE_PCT)
            _, _, avg_l, _, _, _ = _stats(sel, lbl, MAKER_FEE_PCT)
            rows.append(f"| {lbl} | {n} | {w:.0f}% | {aw:.2f}R | {avg:+.2f}R | {avg_l:+.2f}R | {tot:+.1f}R | {ls} |")
            if n >= 15:
                best.setdefault(gname, []).append((avg, lbl, n, w, tot))
        sections.append(f"### {gname}, SL ≥{MIN_SL_PCT}%\n\n" + head + "\n".join(rows))

    report = (f"## Exit comparison (~{total} candles of {TIMEFRAME}, {len(per_coin)} coins)\n\n"
              f"Net = average R per trade after fees (market {FEE_PCT:.2f}%, limit {MAKER_FEE_PCT:.2f}% round trip). "
              "Positive = profitable. ATR TPs are never closer than 1R. Trailing stops start at the normal SL.\n\n"
              + "\n\n".join(sections) +
              f"\n\n### Per coin (score ≥{MIN_SCORE}, Fixed 1R, market fees)\n\n"
              "| Coin | Period | Trades | Win% | Net total |\n|---|---|---|---|---|\n" + "\n".join(per_coin) +
              "\n\n_Slippage and funding are not included. Past results do not guarantee future results._\n")
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write(report)

    msg = f"📈 <b>Exit backtest</b> ({TIMEFRAME}, {len(per_coin)} coins, market fees {FEE_PCT:.2f}%)\n"
    for gname, _ in groups:
        ranked = sorted(best.get(gname, []), reverse=True)[:3]
        msg += f"\n<b>{gname}</b> – top exits:\n"
        if not ranked:
            msg += "not enough trades\n"
        for i, (avg, lbl, n, w, tot) in enumerate(ranked, 1):
            msg += f"{i}. {lbl}: {avg:+.2f}R/trade, win {w:.0f}%, {n} trades ({tot:+.1f}R)\n"
    msg += "\nFull table: GitHub → Actions → this run's summary."
    tg(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send a Telegram test message")
    ap.add_argument("--backtest", action="store_true", help="backtest on history")
    ap.add_argument("--candles", type=int, default=5000, help="candles per coin for backtest")
    args = ap.parse_args()
    resolve_symbols()
    if args.test:
        ok = tg(f"👋 <b>S&D bot connected!</b>\n{len(SYMBOLS)} coins: "
                f"{', '.join(f'{x} (={FUTURES_NAME[x]})' if x in FUTURES_NAME else x for x in SYMBOLS)}\n"
                f"Timeframe: {TIMEFRAME} (trend: {HTF}), min grade: {MIN_SCORE}/6\n"
                f"Min SL: {MIN_SL_PCT}%, fees assumed: {FEE_PCT:.2f}%\n"
                f"Exit plan: trailing stop {TRAIL_ATR:g}×ATR (simple option {SIMPLE_TP_R:g}R)\n"
                f"Sizing: ${' / $'.join(f'{m:g}' for m in MARGIN_OPTIONS)} margin, max loss ${RISK_USD:g}/trade, leverage ≤{MAX_LEVERAGE}x")
        sys.exit(0 if ok else 1)
    if args.backtest:
        run_backtest(args.candles)
        return
    run_scan()


if __name__ == "__main__":
    main()
