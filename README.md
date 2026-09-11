# scout

scout finds candidate repos for the Agent Memory Atlas: it watches GitHub search and a small set of subreddits over public RSS for projects that combine agent tooling with memory, skips anything the atlas already holds, and files one GitHub issue per new candidate so a person can decide what enters the atlas. Every command is a dry run by default and only writes when passed `--apply`. It never edits the atlas.

Dry run, writes nothing:

    uv run scout scan    # collect candidates from all sources
    uv run scout check   # drop candidates the atlas already holds
    uv run scout file    # print the issues that would be filed

Run for real:

    cp .env.example .env   # fill in SCOUT_GITHUB_TOKEN
    uv run scout run --apply

## Running on a schedule

### GitHub Action

The workflow in `.github/workflows/scout.yml` runs `scout run` every 6 hours
and on demand. Scheduled runs are dry runs: they scan, check and print the
summary, but nothing is written to GitHub. To file the candidate issues for
real, open the Actions tab, pick the scout workflow, choose "Run workflow"
and set the `apply` input to true. Only that explicit human dispatch with
`apply: true` writes; the cron schedule never does. The workflow prefers a
`scout-notary` GitHub App installation token when the `SCOUT_APP_ID` variable
and `SCOUT_APP_PRIVATE_KEY` secret are set, and falls back to the built-in
`GITHUB_TOKEN` until then. Either way, search needs no special scope and the
issues are filed in this same repository. Only local runs need a personal
token in `.env`.

### Locally with cron or launchd

Copy `.env.example` to `.env`, fill in `SCOUT_GITHUB_TOKEN`, then add a
crontab line like this one, which runs scout every 6 hours and appends the
summary to a local log:

    0 */6 * * * cd /path/to/scout && /path/to/uv run scout run >> state/cron.log 2>&1

On macOS you can also use a launchd plist with the same command on a 6 hour
interval. Either way, keep `--apply` off the command line until you want the
issues to be created, and remember the run log lands in `state/run.log`.

## Scoring, filing and refresh

Every candidate that survives the atlas check gets a score built from nine
components: stars, recency of the last push, a memory-ish term in the repo
name, README size, topic hits, term density in the description, whether the
git tree has a tests directory, a count of top-level source files, and a
penalty for repos that read as curated or awesome-style lists rather than
actual projects. Each component is a weight times a signal, and the weights
live in `scout.toml` under `[tiering]`. When the input a component needs was
never fetched, that component contributes nothing to the total instead of
scoring as zero, and it prints as `unknown` in issue bodies and run reports.
The total score sorts a candidate into tier A, B or C: `tier_a_min` and
`tier_b_min` set those cutoffs, and tier C candidates are never filed.
`tier_b_min` is the knob worth tuning if the bar for filing feels wrong:
raise it to file fewer candidates, lower it to file more.

Filing has its own cap, separate from tiering. `[filing] max_per_run` limits
how many issues one run creates, filing the highest-scored candidates first.
Anything over the cap is held rather than dropped: it stays pending in the
history and gets another chance on the next run, still ranked by score.

A run also refreshes stale rows before it ends. `[refresh] days` sets how
old a row's metadata can get before it needs a fresh look, and `[refresh]
max_per_run` caps how many rows get refreshed per run, oldest first. A
refreshed row gets a new GET to GitHub, a forced README and git tree fetch,
and a rescore. If that rescore lands on a different tier and the row
already has a filed issue, refresh swaps the tier label on that issue, but
only when running with `--apply`; a dry run only prints what it would
relabel. Refresh never files a new issue.

## Candidate rows

Each repo scout has seen gets one row in `data/candidates.jsonl`, marked
`kind: repo`; the file also carries `kind: post` rows for Reddit posts
already checked and one `kind: meta` row for internal bookkeeping. A repo
row's top-level fields, in the order they appear, are `kind`, always `repo`
here; `repo`, the `owner/name`; `first_seen_at` and `last_seen_at`, when
scout first and most recently saw it; `sources`, which scans found it;
`source_urls`, the source URL or URLs that led to it; and `status`, the
legacy `pending` or `filed` value kept so older readers that predate
`discovery` still work. `latest` holds the last candidate payload scout
collected, plus the cached `readme_bytes`, `tree_tests`, `tree_source_files`
and `tree_fetched_at` signals. `issue_number` is the GitHub issue number
once one exists, and `title_only` is true for a row rebuilt from an issue
title with no candidate payload behind it. `discovery` records what scout
did with the repo: `seen`, `filed`, `retracted` or `title_only`.
`assessment` records what the score said: `none`, `tier-a`, `tier-b`,
`tier-c` or `atlas-known`. `score` and `tier` are the numeric total and the
tier it produced. `components` holds one entry per scoring component
shaped as `{input, value}`, with both `null` when the component was absent,
and `absent_components` lists which components had no input that scoring.
`scored_at` is when the row was last scored, and `refresh_attempted_at` and
`refresh_error` record when refresh last tried the row and the error if
that attempt failed.

A discovery state of retracted or title_only says what scout did. It never
means the atlas analysed or rejected the project.

Running `scout repair --retracted-through N` backfills `discovery` and
`assessment` on rows written before those fields existed, and marks issues
at or below `N` as retracted.

Discovery history is persisted in `data/candidates.jsonl`. The local SQLite
database remains disposable runner state; the JSONL index is the durable record
used by scheduled runs to avoid reprocessing the same repos and Reddit posts.
On the first token-authenticated run, Scout imports existing
`scout:candidate` issues into this index. That migration is marked complete
only after every issue page succeeds; a failed migration aborts the run and is
retried on the next execution.
