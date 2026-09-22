# Contributing to Pensieve

Thank you for helping improve Pensieve. Bug reports, focused feature proposals, documentation fixes, and
pull requests are welcome.

## Before opening a change

- Search existing issues and pull requests to avoid duplicate work.
- Open an issue before a large architectural or product change.
- Never include feed contents, API keys, `.env` files, database dumps, or other private data in a report.
- Use the private process in `SECURITY.md` for vulnerabilities.

## Development workflow

1. Fork the repository and create a focused branch.
2. Follow the local setup in `README.md`.
3. Add or update tests for behavior changes.
4. Run `make lint`, `make test`, and `docker compose build`.
5. Open a pull request that explains the user-visible outcome and any deployment impact.

Keep migrations additive when possible and do not rewrite an already released migration. UI changes should
remain keyboard accessible, responsive, and usable with reduced motion enabled.

By contributing, you agree that your contribution is licensed under the MIT License.
