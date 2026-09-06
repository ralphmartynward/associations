#!/usr/bin/env python3
"""Toulouse Agenda — daily event-page scraper with email digest."""

import argparse
import html
import json
import logging
import os
import smtplib
import sys
import time
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

EVENTS_SEEN_PATH = "events_seen.json"
VENUES_PATH = "venues.json"
REQUEST_DELAY = 2  # seconds between venue requests
USER_AGENT = "Mozilla/5.0 (compatible; ToulouseAgenda/1.0; +https://github.com/ralphmartynward/toulouse-agenda)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Per-venue parsers — add new ones here, then reference by name in venues.json.
# Each parser returns a list of {"title": str, "url": str, "start": "YYYY-MM-DD" or ""}.
# ---------------------------------------------------------------------------

def parse_artilect(venue: dict) -> list[dict]:
    """Artilect Fablab — Odoo Events module, schema.org microdata.
    Each posting is <article itemtype="http://schema.org/Event"> wrapped in
    an <a href="/event/.../register">, with [itemprop=name] and
    [itemprop=startDate content=ISO-datetime]."""
    r = _get(venue["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    events = []
    seen: set[str] = set()
    for art in soup.find_all("article", attrs={"itemtype": "http://schema.org/Event"}):
        name_el = art.find(attrs={"itemprop": "name"})
        link = art.find_parent("a", href=True)
        if not name_el or not link:
            continue
        title = name_el.get_text(strip=True)
        if not title:
            continue
        url = urljoin(venue["url"], link["href"])
        start_el = art.find(attrs={"itemprop": "startDate"})
        start = (start_el.get("content", "") if start_el else "")[:10]
        if url not in seen:
            seen.add(url)
            events.append({"title": title, "url": url, "start": start})
    return events


def parse_leshallesdelatransition(venue: dict) -> list[dict]:
    """Les Halles de la Transition — Squarespace events collection.
    Each posting is <article class="eventlist-event"> with
    <a class="eventlist-title-link"> and <time class="event-date" datetime="YYYY-MM-DD">."""
    r = _get(venue["url"])
    soup = BeautifulSoup(r.text, "html.parser")
    events = []
    seen: set[str] = set()
    for art in soup.find_all("article", class_="eventlist-event"):
        a = art.find("a", class_="eventlist-title-link", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        url = urljoin(venue["url"], a["href"])
        time_tag = art.find("time", class_="event-date")
        start = time_tag["datetime"] if time_tag and time_tag.has_attr("datetime") else ""
        if url not in seen:
            seen.add(url)
            events.append({"title": title, "url": url, "start": start})
    return events


def parse_lamelee_events(venue: dict) -> list[dict]:
    """La Cantine Toulouse — its agenda runs on La Mêlée's public WP REST API
    (wp/v2/event_listing custom post type), covering all La Mêlée events
    network-wide. Filtered to company["config"]["city"] via the _ville meta
    field since La Mêlée also runs events in Montpellier etc."""
    city_filter = venue.get("config", {}).get("city", "").lower()
    r = _get(f"{venue['url']}?per_page=100")
    events = []
    seen: set[str] = set()
    for item in r.json():
        meta = item.get("meta", {})
        ville = (meta.get("_ville") or [""])[0]
        if city_filter and city_filter not in ville.lower():
            continue
        title = html.unescape(item.get("title", {}).get("rendered", ""))
        url = item.get("link", "")
        if not title or not url:
            continue
        start = (meta.get("_event_start_date") or [""])[0]
        if url not in seen:
            seen.add(url)
            events.append({"title": title, "url": url, "start": start})
    return events


PARSERS: dict = {
    "artilect": parse_artilect,
    "leshallesdelatransition": parse_leshallesdelatransition,
    "lamelee_events": parse_lamelee_events,
}


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get(url: str) -> requests.Response:
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def scrape_all(venues: list[dict]) -> dict[str, list[dict]]:
    results: dict[str, list[dict]] = {}
    for i, venue in enumerate(venues):
        name = venue["name"]
        parser_name = venue.get("parser", "")
        parser = PARSERS.get(parser_name)
        if not parser:
            log.warning("No parser '%s' for %s — skipping", parser_name, name)
            results[name] = []
            continue
        if i > 0:
            time.sleep(REQUEST_DELAY)
        try:
            log.info("Scraping %s ...", name)
            events = parser(venue)
            results[name] = events
            log.info("  -> %d event(s) found", len(events))
        except Exception as exc:
            log.error("Error scraping %s: %s", name, exc)
            results[name] = []
    return results


def find_new_events(results: dict, events_seen: dict) -> dict[str, list[dict]]:
    """New events, excluding any whose start date has already passed."""
    today_str = date.today().isoformat()
    seen_urls = {venue: set(urls) for venue, urls in events_seen.items()}
    new: dict[str, list[dict]] = {}
    for venue_name, events in results.items():
        fresh = [
            e for e in events
            if e["url"] not in seen_urls.get(venue_name, set())
            and (not e["start"] or e["start"] >= today_str)
        ]
        if fresh:
            new[venue_name] = fresh
    return new


def update_seen(events_seen: dict, results: dict) -> dict:
    for venue_name, events in results.items():
        events_seen[venue_name] = [e["url"] for e in events]
    return events_seen


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_html(new_events: dict) -> str:
    today = date.today().strftime("%d %B %Y")
    rows = []
    for venue_name, events in new_events.items():
        rows.append(f"<h2 style='color:#2c3e50'>{venue_name}</h2><ul>")
        for e in sorted(events, key=lambda x: x["start"] or "9999"):
            date_prefix = f"{e['start']} — " if e["start"] else ""
            rows.append(f'  <li>{date_prefix}<a href="{e["url"]}" style="color:#2980b9">{e["title"]}</a></li>')
        rows.append("</ul>")
    body = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;max-width:640px;margin:auto;padding:24px">
<h1 style="color:#1a1a2e">New agenda items — {today}</h1>
{body}
<hr style="margin-top:32px">
<p style="color:#888;font-size:12px">Sent by Toulouse Agenda</p>
</body></html>"""


def send_email(subject: str, html_body: str) -> None:
    sender = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ["RECIPIENT_EMAIL"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())
    log.info("Email sent -> %s", recipient)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Toulouse Agenda — venue events monitor")
    parser.add_argument("--test", action="store_true",
                        help="Dry run: print findings without sending email or updating events_seen.json")
    args = parser.parse_args()

    venues = load_json(VENUES_PATH, [])
    if not venues:
        log.error("No venues found in %s", VENUES_PATH)
        return 1

    events_seen = load_json(EVENTS_SEEN_PATH, {})
    results = scrape_all(venues)
    new_events = find_new_events(results, events_seen)

    if new_events:
        total = sum(len(v) for v in new_events.values())
        log.info("New events found: %d across %d venue(s)", total, len(new_events))
        for venue_name, events in new_events.items():
            for e in events:
                print(f"[NEW]  {venue_name:28s}  {e['start']:10s}  {e['title']}")
                print(f"       {e['url']}")
    else:
        log.info("No new events today.")

    if args.test:
        log.info("[TEST MODE] Skipping email and events_seen.json update.")
        return 0

    if new_events:
        today_str = date.today().strftime("%Y-%m-%d")
        send_email(f"New agenda items — {today_str}", build_html(new_events))

    events_seen = update_seen(events_seen, results)
    save_json(EVENTS_SEEN_PATH, events_seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
