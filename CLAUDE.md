# CLAUDE.md

Repo-specific notes for AI coding agents (and humans) working on TradingAgents.

## Tier branches are generated — never develop on them

`tier/1`, `tier/2`, and `tier/3` are generated artifacts produced by
`scripts/make_tier.py` and published by `.github/workflows/tiers.yml`. Every
regeneration force-pushes over the branch, so any commit made directly on
one of these branches is destroyed the next time the workflow runs.

All development happens on `master`. To change what a tier contains, edit
`scripts/make_tier.py`'s manifest and the `# TIER:N BEGIN/END` (or
`<!-- TIER:N BEGIN/END -->`) marker blocks in the files it strips — see that
script's module docstring for the full list — then regenerate the tier
branches and images:

    gh workflow run tiers.yml -f tiers=all

(pass e.g. `-f tiers=1,3` to regenerate only specific tiers).

A commit-blocking guard for these branches lives at
`scripts/git-hooks/pre-commit`. It is not active by default; enable it for
your local checkout with:

    git config core.hooksPath scripts/git-hooks
