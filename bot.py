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
STRATEGY = "smc"           # live signals: "smc" (best in backtest), "sd" (supply & demand) or "both"
SMC_ENTRY = "mid"          # SMC limit entry: "mid" = 50% of the FVG (best), "top" = FVG edge
SMC_REQUIRE_DISCOUNT = True  # only buy in discount / sell in premium (consistent in backtest)
SMC_REQUIRE_TREND = False  # also require the 4h trend to agree

# Market-regime filters (0 = off). Turn on only what the backtest shows is consistently better.
FILTER_HTF_ADX = 0         # e.g. 20: skip signals when higher-TF ADX is below this (no trend)
FILTER_ADX = 0             # e.g. 20: same on the entry timeframe
FILTER_CHOP = 0            # e.g. 50: skip signals when Choppiness Index is above this (sideways)
FILTER_SLOPE = 0           # e.g. 1.0: EMA50 must have moved >= this many ATRs in 20 candles
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
STRATEGY = (os.getenv("STRATEGY", "").strip().lower() or STRATEGY)
if STRATEGY not in ("smc", "sd", "both"):
    print(f"Unknown STRATEGY={STRATEGY!r}, using smc")
    STRATEGY = "smc"
SMC_ENTRY = (os.getenv("SMC_ENTRY", "").strip().lower() or SMC_ENTRY)
SMC_REQUIRE_TREND = os.getenv("SMC_REQUIRE_TREND", "").strip().lower() in ("1", "true", "yes") or SMC_REQUIRE_TREND
if os.getenv("SMC_REQUIRE_DISCOUNT", "").strip().lower() in ("0", "false", "no"):
    SMC_REQUIRE_DISCOUNT = False
FILTER_HTF_ADX = _env_num("FILTER_HTF_ADX", FILTER_HTF_ADX)
FILTER_ADX = _env_num("FILTER_ADX", FILTER_ADX)
FILTER_CHOP = _env_num("FILTER_CHOP", FILTER_CHOP)
FILTER_SLOPE = _env_num("FILTER_SLOPE", FILTER_SLOPE)
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
                        "c": float(k[4]), "v": float(k[5]), "ct": int(k[6])} for k in r.json()]
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


def adx(h, l, c, n=14):
    """Wilder's ADX: trend strength 0-100 (below ~20 = no trend / choppy)."""
    size = len(c)
    out = [None] * size
    if size < 2 * n + 1:
        return out
    tr, pdm, mdm = [0.0], [0.0], [0.0]
    for i in range(1, size):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    atr_s, p_s, m_s = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(mdm[1:n + 1])
    dx = []
    for i in range(n, size):
        if i > n:
            atr_s = atr_s - atr_s / n + tr[i]
            p_s = p_s - p_s / n + pdm[i]
            m_s = m_s - m_s / n + mdm[i]
        pdi = 100 * p_s / atr_s if atr_s else 0
        mdi = 100 * m_s / atr_s if atr_s else 0
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0)
        if len(dx) == n:
            out[i] = sum(dx) / n
        elif len(dx) > n:
            out[i] = (out[i - 1] * (n - 1) + dx[-1]) / n
    return out


def chop(h, l, c, n=14):
    """Choppiness Index: above ~50-60 = sideways, below ~40 = trending."""
    import math
    size = len(c)
    out = [None] * size
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, size)]
    for i in range(n, size):
        rng = max(h[i - n + 1:i + 1]) - min(l[i - n + 1:i + 1])
        if rng > 0:
            out[i] = 100 * math.log10(sum(tr[i - n + 1:i + 1]) / rng) / math.log10(n)
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
    return [{"t": x["t"], "ct": x["ct"], "o": -x["o"], "c": -x["c"], "h": -x["l"], "l": -x["h"],
             "v": x.get("v", 0.0)} for x in candles]

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

    ADX = adx(h, l, cl)
    CH = chop(h, l, cl)
    hc = [x["c"] for x in H]
    he = ema(hc, EMA_LEN)
    HADX = adx([x["h"] for x in H], [x["l"] for x in H], hc)
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
                e, k = s["e"], bisect.bisect_right(hct, C[s["e"]]["ct"]) - 1
                s["adx"] = ADX[e] or 0.0
                s["chop"] = CH[e] if CH[e] is not None else 100.0
                s["hadx"] = HADX[k] if k >= 0 and HADX[k] is not None else 0.0
                s["slope"] = ((E[e] - E[e - 20]) / A[e]) if E[e - 20] is not None and A[e] else 0.0
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


def passes_filters(s):
    """Skip choppy / sideways markets according to the FILTER_* settings."""
    if FILTER_HTF_ADX and s.get("hadx", 0) < FILTER_HTF_ADX:
        return False
    if FILTER_ADX and s.get("adx", 0) < FILTER_ADX:
        return False
    if FILTER_CHOP and s.get("chop", 100) > FILTER_CHOP:
        return False
    if FILTER_SLOPE and s.get("slope", 0) < FILTER_SLOPE:
        return False
    return True


def active_filters():
    f = []
    if FILTER_HTF_ADX: f.append(f"ADX {HTF} ≥{FILTER_HTF_ADX:g}")
    if FILTER_ADX: f.append(f"ADX {TIMEFRAME} ≥{FILTER_ADX:g}")
    if FILTER_CHOP: f.append(f"CHOP ≤{FILTER_CHOP:g}")
    if FILTER_SLOPE: f.append(f"EMA slope ≥{FILTER_SLOPE:g} ATR")
    return ", ".join(f) or "none"


def live_setups(C, H, side):
    n = len(C)
    out = []
    for s in setups_for(C, H, side):
        if s["e"] < n - SIGNAL_LOOKBACK or sl_pct(s) < MIN_SL_PCT or not passes_filters(s):
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
        f"✅ Trend confirmed (EMA{EMA_LEN} {TIMEFRAME} + {HTF})\n"
        f"📐 ADX {HTF} {s.get('hadx', 0):.0f} | ADX {TIMEFRAME} {s.get('adx', 0):.0f} | CHOP {s.get('chop', 0):.0f}\n\n"
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
            if tr.get("kind") == "smc":
                manage_smc(st, key, tr, cache[ck])
                continue
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


def paper_stats(st, days=None, strategy=None):
    rows = st["closed"]
    if strategy:
        rows = [c for c in rows if c.get("strategy", "sd") == strategy]
    if days:
        rows = [c for c in rows if c["closed"] > time.time() - days * 86400]
    if not rows:
        return "no closed trades yet"
    w = sum(1 for c in rows if c["net"] > 0)
    usd = sum(c.get("usd", c["net"] * RISK_USD) for c in rows)
    return (f"{len(rows)} trades, win {w / len(rows) * 100:.0f}%, trailing {sum(c['net'] for c in rows):+.1f}R "
            f"({'+' if usd >= 0 else '−'}${abs(usd):.2f}), "
            f"simple {SIMPLE_TP_R:g}R {sum(c['simple'] for c in rows):+.1f}R (after fees)")


# ============================ SMC LIVE ============================

def smc_ok(x):
    """Live / account filters for SMC setups."""
    if SMC_REQUIRE_DISCOUNT and not x["discount"]:
        return False
    if SMC_REQUIRE_TREND and not x["trend"]:
        return False
    return sl_pct(x) >= MIN_SL_PCT and passes_filters(x) and smc_quality_ok(x)


def smc_message(sym, s, source):
    side = s["side"]
    buy = side == "LONG"
    icon = "🟢" if buy else "🔴"
    risk = abs(s["entry"] - s["sl"])
    tp_r = abs(s["tp"] - s["entry"]) / risk
    tp_what = "previous high (liquidity)" if buy else "previous low (liquidity)"
    if s["liq"] is None:
        tp_what = "2R (no clear liquidity)"
    until = datetime.fromtimestamp(s["expires_ct"] / 1000, IST).strftime("%d %b %I:%M %p IST")
    opts = [sizing(s, side, m) for m in MARGIN_OPTIONS]
    z = opts[0]
    checks = [("Liquidity sweep", True), ("Market structure shift", True), ("FVG entry (50%)" if SMC_ENTRY == "mid" else "FVG entry (edge)", True),
              ("Discount" if buy else "Premium", s["discount"]), (f"{HTF} trend agrees", s["trend"])]
    return (
        f"📌 {icon} <b>SMC LIMIT {'BUY' if buy else 'SELL'} {sym}</b>  ({TIMEFRAME})\n"
        + (f"⚠️ On Futures this is <b>{FUTURES_NAME[sym]}</b> (price ×1000)\n" if sym in FUTURES_NAME else "")
        + f"\nLimit entry: <code>{fp(s['entry'])}</code>\n"
        f"Stop Loss: <code>{fp(s['sl'])}</code> ({pct(s['entry'], s['sl'])})\n"
        f"Take Profit: <code>{fp(s['tp'])}</code> ({tp_r:.1f}R, {tp_what})\n\n"
        f"⏳ Valid until {until}. Cancel it if price reaches <code>{fp(s['cancel'])}</code> before filling.\n\n"
        f"💰 <b>Your trade (Isolated)</b>\n"
        + "".join(f"${o['margin']:g} margin → <b>{o['lev']}x</b> (position ${o['pos']:.0f}, "
                  f"SL loss −${o['loss']:.2f}, liq ≈ <code>{fp(o['liq'])}</code>)\n" for o in opts)
        + f"Profit at TP ≈ +${z['loss'] * tp_r:.2f}\n"
        + f"💸 Fees ~${z['fee']:.2f} ({fee_r(s):.2f}R).\n\n"
        + "\n".join(f"{'✅' if v else '❌'} {k}" for k, v in checks) + "\n"
        f"📐 ADX {HTF} {s.get('hadx', 0):.0f} | CHOP {s.get('chop', 0):.0f} | "
        f"displacement {s.get('disp', 0):.1f} ATR | target {s.get('tgt_r', 0):.1f}R\n"
        + (f"🔍 Quality filters passed: {smc_quality_txt()}\n" if SMC_ACTIVE else "") + "\n"
        f"🤖 I'll tell you when it fills, gets cancelled, or hits TP / SL.\n"
        f"📊 {SOURCE_LABEL.get(source, source)}\n"
        f"<i>Manual trade only. Check the chart first. Not financial advice.</i>"
    )


def open_smc_trade(st, key, sym, s, source, candles):
    st["open"][key] = {
        "kind": "smc", "status": "pending", "sym": sym, "side": s["side"], "tf": TIMEFRAME, "source": source,
        "entry": s["entry"], "sl": s["sl"], "tp": s["tp"], "cancel": s["cancel"], "expires_ct": s["expires_ct"],
        "risk": abs(s["entry"] - s["sl"]), "slp": sl_pct(s), "fee_r": fee_r(s),
        "usd_r": sizing(s, s["side"])["loss"], "opened": s["time"], "last_ct": candles[s["sig"]]["ct"],
    }


def update_smc(tr, candles):
    """Walk new closed candles for an SMC order. Returns a list of events."""
    side, ev = tr["side"], []
    E, SL, TP, CX = (_px(tr[k], side) for k in ("entry", "sl", "tp", "cancel"))
    for x in candles:
        if x["ct"] <= tr["last_ct"]:
            continue
        hi, lo, op = (x["h"], x["l"], x["o"]) if side == "LONG" else (-x["l"], -x["h"], -x["o"])
        tr["last_ct"] = x["ct"]
        if tr["status"] == "pending":
            if x["ct"] > tr["expires_ct"]:
                return ev + [("cancelled", "expired, never filled")]
            if lo <= E:
                tr["status"] = "filled"
                ev.append(("filled", tr["entry"]))
                if lo <= SL:                                  # stopped out in the fill candle
                    return ev + [("closed", tr["sl"], -1.0)]
                continue                                      # TP inside the fill candle is ignored
            if hi >= CX:
                return ev + [("cancelled", "price ran away before filling")]
            continue
        if lo <= SL:
            return ev + [("closed", _px(min(op, SL), side), (min(op, SL) - E) / tr["risk"])]
        if hi >= TP:
            return ev + [("closed", tr["tp"], (TP - E) / tr["risk"])]
    if tr["status"] == "filled" and candles and time.time() * 1000 - tr["opened"] > MAX_TRADE_DAYS * 86400000:
        last = _px(candles[-1]["c"], side)
        ev.append(("closed", candles[-1]["c"], (last - E) / tr["risk"]))
    return ev


def manage_smc(st, key, tr, candles):
    icon = "🟢" if tr["side"] == "LONG" else "🔴"
    name = f"{icon} <b>SMC {tr['side']} {tr['sym']}</b> ({tr['tf']})"
    for ev in update_smc(tr, candles):
        if ev[0] == "filled":
            tg(f"✅ {name}: limit order <b>filled</b> at <code>{fp(ev[1])}</code>\n"
               f"SL <code>{fp(tr['sl'])}</code> | TP <code>{fp(tr['tp'])}</code> – make sure both are set.")
        elif ev[0] == "cancelled":
            tg(f"❌ {name}: <b>cancel the limit order</b> ({ev[1]}). No trade.")
            st.setdefault("cancelled", 0)
            st["cancelled"] += 1
            del st["open"][key]
            return
        elif ev[0] == "closed":
            r = ev[2]
            net = r - tr["fee_r"]
            usd = net * tr.get("usd_r", RISK_USD)
            what = "🎯 TP hit" if r > 0 else "🛑 SL hit"
            tg(f"🏁 {name} closed at <code>{fp(ev[1])}</code> – {what}\n"
               f"Result: <b>{r:+.2f}R</b> (after fees ~{net:+.2f}R ≈ <b>{'+' if usd >= 0 else '−'}${abs(usd):.2f}</b>)")
            st["closed"].append({"sym": tr["sym"], "side": tr["side"], "tf": tr["tf"], "strategy": "smc",
                                 "r": round(r, 3), "net": round(net, 3), "simple": round(net, 3),
                                 "usd": round(usd, 2), "closed": int(time.time())})
            del st["open"][key]
            return


def scan_smc(st, sym, C, H, src, btc_fn=None):
    sent = 0
    n = len(C)
    for x in tag_btc(find_smc_setups(C, H, SMC_ENTRY, pending=True), sym, btc_fn):
        if not x.get("pending") or x["sig"] < n - SIGNAL_LOOKBACK or not smc_ok(x):
            continue
        if "SMC_MAX_FILL" in SMC_ACTIVE:                  # validated: only wait N candles for the fill
            x["expires_ct"] = min(x["expires_ct"],
                                  x["time"] + int(SMC_ACTIVE["SMC_MAX_FILL"]) * (C[-1]["ct"] - C[-1]["t"] + 1))
        key = f"{sym}|{x['side']}|smc|{TIMEFRAME}|{x['sweep_t']}"
        if key in st["sent"]:
            continue
        if tg(smc_message(sym, x, src)):
            st["sent"][key] = int(time.time())
            open_smc_trade(st, key, sym, x, src, C)
            sent += 1
    return sent


def run_scan():
    st = load_state()
    sent, errors, cache = 0, [], {}
    btc_fn = None
    if STRATEGY in ("smc", "both") and "SMC_BTC" in SMC_ACTIVE:
        try:
            btc_fn = btc_trend_fn(fetch_klines("BTCUSDT", HTF, CANDLES)[0])
        except Exception as e:
            print("BTC data error", e)
    for sym in SYMBOLS:
        try:
            C, src = fetch_klines(sym, TIMEFRAME, CANDLES)
            cache[(sym, TIMEFRAME)] = C
            H, _ = fetch_klines(sym, HTF, CANDLES, source=src)
            if STRATEGY in ("smc", "both"):
                sent += scan_smc(st, sym, C, H, src, btc_fn)
            for side in (("LONG", "SHORT") if STRATEGY in ("sd", "both") else ()):
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
               f"Strategy: {STRATEGY.upper()} | open/pending trades: {len(st['open'])}\n\n📒 <b>Paper results</b>")
        for strat in (("smc", "sd") if STRATEGY == "both" else (STRATEGY,)):
            msg += (f"\n<b>{strat.upper()}</b> – last 30 days: {paper_stats(st, 30, strat)}\n"
                    f"all time: {paper_stats(st, None, strat)}")
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


FILTER_TESTS = [
    ("No filter", lambda t: True),
    ("ADX HTF ≥20", lambda t: t["hadx"] >= 20),
    ("ADX HTF ≥25", lambda t: t["hadx"] >= 25),
    ("ADX TF ≥20", lambda t: t["adx"] >= 20),
    ("ADX TF ≥25", lambda t: t["adx"] >= 25),
    ("CHOP ≤50", lambda t: t["chop"] <= 50),
    ("CHOP ≤45", lambda t: t["chop"] <= 45),
    ("EMA slope ≥1 ATR", lambda t: t["slope"] >= 1),
    ("EMA slope ≥2 ATR", lambda t: t["slope"] >= 2),
    ("ADX HTF ≥20 + CHOP ≤50", lambda t: t["hadx"] >= 20 and t["chop"] <= 50),
]
FILTER_VARS = {"ADX HTF ≥20": "FILTER_HTF_ADX=20", "ADX HTF ≥25": "FILTER_HTF_ADX=25",
               "ADX TF ≥20": "FILTER_ADX=20", "ADX TF ≥25": "FILTER_ADX=25",
               "CHOP ≤50": "FILTER_CHOP=50", "CHOP ≤45": "FILTER_CHOP=45",
               "EMA slope ≥1 ATR": "FILTER_SLOPE=1", "EMA slope ≥2 ATR": "FILTER_SLOPE=2",
               "ADX HTF ≥20 + CHOP ≤50": "FILTER_HTF_ADX=20 and FILTER_CHOP=50"}


def market_filter_table(trades, tests=None, exits=("Trail 3 ATR", "Fixed 2R"), title=None):
    """Compare regime filters. A filter only counts as 'consistent' if it is profitable after fees
    in the first half, the second half AND the last 30 days (with >= 10 trades in each)."""
    if not trades:
        return "### Market filters\n\nno trades", []
    t0, t1 = min(t["time"] for t in trades), max(t["time"] for t in trades)
    mid, last30 = (t0 + t1) / 2, time.time() * 1000 - 30 * 86400000
    periods = [("1st half", lambda t: t["time"] < mid), ("2nd half", lambda t: t["time"] >= mid),
               ("Last 30d", lambda t: t["time"] >= last30)]
    rows, consistent = [], []
    for fname, f in (tests or FILTER_TESTS):
        sel = [t for t in trades if t["score"] >= MIN_SCORE and f(t)]
        for ex in exits:
            n, w, avg, tot, _, _ = _stats(sel, ex, FEE_PCT)
            cells, ok = [], n > 0
            for _, pf in periods:
                pn, _, pavg, _, _, _ = _stats([t for t in sel if pf(t)], ex, FEE_PCT)
                cells.append(f"{pavg:+.2f} ({pn})")
                ok = ok and pn >= 10 and pavg > 0
            mark = "✅" if ok else ""
            name = fname.replace("HTF", HTF).replace("TF ", f"{TIMEFRAME} ")
            rows.append(f"| {name} | {ex} | {n} | {w:.0f}% | {avg:+.2f}R | " + " | ".join(cells) + f" | {mark} |")
            if ok:
                consistent.append(f"✅ {name}, {ex}: {avg:+.2f}R/trade, {n} trades"
                                  + (f" → set {FILTER_VARS[fname]}" if fname in FILTER_VARS else ""))
    table = (f"### {title or 'Market filters (skip choppy / sideways markets)'}\n\n"
             f"Score ≥{MIN_SCORE}, SL ≥{MIN_SL_PCT}%, market fees. Cells = net R per trade (trades). "
             "✅ = profitable in every period.\n\n"
             "| Filter | Exit | Trades | Win% | Net/trade | 1st half | 2nd half | Last 30d | Consistent |\n"
             "|---|---|---|---|---|---|---|---|---|\n" + "\n".join(rows))
    return table, consistent


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
                             "adx": s["adx"], "hadx": s["hadx"], "chop": s["chop"], "slope": s["slope"],
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

    filter_section, consistent = market_filter_table(trades)
    report = (f"## Exit comparison (~{total} candles of {TIMEFRAME}, {len(per_coin)} coins)\n\n"
              f"Net = average R per trade after fees (market {FEE_PCT:.2f}%, limit {MAKER_FEE_PCT:.2f}% round trip). "
              "Positive = profitable. ATR TPs are never closer than 1R. Trailing stops start at the normal SL.\n\n"
              + "\n\n".join(sections) +
              "\n\n" + filter_section +
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
    msg += "\n<b>Market filters</b> (positive in 1st half, 2nd half AND last 30 days):\n"
    msg += ("\n".join(consistent[:5]) if consistent else "none – no filter was consistently profitable") + "\n"
    msg += "\nFull table: GitHub → Actions → this run's summary."
    tg(msg)


# ============================ AMD (POWER OF 3) ============================
# Accumulation : the Asia session range, UTC 00:00 - AMD_ACC_HOURS (must be tight)
# Manipulation : until AMD_MANIP_END UTC, price sweeps ONE side of that range (stop hunt)
# Distribution : a candle closes back inside (reclaim) -> enter; SL beyond the sweep extreme,
#                targets: opposite side of the range, fixed R, trailing, or exit at end of day.
AMD_ACC_HOURS = 8          # accumulation = first 8 hours of the UTC day
AMD_MANIP_END = 16         # a reclaim must happen before 16:00 UTC
AMD_MAX_RANGE_ATR = 4.0    # accumulation range must be <= 4 x ATR(1h) (tight consolidation)
AMD_SL_BUFFER_ATR = 0.1


def find_amd_setups(C, H, confirm="range"):
    """AMD setups on 1h candles. confirm='range' -> reclaim of the swept range edge,
    confirm='open' -> stricter: close back beyond the day's opening price."""
    n = len(C)
    o = [x["o"] for x in C]; h = [x["h"] for x in C]
    l = [x["l"] for x in C]; cl = [x["c"] for x in C]
    A = atr(h, l, cl, ATR_LEN)
    E = ema(cl, EMA_LEN)
    ADX = adx(h, l, cl)
    CH = chop(h, l, cl)
    hc = [x["c"] for x in H]
    he = ema(hc, EMA_LEN)
    hct = [x["ct"] for x in H]
    HADX = adx([x["h"] for x in H], [x["l"] for x in H], hc)

    days = {}
    for i, x in enumerate(C):
        days.setdefault(x["t"] // 86400000, []).append(i)

    out = []
    for day, idx in sorted(days.items()):
        hours = {(C[i]["t"] // 3600000) % 24: i for i in idx}
        acc = [hours[hr] for hr in range(AMD_ACC_HOURS) if hr in hours]
        if len(acc) < AMD_ACC_HOURS:
            continue
        last_acc = acc[-1]
        a = A[last_acc]
        if not a or E[last_acc] is None:
            continue
        rng_hi, rng_lo = max(h[i] for i in acc), min(l[i] for i in acc)
        rng = rng_hi - rng_lo
        if rng <= 0 or rng > AMD_MAX_RANGE_ATR * a:
            continue
        day_open = o[acc[0]]
        day_end = (day + 1) * 86400000 - 1
        swept_lo = swept_hi = False
        ext_lo, ext_hi = rng_lo, rng_hi
        for hr in range(AMD_ACC_HOURS, AMD_MANIP_END):
            i = hours.get(hr)
            if i is None:
                continue
            if l[i] < rng_lo:
                swept_lo, ext_lo = True, min(ext_lo, l[i])
            if h[i] > rng_hi:
                swept_hi, ext_hi = True, max(ext_hi, h[i])
            if swept_lo and swept_hi:
                break                                   # both sides taken: no clean manipulation
            side = None
            if swept_lo and cl[i] > o[i] and cl[i] > (rng_lo if confirm == "range" else max(rng_lo, day_open)):
                side, entry, sl, target = "LONG", cl[i], ext_lo - AMD_SL_BUFFER_ATR * a, rng_hi
            elif swept_hi and cl[i] < o[i] and cl[i] < (rng_hi if confirm == "range" else min(rng_hi, day_open)):
                side, entry, sl, target = "SHORT", cl[i], ext_hi + AMD_SL_BUFFER_ATR * a, rng_lo
            if not side:
                continue
            risk = abs(entry - sl)
            if risk <= 0 or (side == "LONG" and target <= entry) or (side == "SHORT" and target >= entry):
                break
            k = bisect.bisect_right(hct, C[i]["ct"]) - 1
            up = k >= 0 and he[k] is not None and hc[k] > he[k]
            slope = ((E[i] - E[i - 20]) / A[i]) if i >= 20 and E[i - 20] is not None and A[i] else 0.0
            out.append({
                "side": side, "e": i, "entry": entry, "sl": sl, "atr": A[i] or a, "time": C[i]["ct"],
                "target": target, "day_end": day_end, "score": 6,
                "range_atr": rng / a, "trend": up if side == "LONG" else (k >= 0 and he[k] is not None and hc[k] < he[k]),
                "adx": ADX[i] or 0.0, "chop": CH[i] if CH[i] is not None else 100.0,
                "hadx": HADX[k] if k >= 0 and HADX[k] is not None else 0.0,
                "slope": slope if side == "LONG" else -slope,
            })
            break                                       # one trade per coin per day
    return out


def _simulate_eod(C, s, side, r_target):
    """Exit at SL, at r_target (if given), or at the close of the day's last candle."""
    risk = abs(s["entry"] - s["sl"])
    tp = None if r_target is None else (s["entry"] + r_target * risk if side == "LONG" else s["entry"] - r_target * risk)
    for x in C[s["e"] + 1:]:
        if (x["l"] <= s["sl"]) if side == "LONG" else (x["h"] >= s["sl"]):
            return -1.0
        if tp is not None and ((x["h"] >= tp) if side == "LONG" else (x["l"] <= tp)):
            return r_target
        if x["ct"] >= s["day_end"]:
            return ((x["c"] - s["entry"]) if side == "LONG" else (s["entry"] - x["c"])) / risk
    return None


AMD_EXITS = [
    ("Range target", lambda C, s: _simulate_tp(C, s, s["side"], abs(s["target"] - s["entry"]))),
    ("Fixed 1.5R", lambda C, s: simulate(C, s, s["side"], 1.5)),
    ("Fixed 2R", lambda C, s: simulate(C, s, s["side"], 2.0)),
    ("Fixed 3R", lambda C, s: simulate(C, s, s["side"], 3.0)),
    ("Trail 3 ATR", lambda C, s: _simulate_trail(C, s, s["side"], 3.0)),
    ("2R or end of day", lambda C, s: _simulate_eod(C, s, s["side"], 2.0)),
    ("End of day", lambda C, s: _simulate_eod(C, s, s["side"], None)),
]
AMD_TESTS = [
    ("No filter", lambda t: True),
    ("With 4h trend", lambda t: t["trend"]),
    ("ADX HTF ≥20", lambda t: t["hadx"] >= 20),
    ("ADX HTF ≥25", lambda t: t["hadx"] >= 25),
    ("Trend + ADX HTF ≥25", lambda t: t["trend"] and t["hadx"] >= 25),
    ("Tight range ≤2.5 ATR", lambda t: t["range_atr"] <= 2.5),
    ("CHOP ≤50", lambda t: t["chop"] <= 50),
    ("Range target ≥1R", lambda t: t["tgt_r"] >= 1.0),
    ("Range target ≥1.5R", lambda t: t["tgt_r"] >= 1.5),
]


def run_amd_backtest(total):
    """Backtest the AMD model on 1h candles for both confirmation variants."""
    groups = {"range": [], "open": []}
    coins = 0
    for sym in SYMBOLS:
        try:
            C, src = fetch_history(sym, "1h", total)
            H, _ = fetch_history(sym, "4h", max(total // 4 + EMA_LEN + 50, 300), source=src)
        except Exception as e:
            print("ERROR", e)
            continue
        coins += 1
        for confirm in groups:
            for s in find_amd_setups(C, H, confirm):
                if sl_pct(s) < MIN_SL_PCT:
                    continue
                s["slp"] = sl_pct(s)
                s["tgt_r"] = abs(s["target"] - s["entry"]) / abs(s["entry"] - s["sl"])
                s["res"] = {lbl: f(C, s) for lbl, f in AMD_EXITS}
                s["sym"] = sym
                groups[confirm].append(s)
        print(f"{sym}: range {sum(1 for t in groups['range'] if t['sym'] == sym)}, "
              f"open {sum(1 for t in groups['open'] if t['sym'] == sym)} setups")
        time.sleep(0.1)

    head = ("| Exit | Trades | Win% | Avg win | Net/trade (market) | Net/trade (limit) | Total (market) | Max loss streak |\n"
            "|---|---|---|---|---|---|---|---|\n")
    names = {"range": "Entry: reclaim of the range edge", "open": "Entry: close back beyond the day open (stricter)"}
    sections, msg_parts = [], []
    for g, trades in groups.items():
        rows, ranked = [], []
        for lbl, _ in AMD_EXITS:
            n, w, avg, tot, aw, ls = _stats(trades, lbl, FEE_PCT)
            _, _, avg_l, _, _, _ = _stats(trades, lbl, MAKER_FEE_PCT)
            rows.append(f"| {lbl} | {n} | {w:.0f}% | {aw:.2f}R | {avg:+.2f}R | {avg_l:+.2f}R | {tot:+.1f}R | {ls} |")
            if n >= 20:
                ranked.append((avg, lbl, n, w, tot))
        ftable, consistent = market_filter_table(
            trades, AMD_TESTS, ("Range target", "Fixed 2R", "2R or end of day"),
            title=f"Filters – {names[g]}")
        sections.append(f"### {names[g]}\n\n" + head + "\n".join(rows) + "\n\n" + ftable)
        ranked.sort(reverse=True)
        part = f"\n<b>{names[g]}</b> ({len(trades)} setups)\nTop exits:\n"
        part += "\n".join(f"{i}. {lbl}: {avg:+.2f}R/trade, win {w:.0f}%, {n} trades ({tot:+.1f}R)"
                           for i, (avg, lbl, n, w, tot) in enumerate(ranked[:3], 1)) or "not enough trades"
        part += "\nConsistent filters:\n" + ("\n".join(consistent[:4]) if consistent else "none")
        msg_parts.append(part)

    report = (f"## AMD / Power of 3 backtest (~{total} 1h candles, {coins} coins)\n\n"
              f"Accumulation = UTC 00:00–{AMD_ACC_HOURS:02d}:00 range (≤{AMD_MAX_RANGE_ATR:g} ATR). "
              f"Manipulation = one side swept before {AMD_MANIP_END:02d}:00 UTC. Entry on the reclaim candle close, "
              f"SL beyond the sweep. SL ≥{MIN_SL_PCT}%. Net = R per trade after fees "
              f"(market {FEE_PCT:.2f}%, limit {MAKER_FEE_PCT:.2f}%).\n\n"
              + "\n\n".join(sections) +
              "\n\n_Slippage and funding are not included. Past results do not guarantee future results._\n")
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write(report)
    tg(f"🌀 <b>AMD backtest</b> (1h, {coins} coins, market fees {FEE_PCT:.2f}%)\n"
       + "\n".join(msg_parts) + "\n\nFull tables: GitHub → Actions → run summary.")


# ============================ SMC (SWEEP -> MSS -> FVG) ============================
# 1. Liquidity sweep : price trades below a confirmed swing low, then closes back above it
# 2. MSS / CHoCH     : a close above the swing high between that low and the sweep (displacement)
# 3. Entry           : limit order in the FVG created by that displacement (top edge or 50%)
# 4. SL beyond the sweep extreme; targets: previous high (liquidity), fixed R, trailing.
SMC_SWEEP_MAX = 48         # sweep must come within 48 candles of the swing low
SMC_MSS_MAX = 24           # MSS must come within 24 candles of the sweep
SMC_FILL_MAX = 24          # limit order expires after 24 candles
SMC_SL_BUFFER_ATR = 0.1


TF_MS = {"5m": 300000, "15m": 900000, "30m": 1800000, "1h": 3600000, "2h": 7200000,
         "4h": 14400000, "1d": 86400000}


def _smc_long(C, H, entry_mode, pending=False):
    """Bullish SMC setups in C (use flip() for bearish).
    pending=True also returns setups whose limit order is still waiting at the last candle."""
    n = len(C)
    h = [x["h"] for x in C]; l = [x["l"] for x in C]; cl = [x["c"] for x in C]
    A = atr(h, l, cl, ATR_LEN)
    ADX = adx(h, l, cl)
    CH = chop(h, l, cl)
    hc = [x["c"] for x in H]
    he = ema(hc, EMA_LEN)
    hct = [x["ct"] for x in H]
    HADX = adx([x["h"] for x in H], [x["l"] for x in H], hc)
    o = [x["o"] for x in C]
    vol = [x.get("v", 0.0) for x in C]
    hl = [x["l"] for x in H]
    HA = atr([x["h"] for x in H], hl, hc, ATR_LEN)
    hpl = pivots(hl, PIVOT_LEN, False)                    # 4h swing lows (for the HTF POI check)
    lows = pivots(l, PIVOT_LEN, False)
    out, used = [], set()
    for p, L in lows:
        # 1. sweep: first candle that trades below the swing low
        s = next((k for k in range(p + PIVOT_LEN + 1, min(n, p + SMC_SWEEP_MAX)) if l[k] < L), None)
        if s is None or s in used:
            continue
        sweep_low, rec = l[s], None
        for k in range(s, min(n, s + 3)):                 # reclaim within 3 candles
            sweep_low = min(sweep_low, l[k])
            if cl[k] > L:
                rec = k
                break
        if rec is None:
            continue
        # 2. MSS: close above the high between the swing low and the sweep
        level = max(h[p + 1:s])
        m = None
        for k in range(rec, min(n, s + SMC_MSS_MAX)):
            if k > rec and l[k] < sweep_low:
                break                                     # new low: setup failed
            if cl[k] > level:
                m = k
                break
        if m is None:
            continue
        # 3. FVG inside the displacement leg (latest one up to the MSS candle)
        fv = next((k for k in range(m, s, -1) if k + 1 < n and l[k + 1] > h[k - 1]), None)
        if fv is None:
            continue
        zb, zt = h[fv - 1], l[fv + 1]
        entry = zt if entry_mode == "top" else (zb + zt) / 2
        a = A[m] or A[s]
        if not a:
            continue
        sl = sweep_low - SMC_SL_BUFFER_ATR * a
        risk = entry - sl
        if risk <= 0:
            continue
        # limit order fill (cancel if price runs 2R away first or the order expires)
        start = max(m, fv + 1) + 1
        f, cancelled = None, False
        for k in range(start, min(n, start + SMC_FILL_MAX)):
            if l[k] <= entry:
                f = k
                break
            if h[k] >= entry + 2 * risk:
                cancelled = True
                break
        prev_high = max(h[max(0, s - 48):s])              # external liquidity above
        leg_high = max(h[s:m + 1])
        # ---- quality features (all known at signal time)
        q = {
            "disp": abs(cl[m] - o[m]) / a,                               # MSS candle body in ATRs
            "fvg_atr": (zt - zb) / a,                                    # FVG size in ATRs
            "eq": any(abs(v2 - L) <= 0.15 * a for i2, v2 in lows if p - 48 <= i2 < p),   # equal lows swept
            "session": 7 <= (C[s]["t"] // 3600000) % 24 < 20,            # London / New York hours (UTC)
            "vol": (vol[s] / (sum(vol[s - 20:s]) / 20)) if s >= 20 and sum(vol[s - 20:s]) > 0 else 0.0,
        }
        k4 = bisect.bisect_right(hct, C[s]["ct"]) - 1
        ha = HA[k4] if k4 >= 0 and HA[k4] else a
        q["htf_poi"] = any(abs(v2 - sweep_low) <= 0.5 * ha for i2, v2 in hpl
                           if k4 - 60 <= i2 and i2 + PIVOT_LEN <= k4)    # sweep at a confirmed 4h swing low
        if f is None:
            sig = start - 1                               # candle on which the setup is known
            if pending and not cancelled and start + SMC_FILL_MAX > n and sig < n:
                used.add(s)
                kk = bisect.bisect_right(hct, C[sig]["ct"]) - 1
                tf_ms = C[sig]["ct"] - C[sig]["t"] + 1
                out.append({
                    "pending": True, "sig": sig, "entry": entry, "sl": sl, "atr": A[sig] or a,
                    "time": C[sig]["ct"], "sweep_t": C[s]["t"], "score": 6,
                    "expires_ct": C[sig]["ct"] + SMC_FILL_MAX * tf_ms, "cancel": entry + 2 * risk,
                    "liq": prev_high if prev_high > entry + 0.5 * risk else None,
                    "trend": kk >= 0 and he[kk] is not None and hc[kk] > he[kk],
                    "hadx": HADX[kk] if kk >= 0 and HADX[kk] is not None else 0.0,
                    "adx": ADX[sig] or 0.0, "chop": CH[sig] if CH[sig] is not None else 100.0,
                    "discount": entry <= (sweep_low + leg_high) / 2, "fill_wait": None, **q,
                })
            continue
        used.add(s)
        kk = bisect.bisect_right(hct, C[f]["ct"]) - 1
        out.append({
            "pending": False, "sweep_t": C[s]["t"],
            "e": f, "entry": entry, "sl": sl, "atr": A[f] or a, "time": C[f]["ct"], "score": 6,
            "liq": prev_high if prev_high > entry + 0.5 * risk else None,
            "fill_loss": l[f] <= sl,                      # SL hit in the fill candle: count as a loss
            "trend": kk >= 0 and he[kk] is not None and hc[kk] > he[kk],
            "hadx": HADX[kk] if kk >= 0 and HADX[kk] is not None else 0.0,
            "adx": ADX[f] or 0.0, "chop": CH[f] if CH[f] is not None else 100.0,
            "discount": entry <= (sweep_low + leg_high) / 2, "fill_wait": f - start, **q,
        })
    return out


def find_smc_setups(C, H, entry_mode="top", pending=False):
    longs = [dict(x, side="LONG") for x in _smc_long(C, H, entry_mode, pending)]
    shorts = []
    for x in _smc_long(flip(C), flip(H), entry_mode, pending):
        x = dict(x, side="SHORT", entry=-x["entry"], sl=-x["sl"])
        x["liq"] = -x["liq"] if x["liq"] is not None else None
        if "cancel" in x:
            x["cancel"] = -x["cancel"]
        shorts.append(x)
    for x in longs + shorts:                              # target: liquidity, else 2R
        risk = abs(x["entry"] - x["sl"])
        x["tp"] = x["liq"] if x["liq"] is not None else (
            x["entry"] + 2 * risk if x["side"] == "LONG" else x["entry"] - 2 * risk)
        x["tgt_r"] = abs(x["tp"] - x["entry"]) / risk
        x.setdefault("btc_ok", True)
    return longs + shorts


def btc_trend_fn(HB):
    """ct -> +1 (BTC 4h above EMA50), -1 (below) or 0 (unknown)."""
    hc = [x["c"] for x in HB]
    he = ema(hc, EMA_LEN)
    hct = [x["ct"] for x in HB]

    def f(ct):
        k = bisect.bisect_right(hct, ct) - 1
        if k < 0 or he[k] is None:
            return 0
        return 1 if hc[k] > he[k] else -1
    return f


def tag_btc(setups, sym, btc_fn):
    for x in setups:
        if sym == "BTCUSDT" or btc_fn is None:
            x["btc_ok"] = True
        else:
            tr = btc_fn(x["time"])
            x["btc_ok"] = tr == 0 or (tr > 0) == (x["side"] == "LONG")
    return setups


# Candidate quality filters: (label, variable, value, test). Only the ones that pass the
# train/test validation in `--smc` mode get applied (saved to smc_filters.json).
SMC_QUALITY = [
    ("Strong displacement (MSS body ≥1 ATR)", "SMC_MIN_DISP", 1.0, lambda t, v: t["disp"] >= v),
    ("Big FVG (≥0.3 ATR)", "SMC_MIN_FVG", 0.3, lambda t, v: t["fvg_atr"] >= v),
    ("Equal lows/highs swept", "SMC_EQUAL_LEVELS", 1, lambda t, v: t["eq"]),
    ("London/NY session (07–20 UTC)", "SMC_SESSION", 1, lambda t, v: t["session"]),
    ("4h POI (sweep at a 4h swing)", "SMC_HTF_POI", 1, lambda t, v: t["htf_poi"]),
    ("Target ≥1.5R", "SMC_MIN_RR", 1.5, lambda t, v: t["tgt_r"] >= v),
    ("Fill within 12 candles", "SMC_MAX_FILL", 12, lambda t, v: t["fill_wait"] is None or t["fill_wait"] <= v),
    ("Sweep volume ≥1.5× average", "SMC_MIN_VOL", 1.5, lambda t, v: t["vol"] >= v),
    ("BTC 4h trend agrees", "SMC_BTC", 1, lambda t, v: t["btc_ok"]),
]
SMC_FILTERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smc_filters.json")


def load_smc_quality():
    """Active quality filters: validated file first, explicit repo variables override."""
    active = {}
    try:
        with open(SMC_FILTERS_FILE) as f:
            active = {k: float(v) for k, v in json.load(f).get("filters", {}).items()}
    except Exception:
        pass
    for _, var, _, _ in SMC_QUALITY:
        raw = os.getenv(var, "").strip()
        if raw:
            try:
                val = float(raw)
                if val:
                    active[var] = val
                else:
                    active.pop(var, None)
            except ValueError:
                print(f"Ignoring invalid {var}={raw!r}")
    return active


SMC_ACTIVE = load_smc_quality()


def smc_quality_ok(x, active=None):
    active = SMC_ACTIVE if active is None else active
    for _, var, _, fn in SMC_QUALITY:
        if var in active and not fn(x, active[var]):
            return False
    return True


def smc_quality_txt(active=None):
    active = SMC_ACTIVE if active is None else active
    return ", ".join(lbl for lbl, var, _, _ in SMC_QUALITY if var in active) or "none"


def _smc(fn):
    return lambda C, s: -1.0 if s["fill_loss"] else fn(C, s)


def _smc_liq(C, s):
    if s["liq"] is None:
        return simulate(C, s, s["side"], 2.0)
    return _simulate_tp(C, s, s["side"], abs(s["liq"] - s["entry"]))


SMC_EXITS = [
    ("Fixed 1.5R", _smc(lambda C, s: simulate(C, s, s["side"], 1.5))),
    ("Fixed 2R", _smc(lambda C, s: simulate(C, s, s["side"], 2.0))),
    ("Fixed 3R", _smc(lambda C, s: simulate(C, s, s["side"], 3.0))),
    ("Prev high/low (liquidity)", _smc(_smc_liq)),
    ("Trail 3 ATR", _smc(lambda C, s: _simulate_trail(C, s, s["side"], 3.0))),
]
SMC_TESTS = [
    ("No filter", lambda t: True),
    ("With 4h trend", lambda t: t["trend"]),
    ("ADX HTF ≥25", lambda t: t["hadx"] >= 25),
    ("Trend + ADX HTF ≥25", lambda t: t["trend"] and t["hadx"] >= 25),
    ("Discount (<50% of leg)", lambda t: t["discount"]),
    ("Trend + discount", lambda t: t["trend"] and t["discount"]),
    ("CHOP ≤50", lambda t: t["chop"] <= 50),
]


def validate_quality(trades):
    """Train = everything before the last 60 days, test = last 60 days (never used to choose).
    A filter passes only if it beats the live baseline in BOTH periods and is profitable in the test.
    Passing filters are combined greedily (by train) and the combination must also beat the test baseline."""
    ex = "Prev high/low (liquidity)"
    cut = time.time() * 1000 - 60 * 86400000
    base = [t for t in trades if (not SMC_REQUIRE_DISCOUNT or t["discount"])
            and (not SMC_REQUIRE_TREND or t["trend"]) and passes_filters(t)]

    def ev(sel):
        tr = _stats([t for t in sel if t["time"] < cut], ex, FEE_PCT)
        te = _stats([t for t in sel if t["time"] >= cut], ex, FEE_PCT)
        return tr[0], tr[2], te[0], te[2]

    bn, bavg, btn, btavg = ev(base)
    rows, passed = [], []
    for lbl, var, val, fn in SMC_QUALITY:
        n, avg, tn, tavg = ev([t for t in base if fn(t, val)])
        ok = n >= 60 and tn >= 20 and avg >= bavg + 0.02 and tavg > btavg and tavg > 0
        rows.append(f"| {lbl} | {n} / {avg:+.2f}R | {tn} / {tavg:+.2f}R | {'✅' if ok else '❌'} |")
        if ok:
            passed.append((avg, lbl, var, val))
    passed.sort(reverse=True)

    def apply(active):
        return [t for t in base if smc_quality_ok(t, active)]

    combo = {}
    cur = (bn, bavg, btn, btavg)
    for _, lbl, var, val in passed:
        trial = dict(combo, **{var: val})
        r = ev(apply(trial))
        if r[0] >= 60 and r[2] >= 20 and r[1] >= cur[1] + 0.01:
            combo, cur = trial, r
    if combo and not (cur[3] > btavg and cur[3] > 0):
        combo = {passed[0][2]: passed[0][3]}               # combination failed the test: best single
        cur = ev(apply(combo))
    if not combo:
        cur = (bn, bavg, btn, btavg)

    result = {"filters": combo, "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
              "exit": ex, "baseline": {"train": [bn, round(bavg, 3)], "test": [btn, round(btavg, 3)]},
              "with_filters": {"train": [cur[0], round(cur[1], 3)], "test": [cur[2], round(cur[3], 3)]}}
    with open(SMC_FILTERS_FILE, "w") as f:
        json.dump(result, f, indent=1)

    names = smc_quality_txt(combo)
    table = ("### Quality filters: train / test validation\n\n"
             f"Baseline = live setup ({'discount' if SMC_REQUIRE_DISCOUNT else 'no discount'}), exit = {ex}, market fees. "
             "Train = before the last 60 days, test = last 60 days. Cells = trades / net R per trade.\n\n"
             f"| Filter | Train | Test | Passed |\n|---|---|---|---|\n"
             f"| **Baseline (no quality filter)** | {bn} / {bavg:+.2f}R | {btn} / {btavg:+.2f}R | |\n"
             + "\n".join(rows) +
             f"\n\n**Applied to live signals:** {names} → train {cur[0]} / {cur[1]:+.2f}R, test {cur[2]} / {cur[3]:+.2f}R\n")
    msg = (f"\n<b>Quality filters (train → last 60 days test)</b>\n"
           f"Baseline: train {bavg:+.2f}R ({bn}), test {btavg:+.2f}R ({btn})\n"
           + ("Passed: " + ", ".join(p[1] for p in passed) if passed else "Passed: none") + "\n"
           f"✅ <b>Applied to live:</b> {names}\n"
           f"With them: train {cur[1]:+.2f}R ({cur[0]}), test {cur[3]:+.2f}R ({cur[2]})")
    return table, msg


def run_smc_backtest(total):
    groups = {"top": [], "mid": []}
    coins = 0
    try:
        HB, _ = fetch_history("BTCUSDT", HTF, max(total // 4 + EMA_LEN + 50, 300))
        btc_fn = btc_trend_fn(HB)
    except Exception as e:
        print("BTC data error", e)
        btc_fn = None
    for sym in SYMBOLS:
        try:
            C, src = fetch_history(sym, TIMEFRAME, total)
            H, _ = fetch_history(sym, HTF, max(total // 4 + EMA_LEN + 50, 300), source=src)
        except Exception as e:
            print("ERROR", e)
            continue
        coins += 1
        for mode in groups:
            for st in tag_btc(find_smc_setups(C, H, mode), sym, btc_fn):
                if sl_pct(st) < MIN_SL_PCT:
                    continue
                st["slp"], st["sym"] = sl_pct(st), sym
                st["res"] = {lbl: f(C, st) for lbl, f in SMC_EXITS}
                groups[mode].append(st)
        print(f"{sym}: top {sum(1 for t in groups['top'] if t['sym'] == sym)}, "
              f"mid {sum(1 for t in groups['mid'] if t['sym'] == sym)} setups")
        time.sleep(0.1)

    head = ("| Exit | Trades | Win% | Avg win | Net/trade (market) | Net/trade (limit) | Total (market) | Max loss streak |\n"
            "|---|---|---|---|---|---|---|---|\n")
    names = {"top": "Entry: limit at FVG edge", "mid": "Entry: limit at FVG 50%"}
    sections, parts = [], []
    for g, trades in groups.items():
        rows, ranked = [], []
        for lbl, _ in SMC_EXITS:
            n, w, avg, tot, aw, ls = _stats(trades, lbl, FEE_PCT)
            _, _, avg_l, _, _, _ = _stats(trades, lbl, MAKER_FEE_PCT)
            rows.append(f"| {lbl} | {n} | {w:.0f}% | {aw:.2f}R | {avg:+.2f}R | {avg_l:+.2f}R | {tot:+.1f}R | {ls} |")
            if n >= 20:
                ranked.append((avg, avg_l, lbl, n, w, tot))
        ftable, consistent = market_filter_table(trades, SMC_TESTS, ("Fixed 2R", "Fixed 3R", "Prev high/low (liquidity)"),
                                                 title=f"Filters – {names[g]}")
        sections.append(f"### {names[g]}\n\n" + head + "\n".join(rows) + "\n\n" + ftable)
        ranked.sort(reverse=True)
        part = f"\n<b>{names[g]}</b> ({len(trades)} trades)\nTop exits (market / limit fees):\n"
        part += "\n".join(f"{i}. {lbl}: {avg:+.2f}R / {avl:+.2f}R, win {w:.0f}%, {n} trades"
                           for i, (avg, avl, lbl, n, w, tot) in enumerate(ranked[:3], 1)) or "not enough trades"
        part += "\nConsistent filters:\n" + ("\n".join(consistent[:4]) if consistent else "none")
        parts.append(part)

    report = (f"## SMC backtest: sweep → MSS → FVG (~{total} {TIMEFRAME} candles, {HTF} bias, {coins} coins)\n\n"
              "Sweep of a confirmed swing low/high, reclaim within 3 candles, MSS = close beyond the swing between them, "
              "limit entry in the displacement FVG (expires after "
              f"{SMC_FILL_MAX} candles), SL beyond the sweep. SL ≥{MIN_SL_PCT}%. Net = R per trade after fees "
              f"(market {FEE_PCT:.2f}%, limit {MAKER_FEE_PCT:.2f}%; entry is a limit order so the limit column is realistic "
              "if you also exit with limit/TP orders).\n\n" + "\n\n".join(sections) +
              "\n\n_Slippage and funding are not included. Past results do not guarantee future results._\n")
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write(report)
    qtable, qmsg = validate_quality(groups.get(SMC_ENTRY, groups["mid"]))
    print(qtable)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write("\n" + qtable)
    tg(f"🧠 <b>SMC backtest</b> ({TIMEFRAME}, {HTF} bias, {coins} coins)\n" + "\n".join(parts) + "\n" + qmsg
       + "\n\nFull tables: GitHub → Actions → run summary.")


# ============================ ACCOUNT SIMULATION ============================

def _exit_detail(C, s, side, kind, v, sl):
    """Walk candles after entry. Returns (exit_price, candle_index) or (None, None) if still open.
    kind: 'fixed' (TP at v x R) or 'trail' (v x ATR trailing stop). sl may be the liquidation price."""
    E, S = _px(s["entry"], side), _px(sl, side)
    risk = E - _px(s["sl"], side)
    if s.get("fill_loss"):                                   # SMC: stopped out in the fill candle
        return sl, s["e"]
    if kind == "price":                                      # fixed target price (SMC liquidity TP)
        kind, tp = "fixed", _px(s["tp"], side)
    else:
        tp = E + v * risk if kind == "fixed" else None
    stop, best, dist = S, E, (v * s["atr"] if kind == "trail" else 0.0)
    for i in range(s["e"] + 1, len(C)):
        x = C[i]
        hi, lo, op = (x["h"], x["l"], x["o"]) if side == "LONG" else (-x["l"], -x["h"], -x["o"])
        if lo <= stop:
            return _px(min(op, stop), side), i
        if kind == "fixed":
            if hi >= tp:
                return _px(tp, side), i
        else:
            best = max(best, hi)
            stop = max(stop, best - dist)
    return None, None


def money(x):
    return f"{'+' if x >= 0 else '−'}${abs(x):.2f}"


def send_trade_log(log, balance, exit_name, days):
    """Per-trade P&L list (live exit), in close order, with the running balance."""
    if not log:
        tg("📋 No trades in this period.")
        return
    closed = sorted([t for t in log if t["why"] != "open"], key=lambda t: t["close"])
    still = [t for t in log if t["why"] == "open"]
    bal, rows, lines = balance, [], []
    for i, t in enumerate(closed + still, 1):
        if t["why"] != "open":
            bal += t["pnl"]
        when = datetime.fromtimestamp(t["open"] / 1000, IST).strftime("%d %b %H:%M")
        shut = datetime.fromtimestamp(t["close"] / 1000, IST).strftime("%d %b %H:%M") if t["close"] else "open"
        icon = "🟢" if t["side"] == "LONG" else "🔴"
        tag = {"TP": "🎯TP", "SL": "🛑SL", "LIQ": "💀LIQ", "open": "⏳open"}[t["why"]]
        rows.append(f"| {i} | {when} | {shut} | {t['sym']} | {t['side']} | {t['lev']}x | {fp(t['entry'])} | {fp(t['exit'])} "
                    f"| {tag} | {money(t['pnl'])} | ${bal:.2f} |")
        lines.append(f"{i}. {icon} {t['sym']} {t['lev']}x | {when} → {shut[:6]} | {tag} {money(t['pnl'])} → ${bal:.2f}")
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in closed)
    unreal = sum(t["pnl"] for t in still)
    summary = (f"Closed: {len(closed)} trades ({len(wins)} win / {len(losses)} loss), "
               f"win profit {money(sum(t['pnl'] for t in wins))}, loss {money(sum(t['pnl'] for t in losses))}\n"
               f"Net closed P&L: {money(total)} → balance ${balance + total:.2f} ({total / balance * 100:+.1f}%)"
               + (f"\nStill open: {len(still)} ({money(unreal)} unrealised)" if still else ""))
    md = (f"### Trade log – {exit_name}, last {days} days\n\n"
          "Sorted by close time; balance = $ after each trade closes.\n\n"
          "| # | Opened (IST) | Closed (IST) | Coin | Side | Lev | Entry | Exit | Result | P&L | Balance |\n"
          "|---|---|---|---|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n\n" + summary.replace("\n", "  \n") + "\n")
    print(md)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write("\n" + md)
    chunk = f"📋 <b>Trade log</b> ({exit_name}, last {days} days, ${balance:g} start)\n"
    for ln in lines:
        if len(chunk) + len(ln) > 3500:                     # Telegram message limit
            tg(chunk)
            chunk = ""
        chunk += ln + "\n"
    tg(chunk + "\n<b>" + summary.replace("\n", "</b>\n<b>", 1) + "</b>")


def run_account(balance, margin, leverage, days, mode="risk", risk=1.0, strategy=None):
    """Simulate a real account, one position per coin, a trade only opens if free balance >= its margin.
    mode 'risk' : every trade loses ~`risk` $ at SL (leverage picked per trade, like the live signal).
    mode 'fixed': every trade is margin x leverage, so the $ loss depends on the SL distance.
    Fees included; funding/slippage not."""
    global RISK_USD
    RISK_USD = risk
    strategy = strategy or ("smc" if STRATEGY == "both" else STRATEGY)
    if strategy == "smc":
        exits = [("Liquidity TP", "price", None), ("Fixed 3R", "fixed", 3.0), ("Fixed 2R", "fixed", 2.0)]
    else:
        exits = [("Trail 3 ATR", "trail", TRAIL_ATR), ("Fixed 2R", "fixed", 2.0), ("Fixed 1R", "fixed", 1.0)]
    per_hour = {"15m": 4, "30m": 2, "1h": 1, "2h": 0.5, "4h": 0.25}.get(TIMEFRAME, 1)
    need = int(days * 24 * per_hour) + 400                       # period + warm-up
    start_ms = int(time.time() * 1000) - days * 86400000
    pos = margin * leverage
    liq_dist = max(0.0, 100 / leverage - MMR_PCT)                 # % move that liquidates

    cands, coins, last_close = [], 0, {}
    btc_fn = None
    if strategy == "smc" and "SMC_BTC" in SMC_ACTIVE:
        try:
            btc_fn = btc_trend_fn(fetch_history("BTCUSDT", HTF, max(need // 4 + EMA_LEN + 50, 300))[0])
        except Exception as e:
            print("BTC data error", e)
    for sym in SYMBOLS:
        try:
            C, src = fetch_history(sym, TIMEFRAME, need)
            H, _ = fetch_history(sym, HTF, max(need // 4 + EMA_LEN + 50, 300), source=src)
        except Exception as e:
            print("ERROR", e)
            continue
        coins += 1
        last_close[sym] = C[-1]["c"]
        if strategy == "smc":
            pool = [(x["side"], x) for x in tag_btc(find_smc_setups(C, H, SMC_ENTRY), sym, btc_fn) if smc_ok(x)]
        else:
            pool = [(side, x) for side in ("LONG", "SHORT") for x in setups_for(C, H, side)
                    if x["score"] >= MIN_SCORE and sl_pct(x) >= MIN_SL_PCT and passes_filters(x)]
        for side, s in pool:
                if s["time"] < start_ms:
                    continue
                if mode == "risk":
                    z = sizing(s, side, margin)
                    c_lev, c_pos, c_liq = z["lev"], z["pos"], z["liq_dist"]
                else:
                    c_lev, c_pos, c_liq = leverage, pos, liq_dist
                liq_px = s["entry"] * (1 - c_liq / 100) if side == "LONG" else s["entry"] * (1 + c_liq / 100)
                liquidates = sl_pct(s) >= c_liq                     # SL beyond liquidation
                stop = liq_px if liquidates else s["sl"]
                res = {}
                for lbl, kind, v in exits:
                    px, idx = _exit_detail(C, s, side, kind, v, stop)
                    res[lbl] = (px, C[idx]["ct"] if idx is not None else None)
                cands.append({"sym": sym, "side": side, "t": s["time"], "entry": s["entry"], "res": res,
                              "liq": liquidates, "liq_px": liq_px, "pos": c_pos, "lev": c_lev,
                              "sl": s["sl"], "tp": s.get("tp")})
        print(f"{sym}: {sum(1 for c in cands if c['sym'] == sym)} setups")
        time.sleep(0.1)
    cands.sort(key=lambda c: c["t"])

    def pnl(c, px):
        if c["liq"] and (px <= c["liq_px"] if c["side"] == "LONG" else px >= c["liq_px"]):
            return -margin                                        # liquidation takes the whole margin
        move = (px - c["entry"]) / c["entry"] if c["side"] == "LONG" else (c["entry"] - px) / c["entry"]
        return max(-margin, c["pos"] * move) - c["pos"] * FEE_PCT / 100

    results, trade_log = [], []
    for lbl, _, _ in exits:
        log_this = lbl == exits[0][0]                              # the live exit: keep a per-trade log
        free, open_ = balance, {}                                  # open_: sym -> (close_t, margin+pnl)
        taken = skipped_cash = skipped_coin = wins = liqs = 0
        pnls, fees = [], 0.0
        peak = low_eq = balance
        max_dd = 0.0
        max_open = 0

        def settle(until):
            nonlocal free, peak, max_dd
            for sym_, (ct, back) in sorted(open_.items(), key=lambda kv: kv[1][0] or 1e20):
                if ct is not None and ct <= until:
                    free += back
                    del open_[sym_]
                    eq = free + margin * len(open_)
                    peak = max(peak, eq)
                    max_dd = max(max_dd, peak - eq)

        for c in cands:
            settle(c["t"])
            if c["sym"] in open_:
                skipped_coin += 1
                continue
            if free < margin:
                skipped_cash += 1
                continue
            px, ct = c["res"][lbl]
            free -= margin
            taken += 1
            if px is None:                                         # still open: mark to market
                p = pnl(c, last_close[c["sym"]])
                open_[c["sym"]] = (None, margin + p)
                pnls.append(("open", p))
            else:
                p = pnl(c, px)
                open_[c["sym"]] = (ct, margin + p)
                pnls.append(("closed", p))
                wins += p > 0
                liqs += c["liq"] and p <= -margin * 0.99
            if log_this:
                tol = abs(c["entry"]) * 1e-6
                if px is None:
                    why = "open"
                elif c["liq"] and p <= -margin * 0.99:
                    why = "LIQ"
                elif c.get("tp") is not None and abs(px - c["tp"]) <= tol:
                    why = "TP"
                elif abs(px - c["sl"]) <= tol or p < 0:
                    why = "SL"
                else:
                    why = "TP"
                trade_log.append({"sym": c["sym"], "side": c["side"], "open": c["t"], "close": ct,
                                  "entry": c["entry"], "exit": px if px is not None else last_close[c["sym"]],
                                  "lev": c["lev"], "pnl": p, "why": why})
            fees += c["pos"] * FEE_PCT / 100
            max_open = max(max_open, len(open_))
        settle(1e20)
        unreal = sum(back - margin for ct, back in open_.values())
        final = free + margin * len(open_) + unreal
        closed = [p for k, p in pnls if k == "closed"]
        streak = worst = 0
        for p in closed:
            streak = streak + 1 if p < 0 else 0
            worst = max(worst, streak)
        results.append({"lbl": lbl, "final": final, "taken": taken, "closed": len(closed),
                        "open": len(open_), "unreal": unreal, "wins": wins, "fees": fees,
                        "dd": max_dd, "max_open": max_open, "skip_cash": skipped_cash,
                        "skip_coin": skipped_coin, "best": max(closed, default=0),
                        "worst": min(closed, default=0), "streak": worst, "liqs": liqs})

    levs = [c["lev"] for c in cands] or [leverage]
    how = (f"${margin:g} margin, ~${risk:g} loss at SL, leverage {min(levs)}–{max(levs)}x (per trade)"
           if mode == "risk" else f"${margin:g} × {leverage}x per trade (position ${pos:g})")
    strat_txt = (f"SMC (FVG {'50%' if SMC_ENTRY == 'mid' else 'edge'}"
                 f"{', discount' if SMC_REQUIRE_DISCOUNT else ''}{', 4h trend' if SMC_REQUIRE_TREND else ''}"
                 f", quality: {smc_quality_txt()})"
                 if strategy == "smc" else "S&D")
    how += f" | strategy {strat_txt}, filters: {active_filters()}"
    head = (f"## Account simulation: ${balance:g} start, {how}, last {days} days\n\n"
            f"{TIMEFRAME} chart, {HTF} trend, score ≥{MIN_SCORE}, SL ≥{MIN_SL_PCT}%, {coins} futures coins, "
            f"fees {FEE_PCT:.2f}% round trip. One position per coin; a trade is skipped if free balance < ${margin:g}.\n\n"
            "| Exit | Final balance | Return | Trades | Win% | Max drawdown | Loss streak | Best / worst trade | Fees paid |\n"
            "|---|---|---|---|---|---|---|---|---|\n")
    rows = "\n".join(
        f"| {r['lbl']} | ${r['final']:.2f} | {(r['final'] / balance - 1) * 100:+.1f}% | {r['taken']} "
        f"| {(r['wins'] / r['closed'] * 100 if r['closed'] else 0):.0f}% | −${r['dd']:.2f} | {r['streak']} "
        f"| {money(r['best'])} / {money(r['worst'])} | ${r['fees']:.2f} |" for r in results)
    notes = (f"\n\nLiquidation at {leverage}x is ~{liq_dist:.1f}% away; trades whose SL was further than that "
             "count as liquidated (−margin). Still-open trades are valued at the last price. "
             "Funding and slippage are not included. Past results do not guarantee future results.\n")
    report = head + rows + notes
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write(report)

    msg = (f"💼 <b>Account backtest</b> (last {days} days)\n"
           f"${balance:g} start, {how}\n{coins} coins, {TIMEFRAME}\n")
    for r in results:
        ret = (r["final"] / balance - 1) * 100
        msg += (f"\n<b>{r['lbl']}</b>: ${balance:g} → <b>${r['final']:.2f}</b> ({ret:+.1f}%)\n"
                f"{r['taken']} trades, win {(r['wins'] / r['closed'] * 100 if r['closed'] else 0):.0f}%, "
                f"max drawdown −${r['dd']:.2f}, loss streak {r['streak']}\n"
                f"fees ${r['fees']:.2f}"
                + (f", {r['open']} still open ({money(r['unreal'])})" if r["open"] else "")
                + (f", skipped {r['skip_cash']} (no free balance)" if r["skip_cash"] else "")
                + (f", {r['liqs']} liquidated" if r["liqs"] else "") + "\n")
    msg += "\nFunding & slippage not included. Full table: GitHub → Actions → run summary."
    tg(msg)
    send_trade_log(trade_log, balance, exits[0][0], days)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="send a Telegram test message")
    ap.add_argument("--backtest", action="store_true", help="backtest on history")
    ap.add_argument("--candles", type=int, default=5000, help="candles per coin for backtest")
    ap.add_argument("--account", action="store_true", help="simulate a real account over the last N days")
    ap.add_argument("--amd", action="store_true", help="backtest the AMD / Power of 3 model")
    ap.add_argument("--smc", action="store_true", help="backtest the SMC sweep -> MSS -> FVG model")
    ap.add_argument("--balance", type=float, default=100)
    ap.add_argument("--margin", type=float, default=5)
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--sizing", choices=["risk", "fixed"], default="risk",
                    help="risk = same $ loss per trade (default), fixed = margin x leverage")
    ap.add_argument("--risk", type=float, default=1.0, help="$ loss at SL in risk sizing")
    ap.add_argument("--strategy", choices=["smc", "sd", ""], default="", help="account mode strategy")
    args = ap.parse_args()
    resolve_symbols()
    if args.test:
        ok = tg(f"👋 <b>S&D bot connected!</b>\n{len(SYMBOLS)} coins: "
                f"{', '.join(f'{x} (={FUTURES_NAME[x]})' if x in FUTURES_NAME else x for x in SYMBOLS)}\n"
                f"Timeframe: {TIMEFRAME} (trend: {HTF}), min grade: {MIN_SCORE}/6\n"
                f"Min SL: {MIN_SL_PCT}%, fees assumed: {FEE_PCT:.2f}%\n"
                f"Strategy: {STRATEGY.upper()}"
                + (f" (SMC: FVG {'50%' if SMC_ENTRY == 'mid' else 'edge'}, discount {'on' if SMC_REQUIRE_DISCOUNT else 'off'}, "
                   f"trend {'on' if SMC_REQUIRE_TREND else 'off'}, TP = liquidity)" if STRATEGY != "sd" else "") + "\n"
                f"Filters: {active_filters()}\n"
                + (f"SMC quality filters (validated): {smc_quality_txt()}\n" if STRATEGY != "sd" else "")
                + f"Sizing: ${' / $'.join(f'{m:g}' for m in MARGIN_OPTIONS)} margin, max loss ${RISK_USD:g}/trade, leverage ≤{MAX_LEVERAGE}x")
        sys.exit(0 if ok else 1)
    if args.backtest:
        run_backtest(args.candles)
        return
    if args.smc:
        run_smc_backtest(args.candles)
        return
    if args.amd:
        run_amd_backtest(args.candles)
        return
    if args.account:
        run_account(args.balance, args.margin, args.leverage, args.days, args.sizing, args.risk,
                    args.strategy or None)
        return
    run_scan()


if __name__ == "__main__":
    main()
