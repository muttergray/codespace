#!/usr/bin/env python3
"""Export a public Must (mustapp.com) profile into Letterboxd import CSV files.

Usage:
    python3 must_to_letterboxd.py vladimirsalov
    python3 must_to_letterboxd.py --from-json vladimirsalov_must_backup.json

The first form downloads the profile from Must's public web API. The second
converts a backup made by browser_export.js (or by this script) offline.

Output (in --out-dir, default ./letterboxd_export):
    <user>_letterboxd_watched.csv     upload at https://letterboxd.com/import/
    <user>_letterboxd_watchlist.csv   import from your Letterboxd watchlist page
    <user>_must_tv.csv                TV shows (Letterboxd has films only)
    <user>_must_backup.json           raw Must data, for safekeeping
    <user>_report.txt                 what was exported and what was skipped

Only the Python standard library is needed. Set TMDB_TOKEN (a TMDB "API Read
Access Token") to add tmdbID/imdbID columns for exact matching on Letterboxd.
"""

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

MUST_API = "https://mustapp.com/api"
# Same headers as Must's own website; the products endpoint rejects requests without them.
MUST_HEADERS = {
    "accept": "*/*",
    "bearer": "3a77331c-943f-44e8-b636-5deebcbe33b9",
    "content-type": "application/json;v=1873",
    "x-client-version": "frontend_site/2.24.2-390.390",
    "x-requested-with": "XMLHttpRequest",
}
MUST_BATCH = 100
TMDB_API = "https://api.themoviedb.org/3"
TV_TYPES = {"show", "season", "episode"}
# Letterboxd rejects import files over 1 MB; stay well under it.
MAX_CSV_BYTES = 900 * 1024
BACKUP_FORMAT = "must-backup/1"
USER_AGENT = "must-to-letterboxd/1.0"

WATCHED_COLUMNS = ["tmdbID", "imdbID", "Title", "Year", "Rating10", "WatchedDate", "Tags", "Review"]
WATCHLIST_COLUMNS = ["tmdbID", "imdbID", "Title", "Year"]
TV_COLUMNS = ["List", "MustID", "Type", "Title", "Year", "Status", "Rating10", "Date", "Review"]


def normalize_username(value):
    value = str(value or "").strip()
    value = re.sub(r"^https?://(?:www\.)?mustapp\.com/@?", "", value, flags=re.I)
    return re.split(r"[/?#]", value.lstrip("@"))[0].strip()


# ---------------------------------------------------------------- HTTP

def fetch_json(url, method="GET", body=None, headers=None, retries=4, label="request"):
    data = None if body is None else json.dumps(body).encode("utf-8")
    all_headers = {"user-agent": USER_AGENT}
    all_headers.update(headers or {})
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=data, headers=all_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            retryable = error.code in (408, 429) or error.code >= 500
            if not retryable or attempt == retries:
                detail = error.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"{label}: HTTP {error.code} {detail}") from None
            wait = int(error.headers.get("retry-after") or 0) or 2 ** attempt
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == retries:
                raise RuntimeError(f"{label}: {error}") from None
            wait = 2 ** attempt
        print(f"  {label} failed, retrying in {wait}s...", file=sys.stderr)
        time.sleep(wait)


def fetch_must_backup(username, lang="en"):
    """Download everything needed for the export and return it as a backup dict."""
    profile = fetch_json(f"{MUST_API}/users/uri/{urllib.parse.quote(username)}",
                         headers={"accept-language": lang}, label="Must profile")
    if not isinstance(profile, dict) or profile.get("error"):
        message = (profile.get("error") or {}).get("message") if isinstance(profile, dict) else None
        raise RuntimeError(message or f'Must user "{username}" not found')
    if profile.get("is_private") or not profile.get("lists"):
        raise RuntimeError("This Must profile is private. Make it public in Must settings and try again.")

    lists = profile["lists"]
    ids = unique(list(lists.get("watched") or []) + list(lists.get("want") or []) + list(lists.get("shows") or []))
    headers = dict(MUST_HEADERS, **{"accept-language": lang})
    products, reviews = [], []
    for start in range(0, len(ids), MUST_BATCH):
        batch = ids[start:start + MUST_BATCH]
        url = f"{MUST_API}/users/id/{profile['id']}/products?embed="
        products += fetch_json(url + "product", "POST", {"ids": batch}, headers, label="Must products")
        try:
            reviews += fetch_json(url + "review", "POST", {"ids": batch}, headers, label="Must reviews")
        except RuntimeError as error:
            print(f"  warning: reviews for {len(batch)} titles unavailable ({error})", file=sys.stderr)
        print(f"  Must: {min(start + MUST_BATCH, len(ids))}/{len(ids)}", file=sys.stderr)

    return {
        "format": BACKUP_FORMAT,
        "username": username,
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "lang": lang,
        "profile": profile,
        "products": products,
        "reviews": reviews,
    }


def unique(values):
    seen, result = set(), []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


# ---------------------------------------------------------------- conversion

def product_id(item):
    return (item.get("product") or {}).get("id") or (item.get("user_product_info") or {}).get("product_id")


def review_text(info):
    review = (info or {}).get("review")
    if isinstance(review, dict):
        review = review.get("body")
    return str(review or "").strip()


def date_part(value):
    match = re.match(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return match.group(0) if match else ""


def rating10(value):
    """Must rates films 1-10, the same scale as Letterboxd's Rating10 column."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*[+-]?\d+\s*", value):
        value = int(value)
    return str(value) if isinstance(value, int) and 1 <= value <= 10 else ""


def build_entries(backup):
    """Merge profile lists, products and reviews into normalized entries.

    Returns (films, tv): films are Letterboxd candidates, tv are shows/seasons/episodes.
    Every entry: list, must_id, type, title, year, rating, date, review, tmdb_id, imdb_id.
    """
    lists = backup["profile"].get("lists") or {}
    watched_ids = list(lists.get("watched") or [])
    want_ids = list(lists.get("want") or [])
    show_ids = list(lists.get("shows") or [])

    by_id = {}
    for item in backup.get("products") or []:
        pid = product_id(item)
        if pid is not None:
            by_id[pid] = {"product": dict(item.get("product") or {}),
                          "info": dict(item.get("user_product_info") or {})}

    reviews = backup.get("reviews") or []
    for index, item in enumerate(reviews):
        pid = product_id(item)
        if pid is None and len(reviews) == len(backup.get("products") or []):
            pid = product_id(backup["products"][index])
        if pid in by_id:
            for key, value in (item.get("user_product_info") or {}).items():
                if value is not None and by_id[pid]["info"].get(key) is None:
                    by_id[pid]["info"][key] = value
            text = review_text(item.get("user_product_info"))
            if text:
                by_id[pid]["info"]["review"] = {"body": text}

    films, tv, seen = [], [], set()
    for list_name, ids in (("watched", watched_ids), ("want", want_ids), ("shows", show_ids)):
        for pid in ids:
            if pid in seen or pid not in by_id:
                continue
            seen.add(pid)
            product, info = by_id[pid]["product"], by_id[pid]["info"]
            kind = product.get("type") or "movie"
            entry = {
                "list": list_name,
                "must_id": pid,
                "type": kind,
                "title": str(product.get("title") or "").strip(),
                "year": date_part(product.get("release_date"))[:4],
                "status": info.get("status") or "",
                "rating": rating10(info.get("rate")),
                "date": date_part(info.get("watched_at") or info.get("modified_at")),
                "review": review_text(info),
                "tmdb_id": "",
                "imdb_id": "",
            }
            if kind in TV_TYPES or list_name == "shows":
                tv.append(entry)
            else:
                films.append(entry)
    return films, tv


def apply_date_policy(watched, mode="smart", window_days=30, bulk_per_day=5):
    """Fill entry["watched_date"] and return how many dates were dropped and why.

    Must stores when a film was *marked* in the app, not when it was seen. Films
    bulk-added after signing up all get the same few dates, which would flood the
    Letterboxd diary. "smart" drops dates inside the first `window_days` after the
    earliest one and on any day with `bulk_per_day` or more films.
    """
    stats = {"kept": 0, "window": 0, "bulk": 0, "missing": 0}
    dated = [entry["date"] for entry in watched if entry["date"]]
    first = min(dated) if dated else None
    window_end = None
    if first:
        window_end = (dt.date.fromisoformat(first) + dt.timedelta(days=window_days)).isoformat()
    per_day = Counter(dated)

    for entry in watched:
        date = entry["date"]
        if not date:
            stats["missing"] += 1
            entry["watched_date"] = ""
        elif mode == "none":
            entry["watched_date"] = ""
        elif mode == "smart" and window_days > 0 and date < window_end:
            stats["window"] += 1
            entry["watched_date"] = ""
        elif mode == "smart" and bulk_per_day > 0 and per_day[date] >= bulk_per_day:
            stats["bulk"] += 1
            entry["watched_date"] = ""
        else:
            stats["kept"] += 1
            entry["watched_date"] = date
    stats["first_date"] = first or ""
    stats["window_end"] = window_end or ""
    return stats


def sort_watched(watched):
    # Oldest first, so Letterboxd creates diary entries in the order they happened.
    return sorted(watched, key=lambda e: (e["watched_date"] or "0000", e["date"] or "0000"))


# ---------------------------------------------------------------- TMDB (optional)

def tmdb_enrich(films, token, lang="en-US"):
    headers = {"accept": "application/json", "authorization": f"Bearer {token}"}
    cache = {}
    for number, entry in enumerate(films, 1):
        key = (entry["title"], entry["year"])
        if key not in cache:
            cache[key] = tmdb_match(entry["title"], entry["year"], headers, lang)
        entry["tmdb_id"], entry["imdb_id"] = cache[key]
        if number % 25 == 0 or number == len(films):
            print(f"  TMDB: {number}/{len(films)}", file=sys.stderr)


def tmdb_match(title, year, headers, lang):
    if not title:
        return "", ""
    base = f"{TMDB_API}/search/movie?include_adult=true&language={lang}&query={urllib.parse.quote(title)}"
    try:
        results = fetch_json(base + (f"&year={year}" if year else ""), headers=headers, label="TMDB search")["results"]
        if not results and year:
            results = fetch_json(base, headers=headers, label="TMDB search")["results"]
    except RuntimeError as error:
        print(f"  warning: {error}", file=sys.stderr)
        return "", ""
    if not results:
        return "", ""
    exact = [m for m in results if title in (m.get("title"), m.get("original_title"))] or results
    same_year = [m for m in exact if (m.get("release_date") or "")[:4] == year] or exact
    best = max(same_year, key=lambda m: m.get("popularity") or 0)
    imdb = ""
    try:
        imdb = fetch_json(f"{TMDB_API}/movie/{best['id']}/external_ids", headers=headers,
                          label="TMDB external ids").get("imdb_id") or ""
    except RuntimeError:
        pass
    return str(best["id"]), imdb


# ---------------------------------------------------------------- CSV

def csv_line(values):
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerow(values)
    return buffer.getvalue()


def watched_row(entry, tag):
    review = entry["review"].replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    return [entry["tmdb_id"], entry["imdb_id"], entry["title"], entry["year"], entry["rating"],
            entry["watched_date"], tag if entry["watched_date"] else "", review]


def watchlist_row(entry):
    return [entry["tmdb_id"], entry["imdb_id"], entry["title"], entry["year"]]


def csv_parts(columns, rows, max_bytes=MAX_CSV_BYTES):
    """Render rows as one or more CSV texts, each under max_bytes when encoded as UTF-8."""
    header = csv_line(columns)
    parts, current, size = [], [], len(header.encode("utf-8"))
    for row in rows:
        line = csv_line(row)
        length = len(line.encode("utf-8"))
        if current and size + length > max_bytes:
            parts.append(header + "".join(current))
            current, size = [], len(header.encode("utf-8"))
        current.append(line)
        size += length
    if current or not parts:
        parts.append(header + "".join(current))
    return parts


def write_parts(out_dir, stem, parts):
    paths = []
    for number, text in enumerate(parts, 1):
        suffix = f"_part{number}" if len(parts) > 1 else ""
        path = os.path.join(out_dir, f"{stem}{suffix}.csv")
        with open(path, "w", encoding="utf-8", newline="") as file:
            file.write(text)
        paths.append(path)
    return paths


# ---------------------------------------------------------------- main

def convert(backup, out_dir, dates="smart", window_days=30, bulk_per_day=5, tag="", tmdb_token="",
            include_reviews=True):
    username = backup.get("username") or "must"
    films, tv = build_entries(backup)
    watched = [entry for entry in films if entry["list"] == "watched"]
    want = [entry for entry in films if entry["list"] == "want"]
    if not include_reviews:
        for entry in watched:
            entry["review"] = ""
    if tmdb_token:
        tmdb_enrich(watched + want, tmdb_token)

    date_stats = apply_date_policy(watched, dates, window_days, bulk_per_day)
    watched = sort_watched(watched)

    os.makedirs(out_dir, exist_ok=True)
    files = []
    files += write_parts(out_dir, f"{username}_letterboxd_watched",
                         csv_parts(WATCHED_COLUMNS, [watched_row(e, tag) for e in watched]))
    files += write_parts(out_dir, f"{username}_letterboxd_watchlist",
                         csv_parts(WATCHLIST_COLUMNS, [watchlist_row(e) for e in want]))
    tv_rows = [[e["list"], e["must_id"], e["type"], e["title"], e["year"], e["status"], e["rating"],
                e["date"], e["review"]] for e in tv]
    files += write_parts(out_dir, f"{username}_must_tv", [csv_line(TV_COLUMNS) + "".join(map(csv_line, tv_rows))])

    backup_path = os.path.join(out_dir, f"{username}_must_backup.json")
    with open(backup_path, "w", encoding="utf-8") as file:
        json.dump(backup, file, ensure_ascii=False, indent=1)
    files.append(backup_path)

    report = build_report(username, watched, want, tv, date_stats, dates, window_days, bulk_per_day, bool(tmdb_token))
    report_path = os.path.join(out_dir, f"{username}_report.txt")
    with open(report_path, "w", encoding="utf-8") as file:
        file.write(report)
    files.append(report_path)
    return files, report


def build_report(username, watched, want, tv, stats, dates, window_days, bulk_per_day, used_tmdb):
    rated = sum(1 for e in watched if e["rating"])
    reviewed = sum(1 for e in watched if e["review"])
    lines = [
        f"Must -> Letterboxd export for @{username}",
        "",
        f"Watched films:   {len(watched)} (rated {rated}, with review {reviewed})",
        f"Watchlist films: {len(want)}",
        f"TV (skipped, Letterboxd is films only): {len(tv)}",
        "",
        f"Watch dates (mode: {dates}):",
        f"  kept as diary dates:            {stats['kept']}",
    ]
    if dates == "smart":
        lines += [
            f"  dropped, first {window_days} days on Must:  {stats['window']} "
            f"({stats['first_date']} .. {stats['window_end']})",
            f"  dropped, {bulk_per_day}+ films on one day:   {stats['bulk']}",
        ]
    lines += [f"  no date in Must:                {stats['missing']}", ""]
    if used_tmdb:
        unmatched = [e for e in watched + want if not e["tmdb_id"]]
        lines.append(f"Not found on TMDB ({len(unmatched)}; Letterboxd will match these by title and year):")
        lines += [f"  - {e['title']} ({e['year'] or '?'})" for e in unmatched]
        lines.append("")
    no_year = [e for e in watched + want if not e["year"]]
    if no_year:
        lines.append(f"Films without a release year (check them in the Letterboxd importer): {len(no_year)}")
        lines += [f"  - {e['title']}" for e in no_year]
        lines.append("")
    return "\n".join(lines) + "\n"


def load_backup(path):
    with open(path, encoding="utf-8") as file:
        backup = json.load(file)
    if not isinstance(backup, dict) or "profile" not in backup or "products" not in backup:
        raise RuntimeError(f"{path} is not a Must backup made by browser_export.js or this script")
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export a public Must profile to Letterboxd CSV files.")
    parser.add_argument("username", nargs="?", help="Must username or profile URL (e.g. vladimirsalov)")
    parser.add_argument("--from-json", metavar="FILE", help="convert a saved Must backup instead of downloading")
    parser.add_argument("--out-dir", default="letterboxd_export", help="output directory (default: %(default)s)")
    parser.add_argument("--dates", choices=["smart", "all", "none"], default="smart",
                        help="which Must dates become Letterboxd diary dates (default: %(default)s)")
    parser.add_argument("--window-days", type=int, default=30,
                        help="smart: drop dates this many days after the first one (default: %(default)s)")
    parser.add_argument("--bulk-per-day", type=int, default=5,
                        help="smart: drop dates of days with this many films or more (default: %(default)s)")
    parser.add_argument("--tag", default="", help="Letterboxd tag for imported diary entries, e.g. must-import")
    parser.add_argument("--no-reviews", action="store_true", help="do not export reviews (they are public on Letterboxd)")
    parser.add_argument("--lang", default="en", help="Must title language (default: %(default)s)")
    parser.add_argument("--tmdb-token", default=os.environ.get("TMDB_TOKEN", ""),
                        help="TMDB API Read Access Token for exact IDs (default: $TMDB_TOKEN)")
    args = parser.parse_args(argv)

    if args.from_json:
        backup = load_backup(args.from_json)
    elif args.username:
        username = normalize_username(args.username)
        print(f"Downloading Must profile @{username}...", file=sys.stderr)
        backup = fetch_must_backup(username, args.lang)
    else:
        parser.error("give a Must username or --from-json FILE")

    files, report = convert(backup, args.out_dir, args.dates, args.window_days, args.bulk_per_day,
                            args.tag, args.tmdb_token, not args.no_reviews)
    print(report)
    print("Files:")
    for path in files:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
