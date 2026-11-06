import os, re, io, csv, json, time, logging, sqlite3, threading, webbrowser
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from urllib.parse import urljoin, urlsplit

import requests
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "jobs.db")
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}
VISA = re.compile(
    r"visa|sponsor|relocat|ビザ|international|english[- ]speaking|english is", re.I
)
REMOTE = re.compile(r"remote|work from home|wfh|リモート|在宅", re.I)
STATUSES = {"new", "saved", "applied", "interview", "offer", "rejected"}

env = os.path.join(BASE, ".env")
if os.path.exists(env):
    for line in open(env, encoding="utf-8"):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

TIMEOUT = (
    5,
    float(os.getenv("SOURCE_TIMEOUT", "20")),
)  # seconds
DEADLINE = {
    "linkedin": 180,
    "indeed": 120,
    "jobstreet": 45,
    "adzuna": 45,
    "careercross": 45,
    "tokyodev": 45,
    "japandev": 45,
    "mycareersfuture": 45,
    "remoteok": 45,
    "remotive": 45,
}


# ---------- logging (JSON, satu baris per event) ----------
class JsonFmt(logging.Formatter):
    def format(self, r):
        d = {
            "ts": datetime.fromtimestamp(r.created, timezone.utc).isoformat(),
            "level": r.levelname,
            "service": "milari",
            "event": r.getMessage(),
        }
        d.update(getattr(r, "fields", {}))
        if r.exc_info:
            d["exception"] = self.formatException(r.exc_info)
        return json.dumps(d, ensure_ascii=False, default=str)


def setup_logging():
    os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(
            os.path.join(BASE, "logs", "milari.log"),
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        ),
    ]
    for h in handlers:
        h.setFormatter(JsonFmt())
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers, lg.propagate = (handlers, False) if name else (handlers, True)
        lg.setLevel(os.getenv("LOG_LEVEL", "INFO"))


setup_logging()
log = logging.getLogger("milari")


def emit(level, event, **fields):
    log.log(
        level, event, extra={"fields": fields}, exc_info=fields.pop("exc_info", None)
    )


# ---------- Prometheus (endpoint /metrics) ----------
M_SRC = Counter(
    "milari_source_requests_total",
    "Permintaan ke situs lowongan",
    ["source", "outcome"],
)
M_DUR = Histogram(
    "milari_source_duration_seconds",
    "Durasi pengambilan per sumber",
    ["source"],
    buckets=(1, 3, 5, 10, 20, 45, 90, 180),
)
M_ADD = Counter("milari_jobs_added_total", "Lowongan baru tersimpan", ["source"])
M_HTTP = Counter(
    "milari_http_requests_total", "Request ke dashboard", ["method", "path", "status"]
)


class ConfigError(Exception):
    pass


def safe_url(u):  # buang query string untuk log
    p = urlsplit(str(u or ""))
    return f"{p.scheme}://{p.netloc}{p.path}" if p.netloc else ""


def classify(e):
    if isinstance(e, ConfigError):
        return "config_error"
    if isinstance(e, FutureTimeout):
        return "deadline_exceeded"
    if isinstance(e, requests.Timeout) or "timed out" in str(e).lower():
        return "timeout"
    if isinstance(e, requests.ConnectionError):
        return "connection_error"
    if isinstance(e, requests.HTTPError):
        code = e.response.status_code if e.response is not None else 0
        return (
            "rate_limited"
            if code == 429
            else "blocked"
            if code in (401, 403)
            else "http_error"
        )
    if isinstance(e, (ValueError, KeyError, AttributeError)):
        return "parse_error"
    return "unknown"


def clean(v):
    return "" if v is None or v != v else str(v)


# ---------- sumber lowongan ----------
def s_jobspy(site, q, loc, n, country, remote):
    from jobspy import scrape_jobs  # LinkedIn & Indeed

    df = scrape_jobs(
        site_name=[site],
        search_term=q,
        location=loc,
        results_wanted=n,
        hours_old=24 * 21,
        country_indeed=country,
        is_remote=remote,
        linkedin_fetch_description=True,
    )
    out = []
    for _, r in df.iterrows():
        sal = " ".join(
            x
            for x in [
                clean(r.get("min_amount")),
                "-",
                clean(r.get("max_amount")),
                clean(r.get("currency")),
                clean(r.get("interval")),
            ]
            if x and x != "-"
        )
        out.append(
            dict(
                source=site,
                title=clean(r.get("title")),
                company=clean(r.get("company")),
                location=clean(r.get("location")),
                url=clean(r.get("job_url")),
                description=clean(r.get("description")),
                salary=sal,
                posted=clean(r.get("date_posted"))[:10],
            )
        )
    return out


def s_adzuna(q, loc, n, country):
    i, k = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (i and k):
        raise ConfigError("ADZUNA_APP_ID / ADZUNA_APP_KEY belum diisi di file .env")
    r = requests.get(
        f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
        timeout=TIMEOUT,
        params={
            "app_id": i,
            "app_key": k,
            "what": q,
            "where": loc,
            "results_per_page": min(n, 50),
        },
    )
    r.raise_for_status()
    return [
        dict(
            source="adzuna",
            title=j.get("title", ""),
            company=j.get("company", {}).get("display_name", ""),
            location=j.get("location", {}).get("display_name", ""),
            url=j["redirect_url"],
            description=j.get("description", ""),
            salary=f"{int(j['salary_min'])}-{int(j['salary_max'])}"
            if j.get("salary_min") and j.get("salary_max")
            else "",
            posted=j.get("created", "")[:10],
        )
        for j in r.json().get("results", [])
    ]


def s_jobstreet(q, loc, n):  # endpoint internal
    r = requests.get(
        "https://id.jobstreet.com/api/jobsearch/v5/search",
        headers=UA,
        timeout=TIMEOUT,
        params={
            "siteKey": "ID-Main",
            "keywords": q,
            "where": loc,
            "pageSize": min(n, 30),
            "page": 1,
        },
    )
    r.raise_for_status()
    return [
        dict(
            source="jobstreet",
            title=j.get("title", ""),
            company=j.get("companyName", ""),
            location=(j.get("locations") or [{}])[0].get("label", ""),
            url=f"https://id.jobstreet.com/job/{j['id']}",
            description=j.get("teaser", ""),
            salary=j.get("salaryLabel", ""),
            posted=(j.get("listingDate") or "")[:10],
        )
        for j in r.json().get("data", [])
    ]


def s_careercross(q, n):  # parsing HTML
    from bs4 import BeautifulSoup

    r = requests.get(
        "https://www.careercross.com/en/job-search/results",
        headers=UA,
        timeout=TIMEOUT,
        params={"keywords": q},
    )
    r.raise_for_status()
    soup, seen, out = BeautifulSoup(r.text, "html.parser"), set(), []
    for a in soup.select("a[href*='/job/']"):
        href, title = urljoin(r.url, a["href"]), a.get_text(" ", strip=True)
        if len(title) < 8 or href in seen:
            continue
        seen.add(href)
        card = a.find_parent(["li", "article", "div"])
        out.append(
            dict(
                source="careercross",
                title=title,
                company="",
                location="Japan",
                url=href,
                description=card.get_text(" ", strip=True)[:600] if card else "",
                salary="",
                posted="",
            )
        )
    if not out:  # in case ga ada lowongan terbaca: kemungkinan struktur HTML berubah
        emit(
            logging.WARNING,
            "source_empty_result",
            source="careercross",
            url=safe_url(r.url),
            hint="selector HTML mungkin perlu disesuaikan",
            html_bytes=len(r.text),
        )
    return out[:n]


GENERIC = {
    "software",
    "engineer",
    "developer",
    "senior",
    "junior",
    "programmer",
    "engineering",
}


def strip_html(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h or "")).strip()


def matches(q, txt, strict):
    """strict=True: wajib ada kata kunci inti (mis. 'python'); False: cukup salah satu kata aja."""
    toks = [t for t in re.split(r"\W+", q.lower()) if len(t) > 2]
    core = [t for t in toks if t not in GENERIC]
    want = core if (strict and core) else toks
    return not want or any(t in txt.lower() for t in want)


def scrape_listing(source, listing, pattern, q, n, loc="Japan"):
    """Scraper generik (TokyoDev, Japan Dev)"""
    from bs4 import BeautifulSoup

    r = requests.get(listing, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    soup, seen, out = BeautifulSoup(r.text, "html.parser"), set(), []
    rx = re.compile(pattern)
    for a in soup.find_all("a", href=rx):
        href = urljoin(r.url, a["href"]).split("?")[0]
        if href in seen:
            continue
        card = a.find_parent(["li", "article", "div"])
        txt = (card or a).get_text(" ", strip=True)
        if not matches(q, txt, False):
            continue
        seen.add(href)
        comp = rx.search(a["href"]).group("c").replace("-", " ").title()
        out.append(
            dict(
                source=source,
                title=(a.get_text(" ", strip=True) or txt)[:120],
                company=comp,
                location=loc,
                url=href,
                description=txt[:600],
                salary="",
                posted="",
            )
        )
    if not out:
        emit(
            logging.WARNING,
            "source_empty_result",
            source=source,
            url=safe_url(r.url),
            hint="selector/pola URL mungkin perlu disesuaikan",
            html_bytes=len(r.text),
        )
    return out[:n]


def s_tokyodev(q, n):  # Jepang
    return scrape_listing(
        "tokyodev",
        "https://www.tokyodev.com/jobs",
        r"/companies/(?P<c>[^/]+)/jobs/[^/?#]+",
        q,
        n,
    )


def s_japandev(q, n):  # Jepang, English-friendly
    return scrape_listing(
        "japandev", "https://japan-dev.com/jobs", r"/jobs/(?P<c>[^/]+)/[^/?#]+", q, n
    )


def s_mcf(q, n):  # Singapore
    r = requests.get(
        "https://api.mycareersfuture.gov.sg/v2/jobs",
        headers=UA,
        timeout=TIMEOUT,
        params={"search": q, "limit": min(n, 50), "page": 0},
    )
    r.raise_for_status()
    out = []
    for j in r.json().get("results", []):
        sal, m = j.get("salary") or {}, j.get("metadata") or {}
        out.append(
            dict(
                source="mycareersfuture",
                title=j.get("title", ""),
                company=(j.get("postedCompany") or {}).get("name", ""),
                location="Singapore",
                url=m.get("jobDetailsUrl")
                or f"https://www.mycareersfuture.gov.sg/job/{j.get('uuid', '')}",
                description=strip_html(j.get("description", "")),
                salary=f"{sal.get('minimum')}-{sal.get('maximum')} SGD"
                if sal.get("minimum")
                else "",
                posted=(m.get("newPostingDate") or "")[:10],
            )
        )
    return out


def s_remoteok(q, n):  # tautan balik ke remoteok.com
    r = requests.get("https://remoteok.com/api", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json()[1:]:
        if not matches(
            q, f"{j.get('position', '')} {' '.join(j.get('tags') or [])}", True
        ):
            continue
        out.append(
            dict(
                source="remoteok",
                title=j.get("position", ""),
                company=j.get("company", ""),
                location=j.get("location") or "Remote",
                url=j.get("url", ""),
                description=strip_html(j.get("description", "")),
                salary=f"{j['salary_min']}-{j['salary_max']} USD"
                if j.get("salary_min")
                else "",
                posted=(j.get("date") or "")[:10],
            )
        )
    return out[:n]


def s_remotive(q, n):  # API publik
    core = [t for t in re.split(r"\W+", q.lower()) if len(t) > 2 and t not in GENERIC]
    r = requests.get(
        "https://remotive.com/api/remote-jobs",
        headers=UA,
        timeout=TIMEOUT,
        params={"search": " ".join(core), "category": "software-dev", "limit": n},
    )
    r.raise_for_status()
    return [
        dict(
            source="remotive",
            title=j.get("title", ""),
            company=j.get("company_name", ""),
            location=j.get("candidate_required_location") or "Remote",
            url=j.get("url", ""),
            description=strip_html(j.get("description", ""))[:1500],
            salary=j.get("salary", ""),
            posted=(j.get("publication_date") or "")[:10],
        )
        for j in r.json().get("jobs", [])
    ]


ADZ = {
    "singapore": "sg",
    "usa": "us",
    "uk": "gb",
    "australia": "au",
}  # Adzuna ga ada Jepang/Indonesia


# ---------- database ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY, source TEXT, title TEXT, company TEXT, location TEXT,
        url TEXT UNIQUE, description TEXT, salary TEXT, posted TEXT, remote INT, visa INT,
        status TEXT DEFAULT 'new', notes TEXT DEFAULT '', created TEXT)""")

# ---------- API ----------
app = FastAPI(title="MilariGW")


@app.middleware("http")
async def access_log(request: Request, call_next):
    t0 = time.perf_counter()
    try:
        resp = await call_next(request)
    except Exception:
        emit(
            logging.ERROR,
            "http_unhandled_error",
            method=request.method,
            path=request.url.path,
            exc_info=True,
        )
        M_HTTP.labels(request.method, request.url.path, "500").inc()
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
    route = request.scope.get("route")
    path = route.path if route else "unmatched"
    M_HTTP.labels(request.method, path, str(resp.status_code)).inc()
    if path not in ("/metrics", "/health"):
        emit(
            logging.INFO,
            "http_request",
            method=request.method,
            path=request.url.path,
            status=resp.status_code,
            duration_ms=round((time.perf_counter() - t0) * 1000),
        )
    return resp


class SearchReq(BaseModel):
    q: str = "python software engineer"
    location: str = "Japan"
    country: str = "japan"
    n: int = Field(20, ge=1, le=50)
    remote: bool = False
    sources: list[str] = []


class JobPatch(BaseModel):
    status: str | None = None
    notes: str | None = None


@app.post("/api/search")
def search(req: SearchReq):
    adz = ADZ.get(req.country, os.getenv("ADZUNA_COUNTRY", "sg"))
    q, loc, n = req.q, req.location, req.n
    tasks = {
        "linkedin": lambda: s_jobspy("linkedin", q, loc, n, req.country, req.remote),
        "indeed": lambda: s_jobspy("indeed", q, loc, n, req.country, req.remote),
        "jobstreet": lambda: s_jobstreet(q, loc, n),
        "adzuna": lambda: s_adzuna(q, loc, n, adz),
        "careercross": lambda: s_careercross(q, n),
        "tokyodev": lambda: s_tokyodev(q, n),
        "japandev": lambda: s_japandev(q, n),
        "mycareersfuture": lambda: s_mcf(q, n),
        "remoteok": lambda: s_remoteok(q, n),
        "remotive": lambda: s_remotive(q, n),
    }
    names = [s for s in req.sources if s in tasks]
    emit(
        logging.INFO,
        "search_started",
        sources=names,
        query=q,
        location=loc,
        country=req.country,
    )
    pool, t0 = ThreadPoolExecutor(max_workers=max(len(names), 1)), time.monotonic()
    futs = {s: pool.submit(tasks[s]) for s in names}  # paralel
    jobs, report = [], {}
    for s, f in futs.items():
        try:
            res = f.result(timeout=max(DEADLINE[s] - (time.monotonic() - t0), 0.1))
            dur = time.monotonic() - t0
            M_SRC.labels(s, "ok").inc()
            M_DUR.labels(s).observe(dur)
            emit(
                logging.INFO,
                "source_ok",
                source=s,
                count=len(res),
                duration_ms=round(dur * 1000),
            )
            jobs += res
            report[s] = {"ok": True, "count": len(res)}
        except Exception as e:
            kind, dur = classify(e), time.monotonic() - t0
            resp = getattr(e, "response", None)
            req_obj = getattr(e, "request", None)
            M_SRC.labels(s, kind).inc()
            M_DUR.labels(s).observe(dur)
            emit(
                logging.ERROR,
                "source_failed",
                source=s,
                error_type=kind,
                exception_class=type(e).__name__,
                message=str(e)[:300],
                status_code=getattr(resp, "status_code", None),
                target_url=safe_url(
                    getattr(req_obj, "url", "") or getattr(resp, "url", "")
                ),
                duration_ms=round(dur * 1000),
                timeout_connect_s=TIMEOUT[0],
                timeout_read_s=TIMEOUT[1],
                exc_info=(type(e), e, e.__traceback__) if kind == "unknown" else None,
            )
            report[s] = {"ok": False, "error": f"{kind}: {str(e)[:160]}"}
    pool.shutdown(wait=False)

    added = {}
    with db() as c:
        for j in jobs:
            if not j["url"]:
                continue
            txt = f"{j['title']} {j['description']} {j['location']}"
            cur = c.execute(
                "INSERT OR IGNORE INTO jobs(source,title,company,location,url,description,salary,posted,remote,visa,created)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    j["source"],
                    j["title"],
                    j["company"],
                    j["location"],
                    j["url"],
                    j["description"],
                    j["salary"],
                    j["posted"],
                    int(bool(REMOTE.search(txt))),
                    int(bool(VISA.search(txt))),
                    datetime.now().isoformat(),
                ),
            )
            if cur.rowcount:
                added[j["source"]] = added.get(j["source"], 0) + 1
    for s, k in added.items():
        M_ADD.labels(s).inc(k)
    emit(
        logging.INFO,
        "search_finished",
        added=sum(added.values()),
        added_by_source=added,
        failed=[s for s, r in report.items() if not r["ok"]],
        duration_ms=round((time.monotonic() - t0) * 1000),
    )
    return {"report": report, "added": sum(added.values())}


@app.get("/api/jobs")
def jobs_list(
    status: str = "", q: str = "", skills: str = "", visa: int = 0, remote: int = 0
):
    sk = [s.strip().lower() for s in skills.split(",") if s.strip()]
    sql, ar = "SELECT * FROM jobs WHERE 1=1", []
    if status:
        sql += " AND status=?"
        ar.append(status)
    if visa:
        sql += " AND visa=1"
    if remote:
        sql += " AND remote=1"
    if q:
        sql += " AND (title LIKE ? OR company LIKE ?)"
        ar += [f"%{q}%"] * 2
    rows = [dict(r) for r in db().execute(sql + " ORDER BY created DESC", ar)]
    for r in rows:
        t = f"{r['title']} {r['description']}".lower()
        r["matched"] = [s for s in sk if s in t]
        r["score"] = round(100 * len(r["matched"]) / len(sk)) if sk else 0
        r["description"] = r["description"][:350]
    rows.sort(key=lambda r: -r["score"])
    counts = {
        r[0]: r[1]
        for r in db().execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")
    }
    return {"jobs": rows, "counts": counts}


@app.patch("/api/jobs/{job_id}")
def job_update(job_id: int, body: JobPatch):
    d = body.model_dump(exclude_none=True)
    if "status" in d and d["status"] not in STATUSES:
        raise HTTPException(422, "status tidak valid")
    with db() as c:
        for f, v in d.items():
            c.execute(f"UPDATE jobs SET {f}=? WHERE id=?", (v, job_id))
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def job_delete(job_id: int):
    with db() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return {"ok": True}


@app.get("/api/export.csv")
def export():
    rows = (
        db()
        .execute(
            "SELECT source,title,company,location,salary,posted,status,notes,url FROM jobs"
        )
        .fetchall()
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "source",
            "title",
            "company",
            "location",
            "salary",
            "posted",
            "status",
            "notes",
            "url",
        ]
    )
    w.writerows([tuple(r) for r in rows])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobs.csv"},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/")
def index():
    for d in (os.path.join(BASE, "static"), BASE):
        p = os.path.join(d, "index.html")
        if os.path.exists(p):
            return FileResponse(p)
    raise HTTPException(
        404, "index.html tidak ditemukan di folder static atau folder app.py"
    )


if __name__ == "__main__":
    emit(logging.INFO, "app_started", url="http://127.0.0.1:5000")
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    uvicorn.run(app, host="127.0.0.1", port=5000, log_config=None)
