# .scars/ — Negative knowledge for this repo

This directory records what this codebase **refused to be**: approaches that
were tried and failed (`deadend`), configuration that looks wrong but is
intentional (`fence`), and changes that break non-obvious things elsewhere
(`landmine`).

Before "cleaning up" anything these files anchor to — read the scar first.
Every scar carries evidence (commits, PRs, incidents). If a scar is stale,
challenge it: update or archive it with a note, don't ignore it.

## The contract (humans and agents)

1. **New scars start as candidates.** Copy `template.md`, write to
   `candidates/<slug>.md` with `status: candidate`. Never write directly
   into `.scars/` — only a human reviewer promotes (`scar promote`).
2. **YAML frontmatter is mandatory.** A scar without it is unparseable and
   will NEVER fire in any tool. `scar lint` checks; the hooks warn loudly.
3. **Promotion** = human review: `scar promote candidates/<slug>.md`.
4. **Evidence required.** A scar without a pr/issue/url/commit/incident
   reference is an opinion and can be challenged on sight. Prefer durable
   pr/issue/url refs — feature-branch commit SHAs orphan on squash-merge.

Format details: `template.md`. Project: SCAR.
