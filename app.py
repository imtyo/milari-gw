import os, re, io, csv, sqlite3, threading, webbrowser
from datetime import datetime
from urllib.parse import urljoin
import requests
from flask import Flask, jsonify, request, send_from_directory, Response

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "jobs.db")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
VISA = re.compile(r"visa|sponsor|relocat|ビザ|international|english[- ]speaking|english is", re.I)
REMOTE = re.compile(r"remote|work from home|wfh|リモート|在宅", re.I)

env = os.path.join(BASE, ".env")
if os.path.exists(env):
    for line in open(env, encoding="utf-8"):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

app = Flask(__name__, static_folder=os.path.join(BASE, "static"))


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS jobs(
        id INTEGER PRIMARY KEY, source TEXT, title TEXT, company TEXT, location TEXT,
        url TEXT UNIQUE, description TEXT, salary TEXT, posted TEXT, remote INT, visa INT,
        status TEXT DEFAULT 'new', notes TEXT DEFAULT '', created TEXT)""")


def clean(v):
    return "" if v is None or v != v else str(v)


# ---------- sumber lowongan ----------
def s_jobspy(site, q, loc, n, country, remote):
    from jobspy import scrape_jobs  # LinkedIn & Indeed
    df = scrape_jobs(site_name=[site], search_term=q, location=loc, results_wanted=n,
                     hours_old=24 * 21, country_indeed=country, is_remote=remote,
                     linkedin_fetch_description=True)
    out = []
    for _, r in df.iterrows():
        sal = " ".join(x for x in [clean(r.get("min_amount")), "-", clean(r.get("max_amount")),
                                   clean(r.get("currency")), clean(r.get("interval"))] if x and x != "-")
        out.append(dict(source=site, title=clean(r.get("title")), company=clean(r.get("company")),
                        location=clean(r.get("location")), url=clean(r.get("job_url")),
                        description=clean(r.get("description")), salary=sal,
                        posted=clean(r.get("date_posted"))[:10]))
    return out


def s_adzuna(q, loc, n, country):
    i, k = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (i and k):
        raise RuntimeError("ADZUNA_APP_ID / ADZUNA_APP_KEY belum diisi di file .env")
    r = requests.get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", timeout=20,
                     params={"app_id": i, "app_key": k, "what": q, "where": loc, "results_per_page": min(n, 50)})
    r.raise_for_status()
    return [dict(source="adzuna", title=j.get("title", ""), company=j.get("company", {}).get("display_name", ""),
                 location=j.get("location", {}).get("display_name", ""), url=j["redirect_url"],
                 description=j.get("description", ""),
                 salary=f"{int(j['salary_min'])}-{int(j['salary_max'])}" if j.get("salary_min") and j.get("salary_max") else "",
                 posted=j.get("created", "")[:10]) for j in r.json().get("results", [])]


def s_jobstreet(q, loc, n):  # best-effort: endpoint internal, bisa berubah/diblokir
    r = requests.get("https://id.jobstreet.com/api/jobsearch/v5/search", headers=UA, timeout=20,
                     params={"siteKey": "ID-Main", "keywords": q, "where": loc, "pageSize": min(n, 30), "page": 1})
    r.raise_for_status()
    return [dict(source="jobstreet", title=j.get("title", ""), company=j.get("companyName", ""),
                 location=(j.get("locations") or [{}])[0].get("label", ""),
                 url=f"https://id.jobstreet.com/job/{j['id']}", description=j.get("teaser", ""),
                 salary=j.get("salaryLabel", ""), posted=(j.get("listingDate") or "")[:10])
            for j in r.json().get("data", [])]


def s_careercross(q, n):  # best-effort: parsing HTML, selector bisa perlu disesuaikan
    from bs4 import BeautifulSoup
    r = requests.get("https://www.careercross.com/en/job-search/results", headers=UA, timeout=20,
                     params={"keywords": q})
    r.raise_for_status()
    soup, seen, out = BeautifulSoup(r.text, "html.parser"), set(), []
    for a in soup.select("a[href*='/job/']"):
        href, title = urljoin(r.url, a["href"]), a.get_text(" ", strip=True)
        if len(title) < 8 or href in seen:
            continue
        seen.add(href)
        card = a.find_parent(["li", "article", "div"])
        out.append(dict(source="careercross", title=title, company="", location="Japan", url=href,
                        description=card.get_text(" ", strip=True)[:600] if card else "", salary="", posted=""))
    return out[:n]


ADZ = {"singapore": "sg", "usa": "us", "uk": "gb", "australia": "au"}  # Adzuna tidak punya Jepang/Indonesia


@app.post("/api/search")
def search():
    d = request.json or {}
    q, loc, n = d.get("q", "python software engineer"), d.get("location", "Japan"), int(d.get("n", 20))
    country, remote = d.get("country", "japan"), bool(d.get("remote"))
    adz = ADZ.get(country, os.getenv("ADZUNA_COUNTRY", "sg"))
    tasks = {
        "linkedin": lambda: s_jobspy("linkedin", q, loc, n, country, remote),
        "indeed": lambda: s_jobspy("indeed", q, loc, n, country, remote),
        "jobstreet": lambda: s_jobstreet(q, loc, n),
        "adzuna": lambda: s_adzuna(q, loc, n, adz),
        "careercross": lambda: s_careercross(q, n),
    }
    jobs, report = [], {}
    for s in d.get("sources", []):
        try:
            r = tasks[s]()
            jobs += r
            report[s] = {"ok": True, "count": len(r)}
        except Exception as e:
            report[s] = {"ok": False, "error": str(e)[:200]}
    added = 0
    with db() as c:
        for j in jobs:
            if not j["url"]:
                continue
            txt = f"{j['title']} {j['description']} {j['location']}"
            cur = c.execute(
                "INSERT OR IGNORE INTO jobs(source,title,company,location,url,description,salary,posted,remote,visa,created)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (j["source"], j["title"], j["company"], j["location"], j["url"], j["description"], j["salary"],
                 j["posted"], int(bool(REMOTE.search(txt))), int(bool(VISA.search(txt))), datetime.now().isoformat()))
            added += cur.rowcount
    return jsonify(report=report, added=added)


@app.get("/api/jobs")
def jobs_list():
    a = request.args
    sk = [s.strip().lower() for s in a.get("skills", "").split(",") if s.strip()]
    sql, ar = "SELECT * FROM jobs WHERE 1=1", []
    if a.get("status"):
        sql += " AND status=?"; ar.append(a["status"])
    if a.get("visa") == "1":
        sql += " AND visa=1"
    if a.get("remote") == "1":
        sql += " AND remote=1"
    if a.get("q"):
        sql += " AND (title LIKE ? OR company LIKE ?)"; ar += [f"%{a['q']}%"] * 2
    rows = [dict(r) for r in db().execute(sql + " ORDER BY created DESC", ar)]
    for r in rows:
        t = f"{r['title']} {r['description']}".lower()
        r["matched"] = [s for s in sk if s in t]
        r["score"] = round(100 * len(r["matched"]) / len(sk)) if sk else 0
        r["description"] = r["description"][:350]
    rows.sort(key=lambda r: -r["score"])
    counts = {r[0]: r[1] for r in db().execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")}
    return jsonify(jobs=rows, counts=counts)


@app.patch("/api/jobs/<int:i>")
def job_update(i):
    d = request.json or {}
    with db() as c:
        for f in ("status", "notes"):
            if f in d:
                c.execute(f"UPDATE jobs SET {f}=? WHERE id=?", (d[f], i))
    return jsonify(ok=True)


@app.delete("/api/jobs/<int:i>")
def job_delete(i):
    with db() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (i,))
    return jsonify(ok=True)


@app.get("/api/export.csv")
def export():
    rows = db().execute("SELECT source,title,company,location,salary,posted,status,notes,url FROM jobs").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["source", "title", "company", "location", "salary", "posted", "status", "notes", "url"])
    w.writerows([tuple(r) for r in rows])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=jobs.csv"})


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    threading.Timer(1, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000)
