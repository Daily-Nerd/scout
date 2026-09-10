# scout

scout finds candidate repos for the Agent Memory Atlas: it watches GitHub search and a small set of subreddits over public RSS for projects that combine agent tooling with memory, skips anything the atlas already holds, and files one GitHub issue per new candidate so a person can decide what enters the atlas. Every command is a dry run by default and only writes when passed `--apply`. It never edits the atlas.

Dry run, writes nothing:

    uv run scout scan    # collect candidates from all sources
    uv run scout check   # drop candidates the atlas already holds
    uv run scout file    # print the issues that would be filed

Run for real:

    cp .env.example .env   # fill in SCOUT_GITHUB_TOKEN
    uv run scout run --apply
