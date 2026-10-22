"""
Tanzania jobs scraper: ZoomTanzania + GreatTanzaniaJobs in ONE standalone file.

Run:
    python tz.py                        # both sources (zoom, then gtj)
    python tz.py --sources zoom         # only ZoomTanzania
    python tz.py --sources gtj,zoom     # any order; or SOURCES=gtj,zoom in the environment

Output (in the current folder), one set of files for both sites:
    scraped_jobs.csv     all scraped jobs (appended on every run)
    scraped_jobs.json    jobs scraped in THIS run
    scraped_seen.txt     job URLs + title/company fingerprints already scraped

Every row uses the column names your WordPress save_job() reads, plus a
"Source" column. A job posted on both sites (same title + company) is only
saved once: the first source that has it wins.

How jobs are found:
  zoom  the /jobs/ page only shows 12 jobs, so the job sitemaps are used
        (jobs-sitemap.xml ... jobs-sitemap9.xml), newest first.
  gtj   the /jobs page loads more while scrolling, using
        /index.php?option=com_jsjobs&task=job.getnextjobs&pagenum=N
        (20 jobs per page, newest first); the scraper walks N = 0, 1, 2 ...

Optional environment variables:
    SOURCES              zoom,gtj (default both)
    JOB_LIMIT            stop a source after this many NEW jobs (default 0 = no limit)
    ZOOM_MAX_JOBS        max NEW zoom jobs per run (default 200, 0 = no limit)
    ZOOM_DELAY           seconds between zoom requests (default 1.0)
    ZOOM_SKIP_TYPES      zoom job types to skip (default: scholarship,grants)
    GTJ_MAX_JOBS         max NEW gtj jobs per run (default 200, 0 = no limit)
    GTJ_MAX_PAGES        safety cap on gtj listing pages per run (default 60)
    GTJ_DELAY            seconds between gtj requests (default 1.0)
    GTJ_STOP_ON_KNOWN    1 (default) = stop paging once a whole gtj page was already scraped
    GTJ_SKIP_CATEGORIES  gtj categories to skip (default: scholarship,tender)
"""
import csv
import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

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

JOB_LIMIT = int(os.environ.get("JOB_LIMIT", "0") or 0)

CSV_FILE = "scraped_jobs.csv"
JSON_FILE = "scraped_jobs.json"
SEEN_FILE = "scraped_seen.txt"
LEGACY_SEEN_FILES = ("zoom_seen.txt", "gtj_seen.txt")   # older single-site runs

OWN_HOSTS = ("zoomtanzania.net", "zoom.tz", "greattanzaniajobs")
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
            raw = r.content
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode(r.apparent_encoding or "latin-1", errors="replace")
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


# ----------------------------------------------------------------------------
# SOURCE REGISTRY
# ----------------------------------------------------------------------------
SOURCES = {
    "zoom": {"name": "ZoomTanzania", "collect": zoom_collect_links, "scrape": zoom_scrape_job,
             "skip": zoom_skip_reason, "delay": lambda: ZOOM_DELAY},
    "gtj": {"name": "GreatTanzaniaJobs", "collect": gtj_collect_links, "scrape": gtj_scrape_job,
            "skip": gtj_skip_reason, "delay": lambda: GTJ_DELAY},
}


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
        "Company Url": "",            # never link back to the source sites
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

    for link in links:
        if link in seen_urls:
            stats["skipped"] += 1
            continue
        try:
            job = src["scrape"](link)
        except Exception as e:
            stats["errors"] += 1
            print(f"  ERROR {link}: {e}")
            time.sleep(src["delay"]())
            continue

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
    raw = raw or os.environ.get("SOURCES", "") or "zoom,gtj"
    keys = []
    for k in re.split(r"[,\s]+", raw.lower()):
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
        except Exception as e:                      # one source must never stop the other
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
