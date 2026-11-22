#!/usr/bin/env python3
"""Check your Goodreads to-read list against Libby/OverDrive library catalogs.

Reads goodreads_library_export.csv (any CSV with "goodreads" in its name),
looks every to-read book up at your libraries (ebook and audiobook separately),
and writes docs/data.json for the website. Sends an ntfy push when an ebook
newly becomes available (only if the NTFY_TOPIC secret is set).

  python check_books.py            # normal run
  python check_books.py --verify   # only test the library addresses
"""
import csv, hashlib, html, json, os, re, sys, time
import urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Label -> OverDrive address(es) to try, in order (the part before .overdrive.com).
# The first one that exists is used.
LIBRARIES = {
    "Brooklyn Public Library": ["bklynlibrary", "bklyn", "bpl", "brooklyn", "brooklynpubliclibrary"],
    "New York Public Library": ["nypl"],
    "Harris County Public Library": ["hcpl"],
}

STATE_FILE = Path("state.json")
DATA_FILE = Path("docs/data.json")
CSV_NAME = Path("goodreads_library_export.csv")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
UA = {"User-Agent": "Mozilla/5.0 (book-availability-checker)"}
KINDS = ("ebook", "audiobook")
API = "https://thunder.api.overdrive.com/v2/libraries"


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


# ---------- text helpers ----------
def clean_title(t):
    t = html.unescape(t or "")
    t = re.sub(r"\s*\(.*?\)\s*", " ", t)   # drop "(Series, #1)"
    t = t.split(":")[0]                     # drop subtitle
    return re.sub(r"\s+", " ", t).strip()


def norm(s):
    s = re.sub(r"[^a-z0-9 ]", "", html.unescape(s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def plain(text):
    text = re.sub(r"</p>|</div>", "\n\n", text or "", flags=re.I)
    text = re.sub(r"<\s*br\s*/?>|</li>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# ---------- Goodreads (CSV export) ----------
def find_csv():
    if CSV_NAME.exists():
        return CSV_NAME
    found = sorted(p for p in Path(".").rglob("*.csv") if "goodreads" in p.name.lower())
    return found[0] if found else None


def load_books():
    path = find_csv()
    if not path:
        print("No Goodreads CSV found. Files here:",
              sorted(p.name for p in Path(".").iterdir() if not p.name.startswith(".git")))
        sys.exit("Upload your Goodreads export CSV to the repo (top level).")
    books = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("Exclusive Shelf") != "to-read":
                continue
            if (row.get("Date Read") or "").strip():
                continue
            gid = (row.get("Book Id") or "").strip()
            books.append({
                "title": clean_title(row.get("Title", "")),
                "full_title": html.unescape(row.get("Title", "")).strip(),
                "author": (row.get("Author") or "").strip(),
                "added": (row.get("Date Added") or "").strip().replace("/", "-"),
                "pages": (row.get("Number of Pages") or "").strip(),
                "rating": (row.get("Average Rating") or "").strip(),
                "year": (row.get("Original Publication Year") or row.get("Year Published") or "").strip(),
                "url": f"https://www.goodreads.com/book/show/{gid}" if gid else "",
            })
    print(f"{path}: {len(books)} to-read books")
    if not books:
        sys.exit("No rows with Exclusive Shelf = to-read (and no Date Read) in the CSV.")
    return books


# ---------- OverDrive ----------
def resolve_libraries():
    found = {}
    for label, candidates in LIBRARIES.items():
        for key in candidates:
            try:
                get(f"{API}/{key}?x-client-id=dewey", tries=1)
                found[label] = key
                print(f"OK         {label} -> {key}.overdrive.com")
                break
            except Exception:
                continue
        else:
            print(f"NOT FOUND  {label}. Tried: {', '.join(candidates)}")
    return found


def is_english(item):
    """English only. Records with no language listed are kept (can't tell)."""
    langs = item.get("languages") or []
    if not langs:
        return True
    return any((l.get("id") if isinstance(l, dict) else str(l)).lower().startswith("en")
               or (l.get("name", "") if isinstance(l, dict) else "").lower() == "english"
               for l in langs)


def search_library(key, book):
    q = urllib.parse.urlencode({
        "query": f'{book["title"]} {book["author"]}',
        "perPage": 12, "page": 1, "x-client-id": "dewey",
    })
    data = json.loads(get(f"{API}/{key}/media?{q}"))
    want_t = norm(book["title"])
    last = norm(book["author"].split()[-1]) if book["author"] else ""
    hits = []
    for it in data.get("items", []):
        if it.get("type", {}).get("id") not in KINDS:
            continue
        if not is_english(it):
            continue
        if norm(clean_title(it.get("title", ""))) == want_t and \
                (not last or last in norm(it.get("firstCreatorName", ""))):
            hits.append(it)
    return hits


def cover_of(hits):
    for h in hits:
        c = h.get("covers") or {}
        for k in ("cover300Wide", "cover150Wide", "cover510Wide"):
            href = (c.get(k) or {}).get("href")
            if href:
                return href
    return ""


def summarize(hits, kind, key):
    hits = [h for h in hits if h.get("type", {}).get("id") == kind]
    if not hits:
        return {"status": "not_owned", "text": "Not in catalog"}
    link = lambda h: f"https://{key}.overdrive.com/media/{h.get('id', '')}"

    def copies(h):
        owned, free = h.get("ownedCopies"), h.get("availableCopies")
        return f"{free} of {owned} copies free" if owned is not None and free is not None else ""

    # Only count a title as available if the catalog says so AND a copy is really free
    # AND it is not a pre-release (pre-release titles are hold-only until publication).
    avail = [h for h in hits if h.get("isAvailable") and not h.get("isPreReleaseTitle")
             and (h.get("availableCopies") is None or h.get("availableCopies") > 0)]
    if avail:
        h = avail[0]
        return {"status": "available", "text": "Available now", "cover": cover_of(avail),
                "url": link(h), "matched": h.get("title", ""), "copies": copies(h)}
    best = min(hits, key=lambda h: h.get("estimatedWaitDays") or 9999)
    days, holds = best.get("estimatedWaitDays"), best.get("holdsCount", 0)
    text = f"{holds} holds, ~{days} days" if days else f"{holds} holds"
    if best.get("isPreReleaseTitle"):
        text = "Not released yet (hold only)"
    return {"status": "wait", "days": days or 9999, "cover": cover_of(hits), "url": link(best),
            "text": text, "matched": best.get("title", ""), "copies": copies(best)}


DESC_KEYS = ("fullDescription", "description", "shortDescription")


def full_description(hit):
    """Search results often carry a shortened blurb, so also read the title's own record."""
    texts = [plain(hit[k]) for k in DESC_KEYS if hit.get(k)]
    have = max(map(len, texts), default=0)
    key, rid = hit.get("_lib"), hit.get("id")
    if key and rid:
        for url in (f"{API}/{key}/media/{rid}?x-client-id=dewey",
                    f"https://thunder.api.overdrive.com/v2/media/{rid}?x-client-id=dewey"):
            try:
                d = json.loads(get(url, tries=2))
            except Exception:
                continue
            more = [plain(d[k]) for k in DESC_KEYS if d.get(k)]
            texts += more
            if max(map(len, more), default=0) > have:
                break                      # got something longer, no need for the fallback
    return max(texts, key=len) if texts else ""


def build_info(all_hits):
    """Blurb, genres, publisher etc. from the best matching catalog record."""
    if not all_hits:
        return {}
    ranked = sorted(all_hits, key=lambda h: 0 if h.get("type", {}).get("id") == "ebook" else 1)
    best = next((h for h in ranked if any(h.get(k) for k in DESC_KEYS)), ranked[0])
    subjects = []
    for s in best.get("subjects") or []:
        n = (s.get("name") if isinstance(s, dict) else str(s)) or ""
        if n and n not in subjects:
            subjects.append(n)
    series = best.get("detailedSeries") or {}
    return {
        "description": full_description(best),
        "genres": subjects[:8],
        "publisher": (best.get("publisher") or {}).get("name", ""),
        "published": (best.get("publishDate") or "")[:4],
        "series": series.get("seriesName", "") if isinstance(series, dict) else "",
    }


# ---------- output ----------
def notify(title, message):
    if not NTFY_TOPIC:
        print("[no NTFY_TOPIC set] would notify:", title, message)
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"),
        headers={"Title": title, "Tags": "books", "Click": "https://libbyapp.com"})
    urllib.request.urlopen(req, timeout=20)


def main():
    if "--verify" in sys.argv:
        resolve_libraries()
        return
    books = load_books()
    libs_found = resolve_libraries()
    if not libs_found:
        sys.exit("None of the library addresses worked. Run with verify and fix LIBRARIES.")
    old = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    first_run = not old
    results, new_state, alerts = [], {}, []

    for b in books:
        bk = f'{norm(b["title"])}|{norm(b["author"])}'

        def lookup(item, b=b, bk=bk):
            label, key = item
            try:
                time.sleep(0.2)
                return label, key, search_library(key, b)
            except Exception as e:
                print(f"{label}: error for {b['title']}: {e}")
                return label, key, None

        libs, all_hits = {}, []
        with ThreadPoolExecutor(max_workers=len(libs_found)) as pool:
            for label, key, hits in pool.map(lookup, libs_found.items()):
                if hits is None:                       # lookup failed: keep last known result
                    prev = old.get(bk, {}).get(label)
                    if isinstance(prev, dict) and "ebook" in prev:
                        libs[label] = prev
                    continue
                for h in hits:
                    h["_lib"] = key
                all_hits.extend(hits)
                libs[label] = {k: summarize(hits, k, key) for k in KINDS}
        new_state[bk] = libs

        image = next((s["cover"] for l in libs.values() for s in l.values() if s.get("cover")), "")
        results.append({
            "id": hashlib.md5(bk.encode()).hexdigest()[:8],
            "title": b["title"], "full_title": b["full_title"], "author": b["author"],
            "added": b["added"], "pages": b["pages"], "rating": b["rating"],
            "year": b["year"], "url": b["url"], "image": image,
            "info": build_info(all_hits), "libs": libs,
        })
        for label, l in libs.items():
            was = old.get(bk, {}).get(label, {}).get("ebook", {}).get("status")
            if l["ebook"]["status"] == "available" and was != "available":
                alerts.append(f'{b["title"]} (ebook) is available at {label}')

    print(f"\n=== Summary: {len(results)} to-read books checked ===")
    for label in libs_found:
        for k in KINDS:
            owned = sum(1 for r in results if r["libs"].get(label, {}).get(k, {}).get("status") in ("available", "wait"))
            avail = sum(1 for r in results if r["libs"].get(label, {}).get(k, {}).get("status") == "available")
            print(f"{label} {k}s: owns {owned}, available now {avail}")
    STATE_FILE.write_text(json.dumps(new_state, indent=1))
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(
        {"updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "books": results},
        indent=1), encoding="utf-8")
    if first_run:
        n = sum(any(l["ebook"]["status"] == "available" for l in r["libs"].values()) for r in results)
        notify("Book checker is running", f"{n} of {len(results)} books have an ebook available right now.")
    elif alerts:
        notify("Library ebook available", "\n".join(alerts[:10]))
    print("Alerts:", alerts)


if __name__ == "__main__":
    main()
