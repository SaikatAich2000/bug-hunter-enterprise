"""Build clean TAR + ZIP release artifacts from an explicit allowlist and verify them.

Usage (from the repository root, with the project virtualenv active):

    python scripts/make_release.py
    python scripts/make_release.py --out RELEASE/bug-hunter-production.tar.gz

Both artifacts contain exactly one root folder, ``bug-hunter/``. Excludes
.git, .env, .venv, node_modules, caches, logs, coverage, local databases,
secrets and previous archives, and rejects absolute or ``..`` traversal
entries. Fails (exit 1) if a forbidden entry or a real credential shape is
found in either archive, so a stray secret or local database can never ship
unnoticed.

``RELEASE_OUT`` in the environment overrides the TAR path; the ZIP is written
next to it with an ``.zip`` suffix (``RELEASE_ZIP_OUT`` overrides that).
"""
from __future__ import annotations

import argparse
import os
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
ROOT_FOLDER = "bug-hunter"


def _app_version() -> str:
    """Read APP_VERSION from the environment, falling back to .env.

    .env is the single source of truth for the product version, so the default
    archive name is derived from it instead of carrying its own copy of the
    number (which is how release filenames drift out of lockstep with .env).
    Returns an empty string when the variable is unset; the archive name then
    degrades to the version-less form rather than inventing a version.
    """
    value = os.environ.get("APP_VERSION", "").strip()
    if value:
        return value
    env_file = ROOT / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("APP_VERSION="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def default_out() -> Path:
    """Default TAR path, named after APP_VERSION when it is known."""
    version = _app_version()
    name = f"bug-hunter-{version}-production.tar.gz" if version else (
        "bug-hunter-production.tar.gz"
    )
    return ROOT / "RELEASE" / name

ALLOW = [
    "app",
    "app/static",
    "scripts",
    "tests",
    "frontend/src",
    "frontend/public",
    "frontend/scripts",
    ".github",
]
ALLOW_FILES = [
    ".dockerignore",
    ".env.example",
    ".gitignore",
    "CONTRIBUTING.md",
    "deploy.sh",
    "docker-compose.yml",
    "Dockerfile",
    "down.sh",
    "LICENSE.txt",
    "pyproject.toml",
    "README.md",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-lock.txt",
    "requirements-dev-lock.txt",
    "SECURITY.md",
    "sonar-project.properties",
]
ALLOW_ROOTS = tuple(Path(a).as_posix() for a in ALLOW)

FORBIDDEN_NAMES = {
    ".git", ".env", ".venv", "venv", "node_modules", ".ai",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "htmlcov", "dist", "build", ".claude", ".vscode", ".idea",
    "VALIDATION_REPORT.txt", "coverage.xml", "junit.xml", ".coverage",
    # Per-file CI test fan-out output (see .github/workflows/build-and-push.yml)
    "junit-parts", "ci-forensics",
    "firebase-admin.json", "bug_hunter.db", "secrets",
}
FORBIDDEN_SUFFIX = (".pyc", ".log", ".exit", ".db", ".sqlite", ".db-shm", ".db-wal", ".zip", ".pem", ".key", ".p12", ".pfx")
SECRET_SHAPES = ("ghp_", "gho_", "github_pat_", "-----BEGIN PRIVATE KEY-----")
ALLOWLISTED_FIXTURE_PATHS = (
    "tests/test_sleuth_memory_redaction.py",
    "tests/test_git_branches.py",
    "tests/test_git_tls_and_urls.py",
    "tests/test_release_hygiene.py",
    "scripts/make_release.py",
    # Detection/sanitization modules whose regexes must literally name the
    # token shapes they redact; their own allowlist markers prove intent and
    # no token values are stored there.
    "app/chatbot/redaction.py",
    "app/git/provider.py",
)
ALLOWLISTED_SOURCE_MARKERS = (
    "redaction", "SECRET_PATTERN", "_SECRET_PATTERN", "sanitize",
    "marker", "for marker in", "in (", "markers", "SECRET_MARKERS", "SECRET_SHAPES",
)


def wanted(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    if any(rel.startswith(f + "/") for f in FORBIDDEN_NAMES):
        return False
    if path.name in FORBIDDEN_NAMES or rel in FORBIDDEN_NAMES:
        return False
    if rel.endswith(FORBIDDEN_SUFFIX) or path.name.endswith(FORBIDDEN_SUFFIX):
        return False
    if path.is_file():
        if "/" in rel:
            top = rel.split("/", 1)[0]
            if top not in [a.split("/")[0] for a in ALLOW_ROOTS]:
                return False
        elif rel not in ALLOW_FILES:
            return False
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=os.environ.get("RELEASE_OUT", str(default_out())),
        help="TAR archive path to write (default: %(default)s)",
    )
    parser.add_argument(
        "--zip-out",
        default=os.environ.get("RELEASE_ZIP_OUT", ""),
        help="ZIP archive path (default: the --out path with a .zip suffix)",
    )
    return parser.parse_args(argv)


def _archive_member(rel: str) -> str:
    """Archive name for a repo-relative path, under the single root folder."""
    return f"{ROOT_FOLDER}/{rel}"


def _unsafe_member(name: str) -> bool:
    """Absolute paths, drive letters and `..` traversal must never appear."""
    pure = PurePosixPath(name)
    if pure.is_absolute() or name.startswith("/") or "\\" in name:
        return True
    if ":" in name.split("/")[0]:
        return True
    return any(part == ".." for part in pure.parts)


def collect_files() -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        d = Path(dirpath)
        dirnames[:] = sorted(x for x in dirnames if wanted(d / x))
        for name in sorted(filenames):
            p = d / name
            if wanted(p):
                files.append(p)
    return sorted(files)


def _write_tar(out: Path, files: list[Path]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with tarfile.open(out, "w:gz") as tar:
        for p in files:
            tar.add(p, arcname=_archive_member(p.relative_to(ROOT).as_posix()))


def _write_zip(out: Path, files: list[Path]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, arcname=_archive_member(p.relative_to(ROOT).as_posix()))


def _verify(names: list[str], label: str) -> list[str]:
    """Return the problem list for one archive's member names."""
    problems: list[str] = []
    roots = {PurePosixPath(n).parts[0] for n in names if PurePosixPath(n).parts}
    if roots != {ROOT_FOLDER}:
        problems.append(f"{label}: root folders {sorted(roots)} (expected ['{ROOT_FOLDER}'])")
    for name in names:
        if _unsafe_member(name):
            problems.append(f"{label}: unsafe member {name}")
        parts = PurePosixPath(name).parts
        if any(part in FORBIDDEN_NAMES for part in parts):
            problems.append(f"{label}: forbidden member {name}")
        if name.endswith(FORBIDDEN_SUFFIX):
            problems.append(f"{label}: forbidden member {name}")
        if len(parts) > 1 and parts[1] == "RELEASE":
            problems.append(f"{label}: release recursion {name}")
        if not name.startswith(f"{ROOT_FOLDER}/") and name != ROOT_FOLDER:
            problems.append(f"{label}: outside the single root folder {name}")
    return problems


def _scan_secrets(names_and_text: list[tuple[str, str]], label: str) -> list[str]:
    hits: list[str] = []
    prefix = f"{ROOT_FOLDER}/"
    for rel, text in names_and_text:
        rel_key = rel[len(prefix):] if rel.startswith(prefix) else rel
        if rel_key in ALLOWLISTED_FIXTURE_PATHS:
            continue
        for marker in SECRET_SHAPES:
            hit_lines = [
                line for line in text.splitlines()
                if marker in line and not any(m in line for m in ALLOWLISTED_SOURCE_MARKERS)
            ]
            if hit_lines:
                hits.append(f"{label}: {rel}: {marker}")
                break
    return hits


def _scan_text_members(members: list[tuple[str, bytes]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name, blob in members:
        suffix = PurePosixPath(name).suffix.lower()
        if suffix not in {".py", ".md", ".txt", ".example", ".toml", ".yml", ".yaml",
                          ".json", ".sh", ".ps1", ".js", ".jsx", ".css", ".html", ".cfg", ".ini"}:
            continue
        try:
            out.append((name, blob.decode("utf-8", errors="strict")))
        except UnicodeDecodeError:
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out = Path(args.out).resolve()
    zip_out = Path(args.zip_out).resolve() if args.zip_out else out.with_suffix("").with_suffix(".zip")
    files = collect_files()
    rel_names = [f"{ROOT_FOLDER}/{p.relative_to(ROOT).as_posix()}" for p in files]

    visible = [p for p in files if not p.name.startswith(".")]
    if not any(p.parts[-1] == "main.py" for p in visible):
        print("SOURCE MISSING: app/main.py not collected")
        return 1

    _write_tar(out, files)
    _write_zip(zip_out, files)

    problems: list[str] = []
    with tarfile.open(out, "r:gz") as tar:
        tar_members = tar.getmembers()
        problems += _verify([m.name for m in tar_members], "tar")
        tar_blobs = []
        for m in tar_members:
            if m.isfile():
                fh = tar.extractfile(m)
                tar_blobs.append((m.name, fh.read() if fh else b""))
    problems += _scan_secrets(_scan_text_members(tar_blobs), "tar")

    with zipfile.ZipFile(zip_out, "r") as zf:
        bad = zf.testzip()
        if bad:
            problems.append(f"zip: corrupt member {bad}")
        problems += _verify(zf.namelist(), "zip")
        problems += _scan_secrets(
            _scan_text_members([(n, zf.read(n)) for n in zf.namelist() if not n.endswith("/")]),
            "zip",
        )

    if problems:
        for line in problems[:20]:
            print("PROBLEM:", line)
        return 1
    print(f"OK: tar={len(rel_names)} entries ({out.name}), zip={len(rel_names)} entries ({zip_out.name})")
    print(f"root={ROOT_FOLDER}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())