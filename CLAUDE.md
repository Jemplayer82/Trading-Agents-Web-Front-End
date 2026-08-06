# CLAUDE.md

Repo-specific notes for AI coding agents (and humans) working on ai-trading-desk.

## Tier branches are generated — never develop on them

This project ships at four cumulative tiers. `master` **is** tier 4 (the full
product) and is the only branch anyone develops on. The other three are
generated artifacts produced by `scripts/make_tier.py` and published by
`.github/workflows/tiers.yml`:

| Tier | Branch | Adds |
|---|---|---|
| 1 — Base | `tier-1-base` | single-ticker AI analysis |
| 2 — Brokerage | `tier-2-brokerage` | + Schwab account scanning |
| 3 — Scanner | `tier-3-scanner` | + weekly S&P 500 scanner |
| 4 — Full | `master` | + daily options paper trading |

Every regeneration force-pushes over the tier branches, so any commit made
directly on one is destroyed the next time the workflow runs.

To change what a tier contains, edit `scripts/make_tier.py`'s manifest and the
`# TIER:N BEGIN/END` (or `<!-- TIER:N BEGIN/END -->`) marker blocks in the
files it strips — `web/static/index.html`, `web/nginx.conf`,
`docker-compose.yml`, `web/credentials.py`, and `README.md` — then regenerate:

    gh workflow run tiers.yml -f tiers=all

(pass e.g. `-f tiers=1,3` to regenerate only specific tiers).

### README.md is generated input

`README.md` carries TIER:N marker blocks **and** a `<!-- TIER-IDENTITY -->`
block that `make_tier.py` rewrites per tier, so each generated branch's landing
page states which tier it is. If you edit the identity block, update
`TIER_IDENTITY[4]` in `scripts/make_tier.py` in the same commit — a hard
assertion fails CI otherwise (that assertion is what keeps a tier-4 build
byte-identical to master). Never state a tab or service COUNT in the README;
counts differ per tier and go stale.

## Guard hook

A commit-blocking guard for tier branches lives at
`scripts/git-hooks/pre-commit`. It is not active by default; enable it for
your local checkout with:

    git config core.hooksPath scripts/git-hooks
