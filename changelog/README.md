# Changelog Fragments

This directory contains changelog fragments managed by [towncrier](https://towncrier.readthedocs.io/).

## Adding a Fragment

When making a change to this repository, add a changelog fragment:

```bash
# Create a fragment file: changelog/<PR-number>.<type>.md
echo "Description of your change" > changelog/42.feature.md
```

## Fragment Types

| Type          | Description                                              |
|---------------|----------------------------------------------------------|
| `breaking`    | Breaking changes to deployment, configuration, or APIs   |
| `deprecation` | Deprecated features or configurations                    |
| `feature`     | New deployment capabilities or integration tests         |
| `improvement` | Enhancements to existing deployment or CI workflows      |
| `fix`         | Bug fixes in deployment, Helm charts, or test suite      |
| `docs`        | Documentation improvements                               |
| `trivial`     | Internal changes not worth a changelog entry             |

## Building the Changelog

Fragments are compiled into `CHANGELOG.md` during version bumps:

```bash
uv run towncrier build --version YYYY.MM
```

Or use `--draft` to preview without modifying any files:

```bash
uv run towncrier build --draft
```
