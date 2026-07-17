"""Monitor BookMyShow for tickets at target theatres in a target city.

Sends an email (via Resend) the moment a target theatre opens the
morning first-show for the target date range. Notification is one-shot
per unique (date, theatre, showtime) so you don't get spammed once
tickets open.
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

DRY_RUN = "--dry-run" in sys.argv
SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY", "")
# Cache discovered movie slug+code for this long (seconds) to save credits.
DISCOVERY_TTL_SECONDS = 6 * 3600

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


def http_get(url: str, timeout: int = 60):
    """GET a URL. Routes through ScraperAPI when SCRAPERAPI_KEY is set so
    that BookMyShow's Cloudflare doesn't block GitHub Actions data-center
    IPs. Falls back to direct fetch when running locally (residential IP)."""
    if SCRAPERAPI_KEY:
        from urllib.parse import urlencode
        proxy_url = "http://api.scraperapi.com/?" + urlencode({
            "api_key": SCRAPERAPI_KEY,
            "url": url,
            "country_code": "in",
            "keep_headers": "true",
        })
        return requests.get(proxy_url, headers=BASE_HEADERS, timeout=timeout)
    return requests.get(url, headers=BASE_HEADERS, timeout=timeout)

# BookMyShow uses short region codes in some URLs.
CITY_SHORT_CODES = {
    "chennai": "chen",
    "mumbai": "mumbai",
    "bengaluru": "bang",
    "bangalore": "bang",
    "hyderabad": "hyd",
    "delhi": "ncr",
    "kolkata": "kolk",
    "pune": "pune",
}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"notified": []}


def save_state(state: dict) -> None:
    if DRY_RUN:
        return
    STATE_FILE.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n"
    )


def extract_initial_state(html_text: str):
    """Extract `window.__INITIAL_STATE__ = { ... };` blob with brace matching."""
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html_text)
    if not m:
        return None
    start = m.end()
    depth = 0
    for i in range(start, len(html_text)):
        c = html_text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html_text[start:i + 1])
                except Exception:
                    return None
    return None


def fetch_movie_code(title: str, city: str):
    """Find the BMS slug + event code for `title` in `city`.

    Returns (slug, event_code) or (None, None) if not yet listed.
    """
    urls = [
        f"https://in.bookmyshow.com/explore/movies-{city}",
        f"https://in.bookmyshow.com/explore/upcoming-movies-{city}",
    ]
    title_norm = re.sub(r"[^a-z0-9]", "", title.lower())
    # BookMyShow movie links use the pattern /{city}/movies/{slug}/{code}
    # (e.g. /chennai/movies/the-odyssey/ET00452034)
    pattern = re.compile(
        rf"/{re.escape(city)}/movies/([a-z0-9-]+)/(ET\d+)", re.I
    )
    for url in urls:
        try:
            r = http_get(url, timeout=45)
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


def buytickets_url(slug: str, code: str, city: str, on_date: date) -> str:
    """Movie's per-date showtime listing page (all theatres for that day)."""
    ymd = on_date.strftime("%Y%m%d")
    city_code = CITY_SHORT_CODES.get(city.lower(), city[:4].lower())
    return (
        f"https://in.bookmyshow.com/buytickets/"
        f"{slug}-{city}/movie-{city_code}-{code}-MT/{ymd}"
    )


def seat_layout_url(venue_code: str, event_code: str, session_id: str) -> str:
    """Direct deep-link to the seat-selection page for a specific showtime."""
    return (
        f"https://in.bookmyshow.com/booktickets/"
        f"{venue_code}/{event_code}/{session_id}"
    )


def fetch_showtimes(slug: str, code: str, city: str, on_date: date):
    """Return (page_url, list of show dicts) for the given date."""
    ymd = on_date.strftime("%Y%m%d")
    url = buytickets_url(slug, code, city, on_date)
    try:
        r = http_get(url, timeout=60)
    except requests.RequestException as e:
        print(f"[warn] fetch {url}: {e}", file=sys.stderr)
        return url, []
    if r.status_code != 200:
        return url, []
    state = extract_initial_state(r.text)
    if not state:
        return url, []
    try:
        date_data = state["showtimesByEvent"]["showDates"][ymd]
        widgets = date_data["dynamic"]["data"]["showtimeWidgets"]
    except (KeyError, TypeError):
        return url, []

    shows = []
    for w in widgets:
        if not isinstance(w, dict) or w.get("type") != "groupList":
            continue
        try:
            venue_blocks = w["data"][0]["data"]
        except (KeyError, IndexError, TypeError):
            continue
        for block in venue_blocks:
            if not isinstance(block, dict):
                continue
            add = block.get("additionalData") or {}
            venue_name = add.get("venueName")
            venue_code = add.get("venueCode")
            if not venue_name:
                continue
            for s in (block.get("showtimes") or []):
                sadd = s.get("additionalData") or {}
                cta = s.get("cta") or {}
                cta_analytics = cta.get("analytics") or {}
                meta = cta_analytics.get("metadata") or ""
                seat_hint = ""
                if "fast_filling" in meta:
                    seat_hint = "fast filling"
                elif "sold_out" in meta:
                    seat_hint = "sold out"
                shows.append({
                    "theatre": venue_name,
                    "venue_code": venue_code,
                    "showtime": s.get("title") or sadd.get("showTime") or "",
                    "showtime_code": sadd.get("showTimeCode") or "",
                    "session_id": sadd.get("sessionId") or "",
                    "avail_status": sadd.get("availStatus") or "",
                    "seat_hint": seat_hint,
                })
    return url, shows


def matches_theatre(theatre: str, target: dict) -> bool:
    t = theatre.lower()
    return (
        target["name_contains"].lower() in t
        and target["subname_contains"].lower() in t
    )


def parse_hhmm(show: dict):
    """Return (hour, minute) preferring showTimeCode over display string."""
    code = (show.get("showtime_code") or "").strip()
    if code.isdigit():
        code = code.zfill(4)
        return int(code[:2]), int(code[2:])
    s = (show.get("showtime") or "").strip()
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
    if DRY_RUN:
        print(f"[dry-run] would send email: {subject}")
        print(f"[dry-run] to: {CFG['notify']['email']}")
        return True
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


def build_email_html(title, d, target, show, seat_url=None, date_url=None):
    rows_pref = " > ".join(CFG["tickets"]["row_priority"])
    primary_url = seat_url or date_url or "#"
    fallback_link = ""
    if seat_url and date_url:
        fallback_link = f"""
    <p style="font-size:12px;color:#888;margin:8px 0">
      Backup link (all shows for this date):
      <a href="{html.escape(date_url)}" style="color:#888">{html.escape(date_url)}</a>
    </p>"""
    return f"""
    <h2 style="margin:0 0 8px">{html.escape(title)} — tickets open</h2>
    <p style="font-size:15px;line-height:1.5">
      <b>Theatre:</b> {html.escape(show['theatre'])}<br>
      <b>Date:</b> {d.strftime('%a, %d %b %Y')}<br>
      <b>Show:</b> {html.escape(show['showtime'])} (morning first show)<br>
      <b>Tickets:</b> {CFG['tickets']['count']} together<br>
      <b>Row preference (back rows):</b> {html.escape(rows_pref)}<br>
      <b>Availability hint:</b> {html.escape(show.get('seat_hint') or 'listed')}
    </p>
    <p style="margin:20px 0">
      <a href="{html.escape(primary_url)}"
         style="display:inline-block;padding:14px 22px;background:#e50914;color:#fff;
                text-decoration:none;font-weight:600;border-radius:6px;font-size:16px">
         Open seat selection — pick 5 seats now
      </a>
    </p>
    <p style="color:#666;font-size:12px">
      This link opens the seat-selection page for
      <b>{html.escape(show['theatre'])}</b> — {html.escape(show['showtime'])} on
      {d.strftime('%a %d %b')} directly. Pick 5 seats in row
      <b>{html.escape(CFG['tickets']['row_priority'][0])}</b> first
      (fall back to {' / '.join(CFG['tickets']['row_priority'][1:])}).
      Pay with UPI or OTP. Seats stay locked for ~8 minutes.
    </p>{fallback_link}
    """


def get_movie_code_cached(state: dict, title: str, city: str):
    """Return (slug, code) using state cache when fresh; otherwise re-discover."""
    now = int(time.time())
    cache = state.get("discovery") or {}
    if (cache.get("title") == title and cache.get("city") == city
            and cache.get("slug") and cache.get("code")
            and now - int(cache.get("at", 0)) < DISCOVERY_TTL_SECONDS):
        print(f"[cache] using cached movie code for '{title}' "
              f"(age {now - int(cache['at'])}s)")
        return cache["slug"], cache["code"]
    slug, code = fetch_movie_code(title, city)
    if slug and code:
        state["discovery"] = {
            "title": title, "city": city,
            "slug": slug, "code": code, "at": now,
        }
    return slug, code


def check_once(state: dict) -> bool:
    movie = CFG["movie"]
    city = movie["city"]
    slug, code = get_movie_code_cached(state, movie["title"], city)
    if not slug or not code:
        print(f"[info] '{movie['title']}' not yet listed in {city}.")
        return False
    print(f"[found] {movie['title']} -> slug={slug} code={code}")

    time_cutoff = CFG["showtime"].get("time_before", "12:00")
    cutoff_h, cutoff_m = map(int, time_cutoff.split(":"))
    sent_any = False

    today = date.today()
    for d in date_range(CFG["date_range"]["start"], CFG["date_range"]["end"]):
        if d < today:
            continue
        url, shows = fetch_showtimes(slug, code, city, d)
        if not shows:
            continue
        print(f"[data] {d.isoformat()}: {len(shows)} shows across "
              f"{len({s['theatre'] for s in shows})} venues")
        for target in CFG["target_theatres"]:
            matched = [s for s in shows if matches_theatre(s["theatre"], target)]
            if not matched:
                continue
            morning = []
            for s in matched:
                hm = parse_hhmm(s)
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

            # Build the direct seat-selection deep-link when we have both
            # a venue_code and session_id. This drops the user straight
            # onto the seat picker for the exact theatre + showtime.
            seat_url = None
            if first_show.get("venue_code") and first_show.get("session_id"):
                seat_url = seat_layout_url(
                    first_show["venue_code"], code, first_show["session_id"]
                )

            subj = (
                f"[TICKET ALERT] {movie['title']} - "
                f"{target['name_contains']} {target['subname_contains']} - "
                f"{d.strftime('%a %d %b')} {first_show['showtime']}"
            )
            body = build_email_html(
                movie["title"], d, target, first_show,
                seat_url=seat_url, date_url=url,
            )
            if send_email(subj, body):
                state["notified"].append(key)
                sent_any = True
            break
    return sent_any


def main():
    state = load_state()
    if DRY_RUN:
        n_runs, sleep_s = 1, 0
    else:
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
