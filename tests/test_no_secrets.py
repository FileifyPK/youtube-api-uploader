"""Repo hygiene: no real secrets, no live credentials, and the
publishing model the owner ordered (no approval/scheduled states).

The forbidden token patterns are built dynamically below so this very
test file does not itself contain them.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Token-looking patterns, assembled so the literals never appear here.
OAUTH_TOKEN_PREFIX = "ya" + "29."  # Google OAuth access token
API_KEY_PREFIX = "AI" + "za"  # Google API key

SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", ".eggs"}
SECRET_ENV_KEYS = {"GOOGLE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "DATABASE_URL"}


def _repo_files():
    import os

    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        # Prune in place so the venv (thousands of files) is never walked.
        dirnames[:] = [
            d
            for d in dirnames
            if d not in SKIP_DIRS and not d.endswith(".egg-info")
        ]
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # binary / unreadable: skipped


def test_env_file_not_tracked_by_git():
    """A live .env may exist locally (setup day onwards) but must
    never be tracked by git or committed. The old
    test_no_env_file_exists asserted no .env exists at all; that
    broke once the real setup stored the refresh token in .env
    (20 Nov 2026). The invariant that matters is: git must not
    track it, and .gitignore must cover it."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", ".env"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert tracked.stdout.strip() == ""
    ignored = subprocess.run(
        ["git", "check-ignore", ".env"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ignored.returncode == 0


def test_gitignore_lists_env():
    gitignore = REPO_ROOT / ".gitignore"
    assert gitignore.is_file()
    lines = gitignore.read_text(encoding="utf-8").splitlines()
    assert ".env" in lines


def _is_placeholder(value: str) -> bool:
    """True if a .env.example value is an obvious placeholder: a YOUR_
    label, a <...> marker, or a DSN built from literal placeholder
    tokens (USER:PASSWORD@HOST/DATABASE — never a real host)."""
    return (
        value == "<redacted>"
        or "YOUR_" in value
        or "USER:PASSWORD@HOST" in value
        or value.startswith("<")
    )


def test_env_example_contains_only_placeholders():
    example = REPO_ROOT / ".env.example"
    assert example.is_file()
    for line in example.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        assert OAUTH_TOKEN_PREFIX not in value
        assert API_KEY_PREFIX not in value
        if key in SECRET_ENV_KEYS:
            assert _is_placeholder(value), (
                f"{key} in .env.example is not a placeholder: {value!r}"
            )
        if len(value) > 40:
            # A long value is only acceptable if it is labelled as a
            # placeholder (e.g. YOUR_CLIENT_ID.apps.googleusercontent.com,
            # or the USER:PASSWORD@HOST DSN).
            assert _is_placeholder(value), f"{key} has a long unlabelled value"


def test_no_python_file_contains_token_patterns():
    offenders = []
    for path in _repo_files():
        if path.suffix != ".py":
            continue
        text = _read_text(path)
        if text is None:
            continue
        if OAUTH_TOKEN_PREFIX in text or API_KEY_PREFIX in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_uploader_hardcodes_private_privacy():
    text = (REPO_ROOT / "src" / "youtube_uploader" / "uploader.py").read_text(
        encoding="utf-8"
    )
    assert 'PRIVACY_STATUS = "private"' in text
    assert '"privacyStatus": PRIVACY_STATUS' in text


def test_no_approval_or_scheduled_state_in_src():
    offenders = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "approved_for_publishing" in text:
            offenders.append(f"{path}: approved_for_publishing")
        # "scheduled" may appear in prose about scheduling, but never
        # as a quoted state name.
        if '"scheduled"' in text or "'scheduled'" in text:
            offenders.append(f"{path}: quoted 'scheduled' state")
    assert offenders == []
