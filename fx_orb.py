"""Long-history check of the London opening-range breakout (and session drift) on FX majors.

Data: Dukascopy hourly bid candles 2012 -> today (downloaded with the dukascopy-node CLI in GitHub Actions).
Rule (unchanged from fx_lab): London session 08:00-17:00 London time; range = the 08:00 hourly candle;
enter at the close of the first later candle that closes above (long) / below (short) the range;
exit at the 16:00 candle's close. One trade per day. Report only.
"""
import glob
import os
import re
import subprocess
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fx_lab  # noqa: E402

PAIRS = [("USD/JPY", "usdjpy", 0.01), ("EUR/USD", "eurusd", 0.0001), ("GBP/USD", "gbpusd", 0.0001)]
COSTS = [1, 2, 3, 5]
START = "2012-01-01"


def download(code):
    end = pd.Timestamp.utcnow().strftime("%Y-%m-%d")
    out = f"dk_{code}"
    os.makedirs(out, exist_ok=True)
    cmd = ["npx", "--yes", "dukascopy-node", "-i", code, "-from", START, "-to", end, "-t", "h1",
           "-f", "csv", "-dir", out, "-r", "3", "-re", "-s"]
    print(" ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, timeout=3000)
    files = [f for f in glob.glob(os.path.join(out, "*.csv")) if os.path.getsize(f) > 0]
    if not files:
        raise RuntimeError("no csv from dukascopy")
    df = pd.read_csv(files[0])
    cols = {c.lower(): c for c in df.columns}
    ts = df[cols.get("timestamp", df.columns[0])]
    idx = pd.to_datetime(ts, unit="ms", utc=True) if pd.api.types.is_numeric_dtype(ts) else pd.to_datetime(ts, utc=True)
    df = pd.DataFrame({"Open": df[cols["open"]].values, "High": df[cols["high"]].values,
                       "Low": df[cols["low"]].values, "Close": df[cols["close"]].values}, index=idx)
    df = df[df["High"] > df["Low"]]                              # drop flat (closed-market) candles
    df.index = df.index.tz_convert("Europe/London")
    return df


def line_stats(tr, pip, cost):
    net = [(e2 - e1) * s / pip - cost for _, s, e1, e2 in tr]
    if not net:
        return "0 trades"
    w = sum(1 for x in net if x > 0)
    return f"{sum(net) / len(net):+.2f} pips/trade, win {w / len(net) * 100:.0f}%, total {sum(net):+,.0f} pips"


def main():
    import bot
    out = ["🧪 <b>FX opening-range breakout – long history</b> (Dukascopy hourly, 2012→today)",
           "Rule unchanged: London 08:00 candle = range; first later close beyond it → trade; exit 17:00 London. "
           "Net pips per trade at round-trip costs of 1/2/3/5 pips. Pass = profit in most years at 2 pips."]
    for name, code, pip in PAIRS:
        try:
            h = download(code)
        except Exception as e:
            out.append(f"\n<b>{name}</b>: download error {e}")
            continue
        days = fx_lab.sessions(h, 8, 17)
        res = fx_lab.intraday_trades(days)
        orb = res["1. Opening-range breakout"]
        drift = res["4a. Session drift LONG"]
        out.append(f"\n<b>{name}</b> – {len(days)} sessions {days[0][0]:%Y}→{days[-1][0]:%Y}")
        out.append("ORB all years: " + " | ".join(f"{c}p cost {sum((e2 - e1) * s / pip - c for _, s, e1, e2 in orb) / max(1, len(orb)):+.2f}"
                                                  for c in COSTS) + f" pips/trade ({len(orb)} trades)")
        years = sorted({d.year for d, *_ in orb})
        pos2 = 0
        rows = []
        for y in years:
            ty = [t for t in orb if t[0].year == y]
            n2 = sum((e2 - e1) * s / pip - 2 for _, s, e1, e2 in ty) / len(ty)
            pos2 += n2 > 0
            rows.append(f"{str(y)[2:]}:{n2:+.1f}")
        out.append(f"ORB per year (net pips/trade at 2p cost): " + " ".join(rows)
                   + f" → profitable years {pos2}/{len(years)} {'✅' if pos2 >= 0.7 * len(years) else '❌'}")
        longs = [t for t in orb if t[1] > 0]
        shorts = [t for t in orb if t[1] < 0]
        out.append(f"ORB longs: {line_stats(longs, pip, 2)} | shorts: {line_stats(shorts, pip, 2)} (2p cost)")
        out.append(f"Session drift LONG (2p cost): {line_stats(drift, pip, 2)}")
    out.append("\nReport only – nothing live. Dukascopy bid prices; NSE futures spreads can be wider. Not financial advice.")
    msg = "\n".join(out)
    print(msg)
    for i in range(0, len(msg), 3900):
        bot.tg(msg[i:i + 3900])
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as fh:
            fh.write("## FX ORB long history\n\n" + re.sub(r"</?b>", "**", msg).replace("\n", "  \n") + "\n")


if __name__ == "__main__":
    main()
