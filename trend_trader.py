"""T2 trend AUTO-TRADER for Binance USD-M futures. DEMO by default (Binance Demo Trading, fake money).
--paper: a simulated account that runs on GitHub Actions (see PaperEx) - same trading logic, no API key.

Same rules as the backtests (trend_robust.py "Realistic"):
  Universe  Top 30 USDT perpetuals by last month's futures quote volume (no BTC/ETH, stablecoins, gold/stock/index
            contracts), recomputed on the 1st of every month; coin needs >= 100 days of history.
  Entry     4h close: EMA50 crosses EMA200 up -> LONG, down -> SHORT (only coins in this month's Top 30).
  Exit      4h close beyond the chandelier trail (best close since entry -/+ 3 x ATR14), or the opposite cross
            (then the reverse trade opens, if the coin is in the Top 30). Market orders.
  Stop      An exchange STOP_MARKET (closePosition, mark price) at trail -/+ 1.5 ATR, moved when the trail improves
            by >= 0.5 ATR. Only a crash guard: normal exits are the 4h-close rules above.
  Size      qty = RISK_USD / (3 x ATR14), rounded down to Binance's step; if below the minimum order the minimum is
            used only when its risk is <= 1.5 x RISK_USD, else skipped. Max 12 open, total size <= 3 x equity.
            Isolated margin, LEVERAGE (default 5) - leverage only sets margin, the risk is set by the size.
Safety      Demo unless TRADER_MODE=live AND LIVE_CONFIRM=I_UNDERSTAND. A file named STOP_TRADER in this folder stops
            new entries (open trades are still managed). Daily loss guard: no new entries for the rest of the UTC
            day if equity fell DAILY_LOSS_PCT % since the day started. A position the bot did not open is never
            touched. If the protective stop cannot be placed, the new position is closed at once.
Missed runs Exits are checked on every missed 4h close; entries only on the newest close (no late entries).

Run:  python trend_trader.py --check      connection / balance / settings check, places no order
      python trend_trader.py --selftest   DEMO only: tiny open -> stop -> move stop -> close round trip
      python trend_trader.py --loop       run now, then after every 4h close (keep the window open)
      python trend_trader.py --close-all  close every position the bot opened and cancel its stops
Config: trader.env (KEY=VALUE lines, never commit it). See trader.env.example.
"""
import argparse
import hashlib
import hmac
import json
import math
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from urllib.parse import urlencode

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
H4 = 4 * 3600000
DAY = 86400000
IST = timezone(timedelta(hours=5, minutes=30))
BASES = {"demo": "https://demo-fapi.binance.com", "live": "https://fapi.binance.com"}
MARKET_BASE = "https://fapi.binance.com"            # public candles (real market) for signals in both modes
EXCLUDE = {"BTCUSDT", "ETHUSDT", "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT", "USDPUSDT", "DAIUSDT",
           "USDEUSDT", "USD1USDT", "RLUSDUSDT", "EURUSDT", "XUSDUSDT", "PYUSDUSDT", "BFUSDUSDT",
           "XAUUSDT", "XAGUSDT", "XPTUSDT", "XPDUSDT", "PAXGUSDT", "XAUTUSDT", "TSLAUSDT", "NVDAUSDT", "AAPLUSDT",
           "MSTRUSDT", "COINUSDT", "HOODUSDT", "AMZNUSDT", "GOOGLUSDT", "METAUSDT", "MSFTUSDT", "WMTUSDT", "SPYUSDT",
           "QQQUSDT", "CRCLUSDT", "INTCUSDT", "AMDUSDT", "NFLXUSDT", "PLTRUSDT", "ORCLUSDT", "BABAUSDT",
           "BTCDOMUSDT", "DEFIUSDT", "FOOTBALLUSDT", "BLUEBIRDUSDT", "ALL1USDT"}
TOP_N, MIN_HIST, TOL = 30, 100 * DAY, 1.5


# ----------------------------------------------------------------------------------------------- config
def load_env(path=os.path.join(HERE, "trader.env")):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Cfg:
    def __init__(self):
        self.mode = os.getenv("TRADER_MODE", "demo").strip().lower()
        if self.mode not in BASES and self.mode != "paper":
            raise SystemExit(f"TRADER_MODE must be paper, demo or live, not {self.mode!r}")
        if self.mode == "live" and os.getenv("LIVE_CONFIRM", "") != "I_UNDERSTAND":
            raise SystemExit("Live trading needs LIVE_CONFIRM=I_UNDERSTAND in trader.env. Stopping.")
        self.key = os.getenv("BINANCE_API_KEY", "").strip()
        self.secret = os.getenv("BINANCE_API_SECRET", "").strip()
        self.risk = float(os.getenv("RISK_USD", "1") or 1)
        self.max_open = int(os.getenv("MAX_OPEN", "12") or 12)
        self.max_lev = float(os.getenv("MAX_NOTIONAL_X", "3") or 3)
        self.leverage = int(os.getenv("LEVERAGE", "5") or 5)
        self.em_atr = float(os.getenv("EMERGENCY_ATR", "1.5") or 1.5)
        self.daily_loss = float(os.getenv("DAILY_LOSS_PCT", "5") or 5)
        self.acct_size = float(os.getenv("ACCOUNT_SIZE_USD", "0") or 0)    # demo: act like a $300 account
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chats = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
        self.state_file = os.path.join(HERE, f"trader_state_{self.mode}.json")
        self.log_file = os.path.join(HERE, f"trader_trades_{self.mode}.csv")


# ----------------------------------------------------------------------------------------------- indicators
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


def dround(x, step, mode=ROUND_DOWN):
    s = Decimal(str(step))
    return float((Decimal(str(x)) / s).to_integral_value(rounding=mode) * s)


def fmt(x, step):
    s = Decimal(str(step)).normalize()
    places = max(0, -s.as_tuple().exponent)
    return f"{x:.{places}f}"


# ----------------------------------------------------------------------------------------------- Binance
class BinanceError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"Binance error {code}: {msg}")
        self.code = code


class Binance:
    """Minimal signed REST client for USD-M futures (demo or live)."""

    def __init__(self, cfg):
        self.cfg, self.base, self.offset = cfg, BASES[cfg.mode], 0
        self.s = requests.Session()
        self.s.headers["X-MBX-APIKEY"] = cfg.key

    def _req(self, method, path, params=None, signed=False, base=None):
        params = dict(params or {})
        url = (base or self.base) + path
        if signed:
            params["timestamp"] = int(time.time() * 1000 + self.offset)
            params["recvWindow"] = 10000
            qs = urlencode(params)
            sig = hmac.new(self.cfg.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
            url, params = f"{url}?{qs}&signature={sig}", None
        for attempt in range(3):
            try:
                r = self.s.request(method, url, params=params, timeout=20)
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 + 3 * attempt)
                continue
            if r.status_code in (418, 429) or r.status_code >= 500:
                time.sleep(5 + 10 * attempt)
                continue
            data = r.json() if r.text else {}
            if isinstance(data, dict) and data.get("code") not in (None, 200) and int(data.get("code", 0)) < 0:
                raise BinanceError(data.get("code"), data.get("msg"))
            if r.status_code != 200:
                raise BinanceError(r.status_code, r.text[:200])
            return data
        raise BinanceError(r.status_code, "retries exhausted")

    def sync_time(self):
        t = self._req("GET", "/fapi/v1/time")["serverTime"]
        self.offset = t - time.time() * 1000

    # market data (public, real market)
    def klines(self, sym, interval, limit=1000, start=None, end=None):
        p = {"symbol": sym, "interval": interval, "limit": limit}
        if start is not None:
            p["startTime"] = start
        if end is not None:
            p["endTime"] = end
        rows = self._req("GET", "/fapi/v1/klines", p, base=MARKET_BASE)
        return [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                 "ct": int(r[6]), "qv": float(r[7])} for r in rows]

    def market_symbols(self):
        return self._req("GET", "/fapi/v1/exchangeInfo", base=MARKET_BASE)["symbols"]

    # trading account
    def exchange_info(self):
        return self._req("GET", "/fapi/v1/exchangeInfo")["symbols"]

    def account(self):
        a = self._req("GET", "/fapi/v2/account", signed=True)
        pos = {p["symbol"]: float(p["positionAmt"]) for p in a.get("positions", []) if float(p["positionAmt"]) != 0}
        return {"wallet": float(a["totalWalletBalance"]), "unreal": float(a["totalUnrealizedProfit"]),
                "available": float(a["availableBalance"]), "positions": pos}

    def hedge_mode(self):
        return bool(self._req("GET", "/fapi/v1/positionSide/dual", signed=True)["dualSidePosition"])

    def prepare(self, sym, leverage):
        try:
            self._req("POST", "/fapi/v1/marginType", {"symbol": sym, "marginType": "ISOLATED"}, signed=True)
        except BinanceError as e:
            if e.code != -4046:                                     # -4046 = already isolated
                raise
        self._req("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": leverage}, signed=True)

    def market(self, sym, side, qty_str, reduce_only=False):
        p = {"symbol": sym, "side": side, "type": "MARKET", "quantity": qty_str, "newOrderRespType": "RESULT"}
        if reduce_only:
            p["reduceOnly"] = "true"
        o = self._req("POST", "/fapi/v1/order", p, signed=True)
        return float(o.get("avgPrice") or 0), float(o.get("executedQty") or 0)

    def place_stop(self, sym, side, trigger_str):
        o = self._req("POST", "/fapi/v1/algoOrder",
                      {"algoType": "CONDITIONAL", "symbol": sym, "side": side, "type": "STOP_MARKET",
                       "triggerPrice": trigger_str, "closePosition": "true", "workingType": "MARK_PRICE",
                       "priceProtect": "true"}, signed=True)
        return o.get("algoId")

    def cancel_stop(self, algo_id):
        try:
            self._req("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id}, signed=True)
        except BinanceError as e:
            if e.code not in (-2011, -2013):
                raise

    def cancel_stops(self, sym):
        try:
            self._req("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": sym}, signed=True)
        except BinanceError as e:
            if e.code not in (-2011, -2013):                        # nothing to cancel
                raise


# ----------------------------------------------------------------------------------------------- trader
class Trader:
    def __init__(self, cfg, ex, notify=print, now=lambda: time.time() * 1000):
        self.cfg, self.ex, self.notify, self.now = cfg, ex, notify, now
        self.st = self.load()
        self.filters = {}

    # state
    def load(self):
        try:
            with open(self.cfg.state_file, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {"last_ct": None, "pos": {}, "universe": [], "universe_month": None, "day": None,
                    "day_equity": None, "paused_day": None, "started": None}

    def save(self):
        tmp = self.cfg.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.st, f, indent=1)
        os.replace(tmp, self.cfg.state_file)

    def log_trade(self, sym, p, exit_px, why, t):
        new = not os.path.exists(self.cfg.log_file)
        r = p["side"] * (exit_px - p["entry"]) * p["qty"]
        with open(self.cfg.log_file, "a", encoding="utf-8") as f:
            if new:
                f.write("symbol,side,entry_time,exit_time,entry,exit,qty,pnl_usd_before_fees,why\n")
            ts = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
            f.write(f"{sym},{'LONG' if p['side'] > 0 else 'SHORT'},{ts(p['t_in'])},{ts(t)},{p['entry']},"
                    f"{exit_px},{p['qty']},{r:.4f},{why}\n")
        return r

    # exchange rules
    def load_filters(self):
        self.filters = {}
        for s in self.ex.exchange_info():
            if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT" or s.get("status") != "TRADING":
                continue
            f = {x["filterType"]: x for x in s["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f["LOT_SIZE"]
            self.filters[s["symbol"]] = {
                "step": float(lot["stepSize"]), "minQty": float(lot.get("minQty", 0)),
                "maxQty": float(lot.get("maxQty", 1e18)), "tick": float(f["PRICE_FILTER"]["tickSize"]),
                "minNotional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5)),
                "onboard": int(s.get("onboardDate", 0))}

    def size(self, sym, price, risk_dist):
        f = self.filters[sym]
        want = self.cfg.risk / risk_dist
        q = dround(want, f["step"])
        if q >= f["minQty"] and q * price >= f["minNotional"]:
            return q
        q = max(f["minQty"], dround(f["minNotional"] / price, f["step"], ROUND_UP))
        while q * price < f["minNotional"]:
            q = dround(q + f["step"], f["step"], ROUND_UP)
        return q if q * risk_dist <= TOL * self.cfg.risk and q <= f["maxQty"] else None

    # universe: Top 30 by last calendar month's futures quote volume
    def refresh_universe(self, now_ms):
        d = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
        month = d.strftime("%Y-%m")
        if self.st.get("universe_month") == month and self.st.get("universe"):
            return False
        first = d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_first = (first - timedelta(days=1)).replace(day=1)
        start, end = int(prev_first.timestamp() * 1000), int(first.timestamp() * 1000) - 1
        if hasattr(self.ex, "top_symbols"):                # paper mode: futures volume files on data.binance.vision
            top, used = self.ex.top_symbols(prev_first)
            self.st["universe"], self.st["universe_month"] = top, month
            self.notify(f"📋 <b>Top 30 for {month}</b> (by {used} futures volume): " + ", ".join(x[:-4] for x in top))
            return True
        cands = []
        for s in self.ex.market_symbols():
            sym = s["symbol"]
            if (s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT" or s.get("status") != "TRADING"
                    or sym in EXCLUDE or s.get("underlyingType", "COIN") != "COIN"):
                continue
            try:
                rows = self.ex.klines(sym, "1d", 40, start, end)
            except Exception:
                continue
            if len(rows) >= 20:
                cands.append((sum(r["qv"] for r in rows), sym))
            time.sleep(0.05)
        top = [s for _, s in sorted(cands, reverse=True)[:TOP_N]]
        if len(top) < TOP_N // 2:
            raise RuntimeError(f"Top 30 ranking failed ({len(top)} coins) - keeping the old list")
        self.st["universe"], self.st["universe_month"] = top, month
        self.notify(f"📋 <b>Top 30 for {month}</b> (by {prev_first:%b} futures volume): "
                    + ", ".join(x[:-4] for x in top))
        return True

    def equity(self, acct):
        """Account equity; with ACCOUNT_SIZE_USD set (demo has a big fake balance) it acts like that account size
        plus the bot's own profit/loss since start."""
        eq = acct["wallet"] + acct["unreal"]
        if self.cfg.acct_size > 0:
            if self.st.get("eq0") is None:
                self.st["eq0"] = eq
            eq = self.cfg.acct_size + (eq - self.st["eq0"])
        return eq

    # main step: called a little after every 4h close
    def run_once(self):
        cfg, st, ex = self.cfg, self.st, self.ex
        now = self.now()
        msgs = []
        if not self.filters:
            self.load_filters()
        try:
            if self.refresh_universe(now):
                self.load_filters()
        except Exception as e:
            msgs.append(f"⚠️ Universe refresh failed: {e}")
        missing = [x for x in set(st["universe"]) | set(st["pos"]) if x not in self.filters]
        if missing and hasattr(ex, "add_known"):            # paper exchange: make sure it knows every coin
            ex.add_known(missing)
            self.load_filters()
        acct = ex.account()
        equity = self.equity(acct)
        day = datetime.fromtimestamp(now / 1000, timezone.utc).strftime("%Y-%m-%d")
        if st.get("day") != day:
            if st.get("day") is not None:
                start_eq = cfg.acct_size or st.get("eq_first") or equity
                op = ", ".join(f"{k[:-4]} {'L' if v['side'] > 0 else 'S'}" for k, v in sorted(st["pos"].items()))
                msgs.append(f"📋 <b>Daily summary</b> ({st['day']}): equity <b>${equity:,.2f}</b> "
                            f"({equity - start_eq:+.2f}$ since start), day {equity - st['day_equity']:+.2f}$\n"
                            f"Open {len(st['pos'])}/{cfg.max_open}: {op or 'none'}")
            st.setdefault("eq_first", equity)
            st["day"], st["day_equity"] = day, equity
        if st.get("started") is None:
            st["started"] = datetime.now(IST).strftime("%d %b %Y %H:%M IST")
        # 1) reconcile with the exchange: closed outside the bot (emergency stop hit / manual close)
        for sym in list(st["pos"]):
            if acct["positions"].get(sym, 0) == 0:
                p = st["pos"].pop(sym)
                try:
                    ex.cancel_stops(sym)
                except Exception:
                    pass
                px = getattr(ex, "stop_fill", lambda s: None)(sym) or p["em"]
                pnl = self.log_trade(sym, p, px, "closed outside the bot (emergency stop or manual)", now)
                msgs.append(f"🧯 <b>{sym} {'LONG' if p['side'] > 0 else 'SHORT'}</b> closed by the emergency stop "
                            f"(or by hand) @ {px:g}, P&L ≈ {pnl:+.2f}$ before fees. Removed from the bot.")
        unmanaged = [s for s in acct["positions"] if s not in st["pos"]]
        if unmanaged:
            msgs.append("ℹ️ Positions not opened by the bot (left alone): " + ", ".join(unmanaged))
        # 2) candles
        syms = sorted(set(st["universe"]) | set(st["pos"]))
        data, newest_ct = {}, st.get("last_ct")
        for sym in syms:
            if sym not in self.filters:
                continue
            try:
                C = [x for x in ex.klines(sym, "4h", 1000) if x["ct"] < now]
            except Exception as e:
                msgs.append(f"⚠️ {sym}: candles failed ({e})")
                continue
            if len(C) >= 260:
                data[sym] = C
        if not data:
            return self.finish(msgs)
        latest = max(C[-1]["ct"] for C in data.values())
        if st.get("last_ct") is None:                       # very first run: start from now, no old signals
            st["last_ct"] = latest
            msgs.insert(0, f"🚀 <b>Auto-trader started</b> ({cfg.mode.upper()}) — ${cfg.risk:g} risk, Top 30, "
                           f"max {cfg.max_open} open. Signals from the next 4h close.")
            return self.finish(msgs)
        if latest <= st["last_ct"]:
            return self.finish(msgs)
        last = st["last_ct"]
        exits, entries = [], []
        for sym, C in data.items():
            cl = [x["c"] for x in C]
            e50, e200, A = ema(cl, 50), ema(cl, 200), atr(C)
            p = st["pos"].get(sym)
            if p and p.get("retry_exit"):                   # an exit that failed last run
                exits.append((sym, p["retry_exit"], C[-1]["ct"], True))
                continue
            flat = p is None
            for i in [i for i in range(201, len(C)) if C[i]["ct"] > last]:
                newest = C[i]["ct"] == latest
                up = e50[i] > e200[i] and e50[i - 1] <= e200[i - 1]
                dn = e50[i] < e200[i] and e50[i - 1] >= e200[i - 1]
                if not flat:
                    s = p["side"]
                    p["best"] = max(p["best"], C[i]["c"]) if s > 0 else min(p["best"], C[i]["c"])
                    trail = p["best"] - s * 3 * A[i]
                    why = ("4h close beyond the trailing stop" if (s > 0 and C[i]["c"] < trail) or
                           (s < 0 and C[i]["c"] > trail) else
                           "EMA50/200 crossed back" if (s > 0 and dn) or (s < 0 and up) else None)
                    if why:
                        exits.append((sym, why, C[i]["ct"], newest))
                        flat = True                         # same candle: a cross may open the next trade
                    else:
                        if (trail - p["note"]) * s >= 0.5 * A[i]:
                            p["note"], p["em_new"] = trail, trail - s * cfg.em_atr * A[i]
                        continue
                if newest and (up or dn) and A[i]:
                    entries.append((sym, 1 if up else -1, A[i], C[i]))
        # 3) exits first (frees room), then stop moves, then entries
        for sym, why, ct, newest in exits:
            p = st["pos"][sym]
            try:
                amt = abs(ex.account()["positions"].get(sym, 0))
                if amt > 0:
                    px, _ = ex.market(sym, "SELL" if p["side"] > 0 else "BUY", fmt(amt, self.filters[sym]["step"]),
                                      reduce_only=True)
                else:
                    px = p["em"]
                ex.cancel_stops(sym)                        # after the close: an old stop must not hit a new trade
                pnl = self.log_trade(sym, p, px, why, now)
                st["pos"].pop(sym)
                late = "" if newest else " (⏱ late: a run was missed)"
                msgs.append(f"{'✅' if pnl > 0 else '❌'} <b>EXIT {sym} {'LONG' if p['side'] > 0 else 'SHORT'}</b> "
                            f"@ {px:g} — {why}{late}\nP&L ≈ {pnl:+.2f}$ before fees")
            except Exception as e:
                p["retry_exit"] = why
                entries = [x for x in entries if x[0] != sym]
                msgs.append(f"🚨 <b>EXIT FAILED {sym}</b>: {e} — will retry next run, check Binance now")
        for sym, p in st["pos"].items():
            if "em_new" in p:
                lvl = p.pop("em_new")
                try:
                    trig = fmt(dround(lvl, self.filters[sym]["tick"], ROUND_HALF_UP), self.filters[sym]["tick"])
                    new_id = ex.place_stop(sym, "SELL" if p["side"] > 0 else "BUY", trig)
                    if p.get("algo_id"):
                        ex.cancel_stop(p["algo_id"])
                    p["em"], p["algo_id"] = float(trig), new_id
                    msgs.append(f"🔼 <b>{sym}</b> trail {p['note']:g}, emergency stop moved to {trig}")
                except Exception as e:
                    msgs.append(f"🚨 <b>STOP MOVE FAILED {sym}</b>: {e} — the old stop is still there, check Binance")
        paused = os.path.exists(os.path.join(HERE, "STOP_TRADER"))
        if st.get("day_equity") and equity < st["day_equity"] * (1 - cfg.daily_loss / 100):
            if st.get("paused_day") != day:
                msgs.append(f"🛑 Daily loss guard: equity {equity:.2f} is {cfg.daily_loss:g}% below today's start "
                            f"{st['day_equity']:.2f}. No new entries until tomorrow (UTC).")
            st["paused_day"] = day
        guard = st.get("paused_day") == day
        for sym, side, a, c in sorted(entries, key=lambda e: e[0]):
            name = f"{sym} {'LONG' if side > 0 else 'SHORT'}"
            if sym not in st["universe"]:
                continue
            if paused or guard:
                msgs.append(f"⏸ Skipped {name}: {'STOP_TRADER file' if paused else 'daily loss guard'}")
                continue
            onboard = ex.onboard_of(sym) if hasattr(ex, "onboard_of") else self.filters[sym]["onboard"]
            if now - onboard < MIN_HIST:
                continue
            acct = ex.account()
            equity = self.equity(acct)
            price = c["c"]
            risk_dist = 3 * a
            qty = self.size(sym, price, risk_dist)
            if qty is None:
                msgs.append(f"⏭ Skipped {name}: minimum order size too big for ${cfg.risk:g} risk")
                continue
            notional = sum(q["qty"] * (data[s][-1]["c"] if s in data else q["entry"]) for s, q in st["pos"].items())
            if len(st["pos"]) >= cfg.max_open or notional + qty * price > cfg.max_lev * equity:
                msgs.append(f"⏭ Skipped {name}: max {cfg.max_open} trades / {cfg.max_lev:g}x size reached")
                continue
            if qty * price / cfg.leverage > acct["available"] * 0.95:
                msgs.append(f"⏭ Skipped {name}: not enough free margin")
                continue
            try:
                ex.prepare(sym, cfg.leverage)
                fill, got = ex.market(sym, "BUY" if side > 0 else "SELL", fmt(qty, self.filters[sym]["step"]))
            except Exception as e:
                msgs.append(f"🚨 <b>ENTRY FAILED {name}</b>: {e}")
                continue
            if got <= 0:
                msgs.append(f"🚨 <b>ENTRY {name}</b>: order not filled")
                continue
            fill = fill or price
            note = fill - side * risk_dist
            lvl = note - side * cfg.em_atr * a
            trig = fmt(dround(lvl, self.filters[sym]["tick"], ROUND_HALF_UP), self.filters[sym]["tick"])
            try:
                algo_id = ex.place_stop(sym, "SELL" if side > 0 else "BUY", trig)
            except Exception as e:
                try:
                    ex.market(sym, "SELL" if side > 0 else "BUY", fmt(got, self.filters[sym]["step"]), reduce_only=True)
                    msgs.append(f"🚨 <b>{name}</b>: protective stop failed ({e}) — position closed again for safety")
                except Exception as e2:
                    msgs.append(f"🚨🚨 <b>{name}</b>: stop failed AND close failed ({e2}) — CLOSE IT BY HAND NOW")
                continue
            st["pos"][sym] = {"side": side, "qty": got, "entry": fill, "risk": risk_dist, "best": fill, "note": note,
                              "em": float(trig), "algo_id": algo_id, "t_in": int(now)}
            msgs.append(f"{'📈' if side > 0 else '📉'} <b>{name}</b> {got:g} @ {fill:g} "
                        f"(~${got * fill:,.0f}, risk ≈ ${got * risk_dist:.2f})\nTrail {note:g} (4h close), "
                        f"emergency stop {trig} on Binance")
        st["last_ct"] = latest
        return self.finish(msgs)

    def finish(self, msgs):
        self.save()
        for m in msgs:
            self.notify(m)
        return msgs

    def close_all(self):
        msgs = []
        acct = self.ex.account()
        if not self.filters:
            self.load_filters()
        for sym, p in list(self.st["pos"].items()):
            amt = abs(acct["positions"].get(sym, 0))
            if amt:
                px, _ = self.ex.market(sym, "SELL" if p["side"] > 0 else "BUY", fmt(amt, self.filters[sym]["step"]),
                                       reduce_only=True)
                self.log_trade(sym, p, px, "close-all command", self.now())
            self.ex.cancel_stops(sym)
            self.st["pos"].pop(sym)
            msgs.append(f"🧹 Closed {sym}")
        return self.finish(msgs or ["🧹 Nothing to close"])


# ----------------------------------------------------------------------------------------------- paper exchange
class PaperEx:
    """Simulated Binance futures account for GitHub (the futures API is blocked there).
    Candles: Binance SPOT 4h/1h via data-api.binance.vision (1000x futures contracts are scaled from the spot coin).
    Top 30: futures quote volume from the monthly files on data.binance.vision (same as the backtest).
    Fills: market orders at the current spot price -/+ PAPER_SLIP_PCT; 0.05% fee per side; protective stops fill when
    a 1h candle's wick touches them (a gap fills at the candle open). Funding is not simulated."""
    SPOT = "https://data-api.binance.vision/api/v3"
    VISION = "https://data.binance.vision"
    FEE = 0.0005

    def __init__(self, cfg, now=lambda: time.time() * 1000):
        self.cfg, self.now = cfg, now
        self.slip = float(os.getenv("PAPER_SLIP_PCT", "0.05") or 0.05) / 100
        self.file = os.path.join(HERE, "paper_exchange.json")
        try:
            with open(self.file, encoding="utf-8") as f:
                self.d = json.load(f)
        except FileNotFoundError:
            self.d = {"wallet": cfg.acct_size or 300.0, "pos": {}, "stops": {}, "next_id": 1, "known": [],
                      "onboard": {}, "fills": []}
        self.s = requests.Session()
        self._spot_syms = None
        self._cache = {}
        self._fills = {}

    def save(self):
        self.d["fills"] = self.d["fills"][-300:]
        tmp = self.file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, indent=1)
        os.replace(tmp, self.file)

    def _get(self, url, params=None):
        for attempt in range(3):
            try:
                r = self.s.get(url, params=params, timeout=20)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 400:
                    raise BinanceError(400, r.text[:150])
            except requests.RequestException:
                pass
            time.sleep(2 + 3 * attempt)
        raise BinanceError(-1, f"GET failed {url}")

    def spot_syms(self):
        if self._spot_syms is None:
            info = self._get(f"{self.SPOT}/exchangeInfo")
            self._spot_syms = {x["symbol"] for x in info["symbols"] if x.get("status") == "TRADING"}
        return self._spot_syms

    @staticmethod
    def spot_of(sym):
        """futures name -> (spot name, price factor). 1000PEPEUSDT -> (PEPEUSDT, 1000)."""
        i = 0
        while i < len(sym) and sym[i].isdigit():
            i += 1
        if i and sym[:i].startswith("1") and set(sym[1:i]) <= {"0"} and i >= 4:
            return sym[i:], float(sym[:i])
        return sym, 1.0

    def klines(self, sym, interval, limit=1000, start=None, end=None):
        key = (sym, interval, limit, start, end)
        if key in self._cache:
            return self._cache[key]
        spot, k = self.spot_of(sym)
        p = {"symbol": spot, "interval": interval, "limit": limit}
        if start is not None:
            p["startTime"] = start
        if end is not None:
            p["endTime"] = end
        rows = self._get(f"{self.SPOT}/klines", p)
        out = [{"t": int(r[0]), "o": float(r[1]) * k, "h": float(r[2]) * k, "l": float(r[3]) * k,
                "c": float(r[4]) * k, "ct": int(r[6]), "qv": float(r[7])} for r in rows]
        if interval == "4h" and limit >= 1000:
            self.d["onboard"][sym] = out[0]["t"] if len(out) < limit else 0
        self._cache[key] = out
        return out

    def onboard_of(self, sym):
        return self.d["onboard"].get(sym, 0)

    def top_symbols(self, prev_first):
        """Top 30 by futures quote volume of the month starting prev_first (falls back one month if that month's
        file is not published yet). Only coins with a Binance spot pair (needed for paper candles)."""
        import csv
        import io
        import re as _re
        import zipfile
        from concurrent.futures import ThreadPoolExecutor
        syms, marker = [], ""
        for _ in range(20):
            r = self.s.get(f"https://s3-ap-northeast-1.amazonaws.com/data.binance.vision",
                           params={"delimiter": "/", "prefix": "data/futures/um/monthly/klines/", "marker": marker},
                           timeout=30)
            pre = _re.findall(r"<Prefix>data/futures/um/monthly/klines/([^/<]+)/</Prefix>", r.text)
            syms += pre
            if "<IsTruncated>true</IsTruncated>" not in r.text or not pre:
                break
            marker = f"data/futures/um/monthly/klines/{pre[-1]}/"
        spot = self.spot_syms()
        syms = [x for x in sorted(set(syms)) if x.endswith("USDT") and "_" not in x and x not in EXCLUDE
                and self.spot_of(x)[0] in spot]
        for back in (0, 1):
            m = prev_first
            for _ in range(back):
                m = (m - timedelta(days=1)).replace(day=1)
            key = m.strftime("%Y-%m")

            def vol(sym):
                try:
                    r = self.s.get(f"{self.VISION}/data/futures/um/monthly/klines/{sym}/1d/{sym}-1d-{key}.zip",
                                   timeout=20)
                    if r.status_code != 200:
                        return None
                    with zipfile.ZipFile(io.BytesIO(r.content)) as z, z.open(z.namelist()[0]) as f:
                        rows = [x for x in csv.reader(io.TextIOWrapper(f, "utf-8")) if x and x[0].strip().isdigit()]
                    return (sum(float(x[7]) for x in rows), sym) if len(rows) >= 20 else None
                except Exception:
                    return None
            with ThreadPoolExecutor(16) as ex:
                got = [x for x in ex.map(vol, syms) if x]
            if len(got) >= TOP_N:
                top = [s for _, s in sorted(got, reverse=True)[:TOP_N]]
                self.d["known"] = sorted(set(self.d["known"]) | set(top))
                return top, m.strftime("%b %Y")
        raise RuntimeError("monthly volume files not available")

    def add_known(self, syms):
        self.d["known"] = sorted(set(self.d["known"]) | set(syms))

    def exchange_info(self):
        names = set(self.d["known"]) | set(self.d["pos"])
        return [{"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING",
                 "onboardDate": 0,
                 "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.000001", "minQty": "0.000001",
                              "maxQty": "1e12"},
                             {"filterType": "PRICE_FILTER", "tickSize": "0.00000001"},
                             {"filterType": "MIN_NOTIONAL", "notional": "5"}]} for s in names]

    def sync_time(self):
        self.offset = 0

    def hedge_mode(self):
        return False

    def prepare(self, sym, leverage):
        pass

    def price(self, sym):
        spot, k = self.spot_of(sym)
        return float(self._get(f"{self.SPOT}/ticker/price", {"symbol": spot})["price"]) * k

    def account(self):
        unreal, used = 0.0, 0.0
        for sym, p in self.d["pos"].items():
            try:
                px = self.klines(sym, "4h", 1000)[-1]["c"]
            except Exception:
                px = p["entry"]
            unreal += p["qty"] * (px - p["entry"])
            used += abs(p["qty"]) * px / max(1, self.cfg.leverage)
        w = self.d["wallet"]
        return {"wallet": w, "unreal": unreal, "available": w + min(0.0, unreal) - used,
                "positions": {s: p["qty"] for s, p in self.d["pos"].items()}}

    def _fill(self, sym, signed_qty, px, kind):
        fee = abs(signed_qty) * px * self.FEE
        self.d["wallet"] -= fee
        p = self.d["pos"].get(sym)
        if p:
            self.d["wallet"] += -signed_qty * (px - p["entry"]) if abs(signed_qty) <= abs(p["qty"]) + 1e-12 else 0
            p["qty"] += signed_qty
            if abs(p["qty"]) < 1e-12:
                self.d["pos"].pop(sym)
        else:
            self.d["pos"][sym] = {"qty": signed_qty, "entry": px}
        self.d["fills"].append([sym, kind, signed_qty, px, int(self.now())])

    def market(self, sym, side, qty_str, reduce_only=False):
        q = float(qty_str) * (1 if side == "BUY" else -1)
        p = self.d["pos"].get(sym)
        if reduce_only:
            if not p or p["qty"] * q >= 0:
                raise BinanceError(-2022, "ReduceOnly Order is rejected")
            q = max(-abs(p["qty"]), min(abs(p["qty"]), q))
        elif p:
            raise BinanceError(-1, "paper: one position per symbol")
        px = self.price(sym) * (1 + (self.slip if q > 0 else -self.slip))
        self._fill(sym, q, px, "reduce" if reduce_only else "open")
        return px, abs(q)

    def place_stop(self, sym, side, trigger_str):
        i = self.d["next_id"]
        self.d["next_id"] += 1
        self.d["stops"][str(i)] = [sym, side, float(trigger_str), int(self.now()), int(self.now())]
        return i

    def cancel_stop(self, algo_id):
        self.d["stops"].pop(str(algo_id), None)

    def cancel_stops(self, sym):
        for k in [k for k, v in self.d["stops"].items() if v[0] == sym]:
            self.d["stops"].pop(k)

    def stop_fill(self, sym):
        return self._fills.pop(sym, None)

    def process_stops(self):
        """Fill protective stops whose price was touched by a 1h candle since they were placed."""
        now = self.now()
        hits = []
        for k, v in sorted(self.d["stops"].items(), key=lambda kv: int(kv[0])):
            sym, side, trig, placed, checked = v
            p = self.d["pos"].get(sym)
            if not p:
                continue
            try:                                            # 1h candles not checked yet (from the placing hour)
                C = [x for x in self.klines(sym, "1h", 1000, start=max(placed, checked) // 3600000 * 3600000)
                     if x["ct"] < now and x["t"] >= placed // 3600000 * 3600000]
            except Exception:
                continue
            if C:
                v[4] = C[-1]["ct"] + 1
            for x in C:
                if side == "SELL" and x["l"] <= trig:
                    px = min(x["o"], trig) * (1 - self.slip)
                elif side == "BUY" and x["h"] >= trig:
                    px = max(x["o"], trig) * (1 + self.slip)
                else:
                    continue
                self._fill(sym, -p["qty"], px, "stop")
                self._fills[sym] = px
                hits.append((sym, px))
                break
        for sym, _ in hits:
            self.cancel_stops(sym)
        return hits


# ----------------------------------------------------------------------------------------------- telegram / main
def telegram(cfg):
    def send(text):
        print(datetime.now(IST).strftime("%d %b %H:%M"), text.replace("<b>", "").replace("</b>", ""), flush=True)
        if not cfg.tg_token or not cfg.tg_chats:
            return
        tag = {"demo": "🧪 [DEMO] ", "paper": "📝 [PAPER] "}.get(cfg.mode, "💰 [LIVE] ")
        for chat in cfg.tg_chats:
            try:
                requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendMessage",
                              json={"chat_id": chat, "text": tag + text, "parse_mode": "HTML",
                                    "disable_web_page_preview": True}, timeout=15)
            except Exception as e:
                print("Telegram error:", e)
    return send


def check(cfg, ex):
    print(f"Mode: {cfg.mode.upper()}  ({BASES[cfg.mode]})")
    if not cfg.key or not cfg.secret:
        raise SystemExit("BINANCE_API_KEY / BINANCE_API_SECRET missing in trader.env")
    ex.sync_time()
    print(f"Clock offset vs Binance: {ex.offset:+.0f} ms")
    a = ex.account()
    print(f"Balance: wallet {a['wallet']:.2f} USDT, unrealized {a['unreal']:+.2f}, available {a['available']:.2f}")
    print("Open positions:", a["positions"] or "none")
    if ex.hedge_mode():
        raise SystemExit("Hedge mode is ON. Switch Binance futures to One-way mode (Preferences -> Position Mode).")
    print("Position mode: One-way (OK)")
    n = len([s for s in ex.exchange_info() if s.get("contractType") == "PERPETUAL"])
    print(f"Tradable perpetuals on this account: {n}")
    c = ex.klines("SOLUSDT", "4h", 3)
    print(f"Market candles OK (SOLUSDT last close {c[-1]['c']})")
    print(f"Telegram: {'configured' if cfg.tg_token and cfg.tg_chats else 'NOT configured (messages print here only)'}")
    print("CHECK PASSED - no order was placed.")


def selftest(cfg, ex):
    """DEMO only: open the smallest position on XRPUSDT, place + move + cancel a protective stop, close it."""
    if cfg.mode != "demo":
        raise SystemExit("--selftest runs on DEMO only.")
    check(cfg, ex)
    sym = "XRPUSDT"
    tr = Trader(cfg, ex, notify=print)
    tr.load_filters()
    f = tr.filters[sym]
    if ex.account()["positions"].get(sym):
        raise SystemExit(f"{sym} already has a position on this account - selftest skipped.")
    price = ex.klines(sym, "1m", 2)[-1]["c"]
    qty = max(f["minQty"], dround(f["minNotional"] * 1.2 / price, f["step"], ROUND_UP))
    q = fmt(qty, f["step"])
    print(f"1) prepare {sym}: isolated, {cfg.leverage}x")
    ex.prepare(sym, cfg.leverage)
    print(f"2) market BUY {q}")
    fill, got = ex.market(sym, "BUY", q)
    print(f"   filled {got} @ {fill}")
    try:
        t1 = fmt(dround(fill * 0.80, f["tick"], ROUND_HALF_UP), f["tick"])
        a1 = ex.place_stop(sym, "SELL", t1)
        print(f"3) stop placed (algo order) at {t1}, id {a1}")
        t2 = fmt(dround(fill * 0.85, f["tick"], ROUND_HALF_UP), f["tick"])
        a2 = ex.place_stop(sym, "SELL", t2)
        ex.cancel_stop(a1)
        print(f"4) stop moved to {t2} (new id {a2}, old cancelled)")
    finally:
        amt = abs(ex.account()["positions"].get(sym, 0))
        if amt:
            px, _ = ex.market(sym, "SELL", fmt(amt, f["step"]), reduce_only=True)
            print(f"5) closed {amt} @ {px}")
        ex.cancel_stops(sym)
        print("6) all stops on", sym, "cancelled")
    left = ex.account()["positions"].get(sym, 0)
    print("SELFTEST PASSED" if not left else f"SELFTEST: position still open ({left}) - close it by hand")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--close-all", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="DEMO only: tiny open/stop/move/close round trip")
    ap.add_argument("--paper", action="store_true", help="simulated account (for GitHub): no API key, no real orders")
    ap.add_argument("--wait", action="store_true", help="paper on GitHub: wait for the next 4h close, run once, exit")
    a = ap.parse_args()
    load_env()
    if a.paper:
        os.environ["TRADER_MODE"] = "paper"
    cfg = Cfg()
    if cfg.mode == "paper":
        if not cfg.acct_size:
            cfg.acct_size = 300.0
        ex = PaperEx(cfg)
        tr = Trader(cfg, ex, telegram(cfg))
        if a.wait and os.path.exists(cfg.state_file):
            now = time.time() * 1000
            nxt = (now // H4 + 1) * H4 + 4 * 60000                # data-api candles are ready a few min after close
            time.sleep(max(0, (nxt - now) / 1000))
        while True:
            try:
                ex._cache, ex._spot_syms = {}, None
                ex.process_stops()
                tr.run_once()
            except Exception as e:
                tr.notify(f"🚨 Paper trader error: {type(e).__name__}: {e}")
                traceback.print_exc()
            ex.save()
            if not a.loop:
                return
            now = time.time() * 1000
            time.sleep(max(1, ((now // H4 + 1) * H4 + 4 * 60000 - now) / 1000))
    ex = Binance(cfg)
    if a.check:
        return check(cfg, ex)
    if a.selftest:
        return selftest(cfg, ex)
    if not cfg.key or not cfg.secret:
        raise SystemExit("BINANCE_API_KEY / BINANCE_API_SECRET missing in trader.env (run --check first)")
    ex.sync_time()
    if ex.hedge_mode():
        raise SystemExit("Hedge mode is ON. Switch to One-way mode first.")
    tr = Trader(cfg, ex, telegram(cfg))
    if a.close_all:
        return tr.close_all()
    while True:
        try:
            ex.sync_time()
            tr.run_once()
        except Exception as e:
            tr.notify(f"🚨 Trader error: {type(e).__name__}: {e}")
            traceback.print_exc()
        if not a.loop:
            return
        now = time.time() * 1000
        nxt = (now // H4 + 1) * H4 + 90000                    # 90 s after the next 4h close
        while time.time() * 1000 < nxt:                       # short sleeps: survives PC sleep / clock jumps
            time.sleep(min(60, max(1, (nxt - time.time() * 1000) / 1000)))


if __name__ == "__main__":
    main()
