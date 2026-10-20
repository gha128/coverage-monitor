#!/usr/bin/env python3
"""Media monitor: checks news and public social feeds for mentions of the hospice.

Each run fetches the feeds in FEEDS, keeps items that match, and for anything not
seen before: appends it to log.md, rebuilds index.html, and sends a push
notification (ntfy) for name matches. Context matches (hospice funding generally)
are logged but not pushed.

Usage:
    python3 monitor.py OUTPUT_DIR [--seed]

--seed marks everything currently in the feeds as seen without notifying, so the
first real run only reports new items. A run with no state.json seeds automatically.

Environment:
    NTFY_TOPIC   ntfy.sh topic to push to (optional; no push if unset)
    NTFY_SERVER  defaults to https://ntfy.sh
    MAC_NOTIFY   set to 1 to also show a macOS notification
    EXTRA_FEEDS  extra RSS URLs separated by spaces, e.g. Google Alerts feeds
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

def search(base, **params):
    return base + "?" + urllib.parse.urlencode(params)

# (label, kind, url). kind 'search' feeds are already filtered by the query;
# kind 'outlet' feeds are an outlet's full news feed and are matched locally.
FEEDS = [
    ("Google News", "search", search("https://news.google.com/rss/search", q=NAME + " when:7d", hl="en-GB", gl="GB", ceid="GB:en")),
    ("Bing News", "search", search("https://www.bing.com/news/search", q=NAME, format="rss")),
    ("Reddit", "search", search("https://www.reddit.com/search.rss", q=NAME, sort="new")),
    ("Surrey Live", "outlet", "https://www.getsurrey.co.uk/news/?service=rss"),
    ("Surrey Comet", "outlet", "https://www.surreycomet.co.uk/news/rss/"),
    ("Richmond & Twickenham Times", "outlet", "https://www.richmondandtwickenhamtimes.co.uk/news/rss/"),
    ("Epsom Guardian", "outlet", "https://www.epsomguardian.co.uk/news/rss/"),
    ("MyLondon", "outlet", "https://www.mylondon.news/news/?service=rss"),
    ("BBC Surrey", "outlet", "https://feeds.bbci.co.uk/news/england/surrey/rss.xml"),
]
for i, url in enumerate(os.environ.get("EXTRA_FEEDS", "").split()):
    FEEDS.append((f"Google Alert {i + 1}", "search", url))

# Older items are ignored: the search feeds return years of archive by relevance
MAX_AGE = timedelta(days=14)

NAME_RE = re.compile(r"princess alice|\bPAH\b.*hospice|esher hospice", re.I)
CONTEXT_RE = re.compile(r"\bhospices?\b", re.I)

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


def classify(kind, item):
    blob = f"{item['title']} {item['summary']}"
    if kind == "search" or NAME_RE.search(blob):
        return "name"
    if CONTEXT_RE.search(blob):
        return "context"
    return None


def push(item):
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        req = urllib.request.Request(
            f"{server}/{topic}",
            data=item["title"].encode("utf-8"),
            headers={"Title": f"PAH mention: {item['source']}", "Click": item["link"], "Tags": "newspaper"},
        )
        try:
            urllib.request.urlopen(req, timeout=20)
        except OSError as e:
            print(f"ntfy failed: {e}", file=sys.stderr)
    if os.environ.get("MAC_NOTIFY") == "1" and sys.platform == "darwin":
        title = item["title"].replace('"', "'")[:120]
        subprocess.run(["osascript", "-e", f'display notification "{title}" with title "PAH mention" subtitle "{item["source"]}"'], check=False)


def norm(title):
    # Cross-posts and syndicated copies share a title but not a link
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def dedupe(items):
    keep = {}
    for k, v in sorted(items.items(), key=lambda kv: published(kv[1])):
        keep.setdefault(norm(v["title"]) or k, (k, v))
    return dict(keep.values())


def published(it):
    dt = when(it.get("date", ""))
    return dt or datetime.fromisoformat(it["seen"])


def render(out, items):
    rows = []
    for it in sorted(items, key=published, reverse=True)[:300]:
        rows.append(
            f'<tr class="{it["level"]}"><td>{published(it).strftime("%d %b %Y %H:%M")}</td>'
            f'<td>{html.escape(it["source"])}</td>'
            f'<td><a href="{html.escape(it["link"])}" rel="noopener">{html.escape(it["title"])}</a></td>'
            f'<td>{"Name" if it["level"] == "name" else "Context"}</td></tr>'
        )
    page = f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Media monitor</title><meta http-equiv="refresh" content="600">
<style>
:root{{--bg:#fff;--fg:#111;--muted:#666;--line:#ddd;--ctx:#888}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--muted:#aaa;--line:#333;--ctx:#999}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.4 Arial,sans-serif;margin:0 auto;max-width:1000px;padding:16px}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid var(--line);padding:6px;text-align:left;vertical-align:top}}
a{{color:inherit}}tr.context td{{color:var(--ctx)}}p{{color:var(--muted)}}
</style></head><body><h1>Media monitor</h1>
<p>Checked {datetime.now(timezone.utc).strftime('%d %B %Y %H:%M')} UTC. Public news and social feeds. Last 14 days only. 'Context' rows mention hospices generally.</p>
<table><thead><tr><th>Published</th><th>Source</th><th>Item</th><th>Match</th></tr></thead><tbody>
{''.join(rows)}</tbody></table></body></html>"""
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
    seen = state["items"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cutoff = datetime.now(timezone.utc) - MAX_AGE
    # Drop anything that has aged out, so the page and state only hold the last 14 days
    seen = state["items"] = dedupe({k: v for k, v in seen.items() if published(v) >= cutoff})
    titles = {norm(v["title"]) for v in seen.values()}
    new = []
    for source, kind, url in FEEDS:
        try:
            items = list(parse(fetch(url)))
        except Exception as e:  # one bad feed shouldn't stop the run
            print(f"{source}: {e}", file=sys.stderr)
            continue
        for item in items:
            level = classify(kind, item)
            key = item["link"] or item["title"]
            if not level or not key or key in seen:
                continue
            dt = when(item["date"])
            if dt and dt < cutoff:
                continue
            if norm(item["title"]) in titles:
                continue
            titles.add(norm(item["title"]))
            record = {**item, "source": source, "level": level, "seen": now}
            seen[key] = record
            new.append(record)
    if not seed:
        with open(os.path.join(out, "log.md"), "a", encoding="utf-8") as f:
            for it in new:
                f.write(f"- {now[:16].replace('T', ' ')} UTC | {it['source']} | {it['level']} | [{it['title']}]({it['link']})\n")
        for it in new:
            if it["level"] == "name":
                push(it)
    json.dump(state, open(state_path, "w"), indent=1)
    render(out, list(seen.values()))
    print(f"{len(new)} new ({sum(1 for i in new if i['level'] == 'name')} name){' [seed]' if seed else ''}")


if __name__ == "__main__":
    main()
