"""Compute overall scrape progress and signal whether everything is done,
for the GitHub Actions workflow to act on.

Same design as the CGTrader sibling project, including the two bugs that
were found and fixed there -- avoided here from the start rather than
rediscovered:
  1. Completion must tolerate a small absolute residue of permanently
     unreachable models (deleted mid-scrape, etc.), or a scrape that is
     genuinely finished can never satisfy a strict >= check and the
     notification never fires.
  2. Any multi-line value handed to actions/github-script MUST go through
     `env`, never through raw ${{ }} string interpolation -- a value with a
     newline in it is a guaranteed JS syntax error that way.
"""
import json
import os

RAW_DIR = "sketchfab_raw"
DETAILS_DIR = "sketchfab_details"

CATEGORIES = [
    "animals-pets", "architecture", "art-abstract", "cars-vehicles",
    "characters-creatures", "cultural-heritage-history", "electronics-gadgets",
    "fashion-style", "food-drink", "furniture-home", "music", "nature-plants",
    "news-politics", "people", "places-travel", "science-technology",
    "sports-fitness", "weapons-military",
]

# Absolute count, not a percentage -- see the CGTrader project's README for
# why: a percentage of a multi-million-model catalog would hide thousands of
# silently-dropped models. Chosen generously since Sketchfab is expected to
# be several times CGTrader's scale.
RESIDUE_TOLERANCE = 100


def uids_in_jsonl(path):
    ids = set()
    if not os.path.exists(path):
        return ids
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                uid = json.loads(line).get("uid")
            except json.JSONDecodeError:
                continue
            if uid:
                ids.add(uid)
    return ids


def uids_in_shard_dir(d):
    ids = set()
    if not os.path.isdir(d):
        return ids
    for fn in os.listdir(d):
        if fn.startswith("part_") and fn.endswith(".jsonl"):
            ids |= uids_in_jsonl(os.path.join(d, fn))
    return ids


def all_categories_listed_complete():
    return all(os.path.exists(os.path.join(RAW_DIR, f"{c}.complete")) for c in CATEGORIES)


def main():
    target = set()
    lines = []
    for cat in CATEGORIES:
        cat_ids = uids_in_jsonl(os.path.join(RAW_DIR, f"{cat}.jsonl"))
        target |= cat_ids
        done_flag = "yes" if os.path.exists(os.path.join(RAW_DIR, f"{cat}.complete")) else "no"
        lines.append(f"| {cat} | {len(cat_ids):,} | listing complete: {done_flag} |")
        print(f"  {cat:26s} {len(cat_ids):>9,} models listed  (complete: {done_flag})")

    done = uids_in_shard_dir(DETAILS_DIR)
    total_target = len(target)
    total_done = len(done & target)  # ignore any stray ids not in a current listing
    missing = total_target - total_done
    all_listed = all_categories_listed_complete()

    print(f"\nUNIQUE MODELS DISCOVERED: {total_target:,}")
    print(f"DETAILS FETCHED:          {total_done:,}")
    print(f"MISSING:                  {missing:,}")
    print(f"ALL 18 CATEGORY LISTINGS COMPLETE: {all_listed}")

    # Completion requires BOTH: every category's listing has run to its
    # natural end (not just "budget ran out this chunk"), AND the detail
    # backlog is within tolerance. Checking only the second would call the
    # scrape "done" while half the catalog was never even listed yet.
    complete = all_listed and total_target > 0 and missing <= RESIDUE_TOLERANCE

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"complete={'true' if complete else 'false'}\n")
            f.write(f"total_target={total_target}\n")
            f.write(f"total_done={total_done}\n")
            f.write(f"missing={missing}\n")
            f.write("table<<EOF\n")
            f.write("| category | models listed | status |\n|---|---|---|\n")
            f.write("\n".join(lines))
            f.write("\nEOF\n")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## {'DONE' if complete else 'Still scraping'}\n\n")
            f.write(f"**{total_done:,} / {total_target:,}** models detailed "
                    f"({missing:,} missing)\n\n")
            f.write("| category | models listed | status |\n|---|---|---|\n")
            f.write("\n".join(lines) + "\n")

    if complete:
        print("\n=== SCRAPE COMPLETE ===")


if __name__ == "__main__":
    main()
