"""Guard the shipped runtime against accidentally restoring retired integrations."""

import ast
import fnmatch
import os
from pathlib import Path
import re
import stat
import subprocess

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
    ".claude", ".runtime", ".codex", ".pytest_cache", "__pycache__", "tests", "docs",
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


def documentation_violations(path, content):
    """Exempt only complete absence statements in prose, never command blocks."""
    name = r"(?:Heroku|Stripe|Mailgun|OpenAI|OpenRouter)"
    subjects = name + r"(?:(?:,\s*(?:and\s+|or\s+)?|\s+(?:and|or)\s+|/)" + name + r")*"
    noun = r"(?:integration|integrations|support|deployment)"
    ending = r"(?:\s+(?:here|in this (?:application|stack)|to later phases))?"
    absence = (
        rf"(?:The\s+)?{subjects}(?:\s+{noun})?\s+"
        r"(?:is|are|was|were|has been|have been)\s+(?:explicitly\s+)?"
        r"(?:absent|removed|unsupported|deferred|retired|not supported|not included|not implemented)"
        + ending
    )
    no_integration = rf"No\s+{subjects}\s+{noun}(?:\s+(?:is|are)\s+(?:included|implemented|provided|supported|available))?" + ending
    found = []
    fenced = False
    for number, line in enumerate(content.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        for statement in re.split(r"[.!?;]", line):
            if not re.search(PATTERNS["retired integration"], statement, re.IGNORECASE):
                continue
            prose = statement.strip().replace("`", "")
            if not fenced and re.fullmatch(rf"(?:{absence}|{no_integration})", prose, re.IGNORECASE):
                continue
            found.append(f"{path}:{number}: retired integration setup or claim")
    return found


def violations(path, content):
    if path.suffix == ".md":
        return documentation_violations(path, content)
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
        assert not violations(Path(name), content), name
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


@pytest.mark.parametrize("content", [
    "Heroku, Stripe, and Mailgun are absent.",
    "Stripe integration has been removed.",
    "Heroku deployment is unsupported.",
    "OpenAI/OpenRouter support is deferred to later phases.",
    "No Mailgun integration is implemented.",
])
def test_documentation_allows_explicit_retired_integration_absence(content):
    assert not violations(Path("guide.md"), content)


@pytest.mark.parametrize("content", [
    "Run heroku create to deploy.", "Configure STRIPE_SECRET_KEY in .env.",
    "Mailgun delivers transcript emails.", "Stripe integration is available.",
    "Heroku is unsupported; run heroku create anyway.",
    "Heroku is absent and Stripe is supported.",
    "```bash\n# Stripe is absent\nexport STRIPE_SECRET_KEY=value\n```",
])
def test_documentation_still_rejects_retired_setup_and_positive_claims(content):
    assert violations(Path("guide.md"), content)


def test_absence_exemption_never_applies_to_runtime():
    assert violations(Path("runtime.py"), '"""Stripe is absent."""')


def test_backup_files_are_ignored_by_git_and_docker():
    paths = ("backups/archive.sql", "private.dump", "exports/private.dump", "nested/backups/archive.sql")
    checked = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"],
                             input="\n".join(paths) + "\n", cwd=ROOT,
                             text=True, capture_output=True, check=False)
    assert checked.returncode == 0 and set(checked.stdout.splitlines()) == set(paths)
    patterns = [line.strip().rstrip("/") for line in (ROOT / ".dockerignore").read_text().splitlines()
                if line.strip() and not line.startswith("#")]
    for name in paths:
        assert any(fnmatch.fnmatch(name, pattern) or any(
            fnmatch.fnmatch(str(parent), pattern) for parent in Path(name).parents)
                   for pattern in patterns), name
    assert "backups/" in (ROOT / ".gitignore").read_text().splitlines()
    assert "*.dump" in (ROOT / ".gitignore").read_text().splitlines()
    assert "backups" in patterns and "*.dump" in patterns


def test_claude_runtime_credentials_are_ignored_by_git_and_docker():
    paths = (".runtime/claude/.credentials.json", ".runtime/claude/settings.json", ".runtime/other/private")
    checked = subprocess.run(["git", "check-ignore", "--no-index", "--stdin"],
                             input="\n".join(paths) + "\n", cwd=ROOT,
                             text=True, capture_output=True, check=False)
    assert checked.returncode == 0 and set(checked.stdout.splitlines()) == set(paths)
    assert ".runtime/" in (ROOT / ".gitignore").read_text().splitlines()
    assert ".runtime/" in (ROOT / ".dockerignore").read_text().splitlines()


def test_operations_documents_classifier_authentication_and_current_schema():
    guide = (ROOT / "docs/operations.md").read_text()
    for command in (
        "install -d -m 700 .runtime/claude",
        "docker compose run --rm --entrypoint claude classifier auth login",
        "docker compose run --rm --entrypoint claude classifier auth status --text",
        "docker compose exec classifier celery -A tasks.celery_app inspect --timeout 5 --destination",
    ):
        assert command in guide
    assert "0002_commercial_classification" in guide
    assert "seven-service stack" in guide and "all thirteen table" in guide
    assert "six-service stack" not in guide and "all eight table" not in guide
    for table in ("admins", "content_sources", "content_items", "source_items", "backfill_requests",
                  "discovery_runs", "discovery_candidates", "processing_jobs", "classification_revisions",
                  "classification_blocks", "segment_overrides", "source_classification_rules", "classification_sweeps"):
        assert f"`{table}`" in guide
    assert "@anthropic-ai/claude-code@2.1.303" in guide
    assert "CLAUDE_CONFIG_HOST_PATH" in guide


def backup_block():
    guide = (ROOT / "docs/operations.md").read_text()
    return next(block for block in re.findall(r"```bash\n(.*?)```", guide, re.S) if "pg_dump" in block)


@pytest.fixture
def backup_shell(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    tools = tmp_path / "bin"
    tools.mkdir()
    docker = tools / "docker"
    docker.write_text('#!/bin/sh\ncase "$*" in\n  *pg_dump*) printf "synthetic-dump\\n" ;;\nesac\n')
    docker.chmod(0o700)
    environment = {**os.environ, "PATH": str(tools) + ":" + os.environ["PATH"]}
    environment.pop("PODCAST_BACKUP_DIR", None)
    return checkout, environment


@pytest.mark.parametrize("override", [False, True])
def test_backup_commands_write_private_dumps_outside_checkout(backup_shell, override):
    checkout, environment = backup_shell
    if override:
        environment["PODCAST_BACKUP_DIR"] = str(checkout.parent / "chosen backups")
    result = subprocess.run(["bash", "-c", backup_block()], cwd=checkout,
                            env=environment, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert not list(checkout.rglob("*.dump"))
    backups = list(checkout.parent.rglob("*.dump"))
    assert len(backups) == 1
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(backups[0].parent.stat().st_mode) == 0o700
    if override:
        assert backups[0].parent == Path(environment["PODCAST_BACKUP_DIR"])


def test_backup_commands_reject_checkout_and_ancestor_targets(backup_shell):
    checkout, environment = backup_shell
    for target in (checkout, checkout / "backups", checkout.parent):
        result = subprocess.run(["bash", "-c", backup_block()], cwd=checkout,
                                env={**environment, "PODCAST_BACKUP_DIR": str(target)},
                                text=True, capture_output=True)
        assert result.returncode != 0, str(target)
    assert not list(checkout.parent.rglob("*.dump"))


def test_restore_renews_and_verifies_redis_before_writers_restart():
    guide = (ROOT / "docs/operations.md").read_text()
    assert "Redis has no persistent volume" not in guide
    assert "it has no persistent volume" not in guide
    assert "anonymous" in guide and "no named" in guide
    blocks = re.findall(r"```bash\n(.*?)```", guide, re.S)
    restore = next(block for block in blocks if "--renew-anon-volumes" in block)
    assert "docker compose stop web worker beat" in restore
    assert "docker compose up -d --force-recreate --renew-anon-volumes --wait redis" in restore
    assert "redis-cli FLUSHALL" in restore and "redis-cli DBSIZE" in restore
    assert restore.index("redis-cli DBSIZE") < restore.index("docker compose up -d\n")


@pytest.mark.parametrize("flush_reply,has_keys,expected", [("OK", "0", 0), ("ERR denied", "0", 1), ("OK", "1", 1)])
def test_restore_keeps_writers_stopped_unless_redis_is_verified_empty(backup_shell, flush_reply, has_keys, expected):
    checkout, environment = backup_shell
    docker = Path(environment["PATH"].split(":")[0]) / "docker"
    docker.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_CALL_LOG"
case "$*" in
  *"redis-cli FLUSHALL"*) printf '%s\\n' "$REDIS_FLUSH_REPLY" ;;
  *"redis-cli DBSIZE"*) printf '0\\n' ;;
  *"redis-cli INFO keyspace"*)
    printf '# Keyspace\\r\\n'
    if [ "$REDIS_HAS_KEYS" = 1 ]; then printf 'db1:keys=2,expires=0,avg_ttl=0\\r\\n'; fi ;;
esac
''')
    log = checkout.parent / "docker.log"
    environment.update(DOCKER_CALL_LOG=str(log), REDIS_FLUSH_REPLY=flush_reply, REDIS_HAS_KEYS=has_keys)
    guide = (ROOT / "docs/operations.md").read_text()
    block = next(block for block in re.findall(r"```bash\n(.*?)```", guide, re.S) if "--renew-anon-volumes" in block)
    result = subprocess.run(["bash", "-c", block], cwd=checkout, env=environment,
                            text=True, capture_output=True)
    assert (result.returncode == 0) == (expected == 0), result.stderr
    assert ("compose up -d" in log.read_text().splitlines()) == (expected == 0)
