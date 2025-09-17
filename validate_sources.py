# validate_sources.py
import requests, yaml, sys
from pathlib import Path

TIMEOUT=15
HEADERS={"User-Agent":"jobs-alert-bot/validator"}

root = Path(__file__).parent
cfg = yaml.safe_load((root/"companies.yml").read_text(encoding="utf-8")) or {}

def ok(url, method="GET"):
    try:
        r = requests.request(method, url, timeout=TIMEOUT, headers=HEADERS)
        return r.status_code < 400
    except Exception:
        return False

bad = []

for c in cfg.get("greenhouse", []):
    api = f"https://boards-api.greenhouse.io/v1/boards/{c}/jobs"
    if not ok(api):
        bad.append(("greenhouse", c, api))

for c in cfg.get("lever", []):
    api = f"https://api.lever.co/v0/postings/{c}?mode=json"
    if not ok(api):
        bad.append(("lever", c, api))

for t in cfg.get("workday_cxs", []):
    # try wd5/wd1 POST endpoints with empty search
    for base in [f"https://{t}.wd5.myworkdayjobs.com/wday/cxs/{t}/jobs",
                 f"https://{t}.wd1.myworkdayjobs.com/wday/cxs/{t}/jobs"]:
        try:
            r = requests.post(base, json={"appliedFacets":{},"limit":1,"offset":0,"searchText":""},
                              timeout=TIMEOUT, headers=HEADERS)
            if r.status_code < 400:
                break
        except Exception:
            pass
    else:
        bad.append(("workday_cxs", t, "wd1/wd5 both failed"))

print("\nVALIDATION RESULTS")
if bad:
    for row in bad:
        print("❌", row)
    print("\nFix/remove the ❌ entries in companies.yml. Example: Snowflake → snowflakeinc.")
    sys.exit(1)
else:
    print("✅ All sources are reachable.")
