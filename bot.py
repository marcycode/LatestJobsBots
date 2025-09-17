import json, os, re, time, sys
from pathlib import Path
import requests
import yaml
import argparse

REPO_ROOT = Path(__file__).parent
SEEN_FILE = REPO_ROOT / "seen.json"
COMPANIES_FILE = REPO_ROOT / "companies.yml"
FILTERS_FILE = REPO_ROOT / "filters.yml"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Optional Twilio (SMS/WhatsApp)
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_FROM")
TWILIO_TO = os.getenv("TWILIO_TO")

TIMEOUT = 25
HEADERS = {"User-Agent": "jobs-alert-bot/1.1"}

US_HINTS = ("United States", "US", "USA", "U.S.", "Remote - US", "Remote, United States")

def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {"ids": []}

def save_seen(seen_ids):
    SEEN_FILE.write_text(json.dumps({"ids": sorted(seen_ids)}, indent=2), encoding="utf-8")

def has_notifier():
    return bool((TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) or (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and TWILIO_TO))

def telegram_send(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()

def twilio_send(text):
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and TWILIO_TO):
        return
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    data = {"From": TWILIO_FROM, "To": TWILIO_TO, "Body": text}
    # retry/backoff for 429
    for attempt in range(4):
        r = requests.post(url, data=data, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=TIMEOUT)
        if r.status_code == 429:
            retry_after = 2 + attempt * 2
            time.sleep(min(10, retry_after))
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Twilio: Too many requests after retries")

def notify(text):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        telegram_send(text)
    elif TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and TWILIO_TO:
        twilio_send(text)
    else:
        print(text)

def is_us(locations):
    if not locations:
        return False
    lower = [l.lower() for l in locations if isinstance(l, str)]
    return any(any(h.lower() in l for h in US_HINTS) for l in lower)

def fetch_greenhouse(company):
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs"
    r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("jobs", []):
        job_id = f"greenhouse:{company}:{j.get('id')}"
        title = j.get("title", "")
        job_url = j.get("absolute_url")
        loc_name = j.get("location", {}).get("name") if isinstance(j.get("location"), dict) else None
        locations = [loc_name] if loc_name else []
        if is_us(locations):
            jobs.append({"id": job_id, "source": "greenhouse", "company": company, "title": title, "url": job_url, "locations": locations})
    return jobs

def fetch_lever(company):
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data:
        job_id = f"lever:{company}:{j.get('id')}"
        title = j.get("text", "")
        job_url = j.get("hostedUrl") or j.get("applyUrl")
        locs = j.get("categories", {}).get("location")
        locations = [locs] if isinstance(locs, str) else []
        if is_us(locations):
            jobs.append({"id": job_id, "source": "lever", "company": company, "title": title, "url": job_url, "locations": locations})
    return jobs

def fetch_amazon(max_pages=3):
    base = "https://www.amazon.jobs/en/search.json"
    params = {
        "category": "software-development",
        "result_limit": 50,
        "sort": "recent",
        "job_type": "Full-Time",
        "normalized_country_code": "",
        "radius": "24km",
        "query": "software engineer"
    }
    jobs, offset = [], 0
    for _ in range(max_pages):
        p = dict(params)
        p["offset"] = offset
        r = requests.get(base, params=p, timeout=TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        for j in data.get("jobs", []):
            title = j.get("title", "")
            job_path = j.get(" job_path") or j.get("job_path") or ""
            url = f"https://www.amazon.jobs{job_path}" if job_path else "https://www.amazon.jobs"
            loc_city = j.get("city", "")
            loc_country = j.get("country_code", "")
            locations = [", ".join([loc_city, loc_country]).strip(", ")]
            if not is_us(locations):
                continue
            job_id = f"amazon:{j.get('id') or j.get('job_id') or job_path}"
            jobs.append({"id": job_id, "source": "amazon", "company": "amazon", "title": title, "url": url, "locations": locations})
        if not data.get("jobs"):
            break
        offset += params["result_limit"]
        time.sleep(0.6)
    return jobs

def title_matches(title, include_any, exclude_any, must_all=None):
    t = title.lower()
    for p in exclude_any or []:
        if re.search(p, t, flags=re.I):
            return False
    if must_all:
        for p in must_all:
            if not re.search(p, t, flags=re.I):
                return False
    if include_any:
        return any(re.search(p, t, flags=re.I) for p in include_any)
    return True

def format_msg(job):
    company = job["company"].title()
    title = job["title"]
    locations = ", ".join(job["locations"]) if job["locations"] else "Location: N/A"
    url = job["url"]
    return f"{title} — {company}\n{locations}\n{url}"

def send_heartbeat():
    notify("✅ Jobs bot heartbeat: runner OK, secrets OK, notify path OK.")

def main(debug=False):
    if not has_notifier():
        print("⚠️ No notifier configured (Telegram or Twilio). Will print to logs.", file=sys.stderr)

    companies = load_yaml(COMPANIES_FILE)
    filters = load_yaml(FILTERS_FILE)

    include_patterns = filters.get("include_titles", [])
    exclude_patterns = filters.get("exclude_titles", [])
    must_all_patterns = filters.get("must_have_all", [])
    # 'locations_any_of' handled upstream by is_us()

    seen = load_seen()
    seen_ids = set(seen["ids"])
    new_seen = set(seen_ids)

    all_jobs = []

    for c in companies.get("greenhouse", []):
        try:
            all_jobs.extend(fetch_greenhouse(c))
        except Exception as e:
            print(f"[WARN] Greenhouse {c}: {e}", file=sys.stderr)

    for c in companies.get("lever", []):
        try:
            all_jobs.extend(fetch_lever(c))
        except Exception as e:
            print(f"[WARN] Lever {c}: {e}", file=sys.stderr)

    if companies.get("amazon"):
        try:
            all_jobs.extend(fetch_amazon())
        except Exception as e:
            print(f"[WARN] Amazon: {e}", file=sys.stderr)

    # Filter & dedupe; batch messages to avoid Twilio 429
    messages = []
    new_count = 0
    for job in all_jobs:
        if job["id"] in new_seen:
            continue
        if not title_matches(job["title"], include_patterns, exclude_patterns, must_all_patterns):
            continue
        messages.append(format_msg(job))
        new_seen.add(job["id"])
        new_count += 1

    if debug:
        print(f"Fetched: {len(all_jobs)}; New matches: {new_count}")

    if new_count > 0:
        joined = "\n\n".join(messages)
        if len(joined) > 1400:  # keep SMS size sane
            joined = joined[:1370] + "\n…(truncated)"
        notify(f"🔔 {new_count} new roles (US):\n\n{joined}")
    else:
        print("No new matching jobs.")

    save_seen(sorted(list(new_seen)))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true", help="Send a heartbeat and exit")
    parser.add_argument("--debug", action="store_true", help="Print debug stats")
    args = parser.parse_args()

    if args.self_test:
        send_heartbeat()
        sys.exit(0)

    main(debug=args.debug)
