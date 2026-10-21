
import os
import re
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urljoin, urlunsplit

import requests
from bs4 import BeautifulSoup, Comment

import tz_jobs_scraper as tz   # <- the main script; reused for everything below

# =============================================================================
#  CONFIG
# =============================================================================
ZOOM_BASE        = "https://www.zoomtanzania.net"
ZOOM_JOBS_URL    = os.environ.get("ZOOM_JOBS_URL", f"{ZOOM_BASE}/jobs/")
ZOOM_MAX_PAGES   = int(os.environ.get("ZOOM_MAX_PAGES", "10"))
ZOOM_DELAY       = float(os.environ.get("ZOOM_DELAY", "1.0"))
ZOOM_TIMEOUT     = 25
ZOOM_STOP_ON_KNOWN = os.environ.get("ZOOM_STOP_ON_KNOWN", "1") != "0"
ZOOM_SKIP_TYPES  = {t.strip().lower() for t in
                    os.environ.get("ZOOM_SKIP_TYPES", "scholarship,grants").split(",") if t.strip()}
ZOOM_DEFAULT_LOCATION = os.environ.get("ZOOM_DEFAULT_LOCATION", "Tanzania")
ZOOM_UNKNOWN_COMPANY  = "ZoomTanzania Employer"
_OWN_HOSTS = ("zoomtanzania.net", "zoom.tz")

# NOTE: no "br" in Accept-Encoding (requests can't decode brotli without the
# extra package, which would return garbage text).
ZOOM_SESSION = requests.Session()
ZOOM_SESSION.headers.update({
    "User-Agent": tz.USER_AGENTS[0],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
})

# Make sure the tracker knows these hosts never count as an apply destination
tz._NON_APPLY_EMAIL_DOMAINS = tuple(set(tz._NON_APPLY_EMAIL_DOMAINS) | {"zoomtanzania.net"})

# =============================================================================
#  HTTP
# =============================================================================
def zm_get_soup(url: str, retries: int = 3) -> BeautifulSoup:
    last = None
    for attempt in range(retries):
        try:
            r = ZOOM_SESSION.get(url, timeout=ZOOM_TIMEOUT)
            if r.status_code == 429:
                wait = 30 + attempt * 30
                tz.log.warning(f"ZoomTanzania 429 — sleeping {wait}s")
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
            raise                       # 404 etc. -> caller decides (end of pages)
        except Exception as e:
            last = e
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"fetch failed for {url}: {last}")

# =============================================================================
#  STEP 1 — collect job detail URLs from the archive
# =============================================================================
def _norm_zoom_url(href: str) -> str:
    if not href:
        return ""
    p = urlsplit(urljoin(ZOOM_BASE + "/", href))
    if not p.netloc.lower().endswith("zoomtanzania.net"):
        return ""
    path = p.path if p.path.endswith("/") else p.path + "/"
    return urlunsplit(("https", "www.zoomtanzania.net", path, "", ""))

def _is_zoom_job_path(path: str) -> bool:
    """/jobs/<category>/<slug>/  (excludes /jobs/at/<company>, /jobs/page/N)."""
    parts = [s for s in path.split("/") if s]
    return len(parts) == 3 and parts[0] == "jobs" and parts[1] not in ("at", "page")

def _zoom_page_url(base: str, page: int) -> str:
    if page <= 1:
        return base
    base = base if base.endswith("/") else base + "/"
    return f"{base}page/{page}/"

def collect_zoom_links(known_urls: set) -> list:
    print(tz.C_BLUE(f"\n  Collecting job links from: {ZOOM_JOBS_URL}"))
    seen, ordered, empty_streak = set(), [], 0
    for page in range(1, ZOOM_MAX_PAGES + 1):
        url = _zoom_page_url(ZOOM_JOBS_URL, page)
        try:
            soup = zm_get_soup(url)
        except requests.HTTPError as e:
            print(tz.C_DIM(f"  Page {page}: HTTP {getattr(e.response, 'status_code', '?')} — stopping."))
            break
        except Exception as e:
            print(tz.C_DIM(f"  Page {page}: fetch error ({e}) — stopping."))
            break

        anchors = soup.select(".civi-jobs-item a[href]") or soup.find_all("a", href=True)
        page_links = []
        for a in anchors:
            norm = _norm_zoom_url(a["href"])
            if norm and _is_zoom_job_path(urlsplit(norm).path) and norm not in seen:
                seen.add(norm)
                page_links.append(norm)
        ordered.extend(page_links)
        page_known = sum(1 for u in page_links if u in known_urls)
        print(f"    Page {page}: {len(page_links)} link(s), {page_known} already tracked "
              f"(total {len(ordered)})")

        if not page_links:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
            if ZOOM_STOP_ON_KNOWN and page_known == len(page_links):
                print(tz.C_DIM("    Whole page already tracked — stopping (ZOOM_STOP_ON_KNOWN)."))
                break
        time.sleep(ZOOM_DELAY)
    return ordered

# =============================================================================
#  STEP 2 — parse one job page
# =============================================================================
def _txt(el) -> str:
    return el.get_text(" ", strip=True) if el else ""

def _iso(v: str) -> str:
    m = re.search(r"\d{4}-\d{2}-\d{2}", v or "")
    return m.group(0) if m else (tz.parse_any_date(v or "") if v else "")

def _ok_email(em: str) -> bool:
    return bool(em) and tz._is_real_apply_email(em) and not any(h in em.lower() for h in _OWN_HOSTS)

def _ok_url(href: str) -> bool:
    return (tz._is_real_apply_url(href)
            and not any(h in href.lower() for h in _OWN_HOSTS))

def _find_apply(scopes) -> tuple:
    """(email, url) — only genuine public ones; apply popup is checked first."""
    for scope in scopes:
        if scope is None:
            continue
        email = url = ""
        for a in scope.find_all("a", href=True):
            h = a["href"].strip()
            if h.lower().startswith("mailto:"):
                c = tz.clean_email(h[7:].split("?")[0].strip())
                if _ok_email(c) and not email:
                    email = c
            elif not url and _ok_url(h):
                url = tz.strip_tracking_params(h)
        text = scope.get_text(" ")
        if not email:
            for m in tz.EMAIL_PATTERN.finditer(text):
                c = tz.clean_email(m.group(0).rstrip(".,;"))
                if _ok_email(c):
                    email = c
                    break
        if not url:
            for u in tz.URL_PATTERN.findall(text):
                u = u.rstrip(".,);")
                if _ok_url(u):
                    url = tz.strip_tracking_params(u)
                    break
        if email or url:
            return email, url
    return "", ""

def scrape_zoom_detail(url: str) -> dict:
    soup = zm_get_soup(url)
    head = soup.select_one(".jobs-head-details") or soup

    # ── title / company / category / logo ───────────────────────────────────
    h1 = head.select_one("h1") or soup.find("h1")
    title = tz.sanitize_text(_txt(h1))
    title = re.sub(r"\s*\(?\d[\d,]*\s*views?\)?\s*$", "", title, flags=re.I).strip()

    company_name = _txt(head.select_one("a.authour")) or ZOOM_UNKNOWN_COMPANY
    categories = [tz.sanitize_text(_txt(a)) for a in head.select(".categories-warpper a.cate")]
    categories = [c for c in categories if c]

    logo_el = head.select_one("img.logo-comnpany")
    logo = tz.clean_logo_url(logo_el.get("src", "")) if logo_el else ""

    raw_type = _txt(head.select_one("a.label-type"))
    job_type = tz.map_job_type(raw_type)
    location = tz._clean_location_str(_txt(head.select_one("a.label-location"))) or ZOOM_DEFAULT_LOCATION

    # ── "Job Role Insights" grid ────────────────────────────────────────────
    insights = {}
    for li in soup.select(".jobs-insights-details li.list-item"):
        key = _txt(li.select_one(".title-info")).lower()
        spans = [_txt(s) for s in li.select(".details-info span")]
        val = ", ".join(s for s in spans if s) or _txt(li.select_one(".details-info"))
        if key:
            insights[key] = val
    if insights.get("hiring location"):
        location = tz._clean_location_str(insights["hiring location"]) or location

    # ── apply (must run BEFORE html_block_to_text mutates the tree) ─────────
    desc_el = (soup.select_one(".jobs-description-details .civi-description")
               or soup.select_one(".civi-description"))
    popup = soup.select_one("#civi_form_apply_jobs .apply-popup")
    apply_text = tz.sanitize_text(_txt(popup))[:300] if popup else ""

    if desc_el is not None:
        for junk in desc_el.select(".ad-container, ins, script, iframe, style, .toggle-description"):
            junk.decompose()
        for c in desc_el.find_all(string=lambda s: isinstance(s, Comment)):
            c.extract()

    apply_email, apply_url = _find_apply([popup, desc_el])

    # ── description (cut at the "How to Apply" heading) ─────────────────────
    description = ""
    if desc_el is not None:
        lines = [l.strip() for l in tz.html_block_to_text(desc_el).split("\n") if l.strip()]
        cut = len(lines)
        for i, ln in enumerate(lines):
            if tz._is_apply_heading_line(ln):
                cut = i
                if not apply_text:
                    apply_text = "\n".join(lines[i:])[:300]
                break
        body = lines[:cut]
        if len(" ".join(body).split()) < 20:      # heading found too early -> keep everything
            body = lines
        description = re.sub(r"\n{3,}", "\n\n", "\n".join(body)).strip()

    # ── dates ───────────────────────────────────────────────────────────────
    date_posted = _iso(insights.get("date posted", "")) or datetime.now().strftime("%Y-%m-%d")
    deadline = _iso(insights.get("closing date", ""))
    if not deadline:
        deadline = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")

    # ── experience: trust the text first (the insight box is often generic) ─
    experience = tz.extract_experience(description)
    if not experience:
        m = re.search(r"\d+", insights.get("experience", ""))
        if m:
            experience = tz.years_to_band(int(m.group(0)))

    # ── salary ──────────────────────────────────────────────────────────────
    salary = next((v for k, v in insights.items() if "salary" in k and v), "") \
             or tz.extract_salary_tz(description)

    # ── company sidebar (about / industry / address) ────────────────────────
    side = soup.select_one(".jobs-company-sidebar")
    about = industry = company_address = founded = ""
    if side:
        content = side.select_one(".content-truncated .content")
        about = tz.sanitize_text(tz.html_block_to_text(content)) if content else ""
        cate = side.select_one(".list-cate a")
        industry = re.sub(r"\s+Companies$", "", _txt(cate), flags=re.I).strip() if cate else ""
        for info in side.select(".info"):
            if _txt(info.select_one(".title-info")).lower() == "location":
                company_address = _txt(info.select_one(".details-info"))
        fm = re.search(r"(?:founded|established)\s+in\s+((?:19|20)\d{2})", about, re.I)
        founded = fm.group(1) if fm else ""

    return {
        "title": title, "company_name": company_name, "company_logo": logo,
        "job_type": job_type, "raw_type": raw_type, "location": location,
        "job_field": tz.infer_job_field(title, description, ", ".join(categories)),
        "date_posted": date_posted, "deadline": deadline, "description": description,
        "qualification": tz.extract_qualification(description),
        "experience": experience, "salary": salary,
        "apply_email": apply_email, "apply_url": apply_url, "apply_text": apply_text,
        "company_about": about, "company_industry": industry,
        "company_address": company_address or location, "company_founded": founded,
        "job_url": _norm_zoom_url(url),
    }

# =============================================================================
#  STEP 3 — dedupe + gate + paraphrase
# =============================================================================
def _flag_zoom(raw: dict, reason: str):
    tz._init_flagged()
    try:
        import csv
        with open(tz.FLAGGED_FILE, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                "ZoomTanzania", raw.get("title", ""), raw.get("company_name", ""),
                raw.get("location", ""), raw.get("salary", ""), raw.get("deadline", ""),
                reason, raw.get("apply_text", "")[:300], raw.get("job_url", ""),
                datetime.now().isoformat(),
            ])
    except Exception as e:
        tz.log.error(f"Flagged write error: {e}")

def process_zoom(raw: dict, processed_ids: set, processed_urls: set, known_fps: set):
    """Returns (status, job) with status in {'ok','duplicate','flagged','skipped'}."""
    job_url, title = raw.get("job_url", ""), raw.get("title", "")
    company, location = raw.get("company_name", ""), raw.get("location", "")
    if not title or not raw.get("description"):
        return "duplicate", None

    if raw.get("raw_type", "").strip().lower() in ZOOM_SKIP_TYPES:
        print(tz.C_DIM(f"  Skipped type '{raw['raw_type']}': {title}"))
        return "skipped", None

    job_id = tz.make_job_id(job_url, title, company)
    if job_id in processed_ids or job_url in processed_urls:
        print(tz.C_DIM(f"  Already processed (tracker) — skipped: {title}"))
        return "duplicate", None

    # company names on Zoom are real, but never match on the placeholder
    fp = tz.content_fingerprint(title, company if company != ZOOM_UNKNOWN_COMPANY else "")
    if fp and fp in known_fps:
        print(tz.C_DIM(f"  Same title+company already seen (any source) — skipped: {title}"))
        tz.mark_duplicate(job_id, job_url, title, company, "ZoomTanzania")
        processed_ids.add(job_id); processed_urls.add(job_url)
        return "duplicate", None

    apply_email, apply_url = raw.get("apply_email", ""), raw.get("apply_url", "")
    if tz.REQUIRE_PUBLIC_APPLY and not (apply_email or apply_url):
        _flag_zoom(raw, "no public apply email or external URL found")
        print(tz.C_RED(f"  FLAGGED (no public apply) — {title}"))
        return "flagged", None

    if fp:
        known_fps.add(fp)
    tz.mark_scraped(job_id, job_url, title, company, source="ZoomTanzania")
    processed_ids.add(job_id); processed_urls.add(job_url)

    description = raw["description"]
    desc_lang = tz.detect_text_language(description)
    p_title, p_desc, p_about = title, description, raw.get("company_about", "")
    if tz.ENABLE_PARAPHRASE and tz.MISTRAL_API_KEY and desc_lang == "en":
        print(tz.C_BLUE(f"\n  ✍️  Paraphrasing '{title}' ..."))
        p_title = tz.paraphrase_title(title)
        p_desc = tz.paraphrase_description(description)
        if p_about:
            p_about = tz.paraphrase_company(p_about)
        tz.mark_paraphrased(job_id)
    else:
        print(tz.C_DIM(f"  ⚠️  Paraphrasing skipped (lang={desc_lang}, or disabled / no MISTRAL_API_KEY)"))

    return "ok", {
        "jobTitle": p_title, "jobDescription": p_desc, "companyDetails": p_about,
        "originalTitle": title, "originalDesc": description,
        "jobType": raw.get("job_type", "full-time"),
        "jobQualifications": raw.get("qualification", ""),
        "jobExperience": raw.get("experience", ""),
        "jobLocation": location, "jobField": raw.get("job_field", ""),
        "datePosted": raw.get("date_posted", ""), "deadline": raw.get("deadline", ""),
        "application": apply_email or apply_url,
        "companyUrl": "",                       # never link back to zoomtanzania.net
        "companyName": company, "companyLogo": raw.get("company_logo", ""),
        "companyIndustry": raw.get("company_industry", ""),
        "companyFounded": raw.get("company_founded", ""), "companyType": "",
        "companyWebsite": "", "companyAddress": raw.get("company_address", "") or location,
        "jobUrl": job_url, "estimatedDeadline": "", "salaryRange": raw.get("salary", ""),
        "_jobId": job_id,
        "_apply_method": "email" if apply_email else "external_url",
        "_logo_source": "zoomtanzania", "_lang": desc_lang, "_source": "ZoomTanzania",
    }

# =============================================================================
#  RUNNER
# =============================================================================
def run_zoom(ctx: dict):
    st = ctx["stats"]["ZoomTanzania"]
    print()
    print(tz.C_HEADER("=" * 72))
    print(tz.C_HEADER("  SOURCE: ZOOMTANZANIA.NET"))
    print(tz.C_HEADER("=" * 72))
    print(f"  Source URL    : {ZOOM_JOBS_URL}  (max {ZOOM_MAX_PAGES} pages)")
    print(f"  Public-apply  : {'✅ enforced (others flagged)' if tz.REQUIRE_PUBLIC_APPLY else '❌ off (post all)'}")
    print(f"  Skipped types : {', '.join(sorted(ZOOM_SKIP_TYPES)) or 'none'}")

    try:
        links = collect_zoom_links(ctx["processed_urls"])
    except Exception as e:
        print(tz.C_RED(f"  FATAL: could not collect ZoomTanzania links: {e}"))
        st["errors"] += 1
        return
    st["found"] = len(links)
    if not links:
        print(tz.C_RED("  No ZoomTanzania links found."))
        return
    print(tz.C_GREEN(f"\n  Found {len(links)} ZoomTanzania detail page(s).\n"))

    for link in links:
        if link in ctx["processed_urls"]:
            st["skipped"] += 1
            continue
        try:
            raw = scrape_zoom_detail(link)
        except Exception as e:
            st["errors"] += 1
            print(tz.C_RED(f"  ERROR scraping {link} : {e}"))
            time.sleep(ZOOM_DELAY)
            continue
        try:
            status, job = process_zoom(raw, ctx["processed_ids"], ctx["processed_urls"], ctx["known_fps"])
        except Exception as e:
            st["errors"] += 1
            print(tz.C_RED(f"  ERROR processing '{raw.get('title', '')}' : {e}"))
            continue

        if status in ("duplicate", "skipped"):
            st["skipped"] += 1
        elif status == "flagged":
            st["flagged"] += 1
        else:
            st["new"] += 1
            ctx["jobs"].append(job)
            tz.print_job_verbose(job, st["new"], st["found"])
            tz._post_and_track(job, st)
            tz._checkpoint(ctx)
            if tz.JOB_LIMIT and st["new"] >= tz.JOB_LIMIT:
                print(f"\n  JOB_LIMIT ({tz.JOB_LIMIT}) reached for ZoomTanzania, stopping.")
                break
        time.sleep(ZOOM_DELAY)

# =============================================================================
#  MAIN — register the source with the main script, then reuse its main()
# =============================================================================
tz.SOURCE_RUNNERS["zoom"] = ("ZoomTanzania", run_zoom)

if __name__ == "__main__":
    if "--sources" not in sys.argv and not os.environ.get("SOURCES"):
        sys.argv += ["--sources", "zoom"]
    tz.main()
