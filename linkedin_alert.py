#!/usr/bin/env python3
"""LinkedIn Job Alert -> Telegram. Standard library only."""

import csv
import json
import os
import re
import html
import random
import time
import datetime
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------- settings
QUERIES = [q.strip() for q in os.environ.get("QUERIES", '"Business Development"').splitlines() if q.strip()]
LOCATIONS = [x.strip() for x in os.environ.get("LOCATIONS", "Saudi Arabia").split(",") if x.strip()]
TITLE_INCLUDE = [w.strip().lower() for w in os.environ.get("TITLE_INCLUDE", "").split(",") if w.strip()]
EXCLUDE = [w.strip().lower() for w in os.environ.get("EXCLUDE", "intern,internship").split(",") if w.strip()]
TIME_WINDOW = os.environ.get("TIME_WINDOW", "r14400")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "4"))
SUMMARY_HOUR_UTC = int(os.environ.get("SUMMARY_HOUR_UTC", "5"))   # 08:00 Riyadh
FAIL_WARN_AFTER = int(os.environ.get("FAIL_WARN_AFTER", "6"))      # cycles (~15 min)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

STATE_FILE = Path(os.environ.get("STATE_FILE", "seen.json"))
STATUS_FILE = Path(os.environ.get("STATUS_FILE", "status.json"))
LOG_FILE = Path(os.environ.get("LOG_FILE", "jobs.csv"))
MAX_REMEMBERED = 3000
PAGE_SIZE = 25

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
]


# ---------------------------------------------------------------- helpers
def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def clean(text):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or ""))).strip()


def phrase_in(phrase, text):
    """Whole-word match: 'sales' matches 'Sales Manager' but not 'Salesforce'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


class RateLimited(Exception):
    pass


def fetch(url, tries=2):
    for attempt in range(tries):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 999):
                raise RateLimited(str(e.code))
            if e.code == 503 and attempt < tries - 1:
                time.sleep(5)
                continue
            print("[!] HTTP %s on %s" % (e.code, url))
            return None
        except Exception as e:
            if attempt < tries - 1:
                time.sleep(3)
                continue
            print("[!] request failed: %s" % e)
            return None
    return None


def parse_jobs(page_html):
    jobs = []
    for block in re.split(r"<li[\s>]", page_html)[1:]:
        m = re.search(r"urn:li:jobPosting:(\d+)", block)
        if not m:
            continue
        job_id = m.group(1)

        title = ""
        t = re.search(r'class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)</h3>', block, re.S)
        if t:
            title = clean(t.group(1))

        company = ""
        c = re.search(r'class="[^"]*base-search-card__subtitle[^"]*"[^>]*>(.*?)</h4>', block, re.S)
        if c:
            company = clean(c.group(1))

        location = ""
        l = re.search(r'class="[^"]*job-search-card__location[^"]*"[^>]*>(.*?)</span>', block, re.S)
        if l:
            location = clean(l.group(1))

        posted = ""
        p = re.search(r"<time[^>]*>(.*?)</time>", block, re.S)
        if p:
            posted = clean(p.group(1))

        easy = "easy-apply-label" in block

        jobs.append({
            "id": job_id,
            "title": title or "New job",
            "company": company,
            "location": location,
            "posted": posted,
            "easy_apply": True if easy else None,
            "link": "https://www.linkedin.com/jobs/view/%s/" % job_id,
        })
    return jobs


DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/%s"


def check_easy_apply(job):
    """Guest search cards carry no Easy Apply flag; the job page does."""
    if job["easy_apply"] is not None:
        return
    try:
        page = fetch(DETAIL_URL % job["id"], tries=1)
    except RateLimited:
        page = None
    if not page:
        return
    if "apply-link-onsite" in page:
        job["easy_apply"] = True
    elif "apply-link-offsite" in page:
        job["easy_apply"] = False


def search(query, location):
    """Returns (jobs, ok). Follows pages while they come back full."""
    jobs = []
    for page in range(MAX_PAGES):
        params = {
            "keywords": query,
            "location": location,
            "f_TPR": TIME_WINDOW,
            "sortBy": "DD",
            "start": page * PAGE_SIZE,
        }
        page_html = fetch(SEARCH_URL + "?" + urllib.parse.urlencode(params))
        if page_html is None:
            return jobs, False
        batch = parse_jobs(page_html)
        jobs.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        time.sleep(2)
    return jobs, True


def send_telegram(text, button_url=None):
    if not BOT_TOKEN or not CHAT_ID:
        print("[!] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
        return False
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if button_url:
        payload["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "\U0001F517 Open job", "url": button_url}]]}
        )
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % BOT_TOKEN, data=data
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read()).get("ok", False)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            print("[!] telegram HTTP %s" % e.code)
            return False
        except Exception as e:
            print("[!] telegram error: %s" % e)
            time.sleep(2)
    return False


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False))


def log_job(job, detected):
    new_file = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["detected_utc", "posted", "title", "company", "location", "easy_apply", "link"])
        w.writerow([detected, job["posted"], job["title"], job["company"], job["location"],
                    {True: "yes", False: "no"}.get(job["easy_apply"], "unknown"), job["link"]])


def format_job(job):
    if job["easy_apply"] is True:
        apply_line = "⚡ <b>Easy Apply</b>"
    elif job["easy_apply"] is False:
        apply_line = "\U0001F310 تقديم على موقع الشركة"
    else:
        apply_line = "❓ طريقة التقديم غير معروفة"
    return (
        "\U0001F6A8 <b>وظيفة جديدة</b>\n\n"
        "\U0001F4BC <b>" + html.escape(job["title"]) + "</b>\n"
        "\U0001F3E2 " + html.escape(job["company"]) + "\n"
        "\U0001F4CD " + html.escape(job["location"]) + "\n"
        "\U0001F550 " + html.escape(job["posted"]) + "\n"
        + apply_line + "\n\n"
        + job["link"]
    )


def format_summary(stats):
    since = stats.get("since", "")[:16].replace("T", " ")
    return (
        "\U0001F4CA <b>تقرير اليوم</b>\n\n"
        "✅ النظام شغال\n"
        "\U0001F504 فحوصات: %d\n"
        "\U0001F4E8 وظايف اتبعتت: %d\n"
        "⚠️ أخطاء اتصال: %d\n\n"
        "منذ: %s UTC"
    ) % (stats.get("cycles", 0), stats.get("sent", 0), stats.get("errors", 0), since)


# ---------------------------------------------------------------- main
def main():
    detected = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    seen = load_json(STATE_FILE, [])
    seen_set = set(seen)
    status = load_json(STATUS_FILE, {})
    stats = status.setdefault("stats", {"cycles": 0, "sent": 0, "errors": 0, "since": detected})
    stats["cycles"] += 1

    found = {}
    ok_count = 0
    total = 0
    rate_limited = False

    for query in QUERIES:
        for loc in LOCATIONS:
            total += 1
            try:
                jobs, ok = search(query, loc)
            except RateLimited:
                rate_limited = True
                print("[!] rate limited, stopping this cycle")
                break
            if ok:
                ok_count += 1
            for job in jobs:
                if job["id"] in seen_set or job["id"] in found:
                    continue
                title_lc = job["title"].lower()
                if any(phrase_in(w, title_lc) for w in EXCLUDE):
                    continue
                if TITLE_INCLUDE and not any(phrase_in(w, title_lc) for w in TITLE_INCLUDE):
                    continue
                found[job["id"]] = job
            time.sleep(2)
        if rate_limited:
            break

    # ---- connection health
    if rate_limited or ok_count == 0:
        status["fail_streak"] = status.get("fail_streak", 0) + 1
        stats["errors"] += 1
        if status["fail_streak"] >= FAIL_WARN_AFTER and not status.get("warned"):
            send_telegram("⚠️ لينكدإن بيرفض الطلبات مؤقتًا من حوالي %d دقيقة. "
                          "هفضل أحاول، وهبلّغك لما يرجع." % (status["fail_streak"] * 2.5))
            status["warned"] = True
    else:
        if status.get("warned"):
            send_telegram("✅ رجع الاتصال بلينكدإن. أي وظيفة نزلت في الفترة دي هتوصلك دلوقتي.")
        status["fail_streak"] = 0
        status["warned"] = False

    print("[i] queries ok %d/%d, new jobs: %d" % (ok_count, total, len(found)))

    # ---- send everything found; remember only what was actually sent
    sent_ids = []
    for job in found.values():
        check_easy_apply(job)
        time.sleep(1)
        if send_telegram(format_job(job), button_url=job["link"]):
            sent_ids.append(job["id"])
            log_job(job, detected)
            time.sleep(0.5)
        else:
            print("[!] could not send %s, will retry next cycle" % job["id"])
    stats["sent"] += len(sent_ids)
    if sent_ids:
        save_json(STATE_FILE, (seen + sent_ids)[-MAX_REMEMBERED:])

    # ---- daily summary (08:00 Riyadh)
    today = now_utc().strftime("%Y-%m-%d")
    if now_utc().hour == SUMMARY_HOUR_UTC and status.get("last_summary") != today:
        if send_telegram(format_summary(stats)):
            status["last_summary"] = today
            status["stats"] = {"cycles": 0, "sent": 0, "errors": 0, "since": detected}

    save_json(STATUS_FILE, status)
    print("[i] sent %d" % len(sent_ids))


if __name__ == "__main__":
    main()
