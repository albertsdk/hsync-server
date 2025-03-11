## Description

Brief summary of the changes and why they are needed.

## Type of change

- [ ] Bug fix (non-breaking change)
- [ ] New feature (non-breaking change)
- [ ] Breaking change (protocol, API, or configuration)
- [ ] Documentation update
- [ ] Internal refactor / performance improvement

## Checklist

- [ ] Tests pass locally (`poetry run pytest`)
- [ ] Code is linted (`poetry run ruff check --fix . && poetry run ruff format .`)
- [ ] Documentation is updated:
  - [ ] `docs/` (user-facing docs) updated if public API or behavior changed
  - [ ] `llm-wiki/` (internal wiki) updated if internal architecture changed
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` if the change is user-facing
- [ ] OpenAPI schema regenerated if endpoints changed (`python scripts/export_openapi.py`)
- [ ] `docs/api.md` updated if API contract changed

## Related issues

Closes # (issue number) or N/A.
