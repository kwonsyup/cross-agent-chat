# Contributing

Cross Agent Chat is a macOS beta under active correctness work. The
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
provider roots and launchd (the installer additionally requires
`CROSS_AGENT_CHAT_APPROVE=1`). `doctor` is safe to run anywhere; it is
read-only and reports `needs setup` on an unconfigured host.

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
- Exact-recipient selection. Ambiguous names, name selection against an
  incomplete roster, a raw pre-upgrade handle, and a token whose session or
  generation no longer re-attests all refuse before any effect. Endpoint tokens are self-contained;
  there is no binding store to migrate.
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
note the verified versions in the release notes (see
[CHANGELOG.md](CHANGELOG.md)).

### Qualifying a Codex update

Measure the actual owner with `codex app-server daemon version`; `codex
--version` measures the invoking CLI. Native App's embedded binary and helper
route require their own qualification. Keep the exact version allowlist until
both protocol checks and real original-session use pass.

Generate the official experimental schema with each bound binary:

```sh
/path/to/baseline/codex app-server generate-json-schema --experimental --out /tmp/cac-baseline
/path/to/candidate/codex app-server generate-json-schema --experimental --out /tmp/cac-candidate
```

Compare `v1/Initialize{Params,Response}.json` and
`v2/{ThreadRead,ThreadTurnsList,ThreadQueueAdd,TurnSteer}{Params,Response}.json`,
including their referenced types. Check the fields consumed by
`codex_daemon.py`: profile, original thread/cwd/originator, direct-input
eligibility, active turn, expected turn, input and event identity, and exact
acceptance receipts. A schema match alone does not qualify runtime behavior.
Run `pytest -q tests/test_codex_daemon.py` and the affected transport tests.

Use a disposable ordinary TUI original on the candidate's existing daemon.
Verify its route PID is that daemon and its originator is `codex-tui`, then
exercise a useful request and independently checked answer while idle and a
distinct request during an active turn. Preserve unknown events without
replay. In 0.163.0, arbitrary `-c` overrides (including a reasoning-effort
override) select an embedded server instead of reusing the daemon; do not
change the owner check to accommodate that different route. Use the provider's
normal session settings when testing the daemon path. Unset inherited Native
originator metadata when launching a terminal original from a Native tool.

After transactional installation, repeat discovery, request, original use and
answer on the shipped, newly loaded code. Record source, installed files,
loaded process evidence and recipient use separately. Retained productive
sessions keep their loaded code until their normal lifecycle permits adoption.

## Reporting issues

Include, redacted: `cross-agent-chat --version`, provider and harness, macOS
version/architecture, receiving mode, expected vs. actual result,
`doctor --json` output, and relevant event IDs with `chat_status` output. Do
not attach credentials, session identifiers, message bodies, provider
transcripts, or `~/.cache/cross-agent-chat/backups/` snapshots — they can
contain secrets from your configuration files. For security issues use
GitHub's private vulnerability reporting (see [SECURITY.md](SECURITY.md)).
