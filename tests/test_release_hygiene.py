"""Release-hygiene tests for the Bug Hunter release.

These guard the release gates that are easy to regress silently: APP_VERSION in
.env is the ONLY product version (no hardcoded copy anywhere), no
mobile-application claims in user-facing documentation, no secret-bearing
environment files, and ignore rules that keep generated artifacts out of the
repository and Docker builds.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

DOCS = ("README.md", "SECURITY.md", "CONTRIBUTING.md")

#: Files that must never carry a product-version literal. Each of these either
#: derives the version from APP_VERSION/.env at runtime, or is build metadata
#: that does not need one. Tool/dependency versions are deliberately not in
#: this list - only the *application* version is.
VERSION_FREE_FILES = (
    "app/config.py",
    "docker-compose.yml",
    "down.sh",
    "sonar-project.properties",
    "frontend/package.json",
    "scripts/make_release.py",
    "scripts/sonar-scan.sh",
    ".env.example",
    "README.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "tests/conftest.py",
    "tests/test_release_hygiene.py",
    "tests/test_playwright_project_state.py",
)


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


#: Version-shaped placeholders that are deliberately version-less (npm's
#: required "version" field and the test fixtures). They are not product
#: versions, so the scan below does not treat them as one.
NEUTRAL_SEMVER = "0.0.0"
NEUTRAL_VERSION_LITERALS = {NEUTRAL_SEMVER, "0.0.0-test", "9.9.9"}


def dot_env_app_version() -> str:
    """The APP_VERSION value from the checked-in .env, or '' if unset.

    Read from the environment first so CI and a developer's .env both work.
    """
    value = os.environ.get("APP_VERSION", "").strip()
    if value:
        return value
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("APP_VERSION="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def _literal_candidates(text: str, name: str, version: str) -> list[str]:
    """Occurrences of the live product version in *text*, minus noise.

    Anchored to the real .env value rather than a shape heuristic: a generic
    ``\\d+\\.\\d+`` match flags Python 3.12, Vite 5.4.21, 0.5 vCPU and model IDs,
    which are all legitimately pinned. Only the product version matters, and it
    is read from .env so this test needs no number of its own.
    """
    if not version or version in NEUTRAL_VERSION_LITERALS:
        # No real product version to protect (unset, or a test sentinel).
        return []
    pattern = re.compile(rf"(?<![\w.]){re.escape(version)}(?![\w.])")
    found: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "*", '"""', "<!--")):
            continue
        if "APP_VERSION" in stripped:
            continue
        for _match in pattern.finditer(stripped):
            found.append(f"{name}: {version} in {stripped}")
    return found


def test_no_file_outside_dot_env_hardcodes_the_product_version():
    """The one-line release rule: only .env holds the product version.

    A literal copy in any other file is a second source of truth that silently
    goes stale, so this fails on the *current* .env value appearing anywhere
    else - whichever release that happens to be.
    """
    version = dot_env_app_version()
    offenders: list[str] = []
    for name in VERSION_FREE_FILES:
        offenders.extend(_literal_candidates(_read(name), name, version))
    assert offenders == [], (
        f"Hardcoded product version ({version}) found; set APP_VERSION in .env "
        "only:\n" + "\n".join(offenders)
    )


def test_env_example_requires_app_version_without_hardcoding_it():
    example = _read(".env.example")
    assert re.search(r"^APP_VERSION=", example, re.M), ".env.example must declare APP_VERSION"
    value = re.search(r"^APP_VERSION=(.*)$", example, re.M).group(1).strip()
    assert value == "", (
        ".env.example must ship a blank APP_VERSION placeholder; the real "
        f"value belongs in .env (found {value!r})"
    )


def test_config_has_no_app_version_fallback():
    config_src = _read("app/config.py")
    assert re.search(
        r'APP_VERSION[^=]*=\s*os\.getenv\(\s*"APP_VERSION"\s*,\s*""\s*\)', config_src
    ), "app/config.py must read APP_VERSION with an empty default, not a literal"


def test_docker_compose_requires_app_version():
    compose = _read("docker-compose.yml")
    # No `:-` (default) form of APP_VERSION anywhere: a default is a hardcoded
    # version waiting to go stale, silently.
    assert not re.search(r"\$\{APP_VERSION:-", compose), "inline fallback removed"
    assert "${APP_VERSION:?APP_VERSION is required - set it in .env}" in compose, (
        "docker-compose.yml must fail loudly when APP_VERSION is unset"
    )


def test_frontend_package_carries_no_product_version():
    package = json.loads(_read("frontend/package.json"))
    # npm requires a semver-shaped field; it is intentionally the neutral
    # placeholder, not the product version (the SPA reads /api/health).
    assert package["version"] == NEUTRAL_SEMVER, package["version"]
    assert NEUTRAL_SEMVER not in package["description"]


def test_sonar_project_version_is_supplied_at_scan_time():
    props = _read("sonar-project.properties")
    assert not re.search(r"^sonar\.projectVersion=", props, re.M), (
        "sonar.projectVersion must not be hardcoded; it is passed from APP_VERSION"
    )
    scan = _read("scripts/sonar-scan.sh")
    assert "-Dsonar.projectVersion=" in scan, "local scan must pass APP_VERSION"
    workflow = _read(".github/workflows/build-and-push.yml")
    assert "-Dsonar.projectVersion=" in workflow, "CI scan must pass APP_VERSION"


def test_health_endpoint_reports_the_configured_version(admin_client):
    from app.config import get_settings

    body = admin_client.get("/api/health").json()
    assert body["version"] == get_settings().APP_VERSION
    assert body["version"], "APP_VERSION must be set in .env for a released build"


def test_html_templates_receive_the_configured_version(admin_client):
    from app.config import get_settings

    version = get_settings().APP_VERSION
    for path in ("/login", "/"):
        response = admin_client.get(path)
        assert response.status_code == 200, path
        assert "__APP_VERSION__" not in response.text, f"{path} left the placeholder"
        assert f'content="{version}"' in response.text, f"{path} missing the version meta"


# ---------------------------------------------------------------------------
# No mobile-application claims
# ---------------------------------------------------------------------------

def test_documentation_makes_no_mobile_app_claim():
    pattern = re.compile(r"android|mobile app|mobile client|mobile application", re.I)
    for name in DOCS:
        matches = [
            line
            for line in _read(name).splitlines()
            if pattern.search(line)
        ]
        assert matches == [], f"{name} still claims a mobile client: {matches}"


def test_frontend_title_has_no_mobile_wording():
    for path in (ROOT / "app" / "static").glob("*.html"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert not re.search(r"android|mobile app", text, re.I), path.name


# ---------------------------------------------------------------------------
# No secrets or local environment files in the tree
# ---------------------------------------------------------------------------

def test_no_secret_environment_file_is_committed():
    """A local .env may exist in a developer working tree; it must only be
    ignored by git and excluded from the Docker context.
    """
    import subprocess

    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"],
            capture_output=True,
        )
    except OSError:
        tracked = None
    if tracked is not None:
        assert tracked.returncode != 0, ".env is tracked by git — it must be ignored"
    assert not (ROOT / ".env.docker").exists()


def test_env_example_is_placeholder_only():
    text = _read(".env.example")
    for marker in ("ghp_", "gho_", "github_pat_", "-----BEGIN PRIVATE KEY-----"):
        assert marker not in text, f".env.example contains {marker!r}"


def test_no_firebase_service_account_in_tree():
    assert not (ROOT / "secrets" / "firebase-admin.json").exists()


def test_gitignore_and_dockerignore_cover_release_exclusions():
    gitignore = _read(".gitignore")
    for pattern in (".env", ".env.*", "!.env.example", ".venv/", "__pycache__/",
                    "*.py[cod]", ".pytest_cache/", ".coverage", "coverage.xml",
                    "htmlcov/", "*.log", "*.exit"):
        assert pattern in gitignore, f".gitignore missing {pattern!r}"

    dockerignore = _read(".dockerignore")
    for pattern in (".env", ".git", ".venv", "tests", "__pycache__", "*.log",
                    "coverage.xml", ".coverage"):
        assert pattern in dockerignore, f".dockerignore missing {pattern!r}"


def test_no_known_release_artifacts_remain():
    offenders = [
        name
        for name in (
            "coverage.xml", "junit.xml", "VALIDATION_REPORT.txt",
            "_patch.py", "_patch_agile.py", "_smoke.py",
        )
        if (ROOT / name).exists()
    ]
    assert offenders == [], f"stale release artifacts present: {offenders}"
    assert not any((ROOT / ".ai").glob("*")) if (ROOT / ".ai").exists() else True


# ---------------------------------------------------------------------------
# Release archive contract (scripts/make_release.py)
# ---------------------------------------------------------------------------

ROOT_FOLDER = "bug-hunter"

#: Path parts that must never appear anywhere in a shipped archive.
FORBIDDEN_PARTS = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "htmlcov", "test-results", "playwright-report",
    "secrets", "RELEASE",
}
FORBIDDEN_SUFFIXES = (
    ".db", ".db-shm", ".db-wal", ".sqlite", ".pyc", ".log", ".exit",
    ".pem", ".key", ".p12", ".pfx", ".zip", ".tar.gz", ".coverage",
)
#: Credential *values* to hunt for. Bare marker words are not enough: the
#: sanitizer modules legitimately name the shapes they detect, and the helper
#: scripts legitimately forward `SONAR_TOKEN`/`BH_LOAD_PASSWORD` from the
#: process environment, so only an assigned literal value counts as a leak.
SECRET_SCAN_ALLOWLIST = {
    f"{ROOT_FOLDER}/app/chatbot/redaction.py",
    f"{ROOT_FOLDER}/app/git/provider.py",
    f"{ROOT_FOLDER}/tests/test_sleuth_memory_redaction.py",
    f"{ROOT_FOLDER}/tests/test_git_branches.py",
    f"{ROOT_FOLDER}/tests/test_git_tls_and_urls.py",
    f"{ROOT_FOLDER}/tests/test_release_hygiene.py",
    f"{ROOT_FOLDER}/scripts/make_release.py",
}
SECRET_SCAN_PATTERNS = (
    "ghp_[A-Za-z0-9]{16,}",
    "gho_[A-Za-z0-9]{16,}",
    "github_pat_[A-Za-z0-9_]{16,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"BH_LOAD_PASSWORD\s*=\s*[^$\s\"'#][^\s]*",
    r"SONAR_TOKEN\s*=\s*[^$\"'\s][^\s]*",
)


def _build_release(tmp_path):
    """Run the real release builder into a temp dir; return (tar, zip) paths."""
    import subprocess
    import sys

    tar_path = tmp_path / "release.tar.gz"
    zip_path = tmp_path / "release.zip"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "make_release.py"),
            "--out", str(tar_path),
            "--zip-out", str(zip_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return tar_path, zip_path


@pytest.fixture(scope="module")
def release_archives(tmp_path_factory):
    return _build_release(tmp_path_factory.mktemp("release"))


def _tar_members(path):
    import tarfile

    with tarfile.open(path, "r:gz") as tar:
        return tar.getnames()


def _zip_members(path):
    import zipfile

    with zipfile.ZipFile(path, "r") as zf:
        assert zf.testzip() is None, "ZIP contains a corrupt member"
        return zf.namelist()


def test_release_has_exactly_one_root_folder(release_archives):
    tar_path, zip_path = release_archives
    for names in (_tar_members(tar_path), _zip_members(zip_path)):
        assert {n.split("/")[0] for n in names if n} == {ROOT_FOLDER}


@pytest.mark.parametrize(
    "required",
    [
        f"{ROOT_FOLDER}/app/main.py",
        f"{ROOT_FOLDER}/app/routes/git.py",
        f"{ROOT_FOLDER}/frontend/package.json",
        f"{ROOT_FOLDER}/Dockerfile",
        f"{ROOT_FOLDER}/docker-compose.yml",
        f"{ROOT_FOLDER}/README.md",
        f"{ROOT_FOLDER}/LICENSE.txt",
        f"{ROOT_FOLDER}/CONTRIBUTING.md",
        f"{ROOT_FOLDER}/SECURITY.md",
        f"{ROOT_FOLDER}/.env.example",
    ],
)
def test_release_includes_required_project_files(release_archives, required):
    tar_path, zip_path = release_archives
    assert required in _tar_members(tar_path)
    assert required in _zip_members(zip_path)


def test_release_includes_tests_locks_and_ci_config(release_archives):
    tar_path, zip_path = release_archives
    names = set(_tar_members(tar_path)) | set(_zip_members(zip_path))
    for required in (
        f"{ROOT_FOLDER}/tests/test_git_branches.py",
        f"{ROOT_FOLDER}/tests/conftest.py",
        f"{ROOT_FOLDER}/requirements-lock.txt",
        f"{ROOT_FOLDER}/requirements-dev-lock.txt",
        f"{ROOT_FOLDER}/.github/workflows/build-and-push.yml",
        f"{ROOT_FOLDER}/sonar-project.properties",
        f"{ROOT_FOLDER}/scripts/sonar-scan.sh",
    ):
        assert required in names, required


def test_release_includes_final_frontend_source_and_bundle(release_archives):
    tar_path, zip_path = release_archives
    names = set(_tar_members(tar_path)) | set(_zip_members(zip_path))
    assert f"{ROOT_FOLDER}/app/static/index.html" in names
    assert any(
        n.startswith(f"{ROOT_FOLDER}/app/static/assets/") and n.endswith(".js")
        for n in names
    ), "built frontend JS bundle missing from the archive"
    assert any(
        n.startswith(f"{ROOT_FOLDER}/frontend/src/") and n.endswith(".jsx")
        for n in names
    ), "frontend source missing from the archive"


@pytest.mark.parametrize("kind", ["tar", "zip"])
def test_release_has_no_forbidden_or_traversal_members(release_archives, kind):
    tar_path, zip_path = release_archives
    names = _tar_members(tar_path) if kind == "tar" else _zip_members(zip_path)
    offenders = [
        n for n in names
        if any(part in FORBIDDEN_PARTS for part in n.split("/"))
        or n.endswith(FORBIDDEN_SUFFIXES)
        or n.startswith("/")
        or "\\" in n
        or ".." in n.split("/")
    ]
    assert offenders == [], f"{kind} has forbidden/traversal members: {offenders[:10]}"


@pytest.mark.parametrize("kind", ["tar", "zip"])
def test_release_carries_no_real_credentials(release_archives, kind):
    import tarfile
    import zipfile

    tar_path, zip_path = release_archives
    hits: list[str] = []
    if kind == "tar":
        with tarfile.open(tar_path, "r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile() or member.name in SECRET_SCAN_ALLOWLIST:
                    continue
                fh = tar.extractfile(member)
                text = (fh.read() if fh else b"").decode("utf-8", errors="ignore")
                if any(re.search(m, text) for m in SECRET_SCAN_PATTERNS):
                    hits.append(member.name)
    else:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.endswith("/") or name in SECRET_SCAN_ALLOWLIST:
                    continue
                text = zf.read(name).decode("utf-8", errors="ignore")
                if any(re.search(m, text) for m in SECRET_SCAN_PATTERNS):
                    hits.append(name)
    assert hits == [], f"{kind} leaks credentials: {hits[:10]}"


def test_release_never_recurses_into_previous_archives(release_archives):
    tar_path, zip_path = release_archives
    names = _tar_members(tar_path) + _zip_members(zip_path)
    recursive = [n for n in names if "RELEASE/" in n or n.endswith((".zip", ".tar.gz"))]
    assert recursive == [], f"recursive archive members: {recursive[:10]}"


_LOCK_PIN = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^]]*\])?==(\S+)")


def _lock_pins(name: str) -> dict[str, str]:
    pins = {}
    for line in _read(name).splitlines():
        m = _LOCK_PIN.match(line)
        if m:
            pins[m.group(1).lower().replace("_", "-")] = m.group(2)
    return pins


def test_runtime_lock_ships_the_versions_ci_tests():
    """The image installs requirements-lock.txt; CI tests requirements-dev-lock.txt.

    Every runtime pin must exist in the dev lock at the same version, or the
    shipped image runs dependency versions no test ever exercised.
    """
    runtime, dev = _lock_pins("requirements-lock.txt"), _lock_pins("requirements-dev-lock.txt")
    assert runtime, "no pins parsed from requirements-lock.txt"
    drift = {k: (v, dev.get(k)) for k, v in runtime.items() if dev.get(k) != v}
    assert drift == {}, f"runtime vs dev lock drift (runtime, dev): {drift}"


@pytest.mark.parametrize("name", ["requirements-lock.txt", "requirements-dev-lock.txt"])
def test_lockfiles_carry_no_local_paths(name):
    text = _read(name)
    assert not re.search(r"[A-Za-z]:[\/]|/(?:Users|home|tmp)/", text), f"{name} records a local path"


def test_image_does_not_advertise_the_server():
    """uvicorn writes "Server: uvicorn" below the ASGI stack, so the
    security-headers middleware cannot strip it; only the CLI flag can."""
    cmd = next(line for line in _read("Dockerfile").splitlines() if line.startswith("CMD "))
    assert json.loads(cmd[4:])[:2] == ["uvicorn", "app.main:app"]
    assert "--no-server-header" in json.loads(cmd[4:])


def test_no_merge_conflict_markers_are_committed():
    """An unresolved merge leaves `<<<<<<<` blocks that break builds without failing any import."""
    skipped = {".git", ".venv", "node_modules", "static", "__pycache__", ".ruff_cache", ".pytest_cache"}
    text_suffixes = {".py", ".js", ".jsx", ".css", ".html", ".md", ".txt", ".yml", ".yaml", ".sh", ".ps1",
                     ".toml", ".properties", ".json", ".sql", ""}
    opening = re.compile(r"^<<<<<<< ", re.MULTILINE)
    offenders = []
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skipped]
        for name in files:
            path = Path(folder) / name
            if path.suffix.lower() in text_suffixes and path.stat().st_size < 2_000_000                     and opening.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"unresolved merge conflict markers in: {offenders}"
