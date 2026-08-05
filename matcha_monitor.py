#!/usr/bin/env python3
"""
matcha-tg-monitor
------------------
Watches Marukyu Koyamaen matcha products and sends a Telegram message the
moment one goes from sold-out to in-stock.

Detection strategy: the shop's product pages always show the exact text
"currently out of stock and unavailable" when a product is sold out --
regardless of whether the visitor is logged in. When that text is absent
(and the page otherwise looks like a genuine, fully-loaded product page)
the product is treated as buyable. This deliberately does NOT rely on
spotting an "Add to cart" button, because that button is only rendered for
logged-in accounts -- an anonymous request never sees it, in-stock or not.

Scheduling: GitHub's own `schedule:` cron trigger is heavily throttled in
practice (often firing every few hours instead of every few minutes), so it
must not be relied on for a tight cadence. The real 3-minute cadence comes
from an external pinger (e.g. cron-job.org) calling this workflow's
workflow_dispatch API on a timer. See README.md for setup.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
PRODUCTS_FILE = ROOT / "products.json"
STATE_FILE = ROOT / "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

TIMEOUT_SECONDS = 20
MAX_RETRIES = 2
REQUEST_SPACING_SECONDS = 2  # be polite to the shop between products

JST = timezone(timedelta(hours=9))

OUT_OF_STOCK_MARKER = "currently out of stock and unavailable"

# Always present on a genuine, fully-loaded product page, whether or not the
# visitor is logged in and whether or not the item is in stock. Used to
# reject error/placeholder pages.
PAGE_SANITY_MARKERS = ["Product Detail", "SKU"]

# Signs of an anti-bot challenge / block / generic error page. If any of
# these show up, we do NOT trust this read either way -- skip the update
# rather than risk a false positive or a false "still out of stock".
BLOCK_MARKERS = [
    "access denied",
    "attention required",
    "captcha",
    "cloudflare",
    "are you a robot",
    "unusual traffic",
]

MIN_VALID_PAGE_LENGTH = 5000


# --------------------------------------------------------------------------
# Products / state
# --------------------------------------------------------------------------
def load_products() -> dict:
    data = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
    base_url = data["base_url"]
    return {
        p["id"]: {"name": p["name"], "url": base_url + p["id"]}
        for p in data["products"]
        if p.get("watch", True)
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[warn] state.json is corrupted, starting fresh", file=sys.stderr)
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Stock check
# --------------------------------------------------------------------------
def fetch_page(url: str) -> str | None:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=REQUEST_HEADERS, timeout=TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_error = exc
            print(f"[warn] attempt {attempt}/{MAX_RETRIES} failed for {url}: {exc}", file=sys.stderr)
            time.sleep(2)
    print(f"[error] could not fetch {url}: {last_error}", file=sys.stderr)
    return None


def is_available(html: str) -> bool | None:
    """
    Returns:
      True  -> confidently in stock
      False -> confidently out of stock
      None  -> page could not be trusted (too short / not a real product
               page / looks like a bot-block or error page). Caller must
               skip this update rather than treat None as "out of stock".
    """
    if len(html) < MIN_VALID_PAGE_LENGTH:
        return None

    if not all(marker in html for marker in PAGE_SANITY_MARKERS):
        return None

    lowered = html.lower()
    if any(marker in lowered for marker in BLOCK_MARKERS):
        return None

    return OUT_OF_STOCK_MARKER not in html


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[error] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in environment", file=sys.stderr)
        return False

    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(api_url, data=payload, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"[error] failed to send Telegram message: {exc}", file=sys.stderr)
        return False


def build_stock_notification(name: str, url: str) -> str:
    return f"🍵 <b>{name}</b> is now IN STOCK\n{url}\n\nDetected by matcha-tg-monitor."


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    products = load_products()
    state = load_state()
    exit_code = 0

    for idx, (pid, product) in enumerate(products.items()):
        if idx > 0:
            time.sleep(REQUEST_SPACING_SECONDS)

        html = fetch_page(product["url"])
        prev = state.get(pid, {})
        was_in_stock = prev.get("in_stock", False)

        if html is None:
            print(f"[warn] {product['name']}: fetch failed, leaving previous state untouched", file=sys.stderr)
            exit_code = 1
            continue

        available = is_available(html)

        if available is None:
            print(f"[warn] {product['name']}: page not trusted (block/error page?), skipping this read", file=sys.stderr)
            exit_code = 1
            continue

        print(f"[info] {product['name']}: in_stock={available} (was {was_in_stock})")

        if available and not was_in_stock:
            if not send_telegram_message(build_stock_notification(product["name"], product["url"])):
                exit_code = 1

        state[pid] = {
            "name": product["name"],
            "in_stock": available,
            "checked": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        }

    save_state(state)
    return exit_code


if __name__ == "__main__":
    if "--test-telegram" in sys.argv:
        ok = send_telegram_message("✅ matcha-tg-monitor test message -- Telegram credentials are working.")
        sys.exit(0 if ok else 1)
    if "--diagnose" in sys.argv:
        sys.exit(diagnose())
    sys.exit(main())


def diagnose() -> int:
    """Fetch the first watched product's page and print raw diagnostics,
    without touching state.json or sending any notification."""
    products = load_products()
    pid, product = next(iter(products.items()))
    print(f"[diagnose] fetching {product['name']}: {product['url']}")
    html = fetch_page(product["url"])
    if html is None:
        print("[diagnose] fetch_page returned None (network error / all retries failed)")
        return 1
    print(f"[diagnose] response length: {len(html)} chars")
    print(f"[diagnose] MIN_VALID_PAGE_LENGTH: {MIN_VALID_PAGE_LENGTH}")
    print(f"[diagnose] contains OUT_OF_STOCK_MARKER: {OUT_OF_STOCK_MARKER in html}")
    for marker in PAGE_SANITY_MARKERS:
        print(f"[diagnose] contains sanity marker {marker!r}: {marker in html}")
    lowered = html.lower()
    for marker in BLOCK_MARKERS:
        if marker in lowered:
            print(f"[diagnose] MATCHED block marker: {marker!r}")
    print("[diagnose] first 500 chars of response:")
    print(html[:500])
    print("[diagnose] ...")
    print("[diagnose] last 300 chars of response:")
    print(html[-300:])
    return 0
