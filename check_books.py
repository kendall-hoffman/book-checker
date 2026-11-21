#!/usr/bin/env python3
"""Check a Goodreads to-read shelf against OverDrive/Libby library catalogs.

Run daily (GitHub Actions). Sends an ntfy push when a book becomes
available at any of your libraries. Standard library only.

Usage:
  python check_books.py          # normal run
  python check_books.py --verify # just test that each library address works
"""
import csv, html, json, os, re, sys, time
import urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

GOODREADS_USER_ID = "20454106"

# Label -> OverDrive address prefix (the part before .overdrive.com).
# Run with --verify; fix any that report NOT FOUND.
LIBRARIES = {
    "Brooklyn Public Library": "bklyn",
    "New York Public Library": "nypl",
    "Harris County Public Library": "hcpl",
    "Houston Public Library": "houstonlibrary",
    "Bryan + College Station (BCS)": "bcs",
    "Beaumont Public Library": "beaumont",
    "Round Rock Public Library": "roundrock",
    "Montgomery County Memorial": "mcmls",
}

STATE_FILE = Path("state.json")
CSV_FALLBACK = Path("goodreads_library_export.csv")
DASHBOARD = Path("docs/index.html")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
UA = {"User-Agent": "Mozilla/5.0 (book-availability-checker)"}


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"failed: {url}")


# ---------- Goodreads ----------
def clean_title(t):
    t = html.unescape(t or "")
    t = re.sub(r"\s*\(.*?\)\s*", " ", t)   # drop "(Series, #1)"
    t = t.split(":")[0]                     # drop subtitle
    return re.sub(r"\s+", " ", t).strip()


def goodreads_rss():
    books, page = [], 1
    while True:
        url = (f"https://www.goodreads.com/review/list_rss/{GOODREADS_USER_ID}"
               f"?shelf=to-read&per_page=200&page={page}")
        root = ET.fromstring(get(url))
        items = root.findall("./channel/item")
        if not items:
            break
        for it in items:
            books.append({
                "title": clean_title(it.findtext("title")),
                "author": (it.findtext("author_name") or "").strip(),
                "image": it.findtext("book_medium_image_url") or "",
                "url": "https://www.goodreads.com/book/show/" + (it.findtext("book_id") or ""),
            })
        if len(items) < 200:
            break
        page += 1
    return books


def goodreads_csv():
    books = []
    with CSV_FALLBACK.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("Exclusive Shelf") == "to-read":
                books.append({"title": clean_title(row["Title"]),
                              "author": row["Author"].strip()})
    return books


def load_books():
    try:
        books = goodreads_rss()
        if books:
            print(f"Goodreads RSS: {len(books)} books")
            return books
        print("RSS returned nothing.")
    except Exception as e:
        print("RSS failed:", e)
    if CSV_FALLBACK.exists():
        books = goodreads_csv()
        print(f"CSV fallback: {len(books)} books")
        return books
    sys.exit("Could not load your Goodreads list.")


# ---------- OverDrive ----------
def norm(s):
    s = re.sub(r"[^a-z0-9 ]", "", html.unescape(s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def search_library(key, book):
    q = urllib.parse.urlencode({
        "query": f'{book["title"]} {book["author"]}',
        "perPage": 12, "page": 1, "x-client-id": "dewey",
    })
    url = f"https://thunder.api.overdrive.com/v2/libraries/{key}/media?{q}"
    data = json.loads(get(url))
    want_t = norm(book["title"])
    last = norm(book["author"].split()[-1]) if book["author"] else ""
    hits = []
    for it in data.get("items", []):
        if it.get("type", {}).get("id") not in ("ebook", "audiobook"):
            continue
        t = norm(clean_title(it.get("title", "")))
        creator = norm(it.get("firstCreatorName", ""))
        if t == want_t and (not last or last in creator):
            hits.append(it)
    return hits


def summarize(hits):
    """Collapse matching editions into one status for a library."""
    if not hits:
        return {"status": "not_owned", "text": "Not in catalog"}
    avail = [h for h in hits if h.get("isAvailable")]
    if avail:
        kinds = sorted({h["type"]["id"] for h in avail})
        return {"status": "available", "text": "Available now (" + ", ".join(kinds) + ")"}
    best = min(hits, key=lambda h: h.get("estimatedWaitDays") or 9999)
    days = best.get("estimatedWaitDays")
    holds = best.get("holdsCount", 0)
    wait = f"~{days} days" if days else "unknown wait"
    return {"status": "wait", "days": days or 9999,
            "text": f"{holds} holds, {wait} ({best['type']['id']})"}


def verify():
    for label, key in LIBRARIES.items():
        try:
            get(f"https://thunder.api.overdrive.com/v2/libraries/{key}?x-client-id=dewey")
            print(f"OK         {label} ({key})")
        except urllib.error.HTTPError as e:
            print(f"NOT FOUND  {label} ({key}) -> HTTP {e.code}")
        except Exception as e:
            print(f"ERROR      {label} ({key}) -> {e}")


# ---------- Output ----------
def notify(title, message):
    if not NTFY_TOPIC:
        print("[no NTFY_TOPIC set] would notify:", title, message)
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"),
        headers={"Title": title, "Tags": "books", "Click": "https://libbyapp.com"})
    urllib.request.urlopen(req, timeout=20)


def write_dashboard(results, updated):
    DASHBOARD.parent.mkdir(exist_ok=True)
    (DASHBOARD.parent / "data.json").write_text(
        json.dumps({"updated": updated, "books": results}, indent=1), encoding="utf-8")


def main():
    if "--verify" in sys.argv:
        return verify()
    books = load_books()
    old = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    first_run = not old
    results, new_state, alerts = [], {}, []
    dead = set()
    for b in books:
        bk = f'{norm(b["title"])}|{norm(b["author"])}'
        libs = {}
        for label, key in LIBRARIES.items():
            if key in dead:
                continue
            try:
                libs[label] = summarize(search_library(key, b))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    print(f"Library address '{key}' not found; skipping. Run --verify.")
                    dead.add(key)
            except Exception as e:
                print(f"{label}: error for {b['title']}: {e}")
                if label in old.get(bk, {}):  # keep previous status on errors
                    libs[label] = old[bk][label]
            time.sleep(0.3)
        new_state[bk] = libs
        results.append({"title": b["title"], "author": b["author"], "image": b.get("image", ""),
                        "url": b.get("url", ""), "libs": libs})
        for label, l in libs.items():
            was = old.get(bk, {}).get(label, {}).get("status")
            if l["status"] == "available" and was != "available":
                alerts.append(f'{b["title"]} is available at {label}')
    STATE_FILE.write_text(json.dumps(new_state, indent=1))
    write_dashboard(results, time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
    if first_run:
        n = sum(any(l["status"] == "available" for l in r["libs"].values()) for r in results)
        notify("Book checker is running", f"{n} of {len(results)} books are available right now.")
    elif alerts:
        notify("Library book available", "\n".join(alerts[:10]))
    print("Alerts:", alerts)


if __name__ == "__main__":
    main()
