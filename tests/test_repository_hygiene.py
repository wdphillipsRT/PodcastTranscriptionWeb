"""Guard the shipped runtime against accidentally restoring retired integrations."""

import ast
import os
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
OBSOLETE = (
    "Procfile", "app.json", "runtime.txt", "deploy.sh", "heroku-setup.sh",
    "summarize.py", "prompts.py", "storage.py", "podcast_pipeline.py",
    "check_redis_queue.py", "debug_episodes.py", "trigger_discovery.py",
    "DEPLOYMENT.md", "DEPLOY_NOW.md", "LAUNCH.md", "NETWORK_ACCESS.md", "TROUBLESHOOTING.md",
)
EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "env", ".worktrees", ".superpowers", ".planning",
    ".claude", ".codex", ".pytest_cache", "__pycache__", "tests", "docs",
    "node_modules", "data", "logs", "downloads", "tmp", "build", "dist",
}
PATTERNS = {
    "retired integration": r"\b(?:heroku|stripe|mailgun|openai|openrouter)\b|\b(?:DYNO|HEROKU_\w+|STRIPE_\w+|MAILGUN_\w+|OPENAI_\w+|OPENROUTER_\w+)\b",
    "retired route": r"[/'\"](?:register|registration|signup|subscribe|subscription|subscriptions|create-checkout-session)(?:[/'\"?]|\b)",
    "retired task or queue": r"\b(?:send_\w*email\w*|email_task\w*|summarization|summary_queue|summarize_\w+)\b|['\"](?:emails|summaries)['\"]",
    "account authentication": r"\b(?:import\s+oauth\w*|from\s+\w*oauth\w*|oauth_\w+|\w+_oauth|OAuth2?Session)\b|--(?:username|password|netrc)\b",
    "unsafe media option": r"(?<![\w-])--(?:cookies(?:-from-browser)?|plugin-dirs|remote-components|update(?:-to)?|exec(?:-before-download)?|config-locations)(?=[\s='\"]|$)|(?:yt[-_]dlp|args\s*=).*?(?-i:-U)(?=[\s'\"]|$)|\b(?:cookiefile|cookiesfrombrowser|remote_components|plugin_dirs)\b",
    "deleted module import": r"(?:from|import)\s+(?:summarize|prompts|storage|podcast_pipeline|check_redis_queue|debug_episodes|trigger_discovery)(?:\s|\.|$)",
}


def runtime_files(root):
    """Include production subpackages/assets and executable root entrypoints.

    Documentation/specs and tests are excluded, never production files. Positive
    Flask session-cookie security settings and yt-dlp's --no-* controls are valid.
    """
    for directory, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name != "__pycache__"
                          and not (Path(directory) == root and name in EXCLUDED_DIRS)]
        for name in files:
            path = Path(directory) / name
            root_file = path.parent == root
            if (path.suffix in {".py", ".html", ".js", ".css", ".sh", ".yml", ".yaml"}
                    or name.startswith("Dockerfile")
                    or root_file and (name in {".env.example", ".dockerignore", "Procfile", "app.json", "runtime.txt", "requirements.txt"}
                                      or os.access(path, os.X_OK))):
                yield path


def violations(path, content):
    found = []
    for number, line in enumerate(content.splitlines(), 1):
        for label, pattern in PATTERNS.items():
            if re.search(pattern, line, re.IGNORECASE):
                found.append(f"{path}:{number}: {label}")
        if (path.suffix == ".sh" or path.name.startswith("Dockerfile") or content.startswith("#!")):
            if re.search(r"(?:^\s*(?:(?:RUN|CMD|ENTRYPOINT|exec)\s+)?|[;&|]\s*)(?:yt-dlp|ffmpeg|deno)\s", line):
                found.append(f"{path}:{number}: shell-form media execution")
    if path.suffix == ".py":
        for node in ast.walk(ast.parse(content)):
            if not isinstance(node, ast.Call):
                continue
            function = ast.unparse(node.func)
            if function in {"os.system", "os.popen"}:
                found.append(f"{path}:{node.lineno}: shell execution")
            if function.startswith("subprocess."):
                shell = next((keyword.value for keyword in node.keywords if keyword.arg == "shell"), None)
                if shell is not None and not (isinstance(shell, ast.Constant) and shell.value is False):
                    found.append(f"{path}:{node.lineno}: shell execution")
                if node.args and isinstance(node.args[0], (ast.JoinedStr, ast.Constant)):
                    found.append(f"{path}:{node.lineno}: string-form subprocess")
    return found


def test_obsolete_files_are_absent():
    assert not [name for name in OBSOLETE if (ROOT / name).exists()]


def test_production_runtime_has_no_retired_integrations_or_unsafe_media_execution():
    found = []
    for path in runtime_files(ROOT):
        found.extend(violations(path.relative_to(ROOT), path.read_text()))
    assert not found, "\n" + "\n".join(found)


def test_current_documentation_has_no_deleted_links_or_legacy_setup():
    for name in ("README.md", "CHANGES.md", "docs/operations.md"):
        path = ROOT / name
        assert path.is_file(), f"Missing current documentation: {name}"
        content = path.read_text()
        assert not [obsolete for obsolete in OBSOLETE if obsolete in content], name
        assert not re.search(PATTERNS["retired integration"], content, re.IGNORECASE), name
    assert "docs/operations.md" in (ROOT / "README.md").read_text()


@pytest.mark.parametrize("content", [
    "SESSION_COOKIE_SECURE=true\nSESSION_COOKIE_HTTPONLY=True\nREMEMBER_COOKIE_SAMESITE=Lax",
    'args = ["--no-cookies", "--no-cookies-from-browser", "--no-plugin-dirs", "--no-remote-components", "--no-update"]',
    '<details><summary>Retry item</summary></details>',
    'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
])
def test_legitimate_security_controls_are_allowed(content):
    assert not violations(Path("runtime.txt"), content)


@pytest.mark.parametrize("content", [
    'args = ["--cookies", "private.txt"]', 'args = ["--cookies-from-browser=firefox"]',
    'args = ["--plugin-dirs", "/tmp/plugins"]', 'args = ["--remote-components=ejs:github"]',
    'args = ["--update"]', 'args = ["-U"]', 'opts = {"cookiefile": "private.txt"}',
    'subprocess.run(["yt-dlp"], shell=True)', 'subprocess.run("yt-dlp URL")',
    'os.system("yt-dlp URL")', 'import stripe', 'from openai import Client',
    '@app.post("/register")\ndef register(): pass', 'send_transcript_email.delay(1)', 'queues = ["summarization"]',
    'from requests_oauthlib import OAuth2Session',
])
def test_prohibited_runtime_features_are_detected(content):
    assert violations(Path("runtime.py"), content)


def test_scan_includes_assets_nested_runtime_and_root_entrypoints(tmp_path):
    for name in ("web/nested/runtime.py", "web/docs/runtime.py", "templates/new.html", "static/app.js", "Dockerfile", "docker-compose.yml", ".env.example", "entrypoint"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("import stripe")
    (tmp_path / "entrypoint").chmod(0o700)
    for name in ("tests/test_runtime.py", "docs/superpowers/specs/approved.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("import stripe")
    scanned = {str(path.relative_to(tmp_path)) for path in runtime_files(tmp_path)}
    assert scanned == {"web/nested/runtime.py", "web/docs/runtime.py", "templates/new.html", "static/app.js", "Dockerfile", "docker-compose.yml", ".env.example", "entrypoint"}


@pytest.mark.parametrize("name,content", [
    ("download.sh", '#!/bin/sh\nyt-dlp "$url"'),
    ("Dockerfile", "RUN ffmpeg -i input.mp3 output.wav"),
    ("download", '#!/bin/bash\nexec deno run remote.js'),
])
def test_shell_form_media_execution_is_detected(name, content):
    assert violations(Path(name), content)


def test_template_endpoint_and_asset_links_resolve(app):
    from jinja2 import nodes

    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    for path in (ROOT / "templates").rglob("*.html"):
        tree = app.jinja_env.parse(path.read_text())
        for node in tree.find_all(nodes.Call):
            if not isinstance(node.node, nodes.Name) or node.node.name != "url_for":
                continue
            assert node.args and isinstance(node.args[0], nodes.Const), path
            assert node.args[0].value in endpoints, (path, node.args[0].value)
            if node.args[0].value == "static":
                filename = next(keyword.value.value for keyword in node.kwargs if keyword.key == "filename")
                assert (ROOT / "static" / filename).is_file(), (path, filename)
