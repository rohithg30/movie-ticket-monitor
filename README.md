# Jana Nayagan ticket monitor

Watches BookMyShow every ~1 minute for the morning first-show of
**Jana Nayagan** at three Chennai theatres (in priority):

1. PVR Grand Mall, Velachery
2. INOX LUXE Phoenix Marketcity, Velachery
3. PVR Aerohub, Chennai

The instant a theatre appears with a show before 12:00, an email is sent to
`rohithg302002@gmail.com` with a big red button that opens the BookMyShow
seat-selection page on your phone.

**It does not book tickets automatically.** OTP/UPI PIN cannot be bypassed
by any bot. You tap the button, pick 5 seats (row A first, then B / C / D),
pay with UPI. Total time end-to-end after the email: ~30-45 seconds.

## Files

- `monitor.py` — the scraper + notifier
- `config.yml` — movie, theatres, dates, email, polling settings
- `requirements.txt` — Python deps
- `.github/workflows/monitor.yml` — GitHub Actions cron (every 5 min, 4 inner
  polls at 60s each → effective ~1 min interval)
- `state.json` — remembers what has already been notified (auto-committed
  by the workflow after each run)

## One-time setup

1. Sign up at https://resend.com (Google login).
2. Resend dashboard → **API Keys → Create API Key** → copy it.
3. Resend dashboard → **Emails → Sending domains** is NOT needed. Instead,
   go to your Resend **profile** and confirm `rohithg302002@gmail.com` is
   verified for the `onboarding@resend.dev` sender. If Resend asks for
   verification, click the link they email you.
4. Create a new **public** GitHub repo named `movie-ticket-monitor`.
5. Push this folder (see commands below).
6. GitHub repo → **Settings → Secrets and variables → Actions → New
   repository secret**:
   - Name: `RESEND_API_KEY`
   - Value: (paste the Resend API key)
7. GitHub → **Actions** tab → click `movie-ticket-monitor` → **Run workflow**
   to run once manually and verify.

## How the automation runs

Once pushed with the secret set, the workflow runs itself every 5 minutes.
You do nothing. When the movie shows up at a target theatre with a morning
show, you get an email within ~1 minute.

## If you change `config.yml` (e.g. new dates / theatres)

Just push the change to GitHub. The next scheduled run picks up the new
config automatically.

```bash
cd /Users/rohithg/cursor/movie-ticket-monitor
# edit config.yml as needed
git add config.yml
git commit -m "update config"
git push
```

You can also trigger a run immediately after: **GitHub → Actions →
movie-ticket-monitor → Run workflow**.

## To disarm after 10 days

Easiest:

```bash
cd /Users/rohithg/cursor/movie-ticket-monitor
git rm .github/workflows/monitor.yml
git commit -m "disarm monitor"
git push
```

Or just delete the repo on GitHub.

## Debugging locally

```bash
cd /Users/rohithg/cursor/movie-ticket-monitor
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export RESEND_API_KEY=re_xxx_your_key_here
python monitor.py
```

Watch the console output. `[info] '...' not yet listed` = fine, movie page
not yet published by BookMyShow. `[found] ... -> slug=...` = movie page
exists, will check showtimes. `[ok] email sent: ...` = alert dispatched.
