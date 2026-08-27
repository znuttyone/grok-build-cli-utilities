# grok-build-cli-utilities

## Before every commit or PR

Run `make ci`. Do not push on pytest-green alone.

`make ci` is the GitHub Actions `test` job locally:

1. ruff pin `>=0.15.0,<0.16` (`pyproject.toml` `dev` extra)
2. `ruff check .`
3. `ruff format --check .` — CI dies here before pytest
4. mypy
5. pytest with coverage

If format-check fails, run `make format` then `make ci` again.

Never bump local ruff to 0.16.x to make format pass. Match the pin. Pin bumps are the maintainer's (Dependabot).

If you touch `docs/`, also run `make docs-build`.
