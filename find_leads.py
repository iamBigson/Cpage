#!/usr/bin/env python3
"""
Lead finder: searches for `<keyword> in <state>`, finds domains never seen
before (tracked in seen_domains.json), tries to locate each site's contact
page, and sends results to Telegram. Always finds up to 15 new domains
per run (or exhausts a 20-page search cap trying).

Required environment variables (set as GitHub repo secrets):
  SERPER_API_KEY
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

Usage:
  python find_leads.py "luxury private jet charter company" "Texas"
"""

import os
import sys
import json
import re
import time
import requests
from urllib.parse import urlparse, urljoin

SEEN_FILE = "seen_domains.json"
TARGET_NEW = 15
MAX_PAGES = 20
RESULTS_PER_PAGE = 10
REQUEST_TIMEOUT = 10

CONTACT_PATTERNS = [
    "contact", "contact-us", "contactus", "get-in-touch", "getintouch",
    "book", "booking", "inquire", "inquiry", "enquiry", "request-quote",
    "quote", "reach-us", "reach-out", "connect"
]


def get_env(name):
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: missing required environment variable {name}", file=sys.stderr)
        sys.exit(1)
    return val


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_set):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen_set), f, indent=2)


def normalize_domain(url):
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return None


def serper_search(api_key, query, page):
    """Fetch one page (10 results) from Serper. page=1 is first page."""
    resp = requests.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query, "page": page},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("organic", [])


def find_contact_page(homepage_url):
    """
    Fetch homepage HTML, scan all <a> tags (nav + footer + anywhere)
    for contact-like links. Falls back to homepage_url if none found.
    Returns (contact_url, found_bool).
    """
    try:
        resp = requests.get(
            homepage_url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LeadFinderBot/1.0)"},
        )
        resp.raise_for_status()
        html = resp.text
    except Exception:
        return homepage_url, False

    # crude but dependency-free link scan: find href="..." plus nearby text
    links = re.findall(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL)

    for href, text in links:
        clean_text = re.sub(r"<[^>]+>", "", text).strip().lower()
        href_lower = href.lower()
        for pattern in CONTACT_PATTERNS:
            if pattern in href_lower or pattern in clean_text:
                full_url = urljoin(homepage_url, href)
                # avoid mailto/tel links here, we want a page
                if full_url.startswith("http"):
                    return full_url, True

    return homepage_url, False


def send_telegram(bot_token, chat_id, message, parse_mode=None):
    payload = {"chat_id": chat_id, "text": message, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        print(f"WARNING: Telegram send failed: {resp.text}", file=sys.stderr)


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main():
    if len(sys.argv) < 3:
        print('Usage: python find_leads.py "<keyword>" "<state>"', file=sys.stderr)
        sys.exit(1)

    keyword = sys.argv[1]
    state = sys.argv[2]
    query = f"{keyword} in {state}"

    serper_key = get_env("SERPER_API_KEY")
    tg_token = get_env("TELEGRAM_BOT_TOKEN")
    tg_chat = get_env("TELEGRAM_CHAT_ID")

    seen = load_seen()
    new_leads = []  # list of (domain, contact_url, found_bool)
    seen_this_session = set()

    print(f"Query: {query}")
    print(f"Already-seen domains on file: {len(seen)}")

    page = 1
    while len(new_leads) < TARGET_NEW and page <= MAX_PAGES:
        try:
            results = serper_search(serper_key, query, page)
        except Exception as e:
            print(f"Search error on page {page}: {e}", file=sys.stderr)
            break

        if not results:
            print(f"No more results at page {page}, stopping search.")
            break

        for r in results:
            link = r.get("link")
            if not link:
                continue
            domain = normalize_domain(link)
            if not domain:
                continue
            if domain in seen or domain in seen_this_session:
                continue

            seen_this_session.add(domain)
            homepage = f"https://{domain}"
            contact_url, found = find_contact_page(homepage)
            new_leads.append((domain, contact_url, found))
            print(f"  + new domain: {domain} (contact page found: {found})")

            if len(new_leads) >= TARGET_NEW:
                break

        page += 1
        time.sleep(0.5)  # be polite between search pages

    # Update dedup file with everything found this session (even if we stopped early)
    seen.update(seen_this_session)
    save_seen(seen)

    if not new_leads:
        send_telegram(
            tg_token, tg_chat,
            f"🔍 <b>{escape_html(query)}</b>\n\nNo new domains found — all results already seen.",
            parse_mode="HTML",
        )
        print("No new leads found.")
        return

    header = (
        f"🎯 <b>{escape_html(query)}</b>\n"
        f"✅ {len(new_leads)} new lead(s) found\n"
        f"{'─' * 24}\n"
    )

    blocks = []
    for i, (domain, contact_url, found) in enumerate(new_leads, start=1):
        c_page_label = "C page" if found else "C page (not found, homepage below)"
        block = (
            f"<b>{i}. {escape_html(domain)}</b>\n"
            f"🌐 Domain: {escape_html(domain)}\n"
            f"📩 {c_page_label}: {escape_html(contact_url)}"
        )
        blocks.append(block)

    message = header + "\n\n".join(blocks)

    # Telegram has a 4096 char limit per message; chunk if needed
    if len(message) <= 4000:
        send_telegram(tg_token, tg_chat, message, parse_mode="HTML")
    else:
        send_telegram(tg_token, tg_chat, header, parse_mode="HTML")
        chunk = ""
        for block in blocks:
            if len(chunk) + len(block) > 3800:
                send_telegram(tg_token, tg_chat, chunk, parse_mode="HTML")
                chunk = ""
            chunk += block + "\n\n"
        if chunk:
            send_telegram(tg_token, tg_chat, chunk, parse_mode="HTML")

    if len(new_leads) < TARGET_NEW:
        send_telegram(
            tg_token, tg_chat,
            f"⚠️ Only found {len(new_leads)}/{TARGET_NEW} new domains before hitting the search cap ({MAX_PAGES} pages).",
        )

    print(f"Done. Sent {len(new_leads)} leads to Telegram.")


if __name__ == "__main__":
    main()
