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
