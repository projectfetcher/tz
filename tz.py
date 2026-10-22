"""
Tanzania jobs scraper: ZoomTanzania + GreatTanzaniaJobs + JobWebTanzania + LinkedIn
in ONE standalone file.

Run:
    python tz.py                          # all four sources (zoom, gtj, jwt, then linkedin)
    python tz.py --sources zoom           # only ZoomTanzania
    python tz.py --sources linkedin,jwt   # any order; or SOURCES=linkedin,jwt in the environment

Output (in the current folder), one set of files for all sites:
    scraped_jobs.csv     all scraped jobs (appended on every run)
    scraped_jobs.json    jobs scraped in THIS run
    scraped_seen.txt     job URLs + title/company fingerprints already scraped

Every row uses the column names your WordPress save_job() reads, plus a
"Source" column. A job posted on several sites (same title + company) is only
saved once: the first source that has it wins.

How jobs are found:
  zoom  the /jobs/ page only shows 12 jobs, so the job sitemaps are used
        (jobs-sitemap.xml ... jobs-sitemap9.xml), newest first.
  gtj   the /jobs page loads more while scrolling, using
        /index.php?option=com_jsjobs&task=job.getnextjobs&pagenum=N
        (20 jobs per page, newest first); the scraper walks N = 0, 1, 2 ...
  jwt   jobwebtanzania.com/jobs/ and /jobs/page/2/, /jobs/page/3/ ... (newest first).
        Newsletter / feed / social links are never taken as an "apply" link.
  linkedin  LinkedIn's public search (no login): jobs posted in Tanzania in the last
        LI_DAYS days, 25 per page, then each job page and its LinkedIn company page.
        Apply links hidden behind LinkedIn's "Apply" button are decoded. LinkedIn is
        slow and may block data-centre IPs (e.g. GitHub Actions); if it does, the
        source stops by itself and the other sources still run.

Optional environment variables:
    SOURCES              zoom,gtj,jwt,linkedin (default all four)
    JOB_LIMIT            stop a source after this many NEW jobs (default 0 = no limit)
    ZOOM_MAX_JOBS        max NEW zoom jobs per run (default 200, 0 = no limit)
    ZOOM_DELAY           seconds between zoom requests (default 1.0)
    ZOOM_SKIP_TYPES      zoom job types to skip (default: scholarship,grants)
    GTJ_MAX_JOBS         max NEW gtj jobs per run (default 200, 0 = no limit)
    GTJ_MAX_PAGES        safety cap on gtj listing pages per run (default 60)
    GTJ_DELAY            seconds between gtj requests (default 1.0)
    GTJ_STOP_ON_KNOWN    1 (default) = stop paging once a whole gtj page was already scraped
    GTJ_SKIP_CATEGORIES  gtj categories to skip (default: scholarship,tender)
    JWT_JOBS_URL         jobwebtanzania listing page (default https://jobwebtanzania.com/jobs/)
    JWT_MAX_JOBS         max NEW jwt jobs per run (default 200, 0 = no limit)
    JWT_MAX_PAGES        safety cap on jwt listing pages per run (default 20)
    JWT_DELAY            seconds between jwt requests (default 1.0)
    JWT_STOP_ON_KNOWN    1 (default) = stop paging once a whole jwt page was already scraped
    JWT_SKIP_TENDERS     1 (default) = skip tenders / RFPs / public notices
    JWT_REQUIRE_PUBLIC_APPLY  1 = skip jwt jobs with no public apply email/link (default 0:
                         such jobs are saved with the job page as "Application", like the others)
    LI_MAX_JOBS          max NEW LinkedIn jobs per run (default 100, 0 = no limit)
    LI_DAYS              only jobs posted in the last N days (default 7)
    LI_KEYWORDS          comma list of search words (default: none = all jobs)
    LI_COUNTRY           search location (default Tanzania)
    LI_MAX_PAGES         safety cap on LinkedIn result pages per keyword (default 40)
    LI_DELAY             seconds between LinkedIn requests (default 2.0)
"""
import base64
import csv
import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import (parse_qsl, quote, quote_plus, unquote, urlencode, urljoin, urlparse,
                          urlsplit, urlunsplit)

import requests
from bs4 import BeautifulSoup, Comment

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
ZOOM_BASE = "https://www.zoomtanzania.net"
ZOOM_JOBS_URL = f"{ZOOM_BASE}/jobs/"
ZOOM_MAX_JOBS = int(os.environ.get("ZOOM_MAX_JOBS", "200") or 0)
ZOOM_DELAY = float(os.environ.get("ZOOM_DELAY", "1.0"))
ZOOM_SKIP_TYPES = {t.strip().lower() for t in
                   os.environ.get("ZOOM_SKIP_TYPES", "scholarship,grants").split(",") if t.strip()}

GTJ_BASE = "https://www.greattanzaniajobs.com"
GTJ_LIST_URL = GTJ_BASE + "/index.php?option=com_jsjobs&task=job.getnextjobs&pagenum={n}"
GTJ_MAX_JOBS = int(os.environ.get("GTJ_MAX_JOBS", "200") or 0)
GTJ_MAX_PAGES = int(os.environ.get("GTJ_MAX_PAGES", "60") or 60)
GTJ_DELAY = float(os.environ.get("GTJ_DELAY", "1.0"))
GTJ_STOP_ON_KNOWN = os.environ.get("GTJ_STOP_ON_KNOWN", "1") != "0"
GTJ_SKIP_CATEGORIES = {t.strip().lower() for t in
                       os.environ.get("GTJ_SKIP_CATEGORIES", "scholarship,tender").split(",")
                       if t.strip()}

JWT_BASE = "https://jobwebtanzania.com"
JWT_JOBS_URL = os.environ.get("JWT_JOBS_URL", JWT_BASE + "/jobs/")
JWT_MAX_JOBS = int(os.environ.get("JWT_MAX_JOBS", "200") or 0)
JWT_MAX_PAGES = int(os.environ.get("JWT_MAX_PAGES", "20") or 20)
JWT_DELAY = float(os.environ.get("JWT_DELAY", "1.0"))
JWT_STOP_ON_KNOWN = os.environ.get("JWT_STOP_ON_KNOWN", "1") != "0"
JWT_SKIP_TENDERS = os.environ.get("JWT_SKIP_TENDERS", "1") != "0"
JWT_REQUIRE_PUBLIC_APPLY = os.environ.get("JWT_REQUIRE_PUBLIC_APPLY", "0") == "1"

LI_BASE = "https://www.linkedin.com"
LI_COUNTRY = os.environ.get("LI_COUNTRY", "Tanzania")
_li_kw_raw = os.environ.get("LI_KEYWORDS", "")
LI_KEYWORDS = [k.strip() for k in _li_kw_raw.split(",")] if _li_kw_raw.strip() else [""]
LI_DAYS = int(os.environ.get("LI_DAYS", "7") or 7)
LI_MAX_JOBS = int(os.environ.get("LI_MAX_JOBS", "100") or 0)
LI_MAX_PAGES = int(os.environ.get("LI_MAX_PAGES", "40") or 40)
LI_DELAY = float(os.environ.get("LI_DELAY", "2.0"))

JOB_LIMIT = int(os.environ.get("JOB_LIMIT", "0") or 0)
MAX_CONSECUTIVE_ERRORS = 8          # a source stops after this many failed jobs in a row

CSV_FILE = "scraped_jobs.csv"
JSON_FILE = "scraped_jobs.json"
SEEN_FILE = "scraped_seen.txt"
LEGACY_SEEN_FILES = ("zoom_seen.txt", "gtj_seen.txt")   # older single-site runs

OWN_HOSTS = ("zoomtanzania.net", "zoom.tz", "greattanzaniajobs", "jobwebtanzania.com",
             "jobwebzambia.com")
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
    "Company Url", "Job URL", "Source", "Scraped At",
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
def decode_response(r):
    """Response body as text: UTF-8 first, then whatever encoding it looks like."""
    raw = r.content
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(r.apparent_encoding or "latin-1", errors="replace")


def fetch(url, ajax=False, referer=None, timeout=30, retries=3):
    """Page text, or None on 404. Raises RuntimeError after repeated failure."""
    headers = {}
    if ajax:
        headers["X-Requested-With"] = "XMLHttpRequest"
    if referer:
        headers["Referer"] = referer
    last = None
    for attempt in range(retries):
        try:
            r = SESSION.get(url, headers=headers, timeout=timeout)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                wait = 30 + attempt * 30
                print(f"  429 rate limit, sleeping {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return decode_response(r)
        except Exception as e:
            last = e
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"fetch failed for {url}: {last}")


def get_raw(url, timeout=60):
    """Like fetch() but returns None on ANY failure (used for sitemaps)."""
    try:
        return fetch(url, timeout=timeout)
    except Exception:
        return None


def get_soup(url):
    text = fetch(url)
    if text is None:
        raise RuntimeError(f"404 for {url}")
    try:
        return BeautifulSoup(text, "lxml")
    except Exception:
        return BeautifulSoup(text, "html.parser")


# ----------------------------------------------------------------------------
# SHARED HELPERS
# ----------------------------------------------------------------------------
def txt(el):
    return el.get_text(" ", strip=True) if el else ""


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def abs_url(u, base):
    u = (u or "").strip().replace(" ", "%20")
    return urljoin(base + "/", u) if u else ""


def to_iso_date(value):
    """'2026-10-20', 'Wednesday, October 28 2026', '20-10-2026' ... -> YYYY-MM-DD ('' if unknown)."""
    if not value:
        return ""
    value = value.strip()
    m = re.search(r"\d{4}-\d{2}-\d{2}", value)
    if m:
        return m.group(0)
    for fmt in ("%A, %B %d %Y", "%A, %B %d, %Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y",
                "%d %b %Y", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


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
    r"^(job application procedure|how to apply|method of application|application procedure|"
    r"to apply|apply now|how can you apply|application method)\b", re.I)


def cut_at_apply(lines):
    """Description lines up to the 'How to apply' heading (all lines if cut leaves too little)."""
    cut = len(lines)
    for i, ln in enumerate(lines):
        if APPLY_HEAD_RE.match(ln.strip().strip(":*#").strip()):
            cut = i
            break
    body = lines[:cut]
    if len(" ".join(body).split()) < 20:
        body = lines
    return "\n".join(body).strip()


def estimated_deadline_for(date_posted):
    try:
        base_day = datetime.strptime(date_posted, "%Y-%m-%d")
    except ValueError:
        base_day = datetime.now()
    return (base_day + timedelta(days=30)).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------
# FIELD EXTRACTORS (job type, qualifications, experience, salary)
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
    r"\b(ph\.?d|doctorate|master['’]?s?|msc|m\.sc|mba|bachelor['’]?s?|b\.?sc|bsc|b\.?com|"
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


# ============================================================================
#  SOURCE 1: ZOOMTANZANIA
# ============================================================================
SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def zoom_norm_url(href):
    if not href:
        return ""
    p = urlsplit(urljoin(ZOOM_BASE + "/", href))
    if not p.netloc.lower().endswith("zoomtanzania.net"):
        return ""
    path = p.path if p.path.endswith("/") else p.path + "/"
    return urlunsplit(("https", "www.zoomtanzania.net", path, "", ""))


def zoom_is_job_path(path):
    """/jobs/<category>/<slug>/  (skips /jobs/at/<company>, /jobs/page/N)."""
    parts = [s for s in path.split("/") if s]
    return len(parts) == 3 and parts[0] == "jobs" and parts[1] not in ("at", "page")


def zoom_infer_job_field(categories, url):
    if categories:
        return ", ".join(categories[:2])
    parts = [s for s in urlsplit(url).path.split("/") if s]
    return parts[1].replace("-", " ").title() if len(parts) >= 2 else ""


def zoom_sitemap_urls():
    """URLs of the job sitemaps, oldest first (the last one holds the newest jobs)."""
    urls = []
    index = get_raw(f"{ZOOM_BASE}/sitemap.xml") or get_raw(f"{ZOOM_BASE}/sitemap_index.xml")
    if index:
        for loc in SITEMAP_LOC_RE.findall(index):
            if re.search(r"/jobs-sitemap\d*\.xml$", loc):
                urls.append(loc)
    if not urls:                       # index unreadable: probe the usual names
        for n in range(1, 40):
            name = "jobs-sitemap.xml" if n == 1 else f"jobs-sitemap{n}.xml"
            if get_raw(f"{ZOOM_BASE}/{name}") is None:
                break
            urls.append(f"{ZOOM_BASE}/{name}")

    def num(u):
        m = re.search(r"jobs-sitemap(\d*)\.xml$", u)
        return int(m.group(1) or 1)
    return sorted(set(urls), key=num)


def zoom_links_from_archive():
    """Fallback: only the 12 newest jobs shown on /jobs/ (no pagination possible)."""
    soup = get_soup(ZOOM_JOBS_URL)
    anchors = soup.select(".civi-jobs-item a[href]") or soup.find_all("a", href=True)
    out, seen = [], set()
    for a in anchors:
        n = zoom_norm_url(a["href"])
        if n and zoom_is_job_path(urlsplit(n).path) and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def zoom_collect_links(seen_urls):
    """Newest-first list of zoom job URLs not scraped yet (at most ZOOM_MAX_JOBS)."""
    print(f"\n  Collecting job links from the job sitemaps of {ZOOM_BASE}")
    sitemaps = zoom_sitemap_urls()
    if not sitemaps:
        print("  No sitemaps found, falling back to the first /jobs/ page (12 jobs only).")
        links = [u for u in zoom_links_from_archive() if u not in seen_urls]
        return links[:ZOOM_MAX_JOBS] if ZOOM_MAX_JOBS else links

    print(f"  Found {len(sitemaps)} job sitemap(s).")
    ordered, seen, total_in_maps = [], set(), 0
    # Newest jobs are in the LAST sitemap, and each sitemap is oldest-first,
    # so go from the last sitemap backwards and reverse each one.
    for sm in reversed(sitemaps):
        body = get_raw(sm)
        if body is None:
            print(f"  Could not read {sm}, skipping.")
            continue
        locs = [zoom_norm_url(u) for u in SITEMAP_LOC_RE.findall(body)]
        locs = [u for u in locs if u and zoom_is_job_path(urlsplit(u).path)]
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
            if ZOOM_MAX_JOBS and len(ordered) >= ZOOM_MAX_JOBS:
                break
        print(f"  {sm.rsplit('/', 1)[-1]}: {len(locs)} job(s), {fresh} new "
              f"(queue {len(ordered)})")
        if ZOOM_MAX_JOBS and len(ordered) >= ZOOM_MAX_JOBS:
            print(f"  Reached ZOOM_MAX_JOBS ({ZOOM_MAX_JOBS}).")
            break
        time.sleep(ZOOM_DELAY)
    print(f"  {total_in_maps} job URL(s) read from sitemaps so far.")
    return ordered


def zoom_scrape_job(url):
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
        description = cut_at_apply(lines)

    # Deadline = the real closing date; if the site has none, leave it empty
    # and give an Estimated Deadline (posted + 30 days) instead.
    date_posted = to_iso_date(insights.get("date posted", "")) or datetime.now().strftime("%Y-%m-%d")
    deadline = to_iso_date(insights.get("closing date", ""))
    estimated_deadline = "" if deadline else estimated_deadline_for(date_posted)

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

    job_url = zoom_norm_url(url)
    return {
        "title": title, "company": company, "location": location,
        "job_type": raw_type, "job_type_mapped": map_job_type(raw_type),
        "job_field": zoom_infer_job_field(categories, job_url),
        "date_posted": date_posted, "deadline": deadline,
        "estimated_deadline": estimated_deadline, "salary": salary,
        "qualification": extract_qualification(description), "experience": experience,
        "apply_email": apply_email, "apply_url": apply_url,
        "description": description, "company_about": about,
        "company_industry": industry, "company_founded": founded,
        "company_type": company_type, "company_website": website,
        "company_address": company_address or location,
        "logo": abs_url(logo, ZOOM_BASE), "job_url": job_url,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


def zoom_skip_reason(job):
    t = job["job_type"].strip().lower()
    return f"type '{job['job_type']}'" if t in ZOOM_SKIP_TYPES else ""


# ============================================================================
#  SOURCE 2: GREATTANZANIAJOBS
# ============================================================================
GTJ_JOB_HREF_RE = re.compile(r"job-detail/(job-[^\"'<>?#\s]+?-(\d+))(?=[?\"'#<>\s]|$)")


def gtj_canon_url(slug):
    slug = quote(unquote(htmllib.unescape(slug)), safe="-._~,()!*'")
    return f"{GTJ_BASE}/jobs/job-detail/{slug}"


def gtj_extract_links(page_html):
    out, seen = [], set()
    for slug, _id in GTJ_JOB_HREF_RE.findall(page_html or ""):
        u = gtj_canon_url(slug)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def gtj_collect_links(seen_urls):
    """Newest-first list of gtj job URLs not scraped yet (at most GTJ_MAX_JOBS)."""
    print(f"\n  Collecting job links from {GTJ_BASE}/jobs (20 jobs per page)")
    ordered, seen_now, empty_streak = [], set(), 0
    for page in range(0, GTJ_MAX_PAGES):
        try:
            body = fetch(GTJ_LIST_URL.format(n=page), ajax=True, referer=GTJ_BASE + "/jobs")
        except Exception as e:
            print(f"  Page {page}: error ({e}), stopping.")
            break
        links = [u for u in gtj_extract_links(body) if u not in seen_now]
        seen_now.update(links)
        known = sum(1 for u in links if u in seen_urls)
        print(f"  Page {page}: {len(links)} job(s), {known} already scraped")

        if not links:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
            ordered.extend(u for u in links if u not in seen_urls)
            if GTJ_STOP_ON_KNOWN and known == len(links):
                print("  Whole page already scraped, stopping.")
                break
            if GTJ_MAX_JOBS and len(ordered) >= GTJ_MAX_JOBS:
                ordered = ordered[:GTJ_MAX_JOBS]
                print(f"  Reached GTJ_MAX_JOBS ({GTJ_MAX_JOBS}).")
                break
        time.sleep(GTJ_DELAY)
    return ordered


def itemprop(soup, name):
    el = soup.select_one(f'[itemprop="{name}"]')
    return clean(el.get_text(" ", strip=True)) if el else ""


def gtj_apply_scope(soup):
    """The 'Job application procedure' block as its own fragment (or None)."""
    wrap = soup.select_one(".jsjobs_description_data")
    if wrap is None:
        return None
    for strong in wrap.find_all("strong"):
        if strong.find_parent(attrs={"itemprop": True}):
            continue                                   # skip the hidden schema copy
        if re.search(r"job application procedure|how to apply|application procedure",
                     strong.get_text(), re.I):
            start = strong.find_parent("p") or strong
            parts = [str(start)]
            for sib in start.find_next_siblings():
                if sib.name == "p" and "All Jobs" in sib.get_text():
                    break
                parts.append(str(sib))
            return BeautifulSoup("".join(parts), "html.parser")
    return None


def gtj_salary(soup, description):
    sal = soup.select_one('[itemprop="baseSalary"]')
    if sal is not None:
        currency = itemprop(sal, "currency")
        inner = sal.select_one('[itemprop="value"] [itemprop="value"]')
        value = clean(txt(inner)) if inner else ""
        unit = itemprop(sal, "unitText").lower()
        if value:
            try:
                value = f"{float(value.replace(',', '')):,.0f}"
            except ValueError:
                pass
            return clean(f"{currency} {value}" + (f" per {unit}" if unit else ""))
    return extract_salary(description)


def gtj_scrape_job(url):
    soup = get_soup(url)

    title = itemprop(soup, "title")
    if not title:
        title = clean(txt(soup.select_one(".jsjobs-main-page-title")))
        title = re.sub(r"\s+job at\s+.*$", "", title, flags=re.I)
    company = (itemprop(soup.select_one('[itemprop="hiringOrganization"]') or soup, "name")
               or clean(txt(soup.select_one(".jsjobs-company-name"))) or "Unknown")

    logo = itemprop(soup, "logo")
    if not logo:
        img = soup.select_one("img.js_jobs_company_logo")
        logo = img.get("src", "") if img else ""

    # "Job Info" box: category, type, deadline, posted ...
    info = {}
    for w in soup.select(".js_job_data_wrapper"):
        k = clean(txt(w.select_one(".js_job_data_title"))).rstrip(":").strip().lower()
        v = clean(txt(w.select_one(".js_job_data_value")))
        if k and k not in info:
            info[k] = v

    category = re.sub(r"\s+jobs?\s+in\s+Tanzania\s*$", "", info.get("job category", ""),
                      flags=re.I).strip()
    occupation = itemprop(soup, "occupationalCategory")
    if not category and occupation:
        category = occupation.split(", ")[0]

    region = itemprop(soup, "addressRegion")
    locality = itemprop(soup, "addressLocality")
    location = region or locality or "Tanzania"
    address = ", ".join(dict.fromkeys(p for p in (locality, region) if p))

    raw_type = itemprop(soup, "employmentType") or info.get("job type", "")

    # apply info must be read BEFORE block_text() changes the tree
    desc_el = soup.select_one('[itemprop="description"]')
    if desc_el is not None and "<p" in desc_el.get_text()[:400]:      # escaped markup
        desc_el = BeautifulSoup(desc_el.get_text(), "html.parser")
    scope = gtj_apply_scope(soup)
    apply_email, apply_url = find_apply([scope, desc_el])

    if desc_el is not None:
        lines = [l for l in block_text(desc_el).split("\n") if l.strip()]
    else:
        wrap = soup.select_one(".jsjobs_description_data")
        if wrap is not None:
            for hidden in wrap.select("[itemscope]"):
                hidden.decompose()
        full = block_text(wrap)
        idx = full.find("JOB DETAILS:")
        full = full[idx + len("JOB DETAILS:"):] if idx >= 0 else full
        lines = [l for l in full.split("\n") if l.strip()]
    description = cut_at_apply(lines)

    today = datetime.now().strftime("%Y-%m-%d")
    date_posted = (to_iso_date(itemprop(soup, "datePosted")) or to_iso_date(info.get("posted", ""))
                   or today)
    deadline = (to_iso_date(info.get("deadline of this job", ""))
                or to_iso_date(itemprop(soup, "validThrough")))
    estimated_deadline = "" if deadline else estimated_deadline_for(date_posted)

    quals = [clean(txt(li)) for li in soup.select('[itemprop="qualifications"] li')]
    qualification = "; ".join(q[:200] for q in quals if q)[:600]
    if not qualification:
        qualification = extract_qualification(description)
    if not qualification:
        cred = itemprop(soup, "credentialCategory")
        qualification = cred.title() if cred else ""

    experience = extract_experience(description)
    if not experience:
        exp = itemprop(soup, "experienceRequirements")
        mr = re.search(r"(\d{1,2})\s*(?:-|to)\s*(\d{1,2})", exp)
        m1 = re.search(r"(\d{1,2})", exp)
        if mr:
            experience = _years(mr.group(1), mr.group(2))
        elif m1 and int(m1.group(1)) > 0:
            experience = _years(int(m1.group(1)), plus=True)
        elif re.search(r"no\s+req|none|not\s+required", exp, re.I):
            experience = "No experience required"

    website = ""
    for a in soup.select(".jsjobs-location-wrap a[href]"):
        if ok_url(a["href"].strip()):
            website = a["href"].strip()
            break

    return {
        "title": title, "company": company, "location": location,
        "job_type": raw_type, "job_type_mapped": map_job_type(raw_type),
        "category_text": f"{category} {occupation}".lower(), "job_field": category,
        "date_posted": date_posted, "deadline": deadline,
        "estimated_deadline": estimated_deadline,
        "salary": gtj_salary(soup, description),
        "qualification": qualification, "experience": experience,
        "apply_email": apply_email, "apply_url": apply_url,
        "description": description, "company_about": "",
        "company_industry": itemprop(soup, "industry"), "company_founded": "",
        "company_type": "", "company_website": website, "company_address": address or location,
        "logo": abs_url(logo, GTJ_BASE), "job_url": url,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


def gtj_skip_reason(job):
    hit = next((w for w in GTJ_SKIP_CATEGORIES if w in job.get("category_text", "")), "")
    return f"category '{hit}'" if hit else ""


# ============================================================================
#  SOURCE 3: JOBWEBTANZANIA  (WordPress "JobRoller" theme)
# ============================================================================
# Hosts that are never a real "apply" link (own site, social, newsletters, feeds, blogs ...)
JWT_NON_APPLY_HOSTS = (
    "jobwebtanzania.com", "jobwebzambia.com",
    "facebook.", "twitter.", "linkedin.", "instagram.", "wa.me", "whatsapp", "t.me", "telegram",
    "plus.google", "pinterest.", "youtube.", "jobthemes.com",
    "google.com", "google.co.", "feedburner.com",
    "mailchimp.", "list-manage.", "constantcontact.", "cc.email", "hubspot.", "hs-sites.",
    "substack.", "tinyletter.", "feedblitz.", "feeds.feedburner.", "blogtrottr.",
    "blogspot.", "wordpress.com", "medium.com", "tumblr.",
)
# Paths / query strings that are never a real "apply" link
JWT_NON_APPLY_PATHS = (
    "/wp-login", "/submit-job", "/register", "/my-dashboard", "/lists/", "/cart", "/checkout",
    "action=login",
    "/subscribe", "/newsletter", "/feed", "/rss", "/atom", "mailverify", "email-alert",
    "job-alert", "job_alert", "emailalert", "jobalert", "/fb/a/", "uri=",
    "/follow", "/share", "/bookmark", "/print", "/report-job", "/report_job",
)
JWT_TRACKING_EXACT = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "ref", "referrer"}

JWT_APPLY_HEAD_RE = re.compile(
    r"^(?:how\s*(?:and|&)\s*deadline\s*to\s*apply|how\s*to\s*apply(?:\s*(?:and|&)\s*deadline)?|"
    r"how\s*to\s*submit|to\s*apply|application\s*(?:and|&)\s*deadline|"
    r"mode\s*of\s*application|method\s*of\s*application|"
    r"application\s*(?:procedure|process|instructions?|method|guidelines?)|"
    r"submission\s*of\s*applications?|deadline\s*(?:and|&)?\s*(?:how\s*)?to\s*apply)\b", re.I)

JWT_BODY_CUT_MARKERS = (
    "related jobs", "leave your thoughts", "you must be logged in", "email me jobs like these",
    "send to a friend", "company information", "leave a reply", "post a comment",
    "never miss a job update", "subscribe", "apply for this job", "upload resumé",
    "upload cover letter",
)
JWT_BODY_DROP_LINES = {
    "apply for this job", "save", "share", "share:", "bookmark job", "quick view",
    "send to friend", "send to a friend", "clear all", "filter", "view more", "name *",
    "email *", "message *", 'is fire "hot" or "cold"?',
    "upload resumé (pdf, doc, docx, zip, txt, rtf)",
    "upload cover letter (pdf, doc, docx, zip, txt, rtf)",
}
JWT_NAV_LINES = {"home", "about us", "contact", "privacy policy", "login/register", "submit a job",
                 "submit job", "register", "my dashboard", "linkedin page", "search", "save",
                 "share", "bookmark job"}

JWT_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}
JWT_TEXT_DATE_RE = re.compile(
    r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*[/\s,]\s*([A-Za-z]+)\s*[/,]?\s*(\d{4})", re.I)
JWT_DMY_DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
JWT_DEADLINE_LABELS = ("application deadline", "closing date", "deadline", "expiry date",
                       "expires", "application closing date", "closing date for application",
                       "last date")

JWT_TENDER_RE = re.compile(
    r"\b(?:rfq|rfp|reoi|eoi|itb|itt|spn|rfb|rfa|gpn|ifb|rfi)\b"
    r"|invitation\s+to\s+(?:bid|tender)|invitation\s+for\s+bids?"
    r"|request\s+for\s+(?:quotation|proposal|proposals|expression|expressions|bids?)"
    r"|expressions?\s+of\s+interest|\btenders?\b|procurement\s+notice"
    r"|specific\s+procurement|general\s+procurement"
    r"|call\s+for\s+(?:bid|bids|tender|tenders|proposal|proposals|expression|expressions|quotation)"
    r"|matching\s+grant|terms\s+of\s+reference|prior\s+notice\s+of\s+procurement", re.I)

JWT_CATEGORY_NOISE_RE = re.compile(
    r"\s*jobs?\s+in\s+tanzania\b.*$|\s*jobs?\s+in\s+addis\s+ababa\b.*$"
    r"|\s*jobs?\s+in\s+[a-z\s]+\b$|\s*jobs?\s*$", re.I)


def jwt_clean_category(raw):
    cleaned = JWT_CATEGORY_NOISE_RE.sub("", (raw or "").strip()).strip()
    return cleaned if cleaned else (raw or "").strip()


def jwt_clean_location(raw):
    loc = (raw or "").strip()
    loc = re.sub(r"\s+jobs?\s*$", "", loc, flags=re.I).strip()
    return re.sub(r"\s+jobs?\s+in\s+.+$", "", loc, flags=re.I).strip()


def jwt_fix_text(text):
    """Remove odd characters (non-breaking spaces, zero-width marks, control chars)."""
    for bad, good in (("\u00a0", " "), ("\u200b", ""), ("\ufeff", ""), ("Â", "")):
        text = text.replace(bad, good)
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)


def jwt_strip_tracking(url):
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in JWT_TRACKING_EXACT]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def jwt_is_real_apply_url(href):
    """True only for a genuine external application link (not social / newsletter / feed)."""
    low = (href or "").lower().strip()
    if not low.startswith("http"):
        return False
    try:
        p = urlsplit(low)
    except Exception:
        return False
    host = p.netloc
    if host == "x.com" or host.endswith(".x.com"):
        return False
    if any(s in host for s in JWT_NON_APPLY_HOSTS):
        return False
    path_query = p.path + ("?" + p.query if p.query else "")
    return not any(s in path_query for s in JWT_NON_APPLY_PATHS)


# ---- Cloudflare-protected emails -------------------------------------------
def jwt_cf_decode(encoded):
    try:
        data = bytes.fromhex(encoded)
        return "".join(chr(b ^ data[0]) for b in data[1:])
    except Exception:
        return ""


def jwt_decode_cf_emails(soup):
    """Replace Cloudflare '[email protected]' placeholders with the real address."""
    for el in soup.find_all(attrs={"data-cfemail": True}):
        decoded = jwt_cf_decode(el.get("data-cfemail", ""))
        if decoded:
            el.replace_with(decoded)
    for a in soup.find_all("a", href=re.compile(r"email-protection")):
        m = re.search(r"email-protection#([0-9a-fA-F]+)", a.get("href", ""))
        decoded = jwt_cf_decode(m.group(1)) if m else ""
        if decoded:
            a.replace_with(decoded)


def jwt_get_soup(url):
    soup = get_soup(url)
    jwt_decode_cf_emails(soup)
    return soup


# ---- dates -----------------------------------------------------------------
def jwt_dmy_dates(text):
    out = []
    for d, m, y in JWT_DMY_DATE_RE.findall(text or ""):
        try:
            out.append(datetime(int(y), int(m), int(d)).strftime("%Y-%m-%d"))
        except ValueError:
            pass
    return out


def jwt_text_dates(text):
    out = []
    for d, mon, y in JWT_TEXT_DATE_RE.findall(text or ""):
        month = JWT_MONTHS.get(mon.lower())
        if not month:
            continue
        try:
            out.append(datetime(int(y), month, int(d)).strftime("%Y-%m-%d"))
        except ValueError:
            pass
    return out


def jwt_parse_any_date(text):
    ds = jwt_dmy_dates(text) or jwt_text_dates(text)
    return ds[-1] if ds else ""


# ---- collecting job links ---------------------------------------------------
def jwt_norm_url(href):
    if not href:
        return ""
    p = urlsplit(urljoin(JWT_BASE + "/", href))
    if not p.netloc.lower().endswith("jobwebtanzania.com"):
        return ""
    path = p.path if p.path.endswith("/") else p.path + "/"
    return urlunsplit(("https", p.netloc.lower(), path, "", ""))


def jwt_is_job_path(path):
    """/jobs/<slug>/  (skips /jobs/page/N)."""
    parts = [s for s in path.split("/") if s]
    return len(parts) == 2 and parts[0] == "jobs" and parts[1].lower() != "page"


def jwt_page_url(n):
    if n <= 1:
        return JWT_JOBS_URL
    return f"{JWT_JOBS_URL.rstrip('/')}/page/{n}/"


def jwt_collect_links(seen_urls):
    """Newest-first list of jwt job URLs not scraped yet (at most JWT_MAX_JOBS)."""
    print(f"\n  Collecting job links from {JWT_JOBS_URL}")
    ordered, seen_now, empty_streak = [], set(), 0
    for page in range(1, JWT_MAX_PAGES + 1):
        try:
            soup = get_soup(jwt_page_url(page))
        except Exception as e:
            print(f"  Page {page}: {e} - stopping.")
            break
        links = []
        for a in soup.find_all("a", href=True):
            u = jwt_norm_url(a["href"])
            if u and jwt_is_job_path(urlsplit(u).path) and u not in seen_now:
                seen_now.add(u)
                links.append(u)
        known = sum(1 for u in links if u in seen_urls)
        print(f"  Page {page}: {len(links)} job(s), {known} already scraped")

        if not links:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
            ordered.extend(u for u in links if u not in seen_urls)
            if JWT_STOP_ON_KNOWN and known == len(links):
                print("  Whole page already scraped, stopping.")
                break
            if JWT_MAX_JOBS and len(ordered) >= JWT_MAX_JOBS:
                ordered = ordered[:JWT_MAX_JOBS]
                print(f"  Reached JWT_MAX_JOBS ({JWT_MAX_JOBS}).")
                break
        time.sleep(JWT_DELAY)
    return ordered


# ---- parsing one job page ---------------------------------------------------
def jwt_block_to_text(el):
    """Readable text of one block (bullets for list items, line breaks for paragraphs)."""
    if el is None:
        return ""
    for br in el.find_all("br"):
        br.replace_with("\n")
    for li in el.find_all("li"):
        li.replace_with("\n• " + li.get_text(" ", strip=True) + "\n")
    for tag in el.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr"]):
        tag.insert_before("\n")
        tag.insert_after("\n")
    text = jwt_fix_text(el.get_text("\n"))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def jwt_content_div(soup):
    """(content div, the <ul> with company / location / category / type)."""
    main = soup.find(id="mainContent")
    if main:
        single = main.find("div", class_=lambda c: c and "single" in c.split())
        if single:
            kids = [c for c in single.children if getattr(c, "name", None) in ("div", "section")]
            div = kids[1] if len(kids) >= 2 else (kids[0] if kids else None)
            if div:
                return div, div.find("ul")
    for sel in ("div.single-job-content", "div.job-description", "div#job-detail",
                "div.entry-content", "div.noo-job-content", "article", "main"):
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 100:
            return el, el.find("ul")
    return soup.body or soup, None


def jwt_body_blocks(content_div):
    """[(tag, text)] top-level text blocks of the job body (meta <ul> left out)."""
    blocks, added = [], set()
    first_ul = content_div.find("ul")
    skip = {id(first_ul)} if first_ul else set()
    for child in content_div.descendants:
        if not getattr(child, "name", None):
            continue
        if child.name not in ("font", "p", "div", "h2", "h3", "h4", "h5", "h6",
                              "blockquote", "span", "b", "strong"):
            continue
        if any(id(p) in skip or id(p) in added for p in child.parents):
            continue
        text = child.get_text(" ", strip=True)
        if len(text) < 5 or text.lower() in JWT_BODY_DROP_LINES:
            continue
        added.add(id(child))
        blocks.append((child, text))
    return blocks


def jwt_is_apply_heading(line):
    s = line.strip().lstrip("•*-–—#:. ").strip()
    return bool(s) and len(s.split()) <= 9 and bool(JWT_APPLY_HEAD_RE.match(s))


def jwt_split_at_apply(blocks):
    for i, (_tag, text) in enumerate(blocks):
        if jwt_is_apply_heading(text):
            return blocks[:i], blocks[i:]
    return blocks, []


def jwt_apply_details(apply_text, content_div):
    """(email, url) of a PUBLIC way to apply, or ('', '')."""
    email = url = ""
    for a in content_div.find_all("a", href=True):                 # mailto: links first
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            cand = href[7:].split("?")[0].strip()
            if ok_email(cand):
                email = cand
                break
    if not email:
        for m in EMAIL_RE.finditer(apply_text or ""):
            if ok_email(m.group(0).rstrip(".,;")):
                email = m.group(0).rstrip(".,;")
                break
    for a in content_div.find_all("a", href=True):                 # then external links
        href = a["href"].strip()
        if jwt_is_real_apply_url(href):
            url = jwt_strip_tracking(href)
            break
    if not url:
        for u in URL_RE.findall(apply_text or ""):
            u = u.rstrip(".,);")
            if jwt_is_real_apply_url(u):
                url = jwt_strip_tracking(u)
                break
    return email, url


def jwt_scrape_job(url):
    soup = jwt_get_soup(url)

    # ---- title (+ company when the title is "Role at Company") ----
    h1 = (soup.select_one("div.section_header h1") or soup.select_one("h1.job-title")
          or soup.select_one("h1.entry-title") or soup.find("h1"))
    title = clean(jwt_fix_text(txt(h1)))
    company_from_h1 = ""
    parts = re.split(r"\s+at\s+", title, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        title, company_from_h1 = parts[0].strip(), parts[1].strip()
    title = re.sub(r"\s*\d[\d,]*\s*views?\s*$", "", title, flags=re.I).strip()

    content_div, meta_ul = jwt_content_div(soup)

    # ---- company / location / category / job type from the meta list ----
    company = ""
    location = ""
    raw_type = ""
    categories = []
    if meta_ul:
        for li in meta_ul.find_all("li", recursive=False):
            li_text = li.get_text(" ", strip=True)
            low = li_text.lower()
            a = li.find("a")
            a_text = a.get_text(" ", strip=True) if a else ""
            a_href = a.get("href", "") if a else ""
            if low.startswith("company") or "/company/" in a_href:
                company = a_text or re.sub(r"^company\s*:?\s*", "", li_text, flags=re.I).strip()
            elif "/job-location/" in a_href or low.startswith("location"):
                location = jwt_clean_location(
                    a_text or re.sub(r"^location\s*:?\s*", "", li_text, flags=re.I))
            elif "/job-category/" in a_href or low.startswith("categor"):
                categories.append(jwt_clean_category(a_text or li_text))
            elif "/job-type/" in a_href or low.startswith("job type") or low.startswith("type"):
                raw_type = a_text or re.sub(r"^(?:job\s+)?type\s*:?\s*", "", li_text,
                                            flags=re.I).strip()
            elif low.startswith("state") or "/job-state/" in a_href:
                state = jwt_clean_location(a_text or re.sub(r"^state\s*:?\s*", "", li_text,
                                                            flags=re.I))
                location = state or location
    company = company or company_from_h1 or "Unknown"
    if not location:                                   # fall back to any location link on the page
        for a in soup.find_all("a", href=True):
            if "/job-location/" in a["href"]:
                location = jwt_clean_location(a.get_text(" ", strip=True))
                break
    location = location or "Tanzania"
    if not raw_type:
        for a in soup.find_all("a", href=True):
            if "/job-type/" in a["href"]:
                raw_type = a.get_text(" ", strip=True)
                break
    categories = [c for c in dict.fromkeys(categories) if c]

    # ---- body: split description / "how to apply" ----
    blocks = jwt_body_blocks(content_div)
    desc_blocks, apply_blocks = jwt_split_at_apply(blocks)
    apply_text = "\n".join(t for _, t in apply_blocks)

    # apply info must be read BEFORE jwt_block_to_text() changes the tree
    apply_email, apply_url = jwt_apply_details(apply_text, content_div)

    seen_lines, desc_lines, stop = set(), [], False
    for tag, _t in desc_blocks:
        if stop:
            break
        for ln in jwt_block_to_text(tag).split("\n"):
            ln = ln.strip()
            low = ln.lower()
            if not ln or low in JWT_BODY_DROP_LINES:
                continue
            if any(low.startswith(m) for m in JWT_BODY_CUT_MARKERS):
                stop = True
                break
            if ln in seen_lines:
                continue
            seen_lines.add(ln)
            desc_lines.append(ln)
    description = re.sub(r"\n{3,}", "\n\n", "\n".join(desc_lines)).strip()

    if len(description.split()) < 20:                  # block detection failed: use the whole div
        lines = [l.strip() for l in jwt_block_to_text(content_div).split("\n") if l.strip()]
        start = 0
        for i, ln in enumerate(lines):
            low = ln.lower()
            if low in JWT_NAV_LINES or re.match(r"^\d+\s*jobs?\s*$", low) \
                    or re.match(r"^\d{1,2}\s+\w+\s+\d{4}$", ln):
                continue
            if len(ln.split()) >= 4:
                start = i
                break
        lines = lines[start:]
        cut = len(lines)
        for i, ln in enumerate(lines):
            if jwt_is_apply_heading(ln):
                cut = i
                apply_text = "\n".join(lines[i:])
                break
            if any(ln.lower().startswith(m) for m in JWT_BODY_CUT_MARKERS):
                cut = i
                break
        description = "\n".join(lines[:cut]).strip()
        if not (apply_email or apply_url):
            apply_email, apply_url = jwt_apply_details(apply_text, content_div)

    # ---- logo ----
    logo = ""
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content") and "jobwebethio" not in og["content"].lower():
        logo = og["content"].strip()
    if not logo:
        img = soup.select_one("div.company-logo img") or soup.select_one("img.company-logo")
        logo = img.get("src", "").strip() if img else ""

    # ---- dates ----
    date_posted = ""
    date_el = soup.select_one("span.date") or soup.select_one("time")
    if date_el:
        raw = date_el.get_text(" ", strip=True)
        ds = jwt_text_dates(raw) or jwt_dmy_dates(raw)
        date_posted = ds[0] if ds else ""
    date_posted = date_posted or datetime.now().strftime("%Y-%m-%d")

    deadline = ""
    page_text = soup.get_text("\n")
    for lab in JWT_DEADLINE_LABELS:
        m = re.search(rf"{re.escape(lab)}\s*[:\-]?\s*([^\n<]{{3,60}})", page_text, re.I)
        if m:
            deadline = jwt_parse_any_date(m.group(1))
            if deadline:
                break
    if not deadline and apply_text:
        ds = jwt_text_dates(apply_text) or jwt_dmy_dates(apply_text)
        deadline = sorted(ds)[-1] if ds else ""
    estimated_deadline = "" if deadline else estimated_deadline_for(date_posted)

    # ---- company website (a link labelled "Website") ----
    website = ""
    for a in soup.find_all("a", href=True):
        if a.get_text(" ", strip=True).lower() in ("website", "company website") \
                and ok_url(a["href"].strip()):
            website = a["href"].strip()
            break

    field = ", ".join(categories[:2]) or li_infer_job_field(title, description)
    return {
        "title": title, "company": company, "location": location,
        "job_type": raw_type, "job_type_mapped": map_job_type(raw_type),
        "category_text": " ".join(categories).lower(), "job_field": field,
        "date_posted": date_posted, "deadline": deadline,
        "estimated_deadline": estimated_deadline,
        "salary": extract_salary(description),
        "qualification": li_qualification_tier(description) or extract_qualification(description),
        "experience": extract_experience(description),
        "apply_email": apply_email, "apply_url": apply_url,
        "description": description, "company_about": "", "company_industry": "",
        "company_founded": "", "company_type": "", "company_website": website,
        "company_address": location, "logo": abs_url(logo, JWT_BASE),
        "job_url": jwt_norm_url(url),
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


def jwt_skip_reason(job):
    if JWT_SKIP_TENDERS and (JWT_TENDER_RE.search(job["title"])
                             or "tender" in job.get("category_text", "")
                             or "notice" in job.get("category_text", "")):
        return "tender / public notice"
    if JWT_REQUIRE_PUBLIC_APPLY and not (job["apply_email"] or job["apply_url"]):
        return "no public apply email or link"
    return ""


# ============================================================================
#  SOURCE 4: LINKEDIN  (public "guest" job pages, no login needed)
# ============================================================================
LI_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]
_li_ua_idx = 0


def li_headers():
    global _li_ua_idx
    ua = LI_USER_AGENTS[_li_ua_idx % len(LI_USER_AGENTS)]
    _li_ua_idx += 1
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
        "X-Li-Lang": "en_US",
        "X-Requested-With": "XMLHttpRequest",
    }


def li_fetch(url, retries=3, pause=0.0):
    """Text of a LinkedIn page, or None when blocked / missing / empty."""
    for attempt in range(retries):
        try:
            time.sleep(pause + attempt * 2)
            r = requests.get(url, headers=li_headers(), allow_redirects=True, timeout=25)
            if r.status_code == 429:
                wait = 60 + attempt * 60
                print(f"  LinkedIn rate limit (429), waiting {wait}s ...")
                time.sleep(wait)
                continue
            if r.status_code in (400, 403, 999):
                print(f"  LinkedIn blocked this request ({r.status_code}).")
                return None
            if r.status_code != 200:
                return None
            return decode_response(r).strip() or None
        except Exception:
            time.sleep(3 + attempt * 3)
    return None


# ---- LinkedIn URL / apply-link / email cleaning ----------------------------
def li_is_linkedin_url(v):
    return bool(v) and bool(re.search(r"linkedin\.com", v, re.I))


def li_blank(v):
    return "" if li_is_linkedin_url(v) else v


LI_BAD_DOMAINS = ["linkedin.com", "google.com", "youtube.com", "facebook.com", "twitter.com",
                  "x.com", "instagram.com", "t.co", "example.com", "w3.org", "sentry.io",
                  "schema.org"]


def li_is_bad_url(url):
    if not url or not url.startswith("http"):
        return True
    return any(d in url.lower() for d in LI_BAD_DOMAINS)


LI_BAD_APPLY_URL_RE = re.compile(
    r"/(privacy|terms|legal|cookie|gdpr|policy|disclaimer|impressum|datenschutz|confidentialite"
    r"|mentions-legales|cgu|cgv|login|signin|sign-in|register|signup|sign-up|auth"
    r"|unsubscribe|optout|opt-out)[/\?#]?", re.I)
LI_NOISE_EMAIL_DOMAINS = ["example.com", "sentry.io", "google.com", "w3.org", "schema.org",
                          "wixpress.com", "squarespace.com"]
LI_FAKE_LOCAL_RE = re.compile(
    r"^(name|user|email|mail|yourname|your[-_.]?email|sample|test|info|hello"
    r"|noreply|no[-_.]?reply|admin|webmaster|support|contact|example)$", re.I)
LI_FAKE_DOMAIN_RE = re.compile(
    r"^(domain|example|yoursite|yourdomain|yourbrand|company|mycompany"
    r"|website|yourcompany|mysite|placeholder|site)\.[a-z]{2,}$", re.I)
LI_EMAIL_TLDS = [
    "com", "net", "org", "gov", "edu", "info", "biz", "co", "tz", "ke", "ug", "rw", "bi", "zm",
    "mw", "mz", "za", "et", "cd", "sa", "ae", "uk", "us", "in", "eg", "jo", "qa", "kw", "bh",
    "om", "ye", "iq", "lb", "sy", "ma", "tn", "dz", "ly", "sd", "mu", "mr", "pk", "tr", "de",
    "fr", "it", "es", "nl", "ch", "io", "app", "tech", "online", "store", "site", "me", "name",
    "mobi"]
LI_TLD_RE_PART = "|".join(sorted(set(LI_EMAIL_TLDS), key=len, reverse=True))


def li_decode_entities(s):
    if not s:
        return ""
    for old, new in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                     ("&#39;", "'"), ("\\u0026", "&"), ("\\u003D", "="), ("\\u003A", ":"),
                     ("\\u002F", "/")]:
        s = s.replace(old, new)
    return s


def li_clean_email(raw):
    if not raw:
        return ""
    em = re.sub(r"^mailto:", "", raw, flags=re.I)
    em = re.sub(r"\?.*$", "", em)
    for pat, rep in [(r"\\u003[Ee]", ""), (r"\\u003[Cc]", ""), (r"\\u0040", "@"),
                     (r"\\u002[Ee]", "."), (r"\\u0026", ""), (r"u003[Ee]", ""),
                     (r"u003[Cc]", ""), (r"u0040", "@"), (r"&amp;", ""), (r"&lt;", ""),
                     (r"&gt;", ""), (r"&#64;", "@"), (r"&#46;", "."), (r"&nbsp;", ""),
                     (r"%40", "@"), (r"%2[Ee]", "."), (r"%20", ""), (r"[>]+$", ""),
                     (r"[<]+$", "")]:
        em = re.sub(pat, rep, em, flags=re.I)
    em = em.strip().lower()
    if not em or "@" not in em or "." not in em or not re.match(r"^[a-zA-Z0-9]", em):
        return ""
    at = em.rfind("@")
    local, domain = em[:at], em[at + 1:]
    if ".mu" in domain:
        domain = re.sub(r"\.mu.*", ".mu", domain, flags=re.I)
    elif ".uk" in domain:
        domain = re.sub(r"\.uk.*", ".uk", domain, flags=re.I)
    elif re.search(r"\.tz(?![a-z])", domain):          # company.co.tz / agency.go.tz
        domain = re.sub(r"(\.tz)(?![a-z]).*$", r"\1", domain, flags=re.I)
    else:
        m = re.search(rf"^([a-z0-9.\-]+\.(?:{LI_TLD_RE_PART}))(?:[a-z0-9].*)?$", domain, re.I)
        if m:
            domain = m.group(1)
        else:
            domain = re.sub(r"(\.[a-z]{2,6})[a-z0-9\-_/?#+]*$", r"\1", domain, flags=re.I)
    em = local + "@" + domain
    if "@" not in em or "." not in em or not re.match(r"^[a-zA-Z0-9]", em):
        return ""
    return em


def li_clean_application_link(raw):
    if not raw:
        return ""
    raw = raw.strip()
    if li_is_linkedin_url(raw):
        return ""
    if "@" in raw and not raw.startswith("http"):
        return li_clean_email(raw)
    if raw.startswith("http"):
        url = raw
        if LI_BAD_APPLY_URL_RE.search(url):
            return ""
        if ".mu" in url.lower():
            def mu_replace(m):
                tld, path = m.group(1), m.group(2) or ""
                if path and re.match(r"^/[a-z0-9\-/]+$", path, re.I):
                    return tld + path
                return tld
            url = re.sub(r"(\.mu)(\/[^\s]*)?$", mu_replace, url, flags=re.I)
        url = re.sub(r"#.*$", "", url)
        url = re.sub(r"(subject|applysubject|refno|applyref|applyhere|clickhere|applynow)(\?.*)?$",
                     "", url, flags=re.I)
        url = re.sub(r"[.,;:!?)]+$", "", url)
        return url.strip()
    return raw


def li_extract_email(text):
    if not text:
        return ""
    for raw_em in EMAIL_RE.findall(text):
        em = li_clean_email(raw_em)
        if not em or "@" not in em:
            continue
        local, _, domain = em.partition("@")
        if any(d in em for d in LI_NOISE_EMAIL_DOMAINS):
            continue
        if LI_FAKE_LOCAL_RE.match(local) or LI_FAKE_DOMAIN_RE.match(domain):
            continue
        return em
    return ""


def li_decode_apply_url(raw):
    """LinkedIn wraps external apply links (?url=... or ?offsiteApplyUrl=<base64 json>)."""
    if not raw:
        return ""
    raw = li_decode_entities(raw)
    if raw.startswith("http") and "linkedin.com" not in raw:
        return raw
    m = re.search(r"[?&]url=([^&]+)", raw)
    if m:
        try:
            d = unquote(m.group(1))
            if "%" in d:
                d = unquote(d)
            if d.startswith("http") and "linkedin.com" not in d:
                return d
        except Exception:
            pass
    b64m = re.search(r"[?&]offsiteApplyUrl=([^&]+)", raw)
    if b64m:
        try:
            p = json.loads(base64.b64decode(unquote(b64m.group(1))).decode("utf-8"))
            if p and "url" in p:
                return p["url"]
        except Exception:
            pass
    return ""


def li_apply_button(soup):
    for tag in soup.find_all("a", href=True):
        ctrl = tag.get("data-tracking-control-name", "").lower()
        if "offsite" in ctrl or "apply" in ctrl:
            r = li_decode_apply_url(tag["href"])
            if r and not li_is_bad_url(r):
                return r
    for tag in soup.find_all("a", href=True):
        href, text = tag["href"], tag.get_text().lower()
        if ("apply" in text or "/apply" in href) and "linkedin.com" not in href:
            if href.startswith("http") and not li_is_bad_url(href):
                return href
    return ""


def li_clean_logo(raw):
    if not raw:
        return ""
    raw = li_decode_entities(raw).strip()
    if not raw.startswith("http"):
        return ""
    return re.sub(r"[\"')\s]+$", "", raw)


def li_is_placeholder_logo(url):
    if not url:
        return True
    l = url.lower()
    return any(k in l for k in ["ghost", "placeholder", "default-company-logo", "favicon",
                                "static.licdn.com/aero-v1/sc/h/9c8pery4andzj6ohjkjp54ms4",
                                "static.licdn.com/scds/common/u/images/logos/favicons"])


LI_BAD_COMPANY_NAMES = {"sign in", "linkedin", "join now", "log in", "login", "join linkedin",
                        "welcome back", ""}


def li_bad_company_name(name):
    if not name:
        return True
    n = name.strip().lower()
    return n in LI_BAD_COMPANY_NAMES or ("linkedin" in n and len(n) < 30)


def li_strip_tracking(url):
    if not url:
        return ""
    url = li_decode_entities(url)
    m = re.search(r"[?&]url=([^&]+)", url)
    if m:
        try:
            d = unquote(m.group(1))
            if "%" in d:
                d = unquote(d)
            if d.startswith("http") and "linkedin.com" not in d:
                return d
        except Exception:
            pass
    return url


# ---- dates (English + Swahili relative dates) ------------------------------
LI_SW_UNIT_MAP = {"saa": "hour", "siku": "day", "wiki": "week", "mwezi": "month",
                  "miezi": "month", "mwaka": "year", "miaka": "year"}
LI_SW_UNITS_RE = r"(saa|siku|wiki|mwezi|miezi|mwaka|miaka)"
LI_SW_AGO_RE = r"(?:zilizopita|iliyopita|uliopita)"
LI_MONTH_MAP = {"jan": 0, "feb": 1, "mar": 2, "apr": 3, "may": 4, "jun": 5,
                "jul": 6, "aug": 7, "sep": 8, "oct": 9, "nov": 10, "dec": 11}


def li_normalise_date_text(text):
    """'siku 3 zilizopita' / 'jana' / 'leo' -> '3 day ago' / '1 day ago' / '0 days ago'."""
    if not text:
        return ""
    m = re.search(rf"{LI_SW_UNITS_RE}\s+(\d+)\s+{LI_SW_AGO_RE}", text, re.I)
    if m and LI_SW_UNIT_MAP.get(m.group(1).lower()):
        return f"{m.group(2)} {LI_SW_UNIT_MAP[m.group(1).lower()]} ago"
    m = re.search(rf"(\d+)\s+{LI_SW_UNITS_RE}\s+{LI_SW_AGO_RE}", text, re.I)
    if m and LI_SW_UNIT_MAP.get(m.group(2).lower()):
        return f"{m.group(1)} {LI_SW_UNIT_MAP[m.group(2).lower()]} ago"
    if re.match(r"^\s*jana\s*$", text, re.I):
        return "1 day ago"
    if re.search(r"\bleo\b|today", text, re.I):
        return "0 days ago"
    return text


def li_resolve_posted_date(raw):
    """'2026-10-20' or '3 days ago' -> YYYY-MM-DD (today when it can't be read)."""
    today = datetime.now().strftime("%Y-%m-%d")
    if not raw:
        return today
    text = li_normalise_date_text(raw)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text.strip()):
        return text.strip()
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d")
    except Exception:
        pass
    base = datetime.now()
    m = re.search(r"(\d+)\s*(hour|day|week|month|year)", text, re.I)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        if "hour" in unit:
            base -= timedelta(hours=n)
        elif "day" in unit:
            base -= timedelta(days=n)
        elif "week" in unit:
            base -= timedelta(weeks=n)
        elif "month" in unit:
            base -= timedelta(days=30 * n)
        elif "year" in unit:
            base -= timedelta(days=365 * n)
        return base.strftime("%Y-%m-%d")
    return today


def li_try_parse_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%B %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except Exception:
            pass
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s)
    if m:
        mon = LI_MONTH_MAP.get(m.group(2)[:3].lower())
        if mon is not None:
            return datetime(int(m.group(3)), mon + 1, int(m.group(1)))
    return None


LI_DEADLINE_PATTERNS = [
    r"closes?\s+on\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    r"closes?\s+on\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    r"apply\s+by\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    r"apply\s+by\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    r"applications?\s+close[sd]?\s*(?:on)?\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    r"deadline[:\s]+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    r"deadline[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
    r"closing\s+date[:\s]+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
    r"closing\s+date[:\s]+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
    r"on\s+or\s+before\s+(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+,?\s+\d{4})",
    r"not\s+later\s+than\s+(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+,?\s+\d{4})",
]


def li_parse_deadline(text):
    """A closing date written in the job text (only if it is still in the future)."""
    now = datetime.now()
    for p in LI_DEADLINE_PATTERNS:
        m = re.search(p, text, re.I)
        if m:
            raw_date = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", m.group(1)).strip()
            d = li_try_parse_date(raw_date) or li_try_parse_date(raw_date.replace(",", ""))
            if d and d > now:
                return d.strftime("%Y-%m-%d")
    return ""


# ---- JSON-LD + company data embedded in the job page -----------------------
def li_salary_jsonld(obj):
    if not obj:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        val, currency = obj.get("value", {}), obj.get("currency", "")
        if isinstance(val, dict):
            parts = [str(x) for x in (val.get("minValue", ""), val.get("maxValue", "")) if x]
            return f"{currency} {' - '.join(parts)} {val.get('unitText', '')}".strip()
        return f"{currency} {val}".strip()
    return ""


def li_location_jsonld(obj):
    if not obj:
        return ""
    if isinstance(obj, list):
        obj = obj[0] if obj else {}
    if not isinstance(obj, dict):
        return str(obj)
    addr = obj.get("address", {})
    if isinstance(addr, dict):
        return ", ".join(filter(None, [addr.get("addressLocality", ""),
                                       addr.get("addressRegion", ""),
                                       addr.get("addressCountry", "")]))
    return str(addr)


def li_address_jsonld(obj):
    if not obj:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return ", ".join(filter(None, [obj.get("streetAddress", ""), obj.get("addressLocality", ""),
                                       obj.get("addressRegion", ""), obj.get("postalCode", ""),
                                       obj.get("addressCountry", "")]))
    return ""


def li_logo_of(org):
    logo = org.get("logo", "")
    if isinstance(logo, str):
        return li_clean_logo(logo)
    if isinstance(logo, dict):
        return li_clean_logo(logo.get("url", ""))
    return ""


def li_parse_jsonld(html):
    result = {}
    for raw in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                          html, re.S | re.I):
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict)), {})
        if not isinstance(data, dict):
            continue
        schema_type = data.get("@type", "")
        if schema_type == "JobPosting":
            org = data.get("hiringOrganization", {}) or {}
            result.update({
                "date_posted": data.get("datePosted", ""),
                "salary": li_salary_jsonld(data.get("baseSalary", {})),
                "company_name": org.get("name", ""),
                "company_logo": li_logo_of(org),
                "company_website": org.get("sameAs", "") or org.get("url", ""),
                "apply_url": (data.get("url", "")
                              or (data.get("applicationContact", {}) or {}).get("url", "")),
                "location": li_location_jsonld(data.get("jobLocation", {})),
            })
        elif schema_type in ("Organization", "Corporation", "LocalBusiness"):
            result.update({
                "company_name": data.get("name", ""),
                "company_logo": li_logo_of(data),
                "company_website": data.get("sameAs", "") or data.get("url", ""),
                "company_industry": data.get("industry", ""),
                "company_founded": str(data.get("foundingDate", "") or ""),
                "company_address": li_address_jsonld(data.get("address", {})),
                "company_about": data.get("description", ""),
            })
    return result


def li_company_from_job_page(html, soup, ld):
    result = {k: v for k, v in ld.items() if v}

    def _meta(name):
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        return (tag.get("content", "") if tag else "").strip()

    if _meta("og:image") and not result.get("company_logo"):
        result["company_logo"] = li_clean_logo(_meta("og:image"))

    def _sel(*selectors):
        for s in selectors:
            el = soup.select_one(s)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        return ""

    if not result.get("company_name"):
        result["company_name"] = _sel(".topcard__org-name-link",
                                      ".job-details-jobs-unified-top-card__company-name",
                                      ".topcard__flavor")
    if not result.get("company_logo"):
        for sel in [".artdeco-entity-image", "img.company-logo",
                    ".jobs-unified-top-card__company-logo img", ".topcard__logo img"]:
            img = soup.select_one(sel)
            if img:
                src = li_clean_logo(img.get("src", "") or img.get("data-delayed-url", "")
                                    or img.get("data-ghost-url", ""))
                if src and "ghost" not in src.lower() and "placeholder" not in src.lower():
                    result["company_logo"] = src
                    break
    for chip in soup.select(".job-details-jobs-unified-top-card__job-insight,"
                            ".jobs-unified-top-card__job-insight"):
        text = chip.get_text(strip=True)
        if not result.get("company_industry") and re.search(
                r"technology|consulting|financial|healthcare|education|manufacturing|retail|media|"
                r"energy|real estate|construction|automotive|telecom|pharmaceutical|mining|"
                r"agriculture|tourism|hospitality|non-profit", text, re.I):
            result["company_industry"] = text
    for script in soup.find_all("script"):
        s = script.string or ""
        if not s.strip():
            continue
        if not result.get("company_website"):
            for pat in [r'"companyPageUrl"\s*:\s*"([^"]+)"', r'"companyUrl"\s*:\s*"([^"]+)"',
                        r'"websiteUrl"\s*:\s*"([^"]+)"']:
                m = re.search(pat, s)
                if m:
                    u = li_strip_tracking(li_decode_entities(m.group(1)))
                    if u.startswith("http") and "linkedin.com" not in u:
                        result["company_website"] = u
                        break
    return result


def li_company_details(company_url):
    """LinkedIn company page (guest view): name, industry, HQ, type, founded, website, logo, about."""
    empty = {"name": "", "industry": "", "headquarters": "", "type": "", "founded": "",
             "website": "", "logo": "", "about": "", "company_url": ""}
    if not company_url:
        return dict(empty)
    base_url = re.sub(r"\?.*$", "", company_url.rstrip("/"))
    guest_url = base_url.replace("https://www.linkedin.com/company/",
                                 "https://www.linkedin.com/company-guest/")
    html = li_fetch(guest_url, pause=0.5) if guest_url != base_url else None
    if not html:
        html = li_fetch(base_url, pause=1.0)
    if not html:
        out = dict(empty)
        out["company_url"] = base_url
        return out

    soup = BeautifulSoup(html, "html.parser")
    ld = li_parse_jsonld(html)

    def _sel(*selectors):
        for s in selectors:
            el = soup.select_one(s)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        return ""

    def _detail(label):
        lower = label.lower()
        for div in soup.select("section.core-section-container dl > div"):
            dt = div.find("dt")
            if dt and lower in dt.get_text().strip().lower():
                dd = div.find("dd")
                if dd:
                    return dd.get_text(strip=True)
        for row in soup.select(".org-page-details__definition-list dt, .about-us__basicInfo dt"):
            if lower in row.get_text().strip().lower():
                dd = row.find_next_sibling("dd")
                if dd:
                    return dd.get_text(strip=True)
        return ""

    og = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
    raw_logo = (og.get("content", "") if og else "") or ld.get("company_logo", "")
    if not raw_logo:
        for img in soup.select("img.org-top-card-primary-content__logo, img.artdeco-entity-image"):
            src = img.get("src", "") or img.get("data-delayed-url", "")
            if src and "ghost" not in src.lower():
                raw_logo = src
                break
    ws_tag = soup.select_one("a[data-tracking-control-name='about_website']")
    raw_ws = (ws_tag.get("href", "") if ws_tag else "") or _detail("Website") \
        or ld.get("company_website", "")
    website = li_blank(li_decode_apply_url(raw_ws) or raw_ws)

    name = ld.get("company_name", "") or _sel("h1.org-top-card-summary__title", "h1", "title")
    if " | LinkedIn" in name:
        name = name.split(" | ")[0].strip()
    about = ld.get("company_about", "") or _sel(
        "section.about-us p", ".core-section-container__content p",
        ".org-about-us-organization-description__text", ".org-about-module__description")
    return {
        "name": name, "industry": _detail("Industry") or ld.get("company_industry", ""),
        "headquarters": _detail("Headquarters") or ld.get("company_address", ""),
        "type": _detail("Type"), "founded": _detail("Founded") or ld.get("company_founded", ""),
        "website": website, "logo": li_clean_logo(raw_logo), "about": about,
        "company_url": base_url,
    }


# ---- job criteria / field / qualification helpers --------------------------
def li_criteria(soup, label):
    lower = label.lower()
    for li in soup.select(".description__job-criteria-list > li"):
        h3 = li.find("h3")
        if h3 and lower in h3.get_text().strip().lower():
            spans = li.select(".description__job-criteria-text, span")
            if spans:
                return spans[-1].get_text(strip=True)
    meta_map = {"employment type": soup.find("meta", {"name": "employmentType"}),
                "seniority level": soup.find("meta", {"name": "seniorityLevel"}),
                "industries": soup.find("meta", {"name": "industry"})}
    tag = meta_map.get(lower)
    return tag.get("content", "") if tag else ""


def li_workplace_type(soup):
    for s in [".topcard__workplace-type", ".job-details-jobs-unified-top-card__workplace-type",
              ".jobs-unified-top-card__workplace-type"]:
        el = soup.select_one(s)
        if el:
            return el.get_text(strip=True)
    return ""


LI_FIELD_KEYWORD_MAP = [
    ("Information Technology",
     ["software engineer", "developer", "devops", "frontend", "backend", "full stack", "fullstack",
      "sysadmin", "cloud", "cybersecurity", "data engineer", "machine learning",
      "artificial intelligence", "it support", "network engineer", "database", "kubernetes",
      "docker", "aws", "azure", "react", "node.js", "python developer", "java developer"],
     ["programming", "coding", "api", "agile", "scrum", "git", "linux", "server",
      "infrastructure", "software"]),
    ("Finance & Accounting",
     ["accountant", "auditor", "finance manager", "financial analyst", "cfo", "treasurer", "tax",
      "bookkeeper", "payroll", "budget analyst", "credit analyst", "investment",
      "portfolio manager", "risk analyst", "forex", "actuary", "acca", "cfa", "cpa", "nba",
      "credit officer", "loan officer", "microfinance", "bank teller", "relationship manager"],
     ["financial", "accounting", "balance sheet", "p&l", "reconciliation", "ifrs", "gaap",
      "ledger", "invoicing"]),
    ("Sales & Business Development",
     ["sales executive", "sales manager", "business development", "account manager",
      "sales representative", "bd manager", "regional sales", "key account", "sales director",
      "commercial manager", "sales officer", "territory manager", "field sales"],
     ["revenue", "pipeline", "crm", "leads", "prospects", "quota", "target", "upsell",
      "cross-sell", "b2b", "b2c"]),
    ("Marketing & Communications",
     ["marketing manager", "digital marketing", "seo", "sem", "content marketer",
      "social media manager", "brand manager", "marketing executive", "communications manager",
      "pr manager", "copywriter", "growth hacker", "email marketing", "campaign manager"],
     ["marketing", "branding", "advertising", "social media", "content", "campaign", "analytics",
      "google ads", "facebook ads", "influencer"]),
    ("Human Resources",
     ["hr manager", "human resources", "recruiter", "talent acquisition", "hr business partner",
      "hrbp", "hr officer", "compensation", "benefits manager", "organisational development",
      "learning and development", "l&d", "hr generalist", "payroll manager"],
     ["recruitment", "onboarding", "performance management", "employee relations", "hr",
      "workforce"]),
    ("Engineering",
     ["mechanical engineer", "civil engineer", "electrical engineer", "structural engineer",
      "process engineer", "project engineer", "maintenance engineer", "production engineer",
      "quality engineer", "safety engineer", "site engineer", "design engineer"],
     ["engineering", "cad", "autocad", "solidworks", "manufacturing", "plant", "machinery",
      "commissioning"]),
    ("Healthcare & Medicine",
     ["doctor", "physician", "nurse", "pharmacist", "medical officer", "surgeon", "anaesthetist",
      "physiotherapist", "radiographer", "lab technician", "clinical", "healthcare manager",
      "occupational therapist", "dentist", "midwife", "clinical officer", "medical laboratory"],
     ["hospital", "clinic", "patient", "medical", "health", "pharmaceutical", "diagnosis",
      "treatment"]),
    ("Education & Training",
     ["teacher", "lecturer", "professor", "trainer", "educator", "tutor", "school principal",
      "academic", "curriculum", "e-learning", "instructional designer", "teaching assistant",
      "tutorial assistant", "assistant lecturer"],
     ["school", "university", "college", "classroom", "students", "pedagogy", "curriculum",
      "education"]),
    ("Hospitality & Tourism",
     ["hotel manager", "front desk", "housekeeping", "chef", "sous chef", "food and beverage",
      "f&b manager", "restaurant manager", "bartender", "waiter", "concierge", "tour guide",
      "travel agent", "events coordinator", "catering", "safari guide", "tour operator",
      "lodge manager", "reservations"],
     ["hospitality", "hotel", "resort", "tourism", "guest", "accommodation", "restaurant",
      "kitchen", "safari", "national park", "lodge"]),
    ("Logistics & Supply Chain",
     ["supply chain manager", "logistics coordinator", "warehouse manager", "fleet manager",
      "procurement manager", "purchasing manager", "import export", "freight",
      "shipping coordinator", "inventory manager", "demand planner", "clearing and forwarding",
      "customs officer", "procurement officer", "driver"],
     ["logistics", "supply chain", "warehouse", "inventory", "freight", "procurement", "sourcing",
      "port of dar es salaam", "tanzania revenue authority"]),
    ("Legal",
     ["lawyer", "attorney", "legal counsel", "paralegal", "compliance officer", "legal advisor",
      "solicitor", "barrister", "corporate counsel", "legal manager", "contract manager",
      "advocate"],
     ["legal", "law", "contracts", "litigation", "regulatory", "compliance", "gdpr"]),
    ("Administration & Operations",
     ["office manager", "executive assistant", "administrative officer", "operations manager",
      "pa", "personal assistant", "receptionist", "data entry", "office administrator",
      "company secretary", "business analyst"],
     ["administration", "operations", "office", "coordination", "scheduling", "reporting",
      "clerical"]),
    ("Customer Service",
     ["customer service", "call centre", "customer success", "customer support", "help desk",
      "service advisor", "client relations", "customer experience", "contact centre"],
     ["customer", "support", "helpdesk", "tickets", "escalation", "satisfaction",
      "service level"]),
    ("Construction & Real Estate",
     ["quantity surveyor", "site supervisor", "project manager construction", "architect",
      "draughtsman", "property manager", "estate agent", "real estate", "building inspector",
      "land surveyor", "construction manager"],
     ["construction", "building", "property", "real estate", "site", "contractor", "tender"]),
    ("Manufacturing & Production",
     ["production manager", "quality control", "quality assurance", "qa", "qc", "factory manager",
      "plant manager", "production supervisor", "assembly", "cnc operator", "technician"],
     ["production", "manufacturing", "factory", "assembly", "quality", "lean", "six sigma"]),
    ("Design & Creative",
     ["graphic designer", "ui/ux", "product designer", "art director", "creative director",
      "animator", "illustrator", "photographer", "videographer", "motion designer",
      "web designer"],
     ["design", "creative", "adobe", "figma", "photoshop", "illustrator", "indesign", "sketch",
      "branding"]),
    ("Research & Science",
     ["research scientist", "data scientist", "lab researcher", "research analyst",
      "clinical researcher", "environmental scientist", "chemist", "biologist", "statistician",
      "monitoring and evaluation", "m&e officer", "research assistant"],
     ["research", "analysis", "data", "laboratory", "science", "experiment", "findings",
      "methodology"]),
    ("Security",
     ["security officer", "security guard", "security manager", "cctv", "loss prevention",
      "risk manager", "health and safety", "hse officer", "osh", "fire safety"],
     ["security", "safety", "risk", "surveillance", "patrol", "access control", "emergency"]),
    ("Media & Journalism",
     ["journalist", "editor", "reporter", "broadcast", "news anchor", "content creator",
      "media manager", "radio", "television", "producer", "scriptwriter"],
     ["media", "journalism", "broadcast", "news", "editorial", "publishing", "press"]),
    ("Non-Profit & Social Work",
     ["social worker", "ngo", "charity", "programme coordinator", "program coordinator",
      "community development", "welfare officer", "case manager", "development officer",
      "fundraiser", "volunteer coordinator", "project officer", "field officer"],
     ["social", "ngo", "community", "welfare", "beneficiary", "donor", "impact", "charity",
      "usaid", "unicef", "undp", "who", "world bank", "development partner"]),
    ("Mining & Natural Resources",
     ["mining engineer", "geologist", "mine manager", "mine surveyor", "metallurgist",
      "drill and blast", "exploration geologist", "mineral processing", "mine planner",
      "geotechnical engineer", "hse mining"],
     ["mining", "mine site", "gold", "tanzanite", "exploration", "drilling", "ore", "quarry",
      "minerals"]),
    ("Agriculture & Environment",
     ["agronomist", "agricultural officer", "farm manager", "agribusiness", "extension officer",
      "horticulturist", "livestock officer", "veterinarian", "environmental officer",
      "forestry officer", "wildlife officer", "conservation officer", "fisheries officer"],
     ["agriculture", "farming", "crops", "livestock", "irrigation", "conservation", "wildlife",
      "coffee", "tea", "cashew", "sisal", "horticulture", "environmental"]),
]


def li_infer_job_field(title, description):
    combined = ((title or "") + " " + (description or "")).lower()
    best_field, best_score = "", 0
    for label, high_keys, supporting in LI_FIELD_KEYWORD_MAP:
        score = sum(3 for k in high_keys if k in combined)
        score += sum(1 for k in supporting if k in combined)
        if score > best_score:
            best_score, best_field = score, label
    return best_field if best_score >= 3 else ""


# Tanzania: CSEE (Form Four), ACSEE (Form Six), NTA levels 4-10, NBAA, VETA ...
LI_QUALIFICATION_TIERS = [
    ("PhD / Doctorate", ["phd", "ph.d", "doctorate", "doctoral", "doctor of philosophy"]),
    ("Master's Degree", ["master", "msc", "m.sc", "mba", "m.b.a", "meng", "m.eng", "mphil",
                         "postgraduate degree", "nta level 8", "nta 8", "nta level 9",
                         "nta level 10"]),
    ("Postgraduate Diploma", ["postgraduate diploma", "post-graduate diploma",
                              "post graduate diploma", "pgd", "pgdip"]),
    ("Bachelor's Degree", ["bachelor", "bsc", "b.sc", "beng", "b.eng", "bcom", "b.com", "bba",
                           "llb", "degree in", "undergraduate degree", "honours degree",
                           "nta level 7", "nta 7", "first degree", "university degree"]),
    ("Advanced / Higher Diploma", ["hnd", "hnc", "higher national diploma", "higher diploma",
                                   "advanced diploma"]),
    ("Ordinary Diploma", ["ordinary diploma", "nta level 6", "nta 6", "diploma",
                          "associate degree", "foundation degree"]),
    ("Professional Certification", ["acca", "cpa", "cpa(t)", "nbaa", "cfa", "cima", "pmp",
                                    "prince2", "cissp", "aws certified", "comptia", "ccna",
                                    "shrm", "cipd", "chartered", "certified public",
                                    "professional certification"]),
    ("Technician Certificate", ["technician certificate", "nta level 4", "nta level 5",
                                "nta 4", "nta 5", "veta", "vocational certificate",
                                "certificate in", "certificate course"]),
    ("Form Six (ACSEE) / A-Levels", ["acsee", "form six", "form 6", "a-level", "a level",
                                     "higher school certificate"]),
    ("Form Four (CSEE) / O-Levels", ["csee", "form four", "form 4", "ordinary level", "o-level",
                                     "o level", "igcse", "gcse", "school certificate"]),
]


def li_qualification_tier(text):
    if not text or re.search(r"nursery|primary years|ib pyp|aged between|boys and girls", text, re.I):
        return ""
    lower = text.lower()
    for label, keywords in LI_QUALIFICATION_TIERS:
        if any(k in lower for k in keywords):
            return label
    return ""


def li_apply_details(soup, desc_el, desc_text, ld):
    """(email, url) of a PUBLIC way to apply, or ('', '') when there is none."""
    def pick(raw):
        u = li_clean_application_link(li_blank(raw or ""))
        return ("", u) if u and u.startswith("http") and not li_is_bad_url(u) else None

    for raw in (ld.get("apply_url"), li_apply_button(soup)):      # JSON-LD, then apply button
        r = pick(raw)
        if r:
            return r
    for script in soup.find_all("script"):                        # embedded script data
        s = script.string or ""
        for pat in (r'"applyStartUrl"\s*:\s*"([^"]+)"', r'"applicationUrl"\s*:\s*"([^"]+)"'):
            m = re.search(pat, s)
            if m:
                r = pick(li_decode_entities(m.group(1)).replace("\\", ""))
                if r:
                    return r
    if desc_el is not None:                                       # links in the description
        for a in desc_el.find_all("a", href=True):
            r = pick(a.get("href", ""))
            if r:
                return r
    for u in re.findall(r"https?://[^\s\"'<>)(,\]]+", desc_text):  # bare URLs in the text
        r = pick(re.sub(r"[.,;:!?)]+$", "", u))
        if r:
            return r
    em = li_extract_email(desc_text)                              # email in the text
    return (em, "") if em else ("", "")


# ---- collecting job links ---------------------------------------------------
def li_search_url(keyword, start):
    return (f"{LI_BASE}/jobs-guest/jobs/api/seeMoreJobPostings/search"
            f"?location={quote_plus(LI_COUNTRY)}&f_TPR=r{LI_DAYS * 86400}"
            f"&keywords={quote_plus(keyword)}&start={start}")


LI_CARD_ID_RE = re.compile(r"/jobs/view/(?:[^\"'\s<>?/]*?-)?(\d{6,})(?=[/?\"'<>\s&]|$)")


def li_job_url(job_id):
    return f"{LI_BASE}/jobs/view/{job_id}/"


def li_card_ids(page_html):
    """Job ids on one page of search results, in order, no repeats."""
    return list(dict.fromkeys(LI_CARD_ID_RE.findall(page_html or "")))


def li_collect_links(seen_urls):
    """Job URLs not scraped yet (at most LI_MAX_JOBS), from the public search results."""
    print(f"\n  Searching LinkedIn jobs in {LI_COUNTRY} (posted in the last {LI_DAYS} day(s))")
    ordered, seen_now = [], set()
    for kw in LI_KEYWORDS:
        label = kw or "(all)"
        empty_streak = 0
        for page in range(LI_MAX_PAGES):
            start = page * 25
            if start > 975:                        # LinkedIn's guest search stops at 1000
                break
            body = li_fetch(li_search_url(kw, start), pause=LI_DELAY)
            if body is None:
                print(f"  [{label}] page {page + 1}: no more results (or blocked).")
                break
            ids = li_card_ids(body)
            new = []
            for jid in ids:
                u = li_job_url(jid)
                if u in seen_now:
                    continue
                seen_now.add(u)
                if u not in seen_urls:
                    new.append(u)
            print(f"  [{label}] page {page + 1}: {len(ids)} job(s), {len(new)} new")
            ordered.extend(new)
            if not ids:
                empty_streak += 1
                if empty_streak >= 5:
                    break
            else:
                empty_streak = 0
            if LI_MAX_JOBS and len(ordered) >= LI_MAX_JOBS:
                print(f"  Reached LI_MAX_JOBS ({LI_MAX_JOBS}).")
                return ordered[:LI_MAX_JOBS]
            if (page + 1) % 10 == 0:
                print("  Pausing 20s (every 10 pages) ...")
                time.sleep(20)
        time.sleep(LI_DELAY * 2)
    return ordered


# ---- one job page -----------------------------------------------------------
def li_scrape_job(url):
    html = li_fetch(url, pause=0.3)
    if not html:
        raise RuntimeError("LinkedIn page unavailable (blocked, removed or rate-limited)")
    soup = BeautifulSoup(html, "html.parser")

    def sel_text(*selectors):
        for s in selectors:
            el = soup.select_one(s)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        return ""

    title = sel_text(".top-card-layout__title", "h1.topcard__title",
                     ".job-details-jobs-unified-top-card__job-title", "h1")
    desc_el = soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
    if not title or desc_el is None:
        # a sign-in wall or an error page: do NOT mark it as seen, try again next run
        raise RuntimeError("no job content on the page (sign-in wall or removed job)")

    company_fallback = sel_text(".topcard__org-name-link",
                                ".job-details-jobs-unified-top-card__company-name",
                                ".topcard__flavor")
    company_url_el = (soup.select_one(".topcard__org-name-link")
                      or soup.select_one(".job-details-jobs-unified-top-card__company-name a"))
    company_url_raw = company_url_el.get("href", "") if company_url_el else ""

    location = sel_text(".topcard__flavor--bullet", ".job-details-jobs-unified-top-card__bullet")
    workplace = li_workplace_type(soup)

    time_el = soup.find("time")
    raw_posted = (time_el.get("datetime", "") if time_el else "") or sel_text(
        ".posted-time-ago__text", ".job-details-jobs-unified-top-card__posted-date")
    posted_date = li_resolve_posted_date(raw_posted)

    ld = li_parse_jsonld(html)
    desc_text = desc_el.get_text(" ")
    apply_email, apply_url = li_apply_details(soup, desc_el, desc_text, ld)   # before block_text()
    full_text = soup.get_text(" ")
    real_deadline = li_parse_deadline(full_text)

    lines = [l for l in block_text(desc_el).split("\n") if l.strip()]
    description = cut_at_apply(lines)

    salary = ""
    for s in [".compensation__salary", ".salary", "[class*='salary']", "[class*='compensation']"]:
        el = soup.select_one(s)
        if el and el.get_text(strip=True):
            salary = el.get_text(strip=True)
            break
    salary = salary or ld.get("salary", "") or extract_salary(description)

    raw_type = li_criteria(soup, "Employment type") or workplace
    li_function = li_criteria(soup, "Job function")
    li_industry = li_criteria(soup, "Industries")

    job_page_co = li_company_from_job_page(html, soup, ld)
    time.sleep(0.5)
    company = li_company_details(company_url_raw)

    def first(*vals):
        for v in vals:
            if v and str(v).strip():
                return str(v).strip()
        return ""

    def good_name(n):
        return "" if li_bad_company_name(n) else n

    name = first(good_name(company.get("name", "")), good_name(job_page_co.get("company_name", "")),
                 good_name(company_fallback))
    logo = first("" if li_is_placeholder_logo(company.get("logo", "")) else company.get("logo", ""),
                 job_page_co.get("company_logo", ""))
    website = li_blank(first(company.get("website"), job_page_co.get("company_website")))
    industry = first(company.get("industry"), job_page_co.get("company_industry"), li_industry)
    about = first(company.get("about"), job_page_co.get("company_about"))
    if li_bad_company_name(name) and website:
        base = re.sub(r"^www\.", "", urlparse(website).netloc).split(".")[0]
        name = base.replace("-", " ").title() if base else name

    # short label like "Bachelor's Degree" (as your LinkedIn script gave); full lines as a fallback
    qualification = li_qualification_tier(description) or extract_qualification(description)
    return {
        "title": title, "company": name or "Unknown", "location": location or "Tanzania",
        "job_type": raw_type, "job_type_mapped": map_job_type(raw_type),
        "job_field": li_function or industry or li_infer_job_field(title, description),
        "date_posted": posted_date, "deadline": real_deadline,
        "estimated_deadline": "" if real_deadline else estimated_deadline_for(posted_date),
        "salary": salary, "qualification": qualification,
        "experience": extract_experience(description),
        "apply_email": apply_email, "apply_url": apply_url,
        "description": description, "company_about": about, "company_industry": industry,
        "company_founded": first(company.get("founded"), job_page_co.get("company_founded")),
        "company_type": first(company.get("type")), "company_website": website,
        "company_address": first(company.get("headquarters"), job_page_co.get("company_address"),
                                 location),
        "company_url": first(company.get("company_url"), re.sub(r"\?.*$", "", company_url_raw)),
        "logo": li_clean_logo(logo), "job_url": url,
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


# ----------------------------------------------------------------------------
# SOURCE REGISTRY
# ----------------------------------------------------------------------------
SOURCES = {
    "zoom": {"name": "ZoomTanzania", "collect": zoom_collect_links, "scrape": zoom_scrape_job,
             "skip": zoom_skip_reason, "delay": lambda: ZOOM_DELAY},
    "gtj": {"name": "GreatTanzaniaJobs", "collect": gtj_collect_links, "scrape": gtj_scrape_job,
            "skip": gtj_skip_reason, "delay": lambda: GTJ_DELAY},
    "jwt": {"name": "JobWebTanzania", "collect": jwt_collect_links, "scrape": jwt_scrape_job,
            "skip": jwt_skip_reason, "delay": lambda: JWT_DELAY},
    "linkedin": {"name": "LinkedIn", "collect": li_collect_links, "scrape": li_scrape_job,
                 "skip": lambda job: "", "delay": lambda: LI_DELAY},
}
SOURCE_ALIASES = {"li": "linkedin", "greattanzaniajobs": "gtj", "zoomtanzania": "zoom",
                  "jobweb": "jwt", "jobwebtanzania": "jwt"}
DEFAULT_SOURCES = "zoom,gtj,jwt,linkedin"     # LinkedIn last: slowest, and the one that may block


# ----------------------------------------------------------------------------
# ROW / STORAGE / DUPLICATES
# ----------------------------------------------------------------------------
def to_row(j, source_name):
    """Internal job dict -> one row with the column names save_job() reads."""
    return {
        "Job Title": j["title"], "Job Description": j["description"],
        "Company Details": j["company_about"], "Job Type": j["job_type_mapped"],
        "Job Qualifications": j["qualification"], "Job Experience": j["experience"],
        "Job Location": j["location"], "Job Field": j["job_field"],
        "Date Posted": j["date_posted"], "Deadline": j["deadline"],
        "Estimated Deadline": j["estimated_deadline"], "Salary Range": j["salary"],
        # public email / external link if there is one, else the job's source page
        "Application": j["apply_email"] or j["apply_url"] or j["job_url"],
        "Company Name": j["company"], "Company Logo": j["logo"],
        "Company Industry": j["company_industry"], "Company Founded": j["company_founded"],
        "Company Type": j["company_type"], "Company Website": j["company_website"],
        "Company Address": j["company_address"],
        # only LinkedIn rows have one (the LinkedIn company page); never link back to the job sites
        "Company Url": j.get("company_url", ""),
        "Job URL": j["job_url"], "Source": source_name, "Scraped At": j["scraped_at"],
    }


_COMPANY_NOISE = {"limited", "ltd", "plc", "company", "co", "tanzania", "the", "group", "inc"}


def fingerprint(title, company):
    """Same job posted on two sites -> same fingerprint ('' if nothing to compare)."""
    t = clean(title).lower()
    m = re.search(r"^(.*)\s+(?:job\s+)?at\s+(.+)$", t)          # "Role at Company" -> "Role"
    if m and len(m.group(2).split()) <= 6 and m.group(1).strip():
        t = re.sub(r"\s+job$", "", m.group(1))
    t = re.sub(r"[^a-z0-9]+", "", t)
    words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", clean(company).lower()).split()
             if w not in _COMPANY_NOISE]
    c = "".join(words)
    if not t:
        return ""
    return hashlib.sha1(f"{t}|{c}".encode("utf-8")).hexdigest()[:16]


def load_seen():
    """(urls, fingerprints) already scraped, including older single-site seen files."""
    urls, fps = set(), set()
    for path in (SEEN_FILE,) + LEGACY_SEEN_FILES:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                (fps.add(line[3:]) if line.startswith("fp:") else urls.add(line))
    return urls, fps


def mark_seen(entry):
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


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
# RUNNER
# ----------------------------------------------------------------------------
def run_source(key, seen_urls, fps, all_rows):
    src = SOURCES[key]
    print("\n" + "=" * 70)
    print(f"  SOURCE: {src['name'].upper()}")
    print("=" * 70)
    stats = {"new": 0, "skipped": 0, "dupes": 0, "errors": 0}

    try:
        links = src["collect"](seen_urls)
    except Exception as e:
        print(f"  FATAL: could not collect {src['name']} links: {e}")
        stats["errors"] += 1
        return stats
    print(f"\n  Found {len(links)} new job page(s) to look at.\n")

    in_a_row = 0
    for link in links:
        if link in seen_urls:
            stats["skipped"] += 1
            continue
        try:
            job = src["scrape"](link)
        except Exception as e:
            stats["errors"] += 1
            in_a_row += 1
            print(f"  ERROR {link}: {e}")
            if in_a_row >= MAX_CONSECUTIVE_ERRORS:
                print(f"\n  {in_a_row} jobs failed in a row: {src['name']} is probably blocking "
                      f"this machine. Stopping this source (the others still run).")
                break
            time.sleep(src["delay"]())
            continue
        in_a_row = 0

        if not job["title"] or not job["description"]:
            stats["skipped"] += 1
            print(f"  Skipped (empty page): {link}")
            mark_seen(link)
            seen_urls.add(link)
            continue

        reason = src["skip"](job)
        if reason:
            stats["skipped"] += 1
            print(f"  Skipped {reason}: {job['title']}")
            mark_seen(link)
            seen_urls.add(link)
            continue

        fp = fingerprint(job["title"], job["company"])
        if fp and fp in fps:
            stats["dupes"] += 1
            print(f"  Duplicate (same title+company already scraped): {job['title']}")
            mark_seen(link)
            seen_urls.add(link)
            continue

        row = to_row(job, src["name"])
        append_csv(row)
        mark_seen(link)
        seen_urls.add(link)
        if fp:
            mark_seen("fp:" + fp)
            fps.add(fp)
        all_rows.append(row)
        stats["new"] += 1
        apply = job["apply_email"] or job["apply_url"] or "source url (no public apply)"
        print(f"  [{stats['new']}] {job['title']} | {job['company']} | "
              f"{job['location']} | apply: {apply}")

        if JOB_LIMIT and stats["new"] >= JOB_LIMIT:
            print(f"\n  JOB_LIMIT ({JOB_LIMIT}) reached for {src['name']}, stopping.")
            break
        time.sleep(src["delay"]())
    return stats


def parse_sources(argv):
    raw = ""
    for i, a in enumerate(argv):
        if a == "--sources" and i + 1 < len(argv):
            raw = argv[i + 1]
        elif a.startswith("--sources="):
            raw = a.split("=", 1)[1]
    raw = raw or os.environ.get("SOURCES", "") or DEFAULT_SOURCES
    keys = []
    for k in re.split(r"[,\s]+", raw.lower()):
        k = SOURCE_ALIASES.get(k, k)
        if not k:
            continue
        if k not in SOURCES:
            print(f"Unknown source '{k}'. Valid: {', '.join(SOURCES)}")
            sys.exit(2)
        if k not in keys:
            keys.append(k)
    return keys


def main():
    keys = parse_sources(sys.argv[1:])
    print("=" * 70)
    print(f"  JOBS SCRAPER  ->  {', '.join(SOURCES[k]['name'] for k in keys)}")
    print("=" * 70)

    seen_urls, fps = load_seen()
    print(f"Already scraped before: {len(seen_urls)} job(s)")

    all_rows, totals = [], {}
    for key in keys:
        try:
            totals[key] = run_source(key, seen_urls, fps, all_rows)
        except Exception as e:                      # one source must never stop the others
            print(f"  SOURCE {key} CRASHED: {e}")
            totals[key] = {"new": 0, "skipped": 0, "dupes": 0, "errors": 1}

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 70)
    for key in keys:
        s = totals[key]
        print(f"  {SOURCES[key]['name']:20} new {s['new']:4} | skipped {s['skipped']:4} | "
              f"duplicates {s['dupes']:4} | errors {s['errors']}")
    print(f"  Total new: {len(all_rows)}")
    print(f"  Files: {CSV_FILE}, {JSON_FILE}, {SEEN_FILE}")
    print("=" * 70)


if __name__ == "__main__":
    main()
