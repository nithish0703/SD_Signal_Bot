"""USD/JPY London opening-range breakout – signal bot for NSE USDJPY futures (signals only, no auto trade).

Rules (from the fx_lab test, adapted to NSE hours):
  * Range = the 08:00-09:00 London hourly candle (high / low).
  * Entry = close of the first later hourly candle that closes above the range (BUY) or below it (SELL);
    only candles that close by 18:30 IST. One trade a day.
  * Safety stop = the other side of the range.
  * Exit = 19:15 IST ("close before NSE 19:30"), no fixed target.
Runs every 15 minutes inside the bot's scan loop; state in fx_state.json. Paper tracking of every signal.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notify as bot  # noqa: E402  (Telegram + ntfy helpers)

LDN = ZoneInfo("Europe/London")
IST = ZoneInfo("Asia/Kolkata")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fx_state.json")
PIP = 0.01
LAST_ENTRY_IST = (18, 30)          # entry candle must close by 18:30 IST
EXIT_IST = (19, 15)                # send the exit at 19:15 IST (NSE closes 19:30)
LOT_USD = 1000


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"days": {}, "closed": []}


def save_state(st):
    days = sorted(st["days"])
    for d in days[:-10]:                      # keep 10 days
        del st["days"][d]
    st["closed"] = st["closed"][-500:]
    with open(STATE, "w") as f:
        json.dump(st, f, indent=1)


def hourly(now=None):
    import yfinance as yf
    df = yf.Ticker("JPY=X").history(period="5d", interval="1h", auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError("no USDJPY data")
    idx = df.index if df.index.tz is not None else df.index.tz_localize("UTC")
    now = now or datetime.now(timezone.utc)
    rows = []
    for t, r in zip(idx, df.itertuples()):
        start = t.to_pydatetime().astimezone(timezone.utc)
        if start + timedelta(hours=1) <= now:          # complete candles only
            rows.append({"start": start, "o": float(r.Open), "h": float(r.High), "l": float(r.Low), "c": float(r.Close)})
    return rows


def last_price():
    import yfinance as yf
    try:
        df = yf.Ticker("JPY=X").history(period="1d", interval="1m", auto_adjust=False)
        if df is not None and not df.empty:
            return float(df["Close"].iloc[-1])
    except Exception:
        pass
    return None


def pip_inr():
    """Rupee value of 1 pip on 1 NSE lot ($1,000): 1000 x 0.01 JPY converted to INR."""
    import yfinance as yf
    try:
        usd_inr = float(yf.Ticker("INR=X").history(period="5d", interval="1d")["Close"].iloc[-1])
        usd_jpy = float(yf.Ticker("JPY=X").history(period="5d", interval="1d")["Close"].iloc[-1])
        return LOT_USD * PIP * usd_inr / usd_jpy
    except Exception:
        return 5.8


def ist(dt):
    return dt.astimezone(IST)


def fmt_ist(dt):
    return ist(dt).strftime("%d %b, %I:%M %p IST")


def stats_line(closed):
    if not closed:
        return "Paper results: first trade."
    pips = [c["pips"] for c in closed]
    w = sum(1 for p in pips if p > 0)
    return f"Paper results: {len(pips)} trades, win {w / len(pips) * 100:.0f}%, total {sum(pips):+.1f} pips"


def run(test=False, now=None):
    now = now or datetime.now(timezone.utc)
    now_ist = ist(now)
    if now_ist.weekday() >= 5 and not test:
        return
    st = load_state()
    rows = hourly(now)
    hb_day = now_ist.date().isoformat()
    if not test and now_ist.hour >= 12 and st.get("heartbeat") != hb_day:
        if bot.tg(f"✅ <b>FX bot running</b> – USD/JPY London breakout (NSE USDJPY futures)\n"
                  f"Range after the London 08–09 candle, entries until 6:30 PM IST, exit 7:15 PM IST.\n"
                  f"{stats_line(st['closed'])}"):
            st["heartbeat"] = hb_day
    today = now.astimezone(LDN).date().isoformat()
    day = st["days"].setdefault(today, {"status": "waiting"})
    todays = [r for r in rows if r["start"].astimezone(LDN).date().isoformat() == today]
    rng = next((r for r in todays if r["start"].astimezone(LDN).hour == 8), None)
    pv = None

    if rng and "range" not in day:
        day["range"] = [rng["h"], rng["l"]]
        day["status"] = "armed"
        width = (rng["h"] - rng["l"]) / PIP
        bot.tg(f"🇯🇵 <b>USD/JPY range set</b> (London 08–09)\nHigh {rng['h']:.3f} | Low {rng['l']:.3f} ({width:.0f} pips)\n"
               f"Waiting for an hourly close above / below until {LAST_ENTRY_IST[0] - 12}:{LAST_ENTRY_IST[1]:02d} PM IST.")

    if day.get("status") == "armed":
        hi, lo = day["range"]
        for r in todays:
            if r["start"].astimezone(LDN).hour <= 8 or r["start"].isoformat() <= day.get("seen", ""):
                continue
            day["seen"] = r["start"].isoformat()
            end_ist = ist(r["start"] + timedelta(hours=1))
            if (end_ist.hour, end_ist.minute) > LAST_ENTRY_IST:
                day["status"] = "no trade"
                break
            side = 1 if r["c"] > hi else -1 if r["c"] < lo else 0
            if side:
                entry, sl = r["c"], (lo if side > 0 else hi)
                risk = abs(entry - sl) / PIP
                pv = pv or pip_inr()
                day.update({"status": "open", "side": side, "entry": entry, "sl": sl, "t": r["start"].isoformat()})
                word = "BUY" if side > 0 else "SELL"
                bot.notify(
                    f"{'🟢' if side > 0 else '🔴'} <b>USD/JPY — {word}</b> | London breakout · NSE USDJPY futures\n"
                    f"🕒 {fmt_ist(r['start'] + timedelta(hours=1))}\n"
                    f"📍 Entry: market now (spot ~ {entry:.3f})\n"
                    f"🛑 SL: {sl:.3f} ({risk:.0f} pips ≈ ₹{risk * pv:,.0f} per lot)\n"
                    f"⏰ Exit: {EXIT_IST[0] - 12}:{EXIT_IST[1]:02d} PM IST today (before NSE 7:30 PM close) – no fixed target\n"
                    f"📦 1 lot = $1,000 · 1 pip ≈ ₹{pv:.1f}\n"
                    f"⚠️ Paper-test signal – strategy not proven long-term. Not financial advice.",
                    f"SIGNAL USDJPY {word}", 5, ["rotating_light"])
                break

    if day.get("status") == "open":
        side, entry, sl = day["side"], day["entry"], day["sl"]
        hit = None
        for r in todays:
            if r["start"].isoformat() <= day["t"]:
                continue
            if (side > 0 and r["l"] <= sl) or (side < 0 and r["h"] >= sl):
                hit = ("SL hit", sl, r["start"] + timedelta(hours=1))
                break
        if hit is None:
            px_now = last_price()
            if px_now is not None and ((side > 0 and px_now <= sl) or (side < 0 and px_now >= sl)):
                hit = ("SL hit", sl, now)
        if hit is None and (now_ist.hour, now_ist.minute) >= EXIT_IST:
            px = last_price() or (todays[-1]["c"] if todays else entry)
            hit = ("exit at NSE close", px, now)
        if hit:
            why, px, t = hit
            pips = (px - entry) * side / PIP
            pv = pv or pip_inr()
            st["closed"].append({"day": today, "side": side, "entry": entry, "exit": px, "pips": round(pips, 1), "why": why})
            day["status"] = "closed"
            icon = "🎯" if pips > 0 else "🛑"
            bot.notify(f"{icon} <b>USD/JPY {'BUY' if side > 0 else 'SELL'} closed</b> – {why}\n"
                       f"Entry {entry:.3f} → exit {px:.3f} = {pips:+.1f} pips (≈ ₹{pips * pv:+,.0f} per lot)\n"
                       + ("👉 Close your NSE position now.\n" if why.startswith("exit") else "")
                       + stats_line(st["closed"]),
                       "USDJPY closed", 4 if why.startswith("exit") else 3, ["bell"])

    if day.get("status") in ("waiting", "armed") and (now_ist.hour, now_ist.minute) >= EXIT_IST:
        day["status"] = "no trade"
    save_state(st)
    if test:
        bot.tg(f"🧪 FX bot test – {now_ist:%d %b %I:%M %p IST}\nToday ({today}): {json.dumps(day)}\n"
               f"Last complete candle: {rows[-1]['start'].astimezone(IST):%d %b %I:%M %p IST} close {rows[-1]['c']:.3f}\n"
               f"1 pip on 1 lot ≈ ₹{pip_inr():.2f}\n{stats_line(st['closed'])}")


if __name__ == "__main__":
    try:
        run(test="--test" in sys.argv)
    except Exception as e:
        print("fx_bot error:", e)
        if "--test" in sys.argv:
            bot.tg(f"⚠️ FX bot test error: {e}")
