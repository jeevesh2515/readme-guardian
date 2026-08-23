<p align="center">
  <a href="#readme-status">
    <img src="./readme-badge.svg" alt="Live README freshness status" />
  </a>
  <img src="https://img.shields.io/badge/version-1.1.2-1b75bb?style=flat-square" alt="Version 1.1.2" />
  <img src="https://img.shields.io/badge/license-MIT-2ea44f?style=flat-square" alt="MIT license" />
  <img src="https://img.shields.io/badge/AI--agent-ready-111827?style=flat-square" alt="AI agent ready" />
</p>

<h1 align="center">readme-guardian</h1>

<p align="center">
  <strong>Documentation freshness for fast-moving AI-assisted projects.</strong><br />
  Detect README drift, preview the facts, and keep a visible status signal beside the code.
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> &bull;
  <a href="#readme-status">Status badge</a> &bull;
  <a href="#automatic-guard">Push-time guard</a> &bull;
  <a href="#for-ai-agents">AI agents</a> &bull;
  <a href="#security">Security</a>
</p>

<!-- readme-guardian:stats -->
| Metric | Value |
|--------|-------|
| Language | python |
| Version | 1.1.2 |
| Tests | 15 passing (100%) |
| Lint | ruff passing |
| Docker | supported |
| Monorepo | no |

<!-- /readme-guardian -->

---

## The Problem

AI agents make it easy to ship code quickly. They also make it easy to leave a README describing a project that no longer exists.

`readme-guardian` turns a small, machine-verifiable part of the README into a checked artifact: project version, detected routes, modules, components, test result, and a local SVG freshness badge. It never replaces the hand-written explanation of your product.

## Quick Start

Run these commands from a project root:

```bash
# Check whether managed README facts are current. No files or project commands run.
readme-guardian --status

# One time only: append managed sections without replacing existing prose.
readme-guardian --init

# Preview the exact README.md and readme-badge.svg diff.
readme-guardian

# Apply only the reviewed managed sections.
readme-guardian --apply

# Enforce freshness in CI or before a pull request.
readme-guardian --check
```

To record a live test and lint result, opt in explicitly:

```bash
readme-guardian --apply --run-checks
readme-guardian --check --run-checks
```

`--run-checks` may execute the repository's own test and lint commands. Normal status, preview, apply, and check commands do not execute project scripts.

## Readme Status

Every initialized project gets a local `readme-badge.svg`:

```markdown
![README status](./readme-badge.svg)
```

<p align="center">
  <img src="https://img.shields.io/badge/README-12_passing-4c1?style=flat-square" alt="Green README badge example" />
  <img src="https://img.shields.io/badge/README-checks_skipped-dfb317?style=flat-square" alt="Yellow README badge example" />
  <img src="https://img.shields.io/badge/README-tests_failed-e05d44?style=flat-square" alt="Red README badge example" />
</p>

| Signal | Meaning | What to do |
|---|---|---|
| Green | Managed facts were generated after passing checks. | Keep shipping. |
| Yellow | Facts are synced, but checks were skipped or unavailable. | Run `--run-checks` when the repository is trusted. |
| Red | The last explicitly requested test run failed or timed out. | Fix the failing check, then apply again. |

The badge reports the result recorded during the last update. `readme-guardian --check` detects when a source change makes the managed README sections stale.

## Automatic Guard

Install the guarded pre-push hook once:

```bash
readme-guardian --install-hook
```

On each push, the hook runs the trusted-project check sequence and updates managed README files when facts changed. It then stops that push so you can review and commit the generated diff. The next push proceeds with the reviewed README.

```text
code changes
    |
    v
pre-push check and README refresh
    |
    +-- no changes --> push proceeds
    |
    +-- README changed --> review + commit --> push proceeds
```

The hook does not amend commits, rewrite history, replace an existing hook, or overwrite already-uncommitted README changes.

## What It Detects

| Project fact | Sources |
|---|---|
| Name and version | `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod` |
| API routes | FastAPI, Flask, Express, Fastify, Next.js App Router, common Go patterns |
| Modules and components | Conventional Python, Node, Go, Rust, React, Vue, and Svelte layouts |
| Test and lint result | Opt-in only, through `--run-checks` |
| Docker and monorepo hints | Conventional project files |

Managed sections use marker pairs such as:

```markdown
<!-- readme-guardian:stats -->
| Metric | Value |
|--------|-------|
| Language | python |
| Version | 1.1.2 |
| Tests | not configured |
| Lint | — |
| Docker | no |
| Monorepo | no |

<!-- /readme-guardian -->
```

Everything outside those markers remains yours: the story, diagrams, screenshots, setup instructions, architecture rationale, and contributor guidance.

## Install

### Python

Install the current public release from PyPI:

```bash
pipx install readme-guardian
readme-guardian --version
```

### npm

The npm package contains the matching Python wheel and runs it with `pipx` in an isolated environment. When registry publication is pending, use the matching GitHub release asset instead; it contains the same pinned wheel.

```bash
npm install -g readme-guardian
readme-guardian --status
```

```bash
# Release-asset fallback for the current version
npm install -g "https://github.com/jeevesh2515/readme-guardian/releases/download/v1.1.2/readme-guardian-1.1.2.tgz"
```

Release publication is automated through [PUBLISHING.md](PUBLISHING.md), which uses PyPI Trusted Publishing and moves npm to OIDC after its initial bootstrap publication.

## CI

Use a trusted CI environment to prevent managed facts from drifting:

```yaml
- run: pipx install "readme-guardian>=1.1.2,<2"
- run: readme-guardian --check --run-checks
```

`--check` exits nonzero when `README.md` or `readme-badge.svg` differs from the current detected project state.

## For AI Agents

The included [SKILL.md](SKILL.md) gives coding agents a disciplined README workflow: inspect the changes, check freshness, preview, apply managed facts, add human context, and verify.

```bash
# OpenCode project skill
mkdir -p .opencode/skills/readme-guardian
cp /path/to/readme-guardian/SKILL.md .opencode/skills/readme-guardian/SKILL.md

# Codex global skill
mkdir -p ~/.codex/skills/readme-guardian
cp /path/to/readme-guardian/SKILL.md ~/.codex/skills/readme-guardian/SKILL.md
```

The CLI supplies evidence. The agent supplies the explanation of why the project changed and how users should work with it.

## Security

- No telemetry or network calls from the Python CLI.
- No replacement of an unmarked README; `--init` appends managed sections.
- Writes only managed `README.md` content and `readme-badge.svg`.
- Refuses symlinked README, badge, and hook targets.
- Skips symlinks and common generated directories while scanning; source scans are bounded by file count and size.
- Runs project commands only with explicit `--run-checks` consent.
- npm delegates through argument arrays and a wheel bundled in its package; it does not interpolate shell arguments or silently install a global Python package.

Read the complete [security policy](SECURITY.md) before using `--run-checks` on an unfamiliar repository.

## Contributing

Help improve detection accuracy, guardrails, tests, and agent interoperability. Read [CONTRIBUTING.md](CONTRIBUTING.md), follow the [Code of Conduct](CODE_OF_CONDUCT.md), and include a small reproducible project shape in bug reports.

## License

[MIT](LICENSE)
