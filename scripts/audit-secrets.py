"""Audit: can any real secret be recovered from this git repository?

    python scripts/audit-secrets.py

Run it before a push, and especially before making a repository public.

Two passes.

  1. Known values. Reads the untracked files that legitimately hold secrets
     (.env, terraform.tfvars, terraform state) and searches EVERY blob in
     EVERY commit for each value. The current tree is not enough: a secret
     committed once and deleted later is still published.

  2. Unknown values. Pattern-matches committed content for the shapes secrets
     take, in case something is in there that this script was not told about.

Values are never printed: only a label, a length, and found/not-found. The
files it reads to learn those values are precisely the ones git is told to
ignore, so this needs no configuration and cannot itself leak anything.

A third pass asks the opposite question -- is anything that SHOULD be in the
repository being hidden? A `.gitignore` inherited from a language template can
silently swallow source: a bare `lib/` from the Python template took out
apps/web/lib here, the push succeeded, and nothing local noticed because the
files were sitting on disk the whole time.

Exit status is 0 when nothing sensitive is recoverable and nothing source-like
is hidden, 1 otherwise, so it can gate a release if you want it to.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Strings that are not secrets even though they appear in secret-holding files.
BORING = {
    "true", "false", "none", "null", "localhost", "postgres", "require",
    "support_agent", "eastus2", "centralus", "USER", "PASSWORD", "HOST",
}

# Keys whose values are configuration, not credentials: model deployment names
# and the repository slug. They are published on purpose, so testing them only
# produces noise that hides a real finding.
NOT_SECRET_KEYS = {
    "AZURE_OPENAI_CHAT_DEPLOYMENT",
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
    "AZURE_OPENAI_REALTIME_DEPLOYMENT",
    "AZURE_OPENAI_API_VERSION",
    "github_repository",
    "github_default_branch",
    # Numeric GitHub ids. They live in tfvars because Terraform needs them,
    # but they are public: `gh api repos/OWNER/REPO` returns both to anyone.
    # Flagging them would train the reader to ignore this report, which is the
    # failure mode that makes a scanner worse than none.
    "github_owner_id",
    "github_repository_id",
}
MIN_LEN = 8


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, errors="replace"
    ).stdout


def candidates() -> dict[str, str]:
    """Real secret values, labelled, from the files that are meant to hold them."""
    found: dict[str, str] = {}

    env = REPO / "apps" / "api" / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if len(value) < MIN_LEN or value.lower() in BORING:
                continue
            if key.strip() in NOT_SECRET_KEYS:
                continue
            found[f".env:{key.strip()}"] = value
            # A connection URL hides a password inside it; test that separately.
            match = re.search(r"://([^:/@]+):([^@]+)@", value)
            if match:
                found[f".env:{key.strip()}:password"] = match.group(2)

    tfvars = REPO / "infra" / "terraform.tfvars"
    if tfvars.is_file():
        for line in tfvars.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"')
            if key.strip() in NOT_SECRET_KEYS:
                continue
            if len(value) >= MIN_LEN and value.lower() not in BORING:
                found[f"tfvars:{key.strip()}"] = value

    state = REPO / "infra" / "terraform.tfstate"
    if state.is_file():
        data = json.loads(state.read_text(encoding="utf-8", errors="replace"))
        for resource in data.get("resources", []):
            kind = resource.get("type", "")
            for index, instance in enumerate(resource.get("instances", [])):
                attrs = instance.get("attributes") or {}
                for field in ("result", "administrator_password", "primary_access_key",
                              "secondary_access_key", "password", "admin_password"):
                    value = attrs.get(field)
                    if isinstance(value, str) and len(value) >= MIN_LEN:
                        found[f"state:{kind}[{index}].{field}"] = value
                for key_name in ("primary_key", "secondary_key"):
                    value = attrs.get(key_name)
                    if isinstance(value, str) and len(value) >= MIN_LEN:
                        found[f"state:{kind}[{index}].{key_name}"] = value
    return found


def all_blobs() -> list[tuple[str, str, bytes]]:
    """(sha, last-known path, content) for every blob in every commit."""
    listing = git("rev-list", "--objects", "--all")
    entries = []
    for line in listing.splitlines():
        sha, _, path = line.partition(" ")
        if not path:
            continue
        kind = git("cat-file", "-t", sha).strip()
        if kind != "blob":
            continue
        raw = subprocess.run(
            ["git", "cat-file", "-p", sha], cwd=REPO, capture_output=True
        ).stdout
        entries.append((sha, path, raw))
    return entries


PATTERNS = [
    ("private key block", rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("AWS access key id", rb"AKIA[0-9A-Z]{16}"),
    ("OpenAI-style key", rb"sk-[A-Za-z0-9]{20,}"),
    ("GitHub token", rb"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("Azure storage key", rb"AccountKey=[A-Za-z0-9+/=]{40,}"),
    ("JWT", rb"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("password assignment", rb"(?i)password[\"']?\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"),
    ("connection string with credentials", rb"(?i)(postgresql|postgres|mysql|mongodb)(\+\w+)?://[^:\s]+:[^@\s]+@"),
]

ALLOWED = {
    # Placeholders in the committed template, which is the point of the file.
    ("password assignment", "apps/api/.env.example"),
    ("connection string with credentials", "apps/api/.env.example"),
    ("connection string with credentials", "README.md"),
    # postgres:postgres@localhost -- a service container that exists for the
    # length of one CI job and is reachable only from inside it.
    ("connection string with credentials", ".github/workflows/ci.yml"),
}


# Directories whose contents are source and must always be committed.
SOURCE_ROOTS = [
    "apps/api/support", "apps/api/tests", "apps/api/alembic",
    "apps/web/app", "apps/web/lib", "apps/web/components",
    "infra", "scripts", "fixtures", ".github",
]
SOURCE_SUFFIXES = {
    ".py", ".ts", ".tsx", ".css", ".json", ".md", ".tf", ".ps1", ".mjs",
    ".yml", ".yaml", ".sh", ".mako", ".ini", ".toml",
}


def hidden_source() -> list[tuple[str, str]]:
    """Source files that exist on disk but git has been told to ignore."""
    found: list[tuple[str, str]] = []
    for root in SOURCE_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            rel = path.relative_to(REPO).as_posix()
            check = subprocess.run(
                ["git", "check-ignore", "-v", rel],
                cwd=REPO, capture_output=True, text=True,
            )
            if check.returncode == 0:
                rule = check.stdout.strip().split("	")[0]
                found.append((rel, rule))
    return found


def main() -> int:
    known = candidates()
    blobs = all_blobs()
    print(f"scanning {len(blobs)} blobs across {len(git('rev-list', '--all').splitlines())} commits")
    print(f"testing {len(known)} real secret values taken from untracked files\n")

    leaked = False

    print("--- pass 1: known secret values ---")
    for label, value in sorted(known.items()):
        needle = value.encode()
        hits = [path for _, path, raw in blobs if needle in raw]
        status = "LEAKED in " + ", ".join(sorted(set(hits))) if hits else "not in any blob"
        if hits:
            leaked = True
        print(f"  {'FAIL' if hits else 'ok  '}  {label:48} len={len(value):<4} {status}")

    print("\n--- pass 2: secret-shaped content in committed blobs ---")
    pattern_hits = 0
    for name, pattern in PATTERNS:
        rx = re.compile(pattern)
        for _, path, raw in blobs:
            if rx.search(raw):
                allowed = (name, path) in ALLOWED
                if not allowed:
                    leaked = True
                pattern_hits += 1
                print(f"  {'ok  ' if allowed else 'FAIL'}  {name:36} {path}"
                      f"{'  (placeholder, expected)' if allowed else ''}")
    if pattern_hits == 0:
        print("  ok    no secret-shaped content found")

    hidden = hidden_source()
    print("")
    print("--- pass 3: source files .gitignore is hiding ---")
    if hidden:
        for path, rule in hidden:
            print(f"  FAIL  {path}  <- {rule}")
    else:
        print("  ok    every source file under the tracked roots is visible to git")

    bad = leaked or bool(hidden)
    print("")
    print(("RESULT: the repository is wrong - see above" if bad
                  else "RESULT: clean - no secret is recoverable, no source is hidden"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
