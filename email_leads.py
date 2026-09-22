#!/usr/bin/env python3
"""
Lead finder: searches for `<keyword> in <state>`, finds domains never seen
before (tracked in seen_domains.json), filters to only domains hosted on
Microsoft/Office 365 (Outlook) mail (skips Google Workspace, any other
host, or undetermined), locates each site's contact page, scrapes any
visible email address + basic company info from that contact page, and
sends results to Telegram. Always finds up to 20 new qualifying domains
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
import dns.resolver
from urllib.parse import urlparse, urljoin

SEEN_FILE = "seen_domains.json"
TARGET_NEW = 20
MAX_PAGES = 20
RESULTS_PER_PAGE = 10
REQUEST_TIMEOUT = 10
DNS_TIMEOUT = 5

CONTACT_PATTERNS = [
    "contact", "contact-us", "contactus", "get-in-touch", "getintouch",
    "book", "booking", "inquire", "inquiry", "enquiry", "request-quote",
    "quote", "reach-us", "reach-out", "connect"
]

MICROSOFT_MX_MARKERS = ["outlook.com", "protection.outlook.com", "mail.protection.outlook.com"]
GOOGLE_MX_MARKERS = ["google.com", "googlemail.com", "aspmx.l.google.com"]

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Generic/placeholder addresses we don't want cluttering results
EMAIL_JUNK_PATTERNS = [
    "example.com", "yourdomain", "sentry.io", "wixpress.com", "godaddy.com",
    ".png", ".jpg", ".gif", ".svg", "@2x", "schema.org"
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


def is_office365(domain):
    """
    Look up MX records for the domain. Returns True only if the mail
    hosting is confirmed Microsoft/Office 365 (Outlook). Google Workspace,
    any other provider, or a failed/empty lookup all return False.
    """
    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = DNS_TIMEOUT
        resolver.lifetime = DNS_TIMEOUT
        answers = resolver.resolve(domain, "MX")
        exchanges = [str(r.exchange).rstrip(".").lower() for r in answers]
    except Exception:
        return False

    for exchange in exchanges:
        if any(marker in exchange for marker in MICROSOFT_MX_MARKERS):
            return True
    return False


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


def fetch_html(url):
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LeadFinderBot/1.0)"},
        )
        resp.raise_for_status()
        return resp.text
    except Exception:
        return None


def find_contact_page(homepage_url, homepage_html):
    """
    Scan all <a> tags (nav + footer + anywhere) in already-fetched homepage
    HTML for contact-like links. Falls back to homepage_url if none found.
    Returns (contact_url, found_bool).
    """
    if not homepage_html:
        return homepage_url, False

    links = re.findall(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', homepage_html, re.IGNORECASE | re.DOTALL)

    for href, text in links:
        clean_text = re.sub(r"<[^>]+>", "", text).strip().lower()
        href_lower = href.lower()
        for pattern in CONTACT_PATTERNS:
            if pattern in href_lower or pattern in clean_text:
                full_url = urljoin(homepage_url, href)
                if full_url.startswith("http"):
                    return full_url, True

    return homepage_url, False


def extract_emails(html):
    """Pull mailto: links and plain-text emails from HTML, cleaned of junk/asset false-positives."""
    if not html:
        return []

    found = set()

    for m in re.findall(r'mailto:([^"\'?\s]+)', html, re.IGNORECASE):
        found.add(m.strip().lower())

    for m in EMAIL_REGEX.findall(html):
        found.add(m.strip().lower())

    cleaned = [
        e for e in found
        if not any(junk in e for junk in EMAIL_JUNK_PATTERNS)
    ]
    return sorted(cleaned)


def extract_page_info(html):
    """Grab page <title> and meta description, if present, as lightweight company info."""
    title = ""
    description = ""

    if html:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if title_match:
            title = re.sub(r"\s+", " ", title_match.group(1)).strip()

        desc_match = re.search(
            r'<meta\s+[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)["\']',
            html, re.IGNORECASE
        )
        if not desc_match:
            desc_match = re.search(
                r'<meta\s+[^>]*content=["\']([^"\']*)["\'][^>]*name=["\']description["\']',
                html, re.IGNORECASE
            )
        if desc_match:
            description = re.sub(r"\s+", " ", desc_match.group(1)).strip()

    return title, description


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
    new_leads = []  # list of dicts: domain, contact_url, found, emails, title, description
    seen_this_session = set()
    skipped_not_office = 0

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

            if not is_office365(domain):
                skipped_not_office += 1
                print(f"  - skipped (not Office 365): {domain}")
                continue

            homepage_url = f"https://{domain}"
            homepage_html = fetch_html(homepage_url)
            contact_url, found = find_contact_page(homepage_url, homepage_html)

            if found and contact_url != homepage_url:
                contact_html = fetch_html(contact_url)
            else:
                contact_html = homepage_html

            emails = extract_emails(contact_html)
            title, description = extract_page_info(contact_html or homepage_html)

            new_leads.append({
                "domain": domain,
                "contact_url": contact_url,
                "found": found,
                "emails": emails,
                "title": title,
                "description": description,
            })
            print(f"  + new Office 365 domain: {domain} (contact page found: {found}, emails: {len(emails)})")

            if len(new_leads) >= TARGET_NEW:
                break

        page += 1
        time.sleep(0.5)

    seen.update(seen_this_session)
    save_seen(seen)

    print(f"Skipped (not Office 365 or undetermined): {skipped_not_office}")

    if not new_leads:
        send_telegram(
            tg_token, tg_chat,
            f"🔍 <b>{escape_html(query)}</b>\n\nNo new Office 365 domains found "
            f"({skipped_not_office} checked and skipped — not Office 365 or undetermined).",
            parse_mode="HTML",
        )
        print("No new leads found.")
        return

    header = (
        f"🎯 <b>{escape_html(query)}</b>\n"
        f"✅ {len(new_leads)} new lead(s) found (Office 365 only)\n"
        f"{'─' * 24}\n"
    )

    blocks = []
    for i, lead in enumerate(new_leads, start=1):
        domain = lead["domain"]
        contact_url = lead["contact_url"]
        found = lead["found"]
        emails = lead["emails"]
        title = lead["title"]

        c_page_label = "C page" if found else "C page (not found, homepage below)"
        company_label = escape_html(title) if title else escape_html(domain)

        if emails:
            email_line = "📧 Email: " + ", ".join(escape_html(e) for e in emails)
        else:
            email_line = "📧 Email: none found"

        block = (
            f"<b>{i}. {company_label}</b>\n"
            f"🌐 Domain: {escape_html(domain)}\n"
            f"📩 {c_page_label}: {escape_html(contact_url)}\n"
            f"{email_line}"
        )
        blocks.append(block)

    message = header + "\n\n".join(blocks)

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
            f"⚠️ Only found {len(new_leads)}/{TARGET_NEW} new Office 365 domains before hitting the search cap ({MAX_PAGES} pages).",
        )

    print(f"Done. Sent {len(new_leads)} leads to Telegram.")


if __name__ == "__main__":
    main()
