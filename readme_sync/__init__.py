#!/usr/bin/env python3
"""
readme-guardian — The README freshness guarantee for vibe coders.

Auto-detects your project, scans routes/modules/components, and keeps
README.md in sync. Project test and lint commands run only when explicitly
requested with --run-checks.

Usage:
    readme-guardian                        # Interactive mode
    readme-guardian --init                 # Add managed sections safely
    readme-guardian --apply                # Apply managed-section changes
    readme-guardian --check                # CI mode: exit 1 if stale
    readme-guardian --install-hook         # Pre-push hook (one-time)
    readme-guardian --uninstall-hook       # Remove pre-push hook
    readme-guardian --version              # Show version

"""

import argparse
import difflib
import html
import json
import re
import subprocess
import sys
from pathlib import Path


VERSION = "1.1.2"
MAX_SCAN_FILES = 2_500
MAX_SCAN_FILE_BYTES = 1_000_000


def debug(msg: str) -> None:
    if "--pre-push" in sys.argv:
        return
    print(f"  \U0001f4a1 {msg}", file=sys.stderr)


def ok(msg: str) -> None:
    if "--pre-push" in sys.argv:
        return
    print(f"  \u2713 {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    if "--pre-push" in sys.argv:
        return
    print(f"  \u26a0 {msg}", file=sys.stderr)


def fail(msg: str) -> None:
    if "--pre-push" in sys.argv:
        return
    print(f"  \u2717 {msg}", file=sys.stderr)


def banner() -> None:
    if "--pre-push" in sys.argv:
        return
    print(file=sys.stderr)
    print(f"  \U0001f6e1\ufe0f  readme-guardian v{VERSION}", file=sys.stderr)
    print("  \u2500" * 30, file=sys.stderr)


# ---------------------------------------------------------------------------
# Project detection (language-agnostic, zero config)
# ---------------------------------------------------------------------------

def detect_project(root: Path) -> dict:
    info = {
        "type": None,
        "name": "my-project",
        "version": "",
        "description": "",
        "scripts": {},
        "is_monorepo": False,
        "has_frontend": False,
        "has_docker": False,
        "test_command": None,
        "build_command": None,
        "lint_command": None,
        "tests": 0,
        "test_output": "",
        "test_status": "not-run",
        "routes": [],
        "modules": [],
        "components": [],
        "lint_pass": None,
    }

    # --- Package.json (Node/JS/TS) — detected FIRST for broader reach ---
    if (root / "package.json").exists():
        try:
            pkg = json.loads((root / "package.json").read_text())
            info["name"] = pkg.get("name", info["name"])
            info["version"] = pkg.get("version", "")
            info["description"] = pkg.get("description", "")
            info["scripts"] = pkg.get("scripts", {})

            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if any(x in deps for x in ("next", "react", "vue", "svelte", "vite")):
                info["has_frontend"] = True

            if "test" in info["scripts"]:
                info["test_command"] = ["npm", "test"]
            if "build" in info["scripts"]:
                info["build_command"] = ["npm", "run", "build"]
            if "lint" in info["scripts"]:
                info["lint_command"] = ["npm", "run", "lint"]

            workspaces = pkg.get("workspaces", [])
            info["is_monorepo"] = bool(workspaces)
        except Exception:
            pass

        if not info["type"]:
            info["type"] = "node"

    # --- pyproject.toml (Python) ---
    if (root / "pyproject.toml").exists():
        try:
            content = (root / "pyproject.toml").read_text()
            m = re.search(r'name\s*=\s*"([^"]+)"', content)
            if m:
                info["name"] = m.group(1)
            m = re.search(r'version\s*=\s*"([^"]+)"', content)
            if m:
                info["version"] = m.group(1)
            m = re.search(r'description\s*=\s*"([^"]+)"', content)
            if m:
                info["description"] = m.group(1)
        except Exception:
            pass

        info["type"] = "python"
        if not info["test_command"]:
            info["test_command"] = _find_python_cmd(root, "pytest")
        if not info["lint_command"]:
            info["lint_command"] = _find_python_cmd(root, "ruff")
        if (root / "frontend").is_dir():
            info["has_frontend"] = True

    # --- go.mod (Go) ---
    if (root / "go.mod").exists():
        try:
            first = (root / "go.mod").read_text().split("\n")[0]
            m = re.match(r"module\s+(\S+)", first)
            if m:
                info["name"] = m.group(1)
        except Exception:
            pass
        info["type"] = "go"
        info["test_command"] = ["go", "test", "./..."]
        info["build_command"] = ["go", "build", "./..."]

    # --- Cargo.toml (Rust) ---
    if (root / "Cargo.toml").exists():
        try:
            content = (root / "Cargo.toml").read_text()
            m = re.search(r'name\s*=\s*"([^"]+)"', content)
            if m:
                info["name"] = m.group(1)
            m = re.search(r'version\s*=\s*"([^"]+)"', content)
            if m:
                info["version"] = m.group(1)
        except Exception:
            pass
        info["type"] = "rust"
        info["test_command"] = ["cargo", "test"]
        info["build_command"] = ["cargo", "build"]

    if (root / "Dockerfile").exists():
        info["has_docker"] = True

    # Detect workspaces/monorepo
    if not info["is_monorepo"]:
        for f in ["pnpm-workspace.yaml", "lerna.json", "rush.json"]:
            if (root / f).exists():
                info["is_monorepo"] = True
                break

    return info


def _find_python_cmd(root: Path, tool: str) -> list[str] | None:
    """Check if a Python tool is available via venv or configured."""
    bin_path = root / ".venv" / "bin" / tool
    if bin_path.exists():
        return [str(bin_path)]
    if (root / "pyproject.toml").exists():
        with open(root / "pyproject.toml") as f:
            if tool in f.read():
                venv_python = root / ".venv" / "bin" / "python"
                if tool == "pytest":
                    return [str(venv_python), "-m", "pytest", "-q"] if venv_python.exists() else None
                elif tool == "ruff":
                    return [str(venv_python), "-m", "ruff", "check", "."] if venv_python.exists() else None
    return None


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def _run(cmd: list[str], root: Path, timeout: int = 60) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=root, timeout=timeout)
        return r.returncode, (r.stdout + "\n" + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "(timeout)"
    except FileNotFoundError:
        return -1, "(not found)"


SKIP_DIRS = {
    ".git",
    ".hg",
    ".cache",
    ".idea",
    ".nuxt",
    ".turbo",
    ".vscode",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__tests__",
    "build",
    "dist",
    "node_modules",
    "coverage",
    "spec",
    "test",
    "tests",
    "target",
    "vendor",
    "__pycache__",
}


def iter_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*"):
        if len(paths) >= MAX_SCAN_FILES:
            warn(f"Scan limited to {MAX_SCAN_FILES} source files")
            break
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in suffixes and path.stat().st_size <= MAX_SCAN_FILE_BYTES:
            paths.append(path)
    return sorted(paths)


def collect_tests(info: dict, run_checks: bool) -> int | None:
    cmd = info.get("test_command")
    if not cmd:
        info["test_status"] = "not-configured"
        return 0
    if not run_checks:
        info["test_status"] = "not-run"
        return None
    debug(f"Running: {' '.join(cmd)}")
    rc, output = _run(cmd, Path.cwd())
    info["test_output"] = output

    patterns = [
        (r"(\d+)\s+passed", 1),
        (r"ok\s+(\d+)\s+test", 1),
        (r"test result:\s+ok\.\s+(\d+)\s+passed", 1),
        (r"Tests:\s+(\d+)", 1),
    ]
    for pat, group in patterns:
        m = re.search(pat, output)
        if m:
            n = int(m.group(group))
            info["test_status"] = "passed"
            ok(f"Tests: {n} passing")
            return n
    if rc == 0:
        info["test_status"] = "passed"
        ok("Tests: passed (count unknown)")
        return -1
    info["test_status"] = "failed"
    warn("Tests: failed or timed out")
    return 0


def collect_routes(info: dict) -> list[str]:
    routes = set()
    root = Path.cwd()

    # FastAPI/Flask
    for path in iter_files(root, (".py",)):
        try:
            content = path.read_text(errors="ignore")
            for m in re.finditer(
                r'@(?:router|app)\.(get|post|put|delete|patch|options|head)\s*\(\s*["\']([^"\']+)',
                content,
                re.IGNORECASE,
            ):
                routes.add(f"{m.group(1).upper()} {m.group(2)}")
            for m in re.finditer(
                r'@(?:app)\.route\s*\(\s*["\']([^"\']+)["\']\s*,\s*methods\s*=\s*\[([^\]]+)\]',
                content,
                re.IGNORECASE,
            ):
                route_path = m.group(1)
                methods = re.findall(r'["\']([A-Z]+)["\']', m.group(2), re.IGNORECASE)
                for method in methods or ["GET"]:
                    routes.add(f"{method.upper()} {route_path}")
        except Exception:
            pass

    # Next.js App Router
    for path in root.glob("app/api/**/route.*"):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SCAN_FILE_BYTES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        rel = path.relative_to(root)
        parts = rel.parts
        route_parts = []
        for p in parts:
            if p == "app" or p.startswith("route."):
                continue
            if p.startswith("["):
                route_parts.append(f":{p.strip('[]')}")
            else:
                route_parts.append(p)
        route_path = "/" + "/".join(route_parts)
        try:
            content = path.read_text(errors="ignore")
            methods = re.findall(r"export\s+(?:async\s+)?function\s+(GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD)\b", content)
        except Exception:
            methods = []
        for method in methods or ["GET"]:
            routes.add(f"{method.upper()} {route_path}")

    # Express/Fastify
    for path in iter_files(root, (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
        try:
            content = path.read_text(errors="ignore")
            for m in re.finditer(r'\.(get|post|put|delete|all|patch|options|head)\s*\(\s*["\'`]([^"\'`]+)', content, re.IGNORECASE):
                routes.add(f"{m.group(1).upper()} {m.group(2)}")
        except Exception:
            pass

    # Go
    for path in iter_files(root, (".go",)):
        try:
            content = path.read_text(errors="ignore")
            for m in re.finditer(r'\.(Get|Post|Put|Delete|Patch|Handle|Options|Head)\s*\(\s*["\']([^"\']+)', content):
                routes.add(f"{m.group(1).upper()} {m.group(2)}")
        except Exception:
            pass

    result = sorted(routes)
    if result:
        ok(f"Routes: {len(result)} found")
    return result


def collect_modules(info: dict) -> list[str]:
    modules = set()
    root = Path.cwd()

    # Python
    for d in root.glob("app/*/__init__.py"):
        modules.add(d.parent.name)
    for d in root.glob("src/*/__init__.py"):
        modules.add(d.parent.name)

    # Node
    for d in root.glob("src/*/index.ts"):
        modules.add(d.parent.name)
    for d in root.glob("src/*/index.js"):
        modules.add(d.parent.name)

    # Go
    for d in root.glob("cmd/*/main.go"):
        modules.add(d.parent.name)
    for d in root.glob("internal/*/"):
        modules.add(d.name)

    # Rust
    for d in root.glob("src/*.rs"):
        modules.add(d.stem)

    # Any top-level source dirs
    for src_dir in ["app", "src", "lib", "cmd", "internal", "pkg"]:
        d = root / src_dir
        if d.is_dir():
            for item in sorted(d.iterdir()):
                if item.is_dir() and not item.name.startswith(("__", ".", "node_modules")):
                    modules.add(item.name)

    return sorted(modules)


def collect_components(info: dict) -> list[str]:
    if not info.get("has_frontend") and info.get("type") != "node":
        return []
    components = set()
    root = Path.cwd()

    frontend_root = root / "frontend" if (root / "frontend").is_dir() else root
    for path in iter_files(frontend_root, (".tsx", ".jsx", ".vue", ".svelte")):
        name = path.stem
        if not name.startswith("_") and name not in ("index", "layout", "page", "loading", "error"):
            components.add(name)

    if components:
        ok(f"Components: {len(components)} found")
    return sorted(components)


def collect_lint(info: dict, run_checks: bool) -> bool | None:
    cmd = info.get("lint_command")
    if not cmd or not run_checks:
        return None
    rc, _ = _run(cmd, Path.cwd())
    if rc == 0:
        ok("Lint: clean")
        return True
    warn("Lint: has issues")
    return False


# ---------------------------------------------------------------------------
# Badge SVG generation — self-updating freshness badge
# ---------------------------------------------------------------------------

BADGE_TEMPLATE = """\
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="20" role="img" aria-label="README: {status}">
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r">
    <rect width="{width}" height="20" rx="3" fill="#fff"/>
  </clipPath>
  <g clip-path="url(#r)">
    <rect width="{left_w}" height="20" fill="#555"/>
    <rect x="{left_w}" width="{right_w}" height="20" fill="{color}"/>
    <rect width="{width}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,DejaVu Sans,Geneva,sans-serif" font-size="11">
    <text x="{left_mid}" y="14" fill="#010101" fill-opacity=".3">{label}</text>
    <text x="{left_mid}" y="13">{label}</text>
    <text x="{right_mid}" y="14" fill="#010101" fill-opacity=".3">{message}</text>
    <text x="{right_mid}" y="13">{message}</text>
  </g>
</svg>"""


def generate_badge(info: dict) -> str:
    """Generate a shields.io-style SVG freshness badge."""
    label = "README"
    tests = info.get("tests", 0)
    lint = info.get("lint_pass")

    test_status = info.get("test_status")
    if test_status == "failed":
        status = "tests failed"
        message = "tests failed"
        color = "#e05d44"
    elif tests and tests > 0:
        status = "fresh"
        message = f"{tests} passing"
        color = "#4c1"  # brightgreen
    elif tests == -1:
        status = "fresh"
        message = "tests pass"
        color = "#4c1"
    elif test_status == "not-run":
        status = "synced"
        message = "checks skipped"
        color = "#dfb317"
    elif tests == 0 and lint is None:
        status = "synced"
        message = "synced"
        color = "#dfb317"
    elif tests == 0:
        status = "no-tests"
        message = "no tests"
        color = "#dfb317"  # yellow
    else:
        status = "stale"
        message = "stale"
        color = "#e05d44"  # red

    left_w = len(label) * 7 + 14
    right_w = len(message) * 7 + 14
    width = left_w + right_w
    left_mid = left_w // 2
    right_mid = left_w + right_w // 2

    return BADGE_TEMPLATE.format(
        width=width,
        left_w=left_w,
        right_w=right_w,
        left_mid=left_mid,
        right_mid=right_mid,
        label=label,
        message=message,
        color=color,
        status=status,
    )


def update_badge(info: dict) -> bool:
    """Generate and write the freshness badge SVG. Returns True if changed."""
    path = Path.cwd() / "readme-badge.svg"
    svg = generate_badge(info)
    if path.exists() and path.read_text() == svg:
        return False
    _write_text_safely(path, svg)
    ok(f"Badge: readme-badge.svg — {info.get('tests', 0)} tests passing")
    return True


# ---------------------------------------------------------------------------
# README generation — clean, beautiful, readable
# ---------------------------------------------------------------------------

def shield(label: str, value: str, color: str = "brightgreen") -> str:
    """Generate a shields.io badge."""
    import urllib.parse
    label_e = urllib.parse.quote(label)
    value_e = urllib.parse.quote(value)
    return f"![{label}](https://img.shields.io/badge/{label_e}-{value_e}-{color})"


def freshness_badge(info: dict) -> str:
    """Generate the green 'README fresh' badge."""
    test_str = (
        f"{info['tests']}%20passing"
        if info.get("tests", 0) and info["tests"] > 0
        else "tests%20failed"
        if info.get("test_status") == "failed"
        else "checks%20skipped"
        if info.get("test_status") == "not-run"
        else "synced"
    )
    return shield("README", f"fresh-{test_str}", "brightgreen")


def generate_readme(info: dict) -> str:
    """Generate a complete, beautiful README."""
    name = info["name"] or "my-project"
    desc = info["description"] or ""
    version = info.get("version", "")
    type_ = info.get("type", "unknown")
    tests = info.get("tests", 0)
    routes = info.get("routes", [])
    modules = info.get("modules", [])
    components = info.get("components", [])
    docker = info.get("has_docker", False)
    lint = info.get("lint_pass")

    # Avoid embedding the commit hash: a README cannot contain the hash of the
    # commit that contains it without becoming stale after every amend.
    lines = []
    lines.append(f"# {name}\n")
    if desc:
        lines.append(f"{desc}\n")

    # --- Badge row ---
    badges = [freshness_badge(info)]
    if version:
        badges.append(shield("version", version, "blue"))
    badges.append(shield("lang", type_, "blue"))
    if tests > 0:
        badges.append(shield("tests", f"{tests}%20passing", "brightgreen"))
    if lint is True:
        badges.append(shield("lint", "clean", "brightgreen"))
    elif lint is False:
        badges.append(shield("lint", "issues", "yellow"))
    if docker:
        badges.append(shield("docker", "ready", "blue"))
    lines.append(" ".join(badges) + "\n")

    # --- Shield.io style ---
    lines.append(
        '<p align="center">\n'
        "  <sub>README auto-synced by "
        '<a href="https://github.com/jeevesh2515/readme-guardian">'
        "readme-guardian</a> \U0001f6e1\ufe0f</sub>\n"
        "</p>\n"
    )

    # --- Quick Start ---
    lines.append("## \U0001f680 Quick Start\n")
    lines.append(_quick_start(info))

    # --- API Routes ---
    if routes:
        lines.append("## \U0001f4e1 API\n")
        lines.append("| Method | Path |\n|--------|------|\n")
        for r in routes[:25]:
            parts = r.split(" ", 1)
            if len(parts) == 2:
                lines.append(f"| **{parts[0]}** | `{parts[1]}` |\n")
        if len(routes) > 25:
            lines.append(f"| ... | {len(routes) - 25} more |\n")
        lines.append("")

    # --- Modules ---
    if modules:
        lines.append("## \U0001f4c2 Modules\n\n")
        for m in modules:
            lines.append(f"- `{m}`\n")
        lines.append("")

    # --- Components ---
    if components:
        lines.append("## \U0001f3a8 Components\n\n")
        for c in components:
            lines.append(f"- `{c}`\n")
        lines.append("")

    # --- Test Suite ---
    lines.append("## \U0001f9ea Test Suite\n\n")
    if tests > 0:
        lines.append(f"**{tests} tests passing** \u2705\n")
    elif tests == 0:
        lines.append("Tests: pending configuration.\n")
    elif tests == -1:
        lines.append("Tests: passing (count unknown).\n")

    # --- Freshness footer ---
    return "".join(lines)


def _quick_start(info: dict) -> str:
    t = info["type"]
    if t == "node":
        lines = [
            "```bash",
            "# Install dependencies",
            "npm install",
            "",
            "# Start development server",
            "npm run dev",
            "```",
        ]
        if info.get("build_command"):
            lines.insert(3, "")
            lines.insert(4, "# Build for production")
            lines.insert(5, "npm run build")
        return "\n".join(lines) + "\n"
    elif t == "python":
        return (
            "```bash\n"
            "# Set up virtual environment\n"
            "python -m venv .venv && source .venv/bin/activate\n"
            "pip install -e .\n"
            "\n"
            "# Start the app [adjust to your entry point]\n"
            "# python -m app.main\n"
            "```\n"
        )
    elif t == "go":
        return (
            "```bash\n"
            "# Build and run\n"
            "go build -o ./bin/app ./cmd/\n"
            "./bin/app\n"
            "```\n"
        )
    elif t == "rust":
        return (
            "```bash\n"
            "# Build and run\n"
            "cargo run\n"
            "```\n"
        )
    return "```bash\n# See project documentation for setup instructions\n```\n"


# ---------------------------------------------------------------------------
# README injection (marker-based, non-destructive)
# ---------------------------------------------------------------------------

MARKERS = {
    "stats": None,  # main stats table
    "routes": None,
    "modules": None,
    "components": None,
}

SECTION_TPL = """\
<!-- readme-guardian:{name} -->
{content}
<!-- /readme-guardian -->"""


class SafetyError(RuntimeError):
    """Raised when a write could escape the repository's expected files."""


def _write_text_safely(path: Path, content: str) -> None:
    """Write only a regular target file; never follow a symlink."""
    if path.exists() and path.is_symlink():
        raise SafetyError(f"Refusing to write symlinked file: {path.name}")
    path.write_text(content)


def _markdown_cell(value: object) -> str:
    """Render detected repository text safely inside a Markdown table cell."""
    return html.escape(str(value), quote=False).replace("|", "\\|").replace("\n", " ")


def _markdown_code(value: object) -> str:
    return _markdown_cell(value).replace("`", "\\`")


def is_initialized() -> bool:
    path = Path.cwd() / "README.md"
    if not path.exists() or path.is_symlink():
        return False
    return "<!-- readme-guardian:" in path.read_text(errors="ignore")


def _stats_content(info: dict) -> str:
    t = _markdown_cell(info.get("type") or "unknown")
    v = _markdown_cell(info.get("version") or "—")
    tests = info.get("tests")
    if info.get("test_status") == "failed":
        t_str = "failed"
    elif info.get("test_status") == "not-run":
        t_str = "not run"
    else:
        t_str = f"{tests} passing" if tests and tests > 0 else "passing (count unknown)" if tests == -1 else "not configured"
    lint = "clean" if info.get("lint_pass") else "issues" if info.get("lint_pass") is False else "—"
    docker = "yes" if info.get("has_docker") else "no"
    monorepo = "yes" if info.get("is_monorepo") else "no"
    return (
        "| Metric | Value |\n"
        "|--------|-------|\n"
        f"| Language | {t} |\n"
        f"| Version | {v} |\n"
        f"| Tests | {t_str} |\n"
        f"| Lint | {lint} |\n"
        f"| Docker | {docker} |\n"
        f"| Monorepo | {monorepo} |\n"
    )


def _routes_content(info: dict) -> str:
    routes = info.get("routes", [])
    if not routes:
        return "No API routes detected."
    lines = ["| Method | Path |", "|--------|------|"]
    for r in routes[:25]:
        parts = r.split(" ", 1)
        if len(parts) == 2:
            lines.append(f"| **{_markdown_cell(parts[0])}** | `{_markdown_code(parts[1])}` |")
    if len(routes) > 25:
        lines.append(f"| ... | {len(routes) - 25} more |")
    return "\n".join(lines)


def _modules_content(info: dict) -> str:
    modules = info.get("modules", [])
    if not modules:
        return "No source modules detected."
    return "\n".join(f"- `{_markdown_code(m)}`" for m in modules)


def _components_content(info: dict) -> str:
    comps = info.get("components", [])
    if not comps:
        return "No UI components detected."
    return "\n".join(f"- `{_markdown_code(c)}`" for c in comps)


def inject_readme(info: dict) -> str | None:
    """Replace markers in README. Returns updated content or None."""
    path = Path.cwd() / "README.md"
    if not path.exists():
        return None

    if path.is_symlink():
        raise SafetyError("Refusing to read symlinked README.md")
    content = path.read_text()
    touched = False

    rendered = {
        "stats": _stats_content(info),
        "routes": _routes_content(info),
        "modules": _modules_content(info),
        "components": _components_content(info),
    }

    for name, rc in rendered.items():
        start = f"<!-- readme-guardian:{name} -->"
        end = "<!-- /readme-guardian -->"
        pat = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
        replacement = start + "\n" + rc + "\n" + end
        if pat.search(content):
            content = pat.sub(replacement, content)
            touched = True

    if not touched:
        return None
    return content


def update_readme(info: dict) -> bool:
    """Apply marker injection. Returns True if changed."""
    path = Path.cwd() / "README.md"
    new = inject_readme(info)
    if new is None:
        return False
    if new == path.read_text():
        ok("README: already up to date")
        return False
    _write_text_safely(path, new)
    ok("README: updated")
    return True


# ---------------------------------------------------------------------------
# Safe initialization for projects without managed sections
# ---------------------------------------------------------------------------

def initialize_readme(info: dict) -> bool:
    """Append managed sections without replacing hand-written documentation."""
    path = Path.cwd() / "README.md"
    if path.exists() and path.is_symlink():
        raise SafetyError("Refusing to write symlinked README.md")
    if path.exists():
        content = path.read_text()
    else:
        content = f"# {_markdown_cell(info.get('name') or 'Project')}\n"

    if "<!-- readme-guardian:" in content:
        warn("README already has readme-guardian markers; use --apply instead")
        return False

    sections = [
        ("stats", _stats_content(info)),
        ("routes", _routes_content(info)),
        ("modules", _modules_content(info)),
        ("components", _components_content(info)),
    ]
    managed = "\n\n".join(SECTION_TPL.format(name=name, content=value) for name, value in sections)
    block = f"## Project facts\n\n![README status](./readme-badge.svg)\n\n{managed}\n"
    _write_text_safely(path, content.rstrip() + "\n\n" + block)
    _write_text_safely(Path.cwd() / "readme-badge.svg", generate_badge(info))
    ok("Initialized managed README sections and readme-badge.svg")
    return True


def desired_readme(info: dict) -> str | None:
    """Return the README content readme-guardian expects for this project."""
    injected = inject_readme(info)
    return injected


def planned_changes(info: dict) -> dict[str, tuple[str, str]]:
    """Return changed files as {path: (current, desired)} without writing."""
    changes: dict[str, tuple[str, str]] = {}

    readme_path = Path.cwd() / "README.md"
    if readme_path.exists() and readme_path.is_symlink():
        raise SafetyError("Refusing to read symlinked README.md")
    current_readme = readme_path.read_text() if readme_path.exists() else ""
    next_readme = desired_readme(info)
    if next_readme is not None and current_readme != next_readme:
        changes["README.md"] = (current_readme, next_readme)

    badge_path = Path.cwd() / "readme-badge.svg"
    if is_initialized():
        if badge_path.exists() and badge_path.is_symlink():
            raise SafetyError("Refusing to read symlinked readme-badge.svg")
        current_badge = badge_path.read_text() if badge_path.exists() else ""
        next_badge = generate_badge(info)
        if current_badge != next_badge:
            changes["readme-badge.svg"] = (current_badge, next_badge)

    return changes


def apply_changes(changes: dict[str, tuple[str, str]]) -> bool:
    for rel, (_, desired) in changes.items():
        _write_text_safely(Path.cwd() / rel, desired)
        ok(f"{rel}: updated")
    if not changes:
        ok("README: already up to date")
        return False
    return True


def print_preview(changes: dict[str, tuple[str, str]]) -> None:
    if not changes:
        ok("README is current")
        return

    warn("README is stale")
    for rel, (current, desired) in changes.items():
        print(file=sys.stderr)
        print(f"--- {rel}", file=sys.stderr)
        diff = difflib.unified_diff(
            current.splitlines(),
            desired.splitlines(),
            fromfile=f"{rel} (current)",
            tofile=f"{rel} (expected)",
            lineterm="",
        )
        for line in diff:
            print(line, file=sys.stderr)


# ---------------------------------------------------------------------------
# CI check
# ---------------------------------------------------------------------------

def ci_check(info: dict, changes: dict[str, tuple[str, str]] | None = None) -> bool:
    changes = planned_changes(info) if changes is None else changes
    if changes:
        for rel in changes:
            warn(f"{rel} is stale")
        fail("README is stale")
        return False
    ok("README is current")
    return True


# ---------------------------------------------------------------------------
# Hook management
# ---------------------------------------------------------------------------

HOOK_TEMPLATE = """\
#!/bin/sh
# readme-guardian pre-push hook - keeps README.md in sync safely
# Installed by: readme-guardian --install-hook
# Remove with: readme-guardian --uninstall-hook
# readme-guardian: managed hook

echo "  \U0001f6e1\ufe0f  readme-guardian: checking README..."
if ! git diff --quiet -- README.md readme-badge.svg 2>/dev/null; then
  echo "  README.md or readme-badge.svg already has uncommitted changes."
  echo "  Review and commit or stash them before pushing."
  exit 1
fi

if command -v readme-guardian >/dev/null 2>&1; then
  if ! readme-guardian --apply --run-checks --pre-push; then
    echo "  readme-guardian could not verify README freshness."
    exit 1
  fi
else
  if ! python3 -m readme_sync --apply --run-checks --pre-push; then
    echo "  readme-guardian could not run. Install it with pipx before pushing."
    exit 1
  fi
fi

if ! git diff --quiet -- README.md readme-badge.svg 2>/dev/null; then
  echo "  readme-guardian updated README.md/readme-badge.svg."
  echo "  Review and commit those changes, then push again."
  exit 1
fi
"""


def _hook_path() -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks/pre-push"],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    if result.returncode != 0:
        return None
    raw_path = Path(result.stdout.strip())
    return raw_path if raw_path.is_absolute() else Path.cwd() / raw_path


def install_hook() -> None:
    hook = _hook_path()
    if hook is None:
        fail("Not a git repository")
        return
    if hook.exists():
        if hook.is_symlink():
            fail("Refusing to replace symlinked pre-push hook")
            return
        if hook.read_text() != HOOK_TEMPLATE:
            fail("A pre-push hook already exists; readme-guardian will not overwrite it")
            return

    hook.parent.mkdir(parents=True, exist_ok=True)
    _write_text_safely(hook, HOOK_TEMPLATE)
    hook.chmod(0o755)
    ok(f"Pre-push hook installed at {hook}")

    ok("Hook will run silently before every `git push`")


def uninstall_hook() -> None:
    hook = _hook_path()
    if hook is not None and hook.exists() and not hook.is_symlink() and hook.read_text() == HOOK_TEMPLATE:
        hook.unlink()
        ok("Pre-push hook removed")
    else:
        warn("No standalone readme-guardian hook found; nothing was removed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="readme-guardian",
        description="Keep README.md and readme-badge.svg in sync with live project facts.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="append managed README sections without replacing existing docs")
    mode.add_argument("--apply", action="store_true", help="write README.md and readme-badge.svg updates")
    mode.add_argument("--check", action="store_true", help="exit 1 when README.md or badge is stale")
    mode.add_argument("--status", action="store_true", help="show whether README.md and badge are current")
    parser.add_argument("--install-hook", action="store_true", help="install the git pre-push hook")
    parser.add_argument("--uninstall-hook", action="store_true", help="remove the git pre-push hook")
    parser.add_argument("--run-checks", action="store_true", help="run detected project test and lint commands")
    parser.add_argument("--pre-push", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", "-v", action="store_true", help="show version")
    return parser


def collect_info(root: Path, run_checks: bool) -> dict:
    info = detect_project(root)
    if not info.get("type"):
        warn("Could not detect project type")
        warn("Supported: Node (package.json), Python (pyproject.toml), Go (go.mod), Rust (Cargo.toml)")
        sys.exit(1)

    debug(f"Detected: {info['type']} — {info['name']}")

    # Collect data
    info["modules"] = collect_modules(info)
    info["routes"] = collect_routes(info)
    info["components"] = collect_components(info)
    info["tests"] = collect_tests(info, run_checks)
    info["lint_pass"] = collect_lint(info, run_checks)

    return info


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.version:
        print(f"readme-guardian v{VERSION}")
        return

    banner()

    if args.install_hook:
        install_hook()
        return
    if args.uninstall_hook:
        uninstall_hook()
        return

    try:
        info = collect_info(Path.cwd(), args.run_checks)
    except SafetyError as exc:
        fail(str(exc))
        sys.exit(2)

    if args.init:
        try:
            initialize_readme(info)
        except SafetyError as exc:
            fail(str(exc))
            sys.exit(2)
        return

    if not is_initialized():
        warn("README is not initialized; run readme-guardian --init to add managed sections safely")
        sys.exit(2)

    try:
        changes = planned_changes(info)
    except SafetyError as exc:
        fail(str(exc))
        sys.exit(2)

    if args.check:
        sys.exit(0 if ci_check(info, changes) else 1)

    if args.status:
        if changes:
            for rel in changes:
                warn(f"{rel} is stale")
            sys.exit(1)
        ok("README is current")
        return

    if args.apply or args.pre_push:
        try:
            apply_changes(changes)
        except SafetyError as exc:
            fail(str(exc))
            sys.exit(2)
        return

    print_preview(changes)


if __name__ == "__main__":
    main()
