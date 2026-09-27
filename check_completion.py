"""Compute scrape progress against the ACTUAL deadline-scoped target and
signal whether it's done, for the GitHub Actions workflow to act on.

Target definition changed 2026-09-27 when the deadline stayed fixed but the
measured SUSTAINED rate (779 models/h over a real 21h window) turned out
far too slow to detail the full ~106k comments+recency-filtered backlog in
time. "Done" now means the same random 18% sample the detail step itself
draws (--require-comments --since 2023-09-26 --sample-pct 18) is fully
detailed -- not "every category fully listed" (which was never realistic
under this deadline; 15 of 18 categories are still actively growing) and
not "every qualifying model detailed" (deliberately not the goal anymore).

Reuses sketchfab_scraper's own load_targets()/_in_random_sample() rather
than re-implementing the filter here, so this can never silently drift out
of sync with what the detail step actually selects.

Same design as the CGTrader sibling project for the two bugs already found
and fixed there:
  1. Completion must tolerate a small absolute residue of permanently
     unreachable models, or a scrape that is genuinely finished can never
     satisfy a strict >= check and the notification never fires.
  2. Any multi-line value handed to actions/github-script MUST go through
     `env`, never through raw ${{ }} string interpolation.
"""
import os

import sketchfab_scraper as S

SINCE = "2023-09-26"
SAMPLE_PCT = 18

# Absolute count, not a percentage: with a ~19k target, even 1% would be 190
# models quietly dropped. Small enough to still mean "essentially done".
RESIDUE_TOLERANCE = 50


def main():
    found_in, meta = S.load_targets()
    qualifying = [uid for uid in found_in
                  if meta.get(uid, (0, ""))[0] > 0
                  and meta.get(uid, (0, ""))[1][:10] >= SINCE]
    target = {uid for uid in qualifying if S._in_random_sample(uid, SAMPLE_PCT)}

    done_all = S.load_done_uids(S.DETAILS_DIR)
    done = done_all & target
    missing = target - done

    print(f"qualifying (comments + since {SINCE}): {len(qualifying):,}")
    print(f"sampled target (--sample-pct {SAMPLE_PCT}): {len(target):,}")
    print(f"detailed so far (within target): {len(done):,}")
    print(f"missing: {len(missing):,}")

    complete = len(target) > 0 and len(missing) <= RESIDUE_TOLERANCE

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"complete={'true' if complete else 'false'}\n")
            f.write(f"total_target={len(target)}\n")
            f.write(f"total_done={len(done)}\n")
            f.write(f"missing={len(missing)}\n")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## {'DONE' if complete else 'Still scraping'}\n\n")
            f.write(f"**{len(done):,} / {len(target):,}** sampled models detailed "
                    f"({len(missing):,} missing)\n\n")
            f.write(f"(qualifying pool before sampling: {len(qualifying):,})\n")

    if complete:
        print("\n=== SAMPLE TARGET COMPLETE ===")


if __name__ == "__main__":
    main()
