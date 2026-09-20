# Contributing

Cross Agent Chat is a macOS prerelease under active correctness work. The
fastest useful contributions are contained bug reports, regression tests that
reproduce a real failure, and focused fixes that preserve the effect and
identity boundaries described below.

## Development setup

Requires macOS and Python ≥ 3.11.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

## Contained checks

```bash
.venv/bin/python -m pytest          # full contained suite; excludes live tests
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy --strict src tests
sh -n install.sh
.venv/bin/python -m build
```

These are the same checks CI runs on every push and pull request.

## Contained vs. live testing

The default suite is contained: `tests/conftest.py` redirects `HOME`,
`CODEX_HOME`, socket roots, temporary files, and provider/service subprocess
calls into fixture-owned paths, and fails any test that reaches for a real
`claude`, `codex`, `launchctl`, `lsof`, or `tailscale` binary. It is a guard,
not an OS sandbox — review any new child-process path you add.

Tests marked `live` spawn real provider subprocesses and are excluded by
default (`-m 'not live'` in `pyproject.toml`). Run them only on a supported
Mac where you intend the provider interaction, e.g. after a provider upgrade:

```bash
.venv/bin/python -m pytest -m live
```

A dev checkout is not an installed product. Do not run `install.sh` or
`cross-agent-chat setup` from a development environment unless you mean to
modify that Mac's real provider configuration — they write to the selected
Claude/Codex roots, `~/.config/devin`, and launchd. `doctor` is safe to run
anywhere; it is read-only and reports `needs setup` on an unconfigured host.

## Where things live

[docs/source-map.md](docs/source-map.md) maps each module to its
responsibility, shows where identity and durable effects are decided, and
lists the test file that covers each area. Read it before moving code between
modules — several test fixtures monkeypatch module names.

## Boundaries to preserve

Reviews reject changes that weaken these, even to make a test pass:

- No replay of accepted or uncertain work. `TRANSPORT_ACCEPTED` is custody,
  `UNKNOWN_DELIVERY` means an effect may have happened; neither may be retried
  by the product.
- Exact-recipient selection. Ambiguous names, incomplete discovery, and a
  handle that moved endpoints refuse before any effect.
- Bound sender identity. Codex thread metadata, Claude process/profile checks,
  and Devin's single-use trusted-hook capability — the model never nominates
  its own sender identity.
- Content-free durable state. Message bodies live in transient files, courier
  memory, or the provider's own queue/transcript — never in durable Cross
  Agent Chat records.
- Private file modes and atomic writes for anything under the state, cache,
  and runtime roots.
- Foreign configuration preservation: setup and uninstall touch only owned
  entries and restore recorded prior values.

When a provider update lands, record the installed provider version and the
native surfaces relied on, run the affected contained adapter contracts, then
update the tested-version notes in `README.md`.

## Reporting issues

Include, redacted: `cross-agent-chat --version`, provider and harness, macOS
version/architecture, receiving mode, expected vs. actual result,
`doctor --json` output, and relevant event IDs with `chat_status` output. Do
not attach credentials, session identifiers, message bodies, provider
transcripts, or `~/.cache/cross-agent-chat/backups/` snapshots — they can
contain secrets from your configuration files. For security issues use
GitHub's private vulnerability reporting (see [SECURITY.md](SECURITY.md)).
