"""Monitor BookMyShow for Jana Nayagan tickets at target Chennai theatres.

Sends an email (via Resend) the moment a target theatre opens the morning
first-show for the target date range. Notification is one-shot per unique
(date, theatre, showtime) so you don't get spammed once tickets open.
"""
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
CFG = yaml.safe_load((ROOT / "config.yml").read_text())
STATE_FILE = ROOT / "state.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Cache-Control": "no-cache",
    "sec-ch-ua": '"Chromium";v="126", "Not:A-Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Upgrade-Insecure-Requests": "1",
}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"notified": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def fetch_movie_code(title: str, city: str):
    """Search BookMyShow city listings for `title`. Return (slug, event_code)."""
    urls = [
        f"https://in.bookmyshow.com/explore/movies-{city}",
        f"https://in.bookmyshow.com/explore/upcoming-movies-{city}",
    ]
    title_norm = re.sub(r"[^a-z0-9]", "", title.lower())
    pattern = re.compile(
        rf"/movies/{re.escape(city)}/([a-z0-9-]+)/(ET\d+)", re.I
    )
    for url in urls:
        try:
            r = requests.get(url, headers=BASE_HEADERS, timeout=30)
        except requests.RequestException as e:
            print(f"[warn] fetch {url}: {e}", file=sys.stderr)
            continue
        if r.status_code != 200:
            print(f"[warn] {url} -> HTTP {r.status_code}", file=sys.stderr)
            continue
        for m in pattern.finditer(r.text):
            slug, code = m.group(1), m.group(2)
            slug_norm = re.sub(r"[^a-z0-9]", "", slug.lower())
            if title_norm in slug_norm or slug_norm in title_norm:
                return slug, code
    return None, None


def fetch_showtimes(slug: str, code: str, city: str, on_date: date):
    """Return (page_url, list of show dicts)."""
    ymd = on_date.strftime("%Y%m%d")
    url = (
        f"https://in.bookmyshow.com/movies/{city}/{slug}/buytickets/{code}/{ymd}"
    )
    try:
        r = requests.get(url, headers=BASE_HEADERS, timeout=30)
    except requests.RequestException as e:
        print(f"[warn] fetch {url}: {e}", file=sys.stderr)
        return url, []
    if r.status_code != 200:
        return url, []
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        r.text, re.S,
    )
    if not m:
        return url, []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return url, []

    shows = []
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            venue = (
                node.get("venueName")
                or node.get("VenueName")
                or node.get("VenueTitle")
                or node.get("displayName")
            )
            showtime = (
                node.get("showTime")
                or node.get("ShowTime")
                or node.get("startTime")
                or node.get("EventTime")
            )
            if venue and showtime:
                shows.append({
                    "theatre": str(venue),
                    "showtime": str(showtime),
                    "seat_hint": str(
                        node.get("availability")
                        or node.get("Availability") or ""
                    ),
                })
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return url, shows


def matches_theatre(theatre: str, target: dict) -> bool:
    t = theatre.lower()
    return (
        target["name_contains"].lower() in t
        and target["subname_contains"].lower() in t
    )


def parse_hhmm(s: str):
    """Parse a show time string. Return (hour, minute) or None."""
    s = s.strip()
    m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", s, re.I)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap:
        ap = ap.upper()
        if ap == "PM" and h != 12:
            h += 12
        elif ap == "AM" and h == 12:
            h = 0
    return h, mi


def send_email(subject: str, html_body: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        print("[error] RESEND_API_KEY not set", file=sys.stderr)
        return False
    payload = {
        "from": CFG["notify"]["from"],
        "to": [CFG["notify"]["email"]],
        "subject": subject,
        "html": html_body,
    }
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload, timeout=30,
        )
    except requests.RequestException as e:
        print(f"[error] resend request: {e}", file=sys.stderr)
        return False
    if r.status_code >= 300:
        print(f"[error] Resend {r.status_code}: {r.text}", file=sys.stderr)
        return False
    print(f"[ok] email sent: {subject}")
    return True


def date_range(start_s: str, end_s: str):
    s = datetime.strptime(start_s, "%Y-%m-%d").date()
    e = datetime.strptime(end_s, "%Y-%m-%d").date()
    d = s
    while d <= e:
        yield d
        d += timedelta(days=1)


def build_email_html(title, d, target, show, url):
    rows_pref = " > ".join(CFG["tickets"]["row_priority"])
    return f"""
    <h2 style="margin:0 0 8px">{html.escape(title)} — tickets open</h2>
    <p style="font-size:15px;line-height:1.5">
      <b>Theatre:</b> {html.escape(target['name_contains'])} {html.escape(target['subname_contains'])}<br>
      <b>Date:</b> {d.strftime('%a, %d %b %Y')}<br>
      <b>Show:</b> {html.escape(show['showtime'])} (morning first show)<br>
      <b>Tickets:</b> {CFG['tickets']['count']} together<br>
      <b>Row preference (back rows):</b> {html.escape(rows_pref)}<br>
      <b>Availability hint:</b> {html.escape(show.get('seat_hint') or 'listed')}
    </p>
    <p style="margin:20px 0">
      <a href="{html.escape(url)}"
         style="display:inline-block;padding:14px 22px;background:#e50914;color:#fff;
                text-decoration:none;font-weight:600;border-radius:6px;font-size:16px">
         Open BookMyShow — pick seats
      </a>
    </p>
    <p style="color:#666;font-size:12px">
      Tap the button on your phone. You land on the showtimes page for that
      date. Tap this theatre and the {html.escape(show['showtime'])} show.
      Pick 5 seats in row {html.escape(CFG['tickets']['row_priority'][0])}
      first (fall back to {' / '.join(CFG['tickets']['row_priority'][1:])}).
      Pay with UPI or OTP. Seats stay locked for ~8 minutes.
    </p>
    """


def check_once(state: dict) -> bool:
    movie = CFG["movie"]
    city = movie["city"]
    slug, code = fetch_movie_code(movie["title"], city)
    if not slug or not code:
        print(f"[info] '{movie['title']}' not yet listed in {city}.")
        return False
    print(f"[found] {movie['title']} -> slug={slug} code={code}")

    time_cutoff = CFG["showtime"].get("time_before", "12:00")
    cutoff_h, cutoff_m = map(int, time_cutoff.split(":"))
    sent_any = False

    for d in date_range(CFG["date_range"]["start"], CFG["date_range"]["end"]):
        url, shows = fetch_showtimes(slug, code, city, d)
        if not shows:
            continue
        for target in CFG["target_theatres"]:
            matched = [s for s in shows if matches_theatre(s["theatre"], target)]
            if not matched:
                continue
            morning = []
            for s in matched:
                hm = parse_hhmm(s["showtime"])
                if hm and (hm[0], hm[1]) < (cutoff_h, cutoff_m):
                    morning.append((hm, s))
            if not morning:
                continue
            morning.sort(key=lambda x: x[0])
            first_show = morning[0][1]

            key = (
                f"{d.isoformat()}|{target['name_contains']}|"
                f"{target['subname_contains']}|{first_show['showtime']}"
            )
            if key in state["notified"]:
                break
            state["notified"].append(key)
            subj = (
                f"[TICKET ALERT] {movie['title']} - "
                f"{target['name_contains']} {target['subname_contains']} - "
                f"{d.strftime('%a %d %b')} {first_show['showtime']}"
            )
            body = build_email_html(movie["title"], d, target, first_show, url)
            if send_email(subj, body):
                sent_any = True
            break
    return sent_any


def main():
    state = load_state()
    n_runs = CFG.get("polling", {}).get("checks_per_run", 1)
    sleep_s = CFG.get("polling", {}).get("sleep_seconds", 60)
    for i in range(n_runs):
        try:
            check_once(state)
        except Exception as e:
            print(f"[warn] check_once: {e}", file=sys.stderr)
        save_state(state)
        if i < n_runs - 1:
            time.sleep(sleep_s)


if __name__ == "__main__":
    main()
