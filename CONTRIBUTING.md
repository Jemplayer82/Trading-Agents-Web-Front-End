# Contributing to Trading-Agents-Web-Front-End

Thank you for your interest in contributing! This project is licensed under the
**Apache License 2.0** and maintained by Landon ([@jemplayer82](https://github.com/jemplayer82)).

---

## Contributor License Agreement (CLA)

By submitting a pull request or patch to this project, you agree to the following:

1. **You own the contribution** — or have the legal right to submit it.
2. **You grant a perpetual, irrevocable license** to the project maintainer(s) to use,
   modify, sublicense, and distribute your contribution under the Apache License 2.0
   **and any future license the maintainer chooses**, including a commercial license.
3. **You grant patent rights** — you license any patents you hold that are necessarily
   infringed by your contribution.
4. **You understand** that your contribution will be publicly visible and may be
   redistributed under the terms of this project's license.

> **Why a CLA?** This project may offer a dual-license model in the future (open source +
> commercial). The CLA ensures the maintainer can offer commercial licenses without
> needing to re-contact every contributor.

If you are contributing on behalf of a company or organization, you represent that
you are authorized to accept these terms on their behalf.

---

## How to Contribute

### Reporting Issues
- Search existing issues before opening a new one.
- Include steps to reproduce, expected behavior, and actual behavior.
- Label appropriately: `bug`, `enhancement`, `question`.

### Submitting Pull Requests
1. Fork the repo and create a feature branch from `master`.
2. Write clear, focused commits — one logical change per commit.
3. Add or update tests where applicable.
4. Update documentation if your change affects behavior or API surface.
5. Open a PR with a clear description of what and why.

### Code Style
- Follow the existing conventions in the codebase.
- `uvx ruff check .` must pass before opening a PR (CI enforces this).
- `uv run --extra web python -m pytest -q` must be green.
- Keep PRs focused — avoid bundling unrelated changes.

### Commit Messages
Use the conventional commits format:
```
feat: add hermes broadcast to named channel
fix: handle reconnect on bus timeout
docs: update README with agent coordination example
```

---

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](./LICENSE).
