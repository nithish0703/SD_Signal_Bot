"""T2 trend PAPER signals bot (signals + paper P&L only - places no orders).

Model (same as the backtests trend_engine.py / trend_account.py, rules unchanged):
  4h candles. LONG when EMA50 crosses above EMA200 at a 4h close, SHORT when it crosses below.
  Exit when the 4h close goes past the chandelier trail (3 x ATR14 from the best close since entry)
  or EMA50 crosses back (then the opposite trade opens at the same time). Exits are CLOSE-based, exactly like
  the backtest; the "emergency stop" (trail +/- 1.5 ATR) is only a crash guard for the exchange, not a signal.
  $5 risk per trade (TREND_RISK_USD), size = risk / (3 x ATR). Max open risk $60 (12 trades) and total size
  <= 3x paper equity, like the account simulation. Fees 0.10% round trip are counted; funding is not.
Runs once per 4h close (01:30, 05:30, 09:30, 13:30, 17:30, 21:30 IST). Candles: Binance spot via
data-api.binance.vision (futures API is blocked from GitHub); futures prices differ very slightly.
State: trend_state.json. The first run only records the current candle (no trades from the past).
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
         "DOTUSDT", "LTCUSDT", "BCHUSDT", "TRXUSDT", "XLMUSDT", "ETCUSDT", "ATOMUSDT", "FILUSDT", "NEARUSDT",
         "UNIUSDT", "AAVEUSDT"]
RISK = float(os.getenv("TREND_RISK_USD", "") or 5)
START_BAL = float(os.getenv("TREND_PAPER_BALANCE", "") or 1000)
MAX_OPEN = 12
MAX_LEV = 3.0
FEE = 0.10
ATR_MULT = 3.0
EMERGENCY = 1.5                    # exchange stop = trail +/- 1.5 ATR: only for sudden crashes between 4h closes
H4 = 4 * 3600000
IST = timezone(timedelta(hours=5, minutes=30))
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trend_state.json")


def ema(vals, n):
    out, k, e = [], 2 / (n + 1), None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def atr(C, n=14):
    out, prev = [], None
    for i, x in enumerate(C):
        tr = x["h"] - x["l"] if i == 0 else max(x["h"], C[i - 1]["c"]) - min(x["l"], C[i - 1]["c"])
        prev = tr if prev is None else (prev * (n - 1) + tr) / n
        out.append(prev if i >= n - 1 else None)
    return out


def load():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"open": {}, "closed": [], "last_ct": {}, "started": None, "summary_day": None}


def save(st):
    st["closed"] = st["closed"][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


def fp(x):
    return f"{x:,.6g}" if abs(x) < 1 else f"{x:,.4f}".rstrip("0").rstrip(".") if abs(x) < 100 else f"{x:,.2f}"


def realized_usd(st):
    return sum(c["usd"] for c in st["closed"])


def close_pos(st, sym, p, px, t, why):
    stop_pct = p["risk"] / p["entry"] * 100
    r = p["side"] * (px - p["entry"]) / p["risk"] - FEE / stop_pct
    usd = r * p["usd_risk"]
    days = (t - p["t_in"]) / 86400000
    st["closed"].append({"sym": sym, "side": p["side"], "entry": p["entry"], "exit": px, "r": round(r, 3),
                         "usd": round(usd, 2), "t_in": p["t_in"], "t_out": t, "why": why})
    side = "LONG" if p["side"] > 0 else "SHORT"
    icon = "✅" if r > 0 else "❌"
    return (f"{icon} <b>TREND EXIT — {sym} {side}</b>\n"
            f"📌 {bot.esc(why)}\n"
            f"💵 Exit now (market)  ~<code>{fp(px)}</code>\n"
            f"📊 Result  <b>{r:+.2f}R</b>  ({usd:+.2f}$)   held {days:.1f} days")


def open_pos(st, sym, side, px, a, t):
    risk = ATR_MULT * a
    qty = RISK / risk
    equity = START_BAL + realized_usd(st)
    notional = sum(p["qty"] * p["entry"] for p in st["open"].values())
    if len(st["open"]) >= MAX_OPEN or notional + qty * px > MAX_LEV * equity:
        return (f"⏭ <b>TREND SKIP — {sym} {'LONG' if side > 0 else 'SHORT'}</b>\n"
                f"Max {MAX_OPEN} open trades / {MAX_LEV:g}x size reached (paper rule).")
    stop = px - side * risk
    emerg = stop - side * EMERGENCY * a
    st["open"][sym] = {"side": side, "entry": px, "risk": risk, "qty": qty, "usd_risk": RISK, "t_in": t,
                       "best": px, "stop_note": stop}
    return (f"{'📈' if side > 0 else '📉'} <b>TREND {'LONG' if side > 0 else 'SHORT'} — {sym} (4h)</b>\n"
            f"💵 Entry   now (market)  ~<code>{fp(px)}</code>\n"
            f"🛑 Trail   <code>{fp(stop)}</code>   ({ATR_MULT:g} × ATR, {risk / px * 100:.1f}% away) — the bot sends "
            f"EXIT when a 4h candle CLOSES beyond it\n"
            f"🧯 Emergency stop on Binance: <code>{fp(emerg)}</code> (only for a sudden crash)\n"
            f"📦 Size    {qty:.6g} {sym[:-4]}  (~${qty * px:,.0f}, risk ${RISK:g})\n"
            f"⚙️ Isolated, low leverage (2–3x is enough)\n"
            f"🎯 No fixed TP — exit on the bot's EXIT signal")


def process(sym, C, st, msgs):
    """Walk the new closed 4h candles of one coin in order. C = closed candles, oldest first."""
    if len(C) < 260:
        print(sym, "not enough candles", len(C))
        return
    cl = [x["c"] for x in C]
    e50, e200, A = ema(cl, 50), ema(cl, 200), atr(C)
    last = st["last_ct"].get(sym)
    if last is None:                                     # first run: start from now, no trades from the past
        st["last_ct"][sym] = C[-1]["ct"]
        return
    for t in range(201, len(C)):
        x = C[t]
        if x["ct"] <= last:
            continue
        px = C[t + 1]["o"] if t + 1 < len(C) else x["c"]   # live: act now at ~the close; catch-up: next open
        tt = C[t + 1]["t"] if t + 1 < len(C) else x["ct"] + 1
        late = ("" if t + 1 >= len(C) else
                f"\n⏱ Late: this was the {datetime.fromtimestamp((x['ct'] + 1) / 1000, IST):%d %b %H:%M} IST close "
                f"(a run was missed) — price shown is the next open")
        up = e50[t] > e200[t] and e50[t - 1] <= e200[t - 1]
        dn = e50[t] < e200[t] and e50[t - 1] >= e200[t - 1]
        p = st["open"].get(sym)
        if p:
            side = p["side"]
            p["best"] = max(p["best"], x["c"]) if side > 0 else min(p["best"], x["c"])
            trail = p["best"] - side * ATR_MULT * A[t]
            if side > 0 and x["c"] < trail or side < 0 and x["c"] > trail:
                msgs.append(close_pos(st, sym, st["open"].pop(sym), px, tt, "4h close beyond the trailing stop") + late)
            elif side > 0 and dn or side < 0 and up:
                msgs.append(close_pos(st, sym, st["open"].pop(sym), px, tt, "EMA50/200 crossed back (trend over)") + late)
            else:
                better = trail - p["stop_note"] if side > 0 else p["stop_note"] - trail
                if better >= 0.5 * A[t]:                  # tell the user to move the protective stop
                    old, p["stop_note"] = p["stop_note"], trail
                    open_r = side * (x["c"] - p["entry"]) / p["risk"]
                    msgs.append(f"{'🔼' if side > 0 else '🔽'} <b>TREND TRAIL MOVED — {sym} "
                                f"{'LONG' if side > 0 else 'SHORT'}</b>\n"
                                f"🛑 Trail  <code>{fp(trail)}</code>  (was {fp(old)}) — EXIT comes on a 4h close beyond it\n"
                                f"🧯 Move the emergency stop to <code>{fp(trail - side * EMERGENCY * A[t])}</code>\n"
                                f"📊 Open P&amp;L  {open_r:+.2f}R" + late)
                continue
        if (up or dn) and A[t]:
            msgs.append(open_pos(st, sym, 1 if up else -1, px, A[t], tt) + late)
    st["last_ct"][sym] = C[-1]["ct"]


def summary(st, prices):
    now = datetime.now(IST)
    closed = st["closed"]
    n = len(closed)
    tot_r = sum(c["r"] for c in closed)
    wins = sum(1 for c in closed if c["r"] > 0)
    lines = [f"📋 <b>TREND paper — daily summary</b> ({now:%d %b %Y})",
             f"Since {st.get('started') or '—'}: {n} closed trades, win {wins / n * 100 if n else 0:.0f}%, "
             f"<b>{tot_r:+.2f}R</b> ({realized_usd(st):+.2f}$ at ${RISK:g} risk)"]
    if st["open"]:
        lines.append(f"Open ({len(st['open'])}/{MAX_OPEN}):")
        for sym, p in sorted(st["open"].items()):
            px = prices.get(sym, p["entry"])
            r = p["side"] * (px - p["entry"]) / p["risk"]
            lines.append(f"• {sym[:-4]} {'LONG' if p['side'] > 0 else 'SHORT'} {r:+.2f}R  trail {fp(p['stop_note'])}")
    else:
        lines.append("No open trades.")
    lines.append("Paper only — no real orders.")
    return "\n".join(lines)


def wait_next_close():
    now = time.time() * 1000
    target = (now // H4 + 1) * H4 + 4 * 60000            # 4 minutes after the next 4h close
    secs = (target - now) / 1000
    if 0 < secs < 4.5 * 3600:
        print(f"waiting {secs / 60:.0f} min for the next 4h close", flush=True)
        time.sleep(secs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", action="store_true", help="sleep until 4 minutes after the next 4h close first")
    args = ap.parse_args()
    if args.wait and os.path.exists(STATE_FILE):          # the very first run starts right away
        wait_next_close()
    st = load()
    first = st.get("started") is None
    if first:
        st["started"] = datetime.now(IST).strftime("%d %b %Y %H:%M IST")
    msgs, prices, errors = [], {}, []
    for sym in COINS:
        try:
            C, _ = bot.fetch_klines(sym, "4h", 1000, source="spot-vision")
        except Exception as e:
            errors.append(sym[:-4])
            print(sym, "data error", e)
            continue
        prices[sym] = C[-1]["c"]
        process(sym, C, st, msgs)
        time.sleep(0.1)
    if first:
        msgs.insert(0, f"🚀 <b>TREND paper bot started</b> ({len(prices)} coins, 4h EMA50/200 + 3 ATR trail)\n"
                       f"Signals from the next 4h close. Paper only — ${RISK:g} risk per trade, no real orders.")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not first and st.get("summary_day") != today and datetime.now(timezone.utc).hour < 4:
        msgs.append(summary(st, prices))                  # once a day, on the 00:00 UTC (05:30 IST) close
        st["summary_day"] = today
    if errors:
        msgs.append("⚠️ Trend bot: no data for " + ", ".join(errors) + " this run (will catch up next run).")
    save(st)
    for m in msgs:
        print(m.replace("<b>", "").replace("</b>", ""))
        bot.tg(m)
        time.sleep(0.3)


if __name__ == "__main__":
    main()
