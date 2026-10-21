"""
ZoomTanzania.net job scraper (standalone, no other files needed).

Run:
    python tz.py

Output (in the current folder):
    zoom_jobs.csv        all scraped jobs (appended on every run)
    zoom_jobs.json       jobs scraped in THIS run
    zoom_seen.txt        URLs already scraped (used to skip duplicates)

How it finds jobs: the site's /jobs/ page only shows 12 jobs and loads more with
JavaScript (/jobs/page/2/ just repeats page 1), so the scraper reads the site's
job sitemaps (jobs-sitemap.xml ... jobs-sitemap9.xml), which list every job,
newest first, and walks back through them page after page.

Optional environment variables:
    ZOOM_MAX_JOBS        max NEW jobs to scrape per run (default 200, 0 = no limit)
    ZOOM_DELAY           seconds between requests (default 1.0)
    ZOOM_SKIP_TYPES      comma list of job types to skip (default: scholarship,grants)
    JOB_LIMIT            stop after this many new jobs (default 0 = no limit)
"""
import csv
import json
import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Comment

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
BASE = "https://www.zoomtanzania.net"
JOBS_URL = f"{BASE}/jobs/"
MAX_JOBS = int(os.environ.get("ZOOM_MAX_JOBS", "200") or 0)
DELAY = float(os.environ.get("ZOOM_DELAY", "1.0"))
SKIP_TYPES = {t.strip().lower() for t in
              os.environ.get("ZOOM_SKIP_TYPES", "scholarship,grants").split(",") if t.strip()}
JOB_LIMIT = int(os.environ.get("JOB_LIMIT", "0") or 0)
TIMEOUT = 25

CSV_FILE = "zoom_jobs.csv"
JSON_FILE = "zoom_jobs.json"
SEEN_FILE = "zoom_seen.txt"

OWN_HOSTS = ("zoomtanzania.net", "zoom.tz")
BAD_URL_HOSTS = OWN_HOSTS + (
    "facebook.com", "twitter.com", "x.com", "linkedin.com/share", "whatsapp.com",
    "wa.me", "instagram.com", "t.me", "youtube.com", "google.com", "pinterest.com",
)

# Column names match the variables your WordPress save_job() reads.
FIELDS = [
    "Job Title", "Job Description", "Company Details", "Job Type",
    "Job Qualifications", "Job Experience", "Job Location", "Job Field",
    "Date Posted", "Deadline", "Estimated Deadline", "Salary Range",
    "Application", "Company Name", "Company Logo", "Company Industry",
    "Company Founded", "Company Type", "Company Website", "Company Address",
    "Company Url", "Job URL", "Scraped At",
]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s<>\"']+")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",   # no brotli: requests can't decode it
})


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def get_soup(url, retries=3):
    last = None
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = 30 + attempt * 30
                print(f"  429 rate limit, sleeping {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            try:
                return BeautifulSoup(r.text, "lxml")
            except Exception:
                return BeautifulSoup(r.text, "html.parser")
        except requests.HTTPError:
            raise
        except Exception as e:
            last = e
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"fetch failed for {url}: {last}")


# ----------------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------------
def txt(el):
    return el.get_text(" ", strip=True) if el else ""


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def norm_url(href):
    if not href:
        return ""
    p = urlsplit(urljoin(BASE + "/", href))
    if not p.netloc.lower().endswith("zoomtanzania.net"):
        return ""
    path = p.path if p.path.endswith("/") else p.path + "/"
    return urlunsplit(("https", "www.zoomtanzania.net", path, "", ""))


def is_job_path(path):
    """/jobs/<category>/<slug>/  (skips /jobs/at/<company>, /jobs/page/N)."""
    parts = [s for s in path.split("/") if s]
    return len(parts) == 3 and parts[0] == "jobs" and parts[1] not in ("at", "page")


def to_iso_date(value):
    if not value:
        return ""
    m = re.search(r"\d{4}-\d{2}-\d{2}", value)
    if m:
        return m.group(0)
    value = value.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return value


def ok_email(em):
    em = em.lower()
    return bool(em) and not any(h in em for h in OWN_HOSTS) \
        and not em.endswith((".png", ".jpg", ".gif", ".webp")) \
        and not em.startswith(("noreply@", "no-reply@", "example@"))


def ok_url(u):
    u = u.lower()
    return u.startswith("http") and not any(h in u for h in BAD_URL_HOSTS)


def find_apply(scopes):
    """Return (email, url) from the first scope that has a public one."""
    for scope in scopes:
        if scope is None:
            continue
        email = url = ""
        for a in scope.find_all("a", href=True):
            h = a["href"].strip()
            if h.lower().startswith("mailto:"):
                c = h[7:].split("?")[0].strip()
                if ok_email(c) and not email:
                    email = c
            elif not url and ok_url(h):
                url = h
        text = scope.get_text(" ")
        if not email:
            for m in EMAIL_RE.finditer(text):
                c = m.group(0).rstrip(".,;")
                if ok_email(c):
                    email = c
                    break
        if not url:
            for u in URL_RE.findall(text):
                u = u.rstrip(".,);")
                if ok_url(u):
                    url = u
                    break
        if email or url:
            return email, url
    return "", ""


def block_text(el):
    """Readable text with line breaks for paragraphs, lists and headings."""
    if el is None:
        return ""
    for br in el.find_all("br"):
        br.replace_with("\n")
    for li in el.find_all("li"):
        li.insert_before("\n- ")
    for tag in el.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr"]):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text = el.get_text("")
    lines = [clean(l) if not l.strip().startswith("-") else "- " + clean(l.strip()[1:])
             for l in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(l for l in lines if l)).strip()


APPLY_HEAD_RE = re.compile(
    r"^(how to apply|method of application|application procedure|to apply|apply now|"
    r"how can you apply|application method)\b", re.I)


# ----------------------------------------------------------------------------
# FIELD EXTRACTORS (job type, qualifications, experience, salary, field ...)
# ----------------------------------------------------------------------------
JOB_TYPE_MAP = [
    ("intern", "internship"), ("volunteer", "volunteer"), ("freelanc", "freelance"),
    ("consult", "contract"), ("contract", "contract"), ("temporar", "temporary"),
    ("part", "part-time"), ("full", "full-time"),
]


def map_job_type(raw):
    r = (raw or "").lower()
    for key, val in JOB_TYPE_MAP:
        if key in r:
            return val
    return "full-time"


def _years(lo, hi=None, plus=False):
    if hi:
        return f"{lo}-{hi} years"
    return f"{lo}+ years" if plus else f"{lo} year" + ("" if lo == 1 else "s")


EXP_RANGE_RE = re.compile(r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.I)
EXP_SINGLE_RE = re.compile(
    r"(?:(minimum|min\.?|at\s*least|over|more\s+than)\s+(?:of\s+)?)?"
    r"(\d{1,2})\s*(\+)?\s*(?:years?|yrs?)\b", re.I)
EXP_ENTRY_RE = re.compile(
    r"entry[\s-]*level|fresh(?:er)?\s+graduate|recent\s+graduate|no\s+(?:prior\s+)?experience"
    r"\s+(?:is\s+)?(?:required|needed|necessary)|graduate\s+trainee", re.I)


def extract_experience(text):
    """e.g. '3+ years', '2-5 years', 'Entry level' ('' when the text says nothing)."""
    text = text or ""
    for m in EXP_RANGE_RE.finditer(text):
        ctx = text[max(0, m.start() - 80): m.end() + 100].lower()
        if "experience" in ctx and int(m.group(1)) < int(m.group(2)):
            return _years(m.group(1), m.group(2))
    for m in EXP_SINGLE_RE.finditer(text):
        ctx = text[max(0, m.start() - 80): m.end() + 100].lower()
        if "experience" in ctx and "years old" not in ctx and int(m.group(2)) > 0:
            return _years(int(m.group(2)), plus=bool(m.group(1) or m.group(3)))
    if EXP_ENTRY_RE.search(text):
        return "Entry level"
    return ""


EDU_RE = re.compile(
    r"\b(ph\.?d|doctorate|master'?s?|msc|m\.sc|mba|bachelor'?s?|b\.?sc|bsc|b\.?com|"
    r"degree|advanced\s+diploma|ordinary\s+diploma|diploma|certificate|cpa\b|cpa\(t\)|acca|"
    r"cfa|nacte|csee|acsee|form\s+(?:four|six)|o-level|a-level)", re.I)
QUAL_HEAD_RE = re.compile(
    r"^(qualifications?|education|academic|requirements?|minimum requirements?|"
    r"key requirements?|skills\s*(?:and|&)\s*qualifications?|education\s*(?:and|&)\s*experience|"
    r"desired candidate|person specification|what we are looking for)\b", re.I)


def _is_heading(line):
    s = line.strip()
    return (not s.startswith("-")) and len(s) <= 70 and (s.endswith(":") or s.isupper()
                                                          or len(s.split()) <= 4)


def extract_qualification(description):
    lines = [l.strip() for l in (description or "").split("\n") if l.strip()]
    picked = []
    for i, ln in enumerate(lines):
        head = ln.strip(":*#- ").strip()
        if _is_heading(ln) and QUAL_HEAD_RE.match(head):
            for nxt in lines[i + 1: i + 9]:
                if _is_heading(nxt) and not EDU_RE.search(nxt):
                    break
                if EDU_RE.search(nxt):
                    picked.append(nxt.lstrip("- ").strip())
            if picked:
                break
    if not picked:
        picked = [l.lstrip("- ").strip() for l in lines if EDU_RE.search(l) and len(l) < 300]
    return "; ".join(p[:200] for p in picked[:3])


SALARY_RE = re.compile(
    r"(?:TZS|TSH|TShs?|USD|US\$|\$)\s?[\d][\d,\.]*\s?(?:k|m|million)?"
    r"(?:\s?(?:-|–|to)\s?(?:TZS|TSH|TShs?|USD|US\$|\$)?\s?[\d][\d,\.]*\s?(?:k|m|million)?)?"
    r"(?:\s?(?:per|/|a)\s?(?:month|year|annum|week|day|hour))?", re.I)


def extract_salary(description):
    m = SALARY_RE.search(description or "")
    return clean(m.group(0)) if m else ""


def infer_job_field(categories, url):
    if categories:
        return ", ".join(categories[:2])
    parts = [s for s in urlsplit(url).path.split("/") if s]
    return parts[1].replace("-", " ").title() if len(parts) >= 2 else ""


def abs_url(u):
    return urljoin(BASE + "/", u) if u else ""


# ----------------------------------------------------------------------------
# STEP 1: collect job links
# ----------------------------------------------------------------------------
SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def get_raw(url, retries=3):
    """Fetch a URL and return its text, or None on 404 / repeated failure."""
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=60)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(30 + attempt * 30)
                continue
            r.raise_for_status()
            return r.text
        except Exception:
            time.sleep(2 + attempt * 2)
    return None


def jobs_sitemap_urls():
    """URLs of the job sitemaps, oldest first (the last one holds the newest jobs)."""
    urls = []
    index = get_raw(f"{BASE}/sitemap.xml") or get_raw(f"{BASE}/sitemap_index.xml")
    if index:
        for loc in SITEMAP_LOC_RE.findall(index):
            if re.search(r"/jobs-sitemap\d*\.xml$", loc):
                urls.append(loc)
    if not urls:                       # index unreadable: probe the usual names
        for n in range(1, 40):
            name = "jobs-sitemap.xml" if n == 1 else f"jobs-sitemap{n}.xml"
            if get_raw(f"{BASE}/{name}") is None:
                break
            urls.append(f"{BASE}/{name}")

    def num(u):
        m = re.search(r"jobs-sitemap(\d*)\.xml$", u)
        return int(m.group(1) or 1)
    return sorted(set(urls), key=num)


def collect_links_from_archive():
    """Fallback: only the 12 newest jobs shown on /jobs/ (no pagination possible)."""
    soup = get_soup(JOBS_URL)
    anchors = soup.select(".civi-jobs-item a[href]") or soup.find_all("a", href=True)
    out, seen = [], set()
    for a in anchors:
        n = norm_url(a["href"])
        if n and is_job_path(urlsplit(n).path) and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def collect_links(seen_urls):
    """Newest-first list of job URLs not scraped yet (at most MAX_JOBS)."""
    print(f"\nCollecting job links from the job sitemaps of {BASE}")
    sitemaps = jobs_sitemap_urls()
    if not sitemaps:
        print("  No sitemaps found, falling back to the first /jobs/ page (12 jobs only).")
        links = [u for u in collect_links_from_archive() if u not in seen_urls]
        return links[:MAX_JOBS] if MAX_JOBS else links

    print(f"  Found {len(sitemaps)} job sitemap(s).")
    ordered, seen, total_in_maps = [], set(), 0
    # Newest jobs are in the LAST sitemap, and each sitemap is oldest-first,
    # so go from the last sitemap backwards and reverse each one.
    for sm in reversed(sitemaps):
        body = get_raw(sm)
        if body is None:
            print(f"  Could not read {sm}, skipping.")
            continue
        locs = [norm_url(u) for u in SITEMAP_LOC_RE.findall(body)]
        locs = [u for u in locs if u and is_job_path(urlsplit(u).path)]
        total_in_maps += len(locs)
        fresh = 0
        for u in reversed(locs):
            if u in seen:
                continue
            seen.add(u)
            if u in seen_urls:
                continue
            ordered.append(u)
            fresh += 1
            if MAX_JOBS and len(ordered) >= MAX_JOBS:
                break
        print(f"  {sm.rsplit('/', 1)[-1]}: {len(locs)} job(s), {fresh} new "
              f"(queue {len(ordered)})")
        if MAX_JOBS and len(ordered) >= MAX_JOBS:
            print(f"  Reached ZOOM_MAX_JOBS ({MAX_JOBS}).")
            break
        time.sleep(DELAY)
    print(f"  {total_in_maps} job URL(s) read from sitemaps so far.")
    return ordered


# ----------------------------------------------------------------------------
# STEP 2: parse one job page
# ----------------------------------------------------------------------------
def scrape_job(url):
    soup = get_soup(url)
    head = soup.select_one(".jobs-head-details") or soup

    h1 = head.select_one("h1") or soup.find("h1")
    title = clean(txt(h1))
    title = re.sub(r"\s*\(?\d[\d,]*\s*views?\)?\s*$", "", title, flags=re.I).strip()

    company = txt(head.select_one("a.authour")) or "Unknown"
    categories = [clean(txt(a)) for a in head.select(".categories-warpper a.cate")]
    categories = [c for c in categories if c]
    logo_el = head.select_one("img.logo-comnpany")
    logo = logo_el.get("src", "") if logo_el else ""

    raw_type = txt(head.select_one("a.label-type"))
    location = clean(txt(head.select_one("a.label-location"))) or "Tanzania"

    # "Job Role Insights" grid
    insights = {}
    for li in soup.select(".jobs-insights-details li.list-item"):
        key = txt(li.select_one(".title-info")).lower()
        spans = [txt(s) for s in li.select(".details-info span")]
        val = ", ".join(s for s in spans if s) or txt(li.select_one(".details-info"))
        if key:
            insights[key] = val
    if insights.get("hiring location"):
        location = clean(insights["hiring location"]) or location

    desc_el = (soup.select_one(".jobs-description-details .civi-description")
               or soup.select_one(".civi-description"))
    popup = soup.select_one("#civi_form_apply_jobs .apply-popup")

    if desc_el is not None:
        for junk in desc_el.select(".ad-container, ins, script, iframe, style, .toggle-description"):
            junk.decompose()
        for c in desc_el.find_all(string=lambda s: isinstance(s, Comment)):
            c.extract()

    # apply info must be read BEFORE block_text() changes the tree
    apply_email, apply_url = find_apply([popup, desc_el])

    description = ""
    if desc_el is not None:
        lines = [l for l in block_text(desc_el).split("\n") if l.strip()]
        cut = len(lines)
        for i, ln in enumerate(lines):
            if APPLY_HEAD_RE.match(ln.strip().strip(":*#").strip()):
                cut = i
                break
        body = lines[:cut]
        if len(" ".join(body).split()) < 20:
            body = lines
        description = "\n".join(body).strip()

    # dates: Deadline = the real closing date; if the site has none, leave it
    # empty and give an Estimated Deadline (posted + 30 days) instead.
    today = datetime.now().strftime("%Y-%m-%d")
    date_posted = to_iso_date(insights.get("date posted", "")) or today
    deadline = to_iso_date(insights.get("closing date", ""))
    estimated_deadline = ""
    if not deadline:
        try:
            base_day = datetime.strptime(date_posted, "%Y-%m-%d")
        except ValueError:
            base_day = datetime.now()
        estimated_deadline = (base_day + timedelta(days=30)).strftime("%Y-%m-%d")

    # experience: trust the job text first (the insights box is often generic)
    experience = extract_experience(description)
    if not experience:
        m = re.search(r"(\d{1,2})", insights.get("experience", ""))
        experience = _years(int(m.group(1)), plus=True) if m and int(m.group(1)) > 0 else ""

    salary = next((v for k, v in insights.items()
                   if "salary" in k and v and v.strip().lower() not in ("n/a", "-", "none")), "")
    salary = salary or extract_salary(description)

    # company sidebar
    about = industry = website = company_type = company_address = founded = ""
    side = soup.select_one(".jobs-company-sidebar")
    if side:
        content = side.select_one(".content-truncated .content")
        about = block_text(content) if content else ""
        cate = side.select_one(".list-cate a")
        industry = re.sub(r"\s+Companies$", "", txt(cate), flags=re.I).strip() if cate else ""
        for info in side.select(".info"):
            k = txt(info.select_one(".title-info")).lower()
            v = txt(info.select_one(".details-info"))
            if not k or not v:
                continue
            if "location" in k or "address" in k:
                company_address = company_address or v
            elif "type" in k:
                company_type = company_type or v
        for a in side.find_all("a", href=True):
            href = a["href"].strip()
            cls = " ".join(a.get("class", [])).lower()
            if ok_url(href) and (re.search(r"web|site|visit", txt(a), re.I) or "website" in cls):
                website = href
                break
        fm = re.search(r"(?:founded|established|since)\s+(?:in\s+)?((?:19|20)\d{2})", about, re.I)
        founded = fm.group(1) if fm else ""

    job_url = norm_url(url)
    return {
        "title": title, "company": company, "location": location,
        "job_type": raw_type, "job_type_mapped": map_job_type(raw_type),
        "categories": categories, "job_field": infer_job_field(categories, job_url),
        "date_posted": date_posted, "deadline": deadline,
        "estimated_deadline": estimated_deadline, "salary": salary,
        "qualification": extract_qualification(description), "experience": experience,
        "apply_email": apply_email, "apply_url": apply_url,
        "description": description, "company_about": about,
        "company_industry": industry, "company_founded": founded,
        "company_type": company_type, "company_website": website,
        "company_address": company_address or location,
        "logo": abs_url(logo), "job_url": job_url,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


def to_row(j):
    """Internal job dict -> one row with the column names save_job() reads."""
    return {
        "Job Title": j["title"], "Job Description": j["description"],
        "Company Details": j["company_about"], "Job Type": j["job_type_mapped"],
        "Job Qualifications": j["qualification"], "Job Experience": j["experience"],
        "Job Location": j["location"], "Job Field": j["job_field"],
        "Date Posted": j["date_posted"], "Deadline": j["deadline"],
        "Estimated Deadline": j["estimated_deadline"], "Salary Range": j["salary"],
        "Application": j["apply_email"] or j["apply_url"],
        "Company Name": j["company"], "Company Logo": j["logo"],
        "Company Industry": j["company_industry"], "Company Founded": j["company_founded"],
        "Company Type": j["company_type"], "Company Website": j["company_website"],
        "Company Address": j["company_address"],
        "Company Url": "",            # never link back to zoomtanzania.net
        "Job URL": j["job_url"], "Scraped At": j["scraped_at"],
    }


# ----------------------------------------------------------------------------
# STORAGE
# ----------------------------------------------------------------------------
def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, encoding="utf-8") as f:
        return {l.strip() for l in f if l.strip()}


def mark_seen(url):
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(url + "\n")


def append_csv(row):
    # an older run may have written different columns: set that file aside
    if os.path.exists(CSV_FILE) and os.path.getsize(CSV_FILE) > 0:
        with open(CSV_FILE, encoding="utf-8-sig") as f:
            first = f.readline().strip()
        if first != ",".join(FIELDS):
            os.replace(CSV_FILE, CSV_FILE.replace(".csv", "_old_columns.csv"))
    new_file = not os.path.exists(CSV_FILE) or os.path.getsize(CSV_FILE) == 0
    with open(CSV_FILE, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(row)


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("  ZOOMTANZANIA SCRAPER")
    print("=" * 70)

    seen_urls = load_seen()
    print(f"Already scraped before: {len(seen_urls)} job(s)")

    links = collect_links(seen_urls)
    print(f"\nFound {len(links)} job page(s) to look at.\n")

    new_jobs, skipped, errors = [], 0, 0
    for i, link in enumerate(links, 1):
        if link in seen_urls:
            skipped += 1
            continue
        try:
            job = scrape_job(link)
        except Exception as e:
            errors += 1
            print(f"  ERROR {link}: {e}")
            time.sleep(DELAY)
            continue

        if not job["title"] or not job["description"]:
            skipped += 1
            print(f"  Skipped (empty page): {link}")
            mark_seen(link)
            seen_urls.add(link)
            continue

        if job["job_type"].strip().lower() in SKIP_TYPES:
            skipped += 1
            print(f"  Skipped type '{job['job_type']}': {job['title']}")
            mark_seen(link)
            seen_urls.add(link)
            continue

        row = to_row(job)
        append_csv(row)
        mark_seen(link)
        seen_urls.add(link)
        new_jobs.append(row)
        apply = job["apply_email"] or job["apply_url"] or "no public apply"
        print(f"  [{len(new_jobs)}] {job['title']} | {job['company']} | "
              f"{job['location']} | apply: {apply}")

        if JOB_LIMIT and len(new_jobs) >= JOB_LIMIT:
            print(f"\nJOB_LIMIT ({JOB_LIMIT}) reached, stopping.")
            break
        time.sleep(DELAY)

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(new_jobs, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 70)
    print(f"  Done. New: {len(new_jobs)} | Skipped: {skipped} | Errors: {errors}")
    print(f"  Files: {CSV_FILE}, {JSON_FILE}, {SEEN_FILE}")
    print("=" * 70)


if __name__ == "__main__":
    main()
