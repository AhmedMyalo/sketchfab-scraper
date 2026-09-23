# sketchfab-scraper

Full-catalog scraper for [Sketchfab](https://sketchfab.com): every model
across all 18 categories, with full metadata and comments. Runs entirely on
GitHub Actions -- never on the owner's machine, never on the owner's IP.

## Why this is simpler than the sibling CGTrader project

Sketchfab exposes a genuinely open, unauthenticated JSON API at
`sketchfab.com/v3/*`. Confirmed empirically before writing any scraping code:
no WAF/Cloudflare challenge on the data endpoints (only the docs/sitemap
pages are protected), no rate limiting observed, and stable deep pagination.
So there is no browser automation, no cookie-transplant trick, no WAF-solving
step anywhere in this repo -- just polite plain HTTP.

Actual file downloads (STL/OBJ/etc.) require an authenticated Sketchfab
account (`/download` returns 401 unauthenticated) and are out of scope here.
This project collects metadata + comments only.

## How it works

Two phases, one script (`sketchfab_scraper.py`):

1. **Listing** (`--list-category <slug>`): walks one category's paginated
   model list (`sort_by=-publishedAt`, the one ordering measured to be
   stable -- the default ordering repeats/reshuffles slightly between
   requests). Resumable via a persisted opaque cursor
   (`sketchfab_raw/<slug>.cursor`); writes `sketchfab_raw/<slug>.complete`
   once a category's listing has genuinely run out of pages.

2. **Detail** (`--detail`): reads every category's listing file, de-
   duplicates by model `uid` (a model can belong to more than one category),
   and fetches full detail + all comments for each model not yet fetched.
   Skips the comments call entirely for models with `commentCount == 0`.

Both phases write append-only, sharded JSONL (`sketchfab_details/`,
`sketchfab_comments/`) so a run can be killed at any point and resumed
without re-fetching anything already on disk.

`--export` rebuilds `sketchfab_models.csv` and `sketchfab_comments.csv` from
the JSONL shards.

## Data notes

- A model's displayed `commentCount` and the number of rows returned by the
  comments endpoint don't always match exactly (observed on a live model:
  232 vs. 252, with some of the 252 marked `isDeleted: true`). This is a
  property of the real data, not a bug here -- both numbers are kept as-is.
- Comments are real, unmoderated user-generated content from a live public
  site. Spam and phishing attempts (e.g. accounts impersonating "Sketchfab
  Support") were observed during testing and will appear in the dataset.
