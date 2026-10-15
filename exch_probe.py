"""Which crypto-futures exchanges (live + demo/testnet) answer from a GitHub Actions runner? Report only.

Calls only PUBLIC endpoints (server time / market data). No API key, no account, no orders.
A public endpoint that answers is a good sign, but a private (trading) endpoint can still be blocked - the real
check is placing a demo order with a demo API key later.
"""
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402

# (exchange, kind, method, url, json body)
TARGETS = [
    ("Binance", "live futures", "GET", "https://fapi.binance.com/fapi/v1/time", None),
    ("Binance", "futures testnet", "GET", "https://testnet.binancefuture.com/fapi/v1/time", None),
    ("Binance", "demo trading", "GET", "https://demo-fapi.binance.com/fapi/v1/time", None),
    ("Bybit", "live", "GET", "https://api.bybit.com/v5/market/time", None),
    ("Bybit", "testnet", "GET", "https://api-testnet.bybit.com/v5/market/time", None),
    ("Bybit", "demo trading", "GET", "https://api-demo.bybit.com/v5/market/time", None),
    ("OKX", "live + demo (same host)", "GET", "https://www.okx.com/api/v5/public/time", None),
    ("Bitget", "live + demo (same host)", "GET", "https://api.bitget.com/api/v2/public/time", None),
    ("Delta Exchange India", "live", "GET", "https://api.india.delta.exchange/v2/products?page_size=1", None),
    ("Delta Exchange India", "testnet", "GET", "https://cdn-ind.testnet.deltaex.org/v2/products?page_size=1", None),
    ("Hyperliquid", "live", "POST", "https://api.hyperliquid.xyz/info", {"type": "meta"}),
    ("Hyperliquid", "testnet", "POST", "https://api.hyperliquid-testnet.xyz/info", {"type": "meta"}),
    ("Kraken Futures", "live", "GET", "https://futures.kraken.com/derivatives/api/v3/tickers", None),
    ("Kraken Futures", "demo", "GET", "https://demo-futures.kraken.com/derivatives/api/v3/tickers", None),
    ("Gate", "live futures", "GET", "https://api.gateio.ws/api/v4/futures/usdt/contracts?limit=1", None),
    ("Gate", "futures testnet", "GET", "https://fx-api-testnet.gateio.ws/api/v4/futures/usdt/contracts?limit=1", None),
    ("KuCoin", "live futures", "GET", "https://api-futures.kucoin.com/api/v1/timestamp", None),
    ("MEXC", "live futures", "GET", "https://contract.mexc.com/api/v1/contract/ping", None),
    ("BingX", "live futures", "GET", "https://open-api.bingx.com/openApi/swap/v2/server/time", None),
    ("CoinDCX", "live", "GET", "https://api.coindcx.com/exchange/v1/markets", None),
]


def probe(method, url, body):
    t0 = time.time()
    try:
        r = (requests.post(url, json=body, timeout=15) if method == "POST" else requests.get(url, timeout=15))
        ms = (time.time() - t0) * 1000
        txt = re.sub(r"\s+", " ", r.text[:120])
        ok = r.status_code == 200 and not re.search(r"restricted|forbidden|not available|blocked", txt, re.I)
        return ok, f"HTTP {r.status_code}, {ms:.0f} ms", txt
    except Exception as e:
        return False, type(e).__name__, str(e)[:120]


def main():
    try:
        ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
        geo = requests.get(f"https://ipinfo.io/{ip}/json", timeout=10).json()
        where = f"{ip} ({geo.get('city', '?')}, {geo.get('region', '?')}, {geo.get('country', '?')}, {geo.get('org', '?')})"
    except Exception as e:
        where = f"unknown ({type(e).__name__})"
    out = ["## Exchange reachability from GitHub Actions\n", f"Runner IP: {where}\n",
           "| Exchange | Endpoint | Result | Detail | First bytes |", "|---|---|---|---|---|"]
    tg = [f"🌐 <b>Exchange check from GitHub</b>\nRunner: {bot.esc(where)}\n(public endpoints only, no key, no orders)"]
    for ex, kind, m, url, body in TARGETS:
        ok, det, txt = probe(m, url, body)
        out.append(f"| {ex} | {kind} | {'✅ reachable' if ok else '❌ blocked/failed'} | {det} | `{txt}` |")
        tg.append(f"{'✅' if ok else '❌'} {bot.esc(ex)} — {bot.esc(kind)}: {bot.esc(det)}")
        print(ex, kind, ok, det, txt, flush=True)
    out.append("\nReachable public endpoint = good sign only; trading endpoints are confirmed later with a demo key.")
    report = "\n".join(out)
    print(report)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.getenv("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write(report + "\n")
    bot.tg("\n".join(tg))


if __name__ == "__main__":
    main()
