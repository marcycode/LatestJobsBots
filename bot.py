import json, os, re, time, sys
from pathlib import Path
import requests
import yaml

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
HEADERS = {"User-Agent": "jobs-alert-bot/1.0 (+https://github.com/you/yourrepo)"}

def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {"ids": []}

def save_seen(seen_ids):
    SEEN_FILE.write_text(json.dumps({"ids": sorted(seen_ids)}, indent=2), encoding="utf-8")

def telegram_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured; skipping send.", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()

def twilio_send(text):
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and TWILIO_TO):
        print("Twilio not configured; skipping send.", file=sys.stderr)
        return
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    data = {"From": TWILIO_FROM, "To": TWILIO_TO, "Body": text}
    r = requests.post(url, data=data, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=TIMEOUT)
    r.raise_for_status()

def notify(text):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        telegram_send(text)
    elif TWILIO_SID:
        twilio_send(text)
    else:
        print(text)

def fetch_greenhouse(company):
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs"
    r = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("jobs", []):
        job_id = f"greenhouse:{company}:{j.get('id')}"
        title = j.get("title", "")
        url = j.get("absolute_url")
        loc_name = j.get("location", {}).get("name") if isinstance(j.get("location"), dict) else None
        locations = [loc_name] if loc_name else []
        jobs.append({"id": job_id, "source": "greenhouse", "company": company, "title": title, "url": url, "locations": locations})
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
        url = j.get("hostedUrl") or j.get("applyUrl")
        locs = j.get("categories", {}).get("location")
        locations = [locs] if isinstance(locs, str) else []
        jobs.append({"id": job_id, "source": "lever", "company": company, "title": title, "url": url, "locations": locations})
    return jobs

# === NEW: Workday CXS fetcher (covers Microsoft, NVIDIA, and many others on Workday) ===
def fetch_workday_cxs(tenant, query="software engineer", limit=100):
    # Generic Workday CXS endpoint; many orgs use wd1, wd5, wd3; try wd5 first then wd1 as fallback.
    bases = [
        f"https://{tenant}.wd5.myworkdayjobs.com/wday/cxs/{tenant}/jobs",
        f"https://{tenant}.wd1.myworkdayjobs.com/wday/cxs/{tenant}/jobs"
    ]
    jobs = []
    for base in bases:
        try:
            payload = {
                "appliedFacets": {},
                "limit": limit,
                "offset": 0,
                "searchText": query
            }
            r = requests.post(base, json=payload, timeout=TIMEOUT, headers=HEADERS)
            if r.status_code >= 400:
                continue
            data = r.json()
            for item in data.get("jobPostings", []):
                # Fields vary a bit by tenant; keep it defensive
                title = item.get("title", "")
                externalPath = item.get("externalPath") or item.get("externalUrl")
                url = f"https://{tenant}.wd5.myworkdayjobs.com{externalPath}" if externalPath and externalPath.startswith("/") else externalPath
                locations = []
                loc_obj = item.get("locationsText") or item.get("locations")
                if isinstance(loc_obj, list):
                    locations = [str(x) for x in loc_obj]
                elif isinstance(loc_obj, str):
                    locations = [loc_obj]
                job_id = f"workday:{tenant}:{item.get('bulletFields', {}).get('jobFamily', '')}:{item.get('id','')}"
                jobs.append({"id": job_id, "source": "workday", "company": tenant, "title": title, "url": url, "locations": locations})
            break  # success on one base; stop trying others
        except Exception as e:
            print(f"[WARN] Workday {tenant}: {e}", file=sys.stderr)
            continue
    return jobs

# === NEW: Amazon Jobs (official JSON endpoint) ===
def fetch_amazon(max_pages=3):
    # Pull SWE category + keyword filter to stay focused.
    # Note: pagination uses 'offset'. Each page returns 'jobs' list.
    base = "https://www.amazon.jobs/en/search.json"
    params = {
        "category": "software-development",
        "result_limit": 50,
        "sort": "recent",
        "job_type": "Full-Time",
        "normalized_country_code": "",  # allow all; use filters.yml locations to narrow
        "radius": "24km",
        "query": "software engineer"
    }
    jobs = []
    offset = 0
    for _ in range(max_pages):
        try:
            p = dict(params)
            p["offset"] = offset
            r = requests.get(base, params=p, timeout=TIMEOUT, headers=HEADERS)
            r.raise_for_status()
            data = r.json()
            for j in data.get("jobs", []):
                title = j.get("title", "")
                job_path = j.get(" job_path") or j.get("job_path") or ""
                url = f"https://www.amazon.jobs{job_path}" if job_path.startswith("/") else f"https://www.amazon.jobs{job_path}"
                loc_city = j.get("city", "")
                loc_country = j.get("country_code", "")
                locations = [", ".join([loc_city, loc_country]).strip(", ")]
                job_id = f"amazon:{j.get('id') or j.get('job_id') or job_path}"
                jobs.append({"id": job_id, "source": "amazon", "company": "amazon", "title": title, "url": url, "locations": locations})
            if not data.get("jobs"):
                break
            offset += params["result_limit"]
            time.sleep(0.6)
        except Exception as e:
            print(f"[WARN] Amazon: {e}", file=sys.stderr)
            break
    return jobs

def title_matches(title, include_any, exclude_any, must_all=None):
    t = title.lower()
    # Exclusions first
    for p in exclude_any or []:
        if re.search(p, t, flags=re.I):
            return False
    # Must-have ALL (AND)
    if must_all:
        for p in must_all:
            if not re.search(p, t, flags=re.I):
                return False
    # Then include-any (OR) if provided; if not provided, it's fine
    if include_any:
        for p in include_any:
            if re.search(p, t, flags=re.I):
                return True
        return False
    return True


def location_matches(locations, allowed):
    if not allowed:
        return True
    if not locations:
        return False
    for loc in locations:
        if any(a.lower() in loc.lower() for a in allowed):
            return True
    return False

def format_msg(job):
    company = job["company"].title()
    title = job["title"]
    locations = ", ".join(job["locations"]) if job["locations"] else "Location: N/A"
    url = job["url"]
    return f"New: {title} — {company}\n{locations}\n{url}"

def main():
    companies = load_yaml(COMPANIES_FILE)
    filters = load_yaml(FILTERS_FILE)
    include_patterns = filters.get("include_titles", [])
    exclude_patterns = filters.get("exclude_titles", [])
    allowed_locations = filters.get("locations_any_of", []) or []

    seen = load_seen()
    seen_ids = set(seen["ids"])
    new_seen = set(seen_ids)

    all_jobs = []

    # Greenhouse / Lever
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

    # Workday CXS (Microsoft, NVIDIA, etc.)
    for tenant in companies.get("workday_cxs", []):
        try:
            all_jobs.extend(fetch_workday_cxs(tenant,query="new grad software engineer", limit=100))
        except Exception as e:
            print(f"[WARN] Workday {tenant}: {e}", file=sys.stderr)

    # Amazon
    if companies.get("amazon"):
        try:
            all_jobs.extend(fetch_amazon())
        except Exception as e:
            print(f"[WARN] Amazon: {e}", file=sys.stderr)

    # TODO (advanced): custom fetchers you can add later
    # if companies.get("google"):
    #     all_jobs.extend(fetch_google_careers(...))
    # if companies.get("meta"):
    #     all_jobs.extend(fetch_meta_graphql(...))
    # if companies.get("apple"):
    #     all_jobs.extend(fetch_apple_search(...))

    # Filter & notify
    new_count = 0
    for job in all_jobs:
        if job["id"] in new_seen:
            continue
        if not title_matches(job["title"], include_patterns, exclude_patterns):
            continue
        if not location_matches(job["locations"], allowed_locations):
            continue
        notify(format_msg(job))
        new_seen.add(job["id"])
        new_count += 1
        time.sleep(0.4)

    print(f"{new_count} new matching jobs." if new_count else "No new matching jobs.")
    save_seen(sorted(list(new_seen)))

if __name__ == "__main__":
    main()
