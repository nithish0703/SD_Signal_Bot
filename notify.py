"""Telegram + ntfy phone alerts (shared helpers for the FX bot)."""
import os
import re

import requests

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHATS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()          # secret topic name (GitHub secret)
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")


def tg(text):
    if not TG_TOKEN or not TG_CHATS:
        print("[telegram not configured]\n" + text)
        return False
    branch = os.getenv("GITHUB_REF_NAME", "")
    if branch and branch != "main":                      # test runs from dev are labelled
        text = f"🧪 <b>[{branch.upper()}]</b> " + text
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


def push(title, text, priority=3, tags=None):
    """Phone push through ntfy. Main branch only (dev test runs never ring the phone)."""
    if not NTFY_TOPIC or os.getenv("GITHUB_REF_NAME", "main") != "main":
        return False
    plain = re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    try:
        r = requests.post(NTFY_SERVER, json={"topic": NTFY_TOPIC, "title": title, "message": plain[:3500],
                                             "priority": priority, "tags": tags or []}, timeout=10)
        if r.status_code != 200:
            print("ntfy error:", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:
        print("ntfy error:", e)
        return False


def notify(text, title, priority=3, tags=None):
    """Telegram + ntfy push."""
    ok = tg(text)
    push(title, text, priority, tags)
    return ok
