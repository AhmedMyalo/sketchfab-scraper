"""
Sketchfab full-catalog scraper.

Unlike the CGTrader/Cults3D siblings, this site exposes a genuinely open,
unauthenticated JSON API (confirmed empirically, see CULTS3D_BRIEFING-style
notes in the accompanying README): no WAF challenge, no browser automation,
no cookie transplant. Plain HTTP + politeness is enough. What IS still
needed from the sibling projects:

  - resumable, append-only, sharded JSONL storage (never re-fetch on restart)
  - a stable sort key for pagination (the API's default ordering repeats/
    reshuffles slightly between requests, same failure mode CGTrader had
    with "best_match" -- `sort_by=-publishedAt` was measured stable, zero
    duplicates over 6+ pages, and is used everywhere here)
  - global de-duplication by model uid, because a model can belong to more
    than one of the 18 categories (its `categories` field is a list) --
    same shape as CGTrader's cross-category overlap
  - politeness even though no rate limiting was observed in a quick burst
    test: a short delay+jitter between requests, because "no limit seen in
    10 requests" is not proof there's no limit under sustained load

Two-phase design, same shape as the CGTrader project:
  Phase 1 (--list-category): walk one category's listing pages (cheap, 24
    models/request) and record every model uid + the summary fields the
    listing already includes. Resumable via a persisted cursor.
  Phase 2 (--detail): read every category's listing file, de-duplicate by
    uid, and for each not-yet-fetched model do ONE detail call
    (/v3/models/<uid>) plus a comments call ONLY if commentCount > 0 (that
    count is already known from phase 1/detail, so models with zero
    comments cost one request instead of two).

Scope, confirmed with the user: model metadata + comments only. Actual 3D
file downloads require an authenticated Sketchfab account (the /download
endpoint returned 401 unauthenticated) and are explicitly out of scope.
"""
import argparse
import json
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

BASE = "https://sketchfab.com/v3"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
PAGE_SIZE = 24  # confirmed server-side hard cap; larger `count` values are silently ignored

CATEGORIES = [
    "animals-pets", "architecture", "art-abstract", "cars-vehicles",
    "characters-creatures", "cultural-heritage-history", "electronics-gadgets",
    "fashion-style", "food-drink", "furniture-home", "music", "nature-plants",
    "news-politics", "people", "places-travel", "science-technology",
    "sports-fitness", "weapons-military",
]

RAW_DIR = "sketchfab_raw"
DETAILS_DIR = "sketchfab_details"
COMMENTS_DIR = "sketchfab_comments"
SHARD_MAX_ROWS = 8000  # same reasoning as CGTrader: keep every file well under
                        # GitHub's 100MB/file limit regardless of row width

MAX_ATTEMPTS = 5
BACKOFF = [3, 8, 20, 45, 90]

_stop = False


def _sigint(signum, frame):
    global _stop
    if _stop:
        print("\n[force quit]")
        sys.exit(130)
    _stop = True
    print("\n[stopping after this item -- progress is saved. Ctrl+C again to quit now]")


def jittered_sleep(base):
    time.sleep(max(0.0, base + random.uniform(-base * 0.3, base * 0.3)))


def _get(url):
    """GET a URL, retrying on transient failures. Raises on a real HTTP error
    that persists past all retries so the caller can decide what to do."""
    last_exc = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # genuinely gone/never existed -- not a retry case
            last_exc = e
        except Exception as e:
            last_exc = e
        wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
        print(f"    {type(last_exc).__name__}: {last_exc} -- retry in {wait}s ({attempt + 1}/{MAX_ATTEMPTS})")
        time.sleep(wait)
    raise last_exc


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

def _license_fields(lic):
    lic = lic or {}
    return {
        "license_label": lic.get("label"),
        "license_slug": lic.get("slug"),
        "license_full_name": lic.get("fullName"),
        "license_requirements": lic.get("requirements"),
        "license_url": lic.get("url"),
    }


def _biggest_thumbnail(model):
    imgs = (model.get("thumbnails") or {}).get("images") or []
    if not imgs:
        return None
    return max(imgs, key=lambda im: im.get("width", 0)).get("url")


def flatten_model(model, found_in_categories=None):
    user = model.get("user") or {}
    row = {
        "uid": model.get("uid"),
        "name": model.get("name"),
        "description": model.get("description"),
        "viewerUrl": model.get("viewerUrl"),
        "embedUrl": model.get("embedUrl"),
        "thumbnail_url": _biggest_thumbnail(model),
        "createdAt": model.get("createdAt"),
        "publishedAt": model.get("publishedAt"),
        "staffpickedAt": model.get("staffpickedAt"),
        "viewCount": model.get("viewCount"),
        "likeCount": model.get("likeCount"),
        "commentCount": model.get("commentCount"),
        "animationCount": model.get("animationCount"),
        "soundCount": model.get("soundCount"),
        "faceCount": model.get("faceCount"),
        "vertexCount": model.get("vertexCount"),
        "isDownloadable": model.get("isDownloadable"),
        "isProtected": model.get("isProtected"),
        "isAgeRestricted": model.get("isAgeRestricted"),
        "price": model.get("price"),
        "tags": ", ".join(t.get("name", "") for t in (model.get("tags") or []) if isinstance(t, dict)) or None,
        "categories_api": ", ".join(sorted({c.get("name") for c in (model.get("categories") or []) if isinstance(c, dict)})) or None,
        "designer_uid": user.get("uid"),
        "designer_username": user.get("username"),
        "designer_displayName": user.get("displayName"),
        "designer_account": user.get("account"),
        "designer_profileUrl": user.get("profileUrl"),
        "found_in_categories": found_in_categories or "",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    row.update(_license_fields(model.get("license")))
    return row


def flatten_comment(model_uid, c):
    user = c.get("user") or {}
    return {
        "model_uid": model_uid,
        "comment_uid": c.get("uid"),
        "username": user.get("username"),
        "displayName": user.get("displayName"),
        "body": c.get("body"),
        "isDeleted": c.get("isDeleted"),
        "createdAt": c.get("createdAt"),
        "updatedAt": c.get("updatedAt"),
    }


# ---------------------------------------------------------------------------
# Phase 1: category listing crawl
# ---------------------------------------------------------------------------

def _cursor_path(slug):
    return os.path.join(RAW_DIR, f"{slug}.cursor")


def _sentinel_path(slug):
    return os.path.join(RAW_DIR, f"{slug}.complete")


def list_category(slug, delay, max_minutes=None):
    os.makedirs(RAW_DIR, exist_ok=True)
    out_path = os.path.join(RAW_DIR, f"{slug}.jsonl")
    cursor_path = _cursor_path(slug)

    if os.path.exists(_sentinel_path(slug)):
        print(f"{slug}: already marked complete (delete {_sentinel_path(slug)} to force a re-crawl)")
        return

    url = None
    if os.path.exists(cursor_path):
        with open(cursor_path, encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            url = saved
            print(f"{slug}: resuming from saved cursor")
    if url is None:
        url = f"{BASE}/models?categories={slug}&count={PAGE_SIZE}&sort_by=-publishedAt"

    t0 = time.time()
    n = 0
    with open(out_path, "a", encoding="utf-8") as fout:
        while url and not _stop:
            if max_minutes and (time.time() - t0) >= max_minutes * 60:
                print(f"{slug}: hit --max-minutes {max_minutes:g} after {n} models this run -- "
                      f"stopping cleanly, cursor saved for resume")
                break
            d = _get(url)
            if d is None:
                print(f"{slug}: got 404 mid-listing (unexpected) -- stopping this run")
                break
            for m in d.get("results", []):
                row = {
                    "uid": m.get("uid"),
                    "name": m.get("name"),
                    "commentCount": m.get("commentCount"),
                    "viewCount": m.get("viewCount"),
                    "likeCount": m.get("likeCount"),
                    "publishedAt": m.get("publishedAt"),
                    "category": slug,
                }
                fout.write(json.dumps(row, ensure_ascii=False) + "\n")
                n += 1
            fout.flush()
            url = d.get("next")
            with open(cursor_path, "w", encoding="utf-8") as fc:
                fc.write(url or "")
            if n % 240 == 0:
                rate = n / max(time.time() - t0, 1) * 3600
                print(f"  {slug}: {n:,} models listed so far ({rate:.0f}/h)")
            if url:
                jittered_sleep(delay)

    if url is None and not _stop:
        os.remove(cursor_path) if os.path.exists(cursor_path) else None
        with open(_sentinel_path(slug), "w", encoding="utf-8") as f:
            f.write(f"{n}\n")
        print(f"{slug}: listing complete, {n:,} models found this run, sentinel written")


# ---------------------------------------------------------------------------
# Sharded storage (models + comments), same pattern as the CGTrader project
# ---------------------------------------------------------------------------

def _shard_paths(d):
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, fn) for fn in os.listdir(d)
                  if fn.startswith("part_") and fn.endswith(".jsonl"))


class ShardedWriter:
    def __init__(self, out_dir, max_rows=SHARD_MAX_ROWS):
        os.makedirs(out_dir, exist_ok=True)
        self.dir = out_dir
        self.max_rows = max_rows
        shards = _shard_paths(out_dir)
        if shards:
            self.path = shards[-1]
            self.index = len(shards)
            with open(self.path, encoding="utf-8") as f:
                self.count = sum(1 for _ in f)
        else:
            self.index = 1
            self.path = os.path.join(out_dir, f"part_{self.index:05d}.jsonl")
            self.count = 0

    def append(self, row):
        if self.count >= self.max_rows:
            self.index += 1
            self.path = os.path.join(self.dir, f"part_{self.index:05d}.jsonl")
            self.count = 0
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.count += 1


def load_done_uids(details_dir):
    done = set()
    for p in _shard_paths(details_dir):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    done.add(json.loads(line)["uid"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return done


def load_targets():
    """Every uid discovered across all category listing files, with the set
    of categories it was found in (a model can be in more than one)."""
    found_in = {}
    if not os.path.isdir(RAW_DIR):
        return found_in
    for slug in CATEGORIES:
        p = os.path.join(RAW_DIR, f"{slug}.jsonl")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                uid = r.get("uid")
                if uid:
                    found_in.setdefault(uid, set()).add(slug)
    return found_in


# ---------------------------------------------------------------------------
# Phase 2: per-model detail + comments
# ---------------------------------------------------------------------------

def get_comments(uid):
    out = []
    url = f"{BASE}/comments?model={uid}"
    while url:
        d = _get(url)
        if d is None:
            break
        out.extend(d.get("results", []))
        url = d.get("next")
        if url:
            time.sleep(0.2)  # comment pages are cheap and rare; a short fixed
                              # pause is enough, no need for full jitter
    return out


def fetch_model_full(uid, found_in_categories):
    model = _get(f"{BASE}/models/{uid}")
    if model is None:
        return None, []
    row = flatten_model(model, found_in_categories=", ".join(sorted(found_in_categories)))
    comments = []
    if (model.get("commentCount") or 0) > 0:
        comments = [flatten_comment(uid, c) for c in get_comments(uid)]
    return row, comments


def run_detail_pass(delay, max_minutes=None):
    found_in = load_targets()
    if not found_in:
        raise SystemExit("no listing files found under sketchfab_raw/ -- run --list-category first")
    done = load_done_uids(DETAILS_DIR)
    todo = [uid for uid in found_in if uid not in done]
    print(f"{len(found_in):,} unique models discovered across all categories, "
          f"{len(done):,} already fetched, {len(todo):,} to fetch")
    if not todo:
        return

    writer = ShardedWriter(DETAILS_DIR)
    cwriter = ShardedWriter(COMMENTS_DIR)
    t0 = time.time()
    ok = failed = comments_total = 0
    for i, uid in enumerate(todo, 1):
        if _stop:
            break
        if max_minutes and (time.time() - t0) >= max_minutes * 60:
            print(f"\n[budget] hit --max-minutes {max_minutes:g} after {i - 1} of {len(todo)} "
                  f"models -- stopping cleanly so this run's progress is committed. Re-run to resume.")
            break
        try:
            row, comments = fetch_model_full(uid, found_in[uid])
        except Exception as exc:
            failed += 1
            print(f"  [{i}/{len(todo)}] FAILED {uid}: {type(exc).__name__}: {exc}")
            jittered_sleep(delay)
            continue
        if row is None:
            # confirmed 404 -- the model is gone; record a placeholder so it
            # is never retried on every future run (same fix that was needed
            # on the CGTrader project for its soft-404 case)
            writer.append({"uid": uid, "name": "[unavailable: 404]",
                            "found_in_categories": ", ".join(sorted(found_in[uid])),
                            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        else:
            writer.append(row)
            for c in comments:
                cwriter.append(c)
            comments_total += len(comments)
            ok += 1
        if (ok + failed) % 25 == 0 or i == len(todo):
            rate = (ok + failed) / max(time.time() - t0, 1) * 3600
            left = (len(todo) - i) / max(rate, 1)
            print(f"  [{i}/{len(todo)}] ok={ok} failed={failed} comments={comments_total:,} "
                  f"({rate:.0f}/h, ~{left:.1f}h left)")
        jittered_sleep(delay)

    print(f"\ndetail pass done: ok={ok} failed={failed} comments_collected={comments_total:,}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

MODEL_CSV_COLS = [
    "uid", "name", "found_in_categories", "categories_api", "tags",
    "viewCount", "likeCount", "commentCount", "designer_username",
    "designer_displayName", "designer_account", "designer_profileUrl",
    "price", "isDownloadable", "isProtected", "isAgeRestricted",
    "license_label", "license_slug", "license_full_name", "license_requirements",
    "license_url", "faceCount", "vertexCount", "animationCount", "soundCount",
    "createdAt", "publishedAt", "staffpickedAt", "description",
    "viewerUrl", "embedUrl", "thumbnail_url", "fetched_at",
]
COMMENT_CSV_COLS = ["model_uid", "comment_uid", "username", "displayName",
                     "body", "isDeleted", "createdAt", "updatedAt"]


def export_csv():
    import csv
    n_models = _export_one(DETAILS_DIR, "sketchfab_models.csv", MODEL_CSV_COLS, key="uid")
    n_comments = _export_one(COMMENTS_DIR, "sketchfab_comments.csv", COMMENT_CSV_COLS, key="comment_uid")
    print(f"exported {n_models:,} models -> sketchfab_models.csv")
    print(f"exported {n_comments:,} comments -> sketchfab_comments.csv")


def _export_one(shard_dir, out_path, cols, key):
    import csv
    seen = set()
    n = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fout:
        w = csv.DictWriter(fout, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for p in _shard_paths(shard_dir):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    k = r.get(key)
                    if k in seen:
                        continue
                    seen.add(k)
                    w.writerow(r)
                    n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-category", choices=CATEGORIES, help="phase 1: crawl one category's listing")
    ap.add_argument("--detail", action="store_true", help="phase 2: fetch full detail + comments for every discovered model")
    ap.add_argument("--test-uid", default=None, help="fetch and print one model's full detail + comments")
    ap.add_argument("--delay", type=float, default=0.4, help="avg seconds between requests")
    ap.add_argument("--max-minutes", type=float, default=None)
    ap.add_argument("--export", action="store_true", help="rebuild CSVs from the JSONL shards")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _sigint)

    if args.test_uid:
        row, comments = fetch_model_full(args.test_uid, {"test"})
        print(json.dumps(row, ensure_ascii=False, indent=2))
        print(f"\n{len(comments)} comments")
        if comments:
            print(json.dumps(comments[0], ensure_ascii=False, indent=2))
        return

    if args.list_category:
        list_category(args.list_category, args.delay, args.max_minutes)
        return

    if args.detail:
        run_detail_pass(args.delay, args.max_minutes)
        return

    if args.export:
        export_csv()
        return

    ap.print_help()


if __name__ == "__main__":
    main()
