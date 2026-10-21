#!/usr/bin/env python3
"""Media monitor: checks news and public social feeds for hospice news.

Three levels:
  pah     mentions of Princess Alice Hospice. The only level that sends alerts.
  local   named neighbouring hospices (LOCAL_HOSPICES)
  sector  UK hospice funding, policy and service news (SECTOR_TERMS)

Each run fetches the feeds, keeps items that match, and for anything not seen
before: appends it to log.md, rebuilds index.html, and sends a push notification
(ntfy) for fresh PAH items.

Usage:
    python3 monitor.py OUTPUT_DIR [--seed]

--seed marks everything currently in the feeds as seen without notifying, so the
first real run only reports new items. A run with no state.json seeds automatically.

Environment (search terms are kept out of the repository as secrets):
    LOCAL_HOSPICES  hospice names separated by |, each optionally name@place,place
    SECTOR_TERMS    search words separated by |, e.g. funding|closure|cuts
    NTFY_TOPIC      ntfy.sh topic to push to (optional; no push if unset)
    NTFY_SERVER     defaults to https://ntfy.sh
    MAC_NOTIFY      set to 1 to also show a macOS notification
    EXTRA_FEEDS     extra RSS URLs separated by spaces, e.g. Google Alerts feeds
"""
import email.utils
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

NAME = '"Princess Alice Hospice"'
# Each entry is a name, optionally with place words that must also appear: "St Catherine's Hospice@Crawley,Sussex"
# (several hospices share a name, so the place words keep out the others)
LOCAL = []
for entry in os.environ.get("LOCAL_HOSPICES", "").split("|"):
    name, _, places = entry.partition("@")
    if name.strip():
        LOCAL.append((name.strip(), [p.strip() for p in places.split(",") if p.strip()]))
SECTOR = [t.strip() for t in os.environ.get("SECTOR_TERMS", "").split("|") if t.strip()]

# How far back each level looks, and how fresh a PAH item must be to alert
MAX_AGE = {"pah": timedelta(days=30), "local": timedelta(days=14), "sector": timedelta(days=14)}
ALERT_AGE = timedelta(days=2)


def google(q, days):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{q} when:{days}d", "hl": "en-GB", "gl": "GB", "ceid": "GB:en"})


def local_query(name, places):
    q = f'"{name}"'
    return q + " (" + " OR ".join(f'"{p}"' if " " in p else p for p in places) + ")" if places else q


# (label, kind, url). 'pah', 'local' and 'sector' feeds are searches for that level;
# 'outlet' feeds are an outlet's full news feed, sorted into levels locally.
FEEDS = [
    ("Google News", "pah", google(NAME, 30)),
    ("Bing News", "pah", "https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": NAME, "format": "rss"})),
    ("Reddit", "pah", "https://www.reddit.com/search.rss?" + urllib.parse.urlencode({"q": NAME, "sort": "new"})),
    ("Surrey Live", "outlet", "https://www.getsurrey.co.uk/news/?service=rss"),
    ("Surrey Comet", "outlet", "https://www.surreycomet.co.uk/news/rss/"),
    ("Richmond & Twickenham Times", "outlet", "https://www.richmondandtwickenhamtimes.co.uk/news/rss/"),
    ("Epsom Guardian", "outlet", "https://www.epsomguardian.co.uk/news/rss/"),
    ("MyLondon", "outlet", "https://www.mylondon.news/news/?service=rss"),
    ("BBC Surrey", "outlet", "https://feeds.bbci.co.uk/news/england/surrey/rss.xml"),
    # Charity trade press, for its hospice stories only. Civil Society has no RSS feed,
    # so it comes through a Google News search of its site
    ("Civil Society", "trade", google('site:civilsociety.co.uk (hospice OR hospices OR palliative OR "end of life")', 30)),
    ("Third Sector", "outlet", "https://www.thirdsector.co.uk/rss/news"),
]
# One search per hospice: combining them in one query loses Google's date limit
for name, places in LOCAL:
    FEEDS.append(("Google News", "local", google(local_query(name, places), 14)))
if SECTOR:
    FEEDS.append(("Google News", "sector", google("hospice (" + " OR ".join(SECTOR) + ")", 14)))
for i, url in enumerate(os.environ.get("EXTRA_FEEDS", "").split()):
    FEEDS.append((f"Google Alert {i + 1}", "pah", url))

PAH_RE = re.compile(r"princess alice|esher hospice", re.I)
def name_re(n):
    return re.compile(re.escape(n).replace("'", "['’]"), re.I)


# The short form ('St Catherine', 'Shooting Star') for checking headlines
LOCAL_SHORT = [name_re(re.sub(r"\s+(children's\s+)?hospices?$", "", n, flags=re.I)) for n, _ in LOCAL]
LOCAL_RE = [(name_re(n),
             re.compile("|".join(re.escape(p) for p in places), re.I) if places else None) for n, places in LOCAL]
HOSPICE_RE = re.compile(r"\bhospices?\b", re.I)
# Google's UK edition still carries American hospice news. Washington and Georgia are left out
# (a Tyne and Wear town, a country) and York only counts as New York
US_STATES = ("Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware Florida Hawaii Idaho "
             "Illinois Indiana Iowa Kansas Kentucky Louisiana Maine Maryland Massachusetts Michigan Minnesota "
             "Mississippi Missouri Montana Nebraska Nevada Ohio Oklahoma Oregon Pennsylvania Tennessee Texas "
             "Utah Vermont Virginia Wisconsin Wyoming").split()
US_RE = re.compile(r"\b(" + "|".join(US_STATES) + r"|New York|New Jersey|New Mexico|New Hampshire|"
                   r"North Carolina|South Carolina|North Dakota|South Dakota|Rhode Island|West Virginia|"
                   r"Medicare|Medicaid|U\.S\.|USA|federal|Province|provincial|Canada|Canadian|Australia|"
                   r"New Zealand|NZ|India|(?<!Northern )Ireland)\b|[$€]")
# Items that get past the filters but shouldn't be there; add to these as they turn up
BLOCKED_SITES = re.compile(r"wjfw\.com", re.I)
BLOCKED_WORDS = re.compile(r"\bAspirus\b", re.I)
FOREIGN_TLD = re.compile(r"\.(ca|nz|au|ie|in|us|za)(/|$)")
LEVELS = [("pah", "PAH"), ("local", "Local hospices"), ("sector", "Hospice sector")]
# One line under the buttons saying what the chosen view holds
NOTES = {
    "pah": "Mentions of Princess Alice Hospice. New ones send a phone alert. Sources: Google News, Bing News, Reddit, "
           "Surrey Live, Surrey Comet, Richmond & Twickenham Times, Epsom Guardian, MyLondon, BBC Surrey.",
    "local": "Neighbouring hospices: St Catherine's (Crawley), Phyllis Tuckwell, Woking and Sam Beare, Shooting Star, "
             "St Raphael's, Royal Trinity. Sources: Google News, plus the local papers above.",
    "sector": "UK hospice news: funding, closures, beds, service changes. Sources: Google News (BBC, ITV, eHospice and "
              "regional papers), Civil Society, Third Sector, plus the local papers above.",
    "all": "Everything above, newest first.",
}

ATOM = "{http://www.w3.org/2005/Atom}"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) media-monitor"}


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def text(el, tag):
    found = el.find(tag)
    if found is None:
        found = el.find(ATOM + tag)
    return (found.text or "").strip() if found is not None else ""


def parse(body):
    root = ET.fromstring(body)
    for it in root.iter("item"):
        yield {
            "title": text(it, "title"),
            "link": text(it, "link"),
            "summary": re.sub(r"<[^>]+>", " ", text(it, "description")),
            "date": text(it, "pubDate"),
            "outlet": text(it, "source"),
            "outlet_url": (it.find("source").get("url", "") if it.find("source") is not None else ""),
        }
    for it in root.iter(ATOM + "entry"):
        link = it.find(ATOM + "link")
        yield {
            "title": text(it, "title"),
            "link": link.get("href", "") if link is not None else "",
            "summary": re.sub(r"<[^>]+>", " ", text(it, "content") or text(it, "summary")),
            "date": text(it, "updated") or text(it, "published"),
        }


def when(raw):
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def blocked(item):
    where = f"{item.get('outlet_url', '')} {item.get('url', '')} {item['link']} {item.get('outlet', '')}"
    return bool(BLOCKED_SITES.search(where) or BLOCKED_WORDS.search(item["title"]))


def classify(kind, item):
    if blocked(item):
        return None
    blob = f"{item['title']} {item['summary']}"
    title = item["title"]
    if kind == "pah" or PAH_RE.search(blob):
        return "pah"
    # Google matches the name anywhere in the article, so the headline must be about a hospice
    if kind == "local" and (HOSPICE_RE.search(title) or any(r.search(title) for r in LOCAL_SHORT)):
        return "local"
    if any(n.search(blob) and (not p or p.search(blob)) for n, p in LOCAL_RE):
        return "local"
    if kind == "trade":
        # The site search already asks for hospice stories; skip the site's own pages
        # ('Event booking', 'Finance'), whose titles are a word or two
        return "sector" if len(re.sub(r"\s+-\s+[^-]{2,60}$", "", title).split()) >= 4 else None
    if kind in ("sector", "outlet") and HOSPICE_RE.search(title if kind == "sector" else blob):
        if US_RE.search(f"{blob} {item.get('outlet', '')}") or FOREIGN_TLD.search(item.get("outlet_url", "")):
            return None
        return "sector"
    return None


def norm(title):
    # Cross-posts and syndicated copies share a title but not a link. Google News adds
    # ' - Outlet' to each headline, so the same story from two papers differs only there
    title = re.sub(r"\s+-\s+[^-]{2,60}$", "", title)
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def published(it):
    dt = when(it.get("date", ""))
    return dt or datetime.fromisoformat(it["seen"])


def level(it):
    # Items saved before the levels existed used 'name' and 'context'
    return {"name": "pah", "context": "sector"}.get(it["level"], it["level"])


def dedupe(items):
    keep = {}
    for k, v in sorted(items.items(), key=lambda kv: published(kv[1])):
        keep.setdefault(norm(v["title"]) or k, (k, v))
    return dict(keep.values())


GOOGLE_HEADERS = {**UA, "Cookie": "CONSENT=YES+cb; SOCS=CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"}


def resolve(link):
    """Turn a Google News link into the article's own address.

    Google's links pass through a consent page for UK visitors, which can stall,
    so the page links to the article directly. Returns None if Google won't say.
    """
    m = re.search(r"news\.google\.com/rss/articles/([^?]+)", link)
    if not m:
        return None
    art = m.group(1)
    try:
        page = urllib.request.urlopen(urllib.request.Request(
            f"https://news.google.com/rss/articles/{art}", headers=GOOGLE_HEADERS), timeout=20).read().decode()
        sg = re.search(r'data-n-a-sg="([^"]+)"', page).group(1)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page).group(1)
        inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                                "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], art, int(ts), sg]
        body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]]))
        reply = urllib.request.urlopen(urllib.request.Request(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body.encode(),
            headers={**GOOGLE_HEADERS, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}),
            timeout=20).read().decode()
        url = json.loads(json.loads(reply.split("\n\n")[1])[0][2])[1]
        return url if url.startswith("http") else None
    except Exception as e:
        print(f"resolve failed: {e}", file=sys.stderr)
        return None


def push(item):
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        req = urllib.request.Request(
            f"{server}/{topic}",
            data=item["title"].encode("utf-8"),
            headers={"Title": f"PAH mention: {item['source']}", "Click": item.get("url") or item["link"], "Tags": "newspaper"},
        )
        try:
            urllib.request.urlopen(req, timeout=20)
        except OSError as e:
            print(f"ntfy failed: {e}", file=sys.stderr)
    if os.environ.get("MAC_NOTIFY") == "1" and sys.platform == "darwin":
        title = item["title"].replace('"', "'")[:120]
        subprocess.run(["osascript", "-e", f'display notification "{title}" with title "PAH mention" subtitle "{item["source"]}"'], check=False)


SUFFIX_RE = re.compile(r"\s+-\s+([^-]{2,60})$")


def outlet_and_title(it):
    # Google News appends ' - Outlet' to headlines; show the outlet as the source instead
    m = SUFFIX_RE.search(it["title"])
    if it["source"] in ("Google News", "Civil Society") and m:
        return it.get("outlet") or m.group(1), it["title"][:m.start()]
    return it["source"], it["title"]


def render(out, items):
    counts = {k: sum(1 for i in items if level(i) == k) for k, _ in LEVELS}
    buttons = "".join(
        f'<button data-level="{k}">{label} <span>{counts[k]}</span></button>' for k, label in LEVELS)
    rows = []
    for it in sorted(items, key=published, reverse=True)[:500]:
        source, title = outlet_and_title(it)
        rows.append(
            f'<tr data-level="{level(it)}"><td>{published(it).strftime("%d %b %Y %H:%M")}</td>'
            f'<td>{html.escape(source)}</td>'
            f'<td><a href="{html.escape(it.get("url") or it["link"])}" rel="noopener">{html.escape(title)}</a></td></tr>'
        )
    page = f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Media monitor</title><meta http-equiv="refresh" content="600">
<style>
:root{{--bg:#fff;--fg:#111;--muted:#666;--line:#ddd;--btn:#f2f2f2;--on:#111;--onfg:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--muted:#aaa;--line:#333;--btn:#222;--on:#eee;--onfg:#111}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.4 Arial,sans-serif;margin:0 auto;max-width:1000px;padding:16px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid var(--line);padding:6px;text-align:left;vertical-align:top}}
td:first-child{{white-space:nowrap}}td:nth-child(2){{white-space:nowrap;max-width:14em;overflow:hidden;text-overflow:ellipsis}}a{{color:inherit}}p{{color:var(--muted)}}
nav{{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}}
button{{background:var(--btn);border:1px solid var(--line);border-radius:6px;color:var(--fg);cursor:pointer;font:inherit;padding:6px 12px}}
button[aria-pressed=true]{{background:var(--on);color:var(--onfg)}}button span{{opacity:.7}}
@media (max-width:600px){{td:nth-child(2),th:nth-child(2){{display:none}}}}
</style></head><body><h1>Media monitor</h1>
<p>Checked {datetime.now(timezone.utc).strftime('%d %B %Y %H:%M')} UTC. Public news and social feeds. PAH: last 30 days. Local hospices and hospice sector: last 14 days.</p>
<nav>{buttons}<button data-level="all">All</button></nav>
<p id="note"></p>
<table><thead><tr><th>Published</th><th>Source</th><th>Item</th></tr></thead><tbody>
{''.join(rows)}</tbody></table>
<p id="empty" hidden>Nothing in this view.</p>
<script>
const btns=[...document.querySelectorAll('nav button')],rows=[...document.querySelectorAll('tbody tr')],notes={json.dumps(NOTES)};
function show(l){{let n=0;rows.forEach(r=>{{const on=l==='all'||r.dataset.level===l;r.hidden=!on;n+=on}});
btns.forEach(b=>b.setAttribute('aria-pressed',b.dataset.level===l));document.getElementById('note').textContent=notes[l]||'';document.getElementById('empty').hidden=n>0;
try{{localStorage.setItem('level',l)}}catch(e){{}}}}
btns.forEach(b=>b.onclick=()=>show(b.dataset.level));
let start='pah';try{{start=localStorage.getItem('level')||'pah'}}catch(e){{}}show(start);
</script></body></html>"""
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    state_path = os.path.join(out, "state.json")
    # The first run only records what is already out there, so nobody gets 60 alerts
    seed = "--seed" in sys.argv or not os.path.exists(state_path)
    state = json.load(open(state_path)) if os.path.exists(state_path) else {"items": {}}
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    fresh = lambda it: published(it) >= now_dt - MAX_AGE[level(it)]
    # Drop anything that has aged out, so the page and state only hold the look-back window
    keep = lambda v: fresh(v) and not blocked(v) and not (level(v) == "sector" and US_RE.search(f"{v['title']} {v['summary']} {v.get('outlet', '')}"))
    seen = state["items"] = dedupe({k: v for k, v in state["items"].items() if keep(v)})
    titles = {norm(v["title"]) for v in seen.values()}
    new = []
    for source, kind, url in FEEDS:
        try:
            items = list(parse(fetch(url)))
        except Exception as e:  # one bad feed shouldn't stop the run
            print(f"{source} ({kind}): {e}", file=sys.stderr)
            continue
        for item in items:
            lvl = classify(kind, item)
            key = item["link"] or item["title"]
            if not lvl or not key or key in seen or norm(item["title"]) in titles:
                continue
            record = {**item, "source": source, "level": lvl, "seen": now}
            if not fresh(record):
                continue
            titles.add(norm(item["title"]))
            seen[key] = record
            new.append(record)
    # Look up direct links for anything that doesn't have one yet (a few seconds each, so capped)
    for it in [v for v in seen.values() if "url" not in v and "news.google.com" in v["link"]][:60]:
        it["url"] = resolve(it["link"]) or it["link"]
    if not seed:
        with open(os.path.join(out, "log.md"), "a", encoding="utf-8") as f:
            for it in new:
                f.write(f"- {now[:16].replace('T', ' ')} UTC | {it['source']} | {it['level']} | [{it['title']}]({it.get('url') or it['link']})\n")
        for it in new:
            # Only alert on fresh PAH items; everything else goes on the page
            if it["level"] == "pah" and published(it) >= now_dt - ALERT_AGE:
                push(it)
    json.dump(state, open(state_path, "w"), indent=1)
    render(out, list(seen.values()))
    by = {k: sum(1 for i in new if i["level"] == k) for k, _ in LEVELS}
    print(f"{len(new)} new ({by['pah']} PAH, {by['local']} local, {by['sector']} sector){' [seed]' if seed else ''}")


if __name__ == "__main__":
    main()
