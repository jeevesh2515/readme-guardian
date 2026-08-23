import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

import readme_sync


@contextmanager
def cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class ReadmeGuardianTests(unittest.TestCase):
    def test_detects_routes_without_scanning_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "pyproject.toml", '[project]\nname = "api-demo"\nversion = "0.1.0"\n')
            write(
                root / "app.py",
                '''
from fastapi import FastAPI
app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/users")
def create_user():
    return {}
''',
            )
            write(
                root / "server.js",
                '''
app.patch("/users/:id", updateUser)
''',
            )
            write(root / "node_modules/pkg/index.js", 'app.get("/vendor", handler)\n')

            with cwd(root):
                self.assertEqual(
                    readme_sync.collect_routes({}),
                    ["GET /health", "PATCH /users/:id", "POST /users"],
                )

    def test_detects_next_routes_and_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(
                root / "package.json",
                '{"name":"web-demo","version":"1.2.3","dependencies":{"react":"latest","next":"latest"}}',
            )
            write(
                root / "app/api/users/[id]/route.ts",
                '''
export async function GET() {}
export function POST() {}
''',
            )
            write(root / "src/components/Button.tsx", "export function Button() { return null }\n")
            write(root / "src/components/page.tsx", "export default function Page() { return null }\n")

            with cwd(root):
                info = readme_sync.detect_project(root)
                self.assertEqual(info["type"], "node")
                self.assertEqual(
                    readme_sync.collect_routes(info),
                    ["GET /api/users/:id", "POST /api/users/:id"],
                )
                self.assertEqual(readme_sync.collect_components(info), ["Button"])

    def test_planned_changes_reports_stale_files_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = """# Demo

<!-- readme-guardian:stats -->
old stats
<!-- /readme-guardian -->
"""
            write(root / "README.md", stale)
            write(root / "readme-badge.svg", "old badge")

            info = {
                "type": "python",
                "version": "1.0.0",
                "tests": 3,
                "lint_pass": True,
                "has_docker": False,
                "is_monorepo": False,
                "routes": [],
                "modules": [],
                "components": [],
            }

            with cwd(root):
                changes = readme_sync.planned_changes(info)
                self.assertEqual(set(changes), {"README.md", "readme-badge.svg"})
                self.assertEqual((root / "README.md").read_text(), stale)
                self.assertEqual((root / "readme-badge.svg").read_text(), "old badge")

    def test_apply_changes_makes_check_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(
                root / "README.md",
                """# Demo

<!-- readme-guardian:stats -->
old stats
<!-- /readme-guardian -->

<!-- readme-guardian:routes -->
old routes
<!-- /readme-guardian -->
""",
            )

            info = {
                "type": "python",
                "version": "1.0.0",
                "tests": -1,
                "lint_pass": None,
                "has_docker": False,
                "is_monorepo": False,
                "routes": ["GET /health"],
                "modules": [],
                "components": [],
            }

            with cwd(root):
                changes = readme_sync.planned_changes(info)
                self.assertTrue(readme_sync.apply_changes(changes))
                self.assertTrue(readme_sync.ci_check(info))
                self.assertEqual(readme_sync.planned_changes(info), {})
                self.assertIn("tests pass", (root / "readme-badge.svg").read_text())

    def test_init_preserves_existing_readme_and_adds_managed_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = "# Demo\n\nThis hand-written section must stay.\n"
            write(root / "README.md", original)
            info = {
                "name": "demo",
                "type": "python",
                "version": "1.0.0",
                "tests": None,
                "test_status": "not-run",
                "lint_pass": None,
                "has_docker": False,
                "is_monorepo": False,
                "routes": [],
                "modules": [],
                "components": [],
            }

            with cwd(root):
                self.assertTrue(readme_sync.initialize_readme(info))
                updated = (root / "README.md").read_text()
                self.assertIn(original.rstrip(), updated)
                self.assertIn("<!-- readme-guardian:stats -->", updated)
                self.assertTrue((root / "readme-badge.svg").exists())

    def test_default_collection_does_not_execute_project_commands(self):
        info = {"test_command": ["definitely-not-a-real-command"]}
        self.assertIsNone(readme_sync.collect_tests(info, run_checks=False))
        self.assertEqual(info["test_status"], "not-run")

    def test_failed_tests_make_a_red_badge(self):
        badge = readme_sync.generate_badge({"tests": 0, "test_status": "failed", "lint_pass": None})
        self.assertIn("tests failed", badge)
        self.assertIn("#e05d44", badge)

    def test_badge_svg_uses_a_valid_gradient_percentage(self):
        badge = readme_sync.generate_badge({"tests": None, "test_status": "not-run", "lint_pass": None})
        self.assertIn('y2="100%"', badge)
        self.assertNotIn("100%%", badge)

    def test_badge_states_have_distinct_status_colors(self):
        fresh = readme_sync.generate_badge({"tests": 3, "test_status": "passed", "lint_pass": True})
        skipped = readme_sync.generate_badge({"tests": None, "test_status": "not-run", "lint_pass": None})
        failed = readme_sync.generate_badge({"tests": 0, "test_status": "failed", "lint_pass": None})
        self.assertIn("#4c1", fresh)
        self.assertIn("#dfb317", skipped)
        self.assertIn("#e05d44", failed)

    def test_escapes_detected_route_text_in_markdown(self):
        table = readme_sync._routes_content({"routes": ["GET /users/`name`|admin"]})
        self.assertIn("/users/\\`name\\`\\|admin", table)

    def test_pre_push_hook_blocks_after_updates_without_amending(self):
        self.assertIn("exit 1", readme_sync.HOOK_TEMPLATE)
        self.assertIn("Review and commit", readme_sync.HOOK_TEMPLATE)
        self.assertIn("--run-checks", readme_sync.HOOK_TEMPLATE)
        self.assertIn("already has uncommitted changes", readme_sync.HOOK_TEMPLATE)
        self.assertNotIn("commit --amend", readme_sync.HOOK_TEMPLATE)
    def test_stats_table_generation_with_custom_metrics(self):
        info = {
            "type": "python",
            "version": "1.1.2",
            "tests": 15,
            "test_status": "passed",
            "lint_pass": True,
            "has_docker": True,
            "is_monorepo": False,
        }
        table = readme_sync._stats_content(info)
        self.assertIn("15 passing", table)
        self.assertIn("clean", table)
        self.assertIn("yes", table)

    def test_empty_routes_returns_clean_fallback(self):
        table = readme_sync._routes_content({"routes": []})
        self.assertIn("No API routes detected.", table)

    def test_module_detection_ignores_hidden_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / ".hidden/secret.py", "pass")
            write(root / "src/core.py", "pass")
            with cwd(root):
                modules = readme_sync.collect_modules({"type": "python"})
                self.assertNotIn(".hidden", str(modules))

    def test_pyproject_version_fallback_handling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "pyproject.toml", '[project]\nname = "test-pkg"\n')
            with cwd(root):
                info = readme_sync.detect_project(root)
                self.assertEqual(info["name"], "test-pkg")


if __name__ == "__main__":
    unittest.main()
