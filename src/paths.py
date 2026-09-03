"""Path resolution that does not depend on where the CLI was invoked from.

Every default path in this project is a relative string -- `config/assumptions.yaml`,
`data/offline_sample`. That reads well and works perfectly from the repo root, which
is why it survived so long: every test, every example and every manual run started
there. From anywhere else the same defaults point at directories that do not exist.

The first audit caught this for config files, where a missing scenarios file was
swallowed by an `.exists()` guard and bear, base and bull all returned the base-case
number under their own headings. The second audit caught the other half: offline
fixtures. That one at least fails loudly, but it breaks the README's headline promise
-- "no API key, no network, and no configuration" -- for anyone who clones the repo
and runs it from their home directory.

Resolution order is CWD first, then the repo root. That ordering matters: a user who
passes `--offline-dir mydata` and has a `mydata` beside them still gets their own
directory, not one buried in the package.
"""

from __future__ import annotations

from pathlib import Path

# Repo root, derived from this file rather than the working directory.
# src/paths.py -> src -> repo root
PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def resolve_path(path: str | Path) -> Path:
    """Resolve a relative path against the CWD first, then the repo root.

    An absolute path is returned untouched. A relative path that exists where the
    user is standing wins, so explicit overrides keep working. Otherwise the same
    path is tried inside the repo, and if that fails too the original is returned
    so the caller raises its own error naming the path the user actually asked for.
    """
    candidate = Path(path)
    if candidate.is_absolute() or candidate.exists():
        return candidate
    from_root = PACKAGE_ROOT / candidate
    return from_root if from_root.exists() else candidate
