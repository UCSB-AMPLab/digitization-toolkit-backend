"""The unit's version: the release it runs, and the revision of its code.

* The release is the one line of the superproject's VERSION file, found at
  `../VERSION` from the working directory, the way config finds `../.env`
  (pydantic-settings reads a relative env_file from the working directory;
  the backend runs from its own directory, inside the superproject).
  APP_VERSION (settings.app_version) overrides it. Neither: "unknown".
* The revision is the backend checkout's HEAD commit, read from its git
  files without running git: `.git` is a directory, or, for a submodule or
  a linked worktree, a file naming the git directory; HEAD is a commit or a
  ref, looked up loose and then in packed-refs, in the common directory a
  worktree's `commondir` names.
* system_version() is what the capture manifest sends as systemVersion:
  `<release>` when the superproject's HEAD is the commit its tag named
  `<release>` points to (annotated tags peeled, from packed-refs or a loose
  tag object), otherwise `<release>+g<first 12 hex of the backend commit>`,
  and `<release>` alone when no commit can be read.
* code_identity() is what the Fisqua version stop is keyed on
  (app.core.fisqua_outbox.config_fingerprint): the backend commit and the
  release, so the stop clears when the installed code changes. With no
  commit readable it carries the time this process started, so the stop
  clears on every restart rather than never. The stop also clears when a
  periodic retest is accepted by Fisqua.

A tag known only from an object inside a pack file, or from packed-refs
without the fully-peeled trait, cannot be peeled without reading the pack;
that checkout counts as untagged. A clone's packed-refs carries the trait.
"""

import re
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core import config

UNKNOWN = "unknown"
SHA = re.compile(r"^[0-9a-f]{40}$")
PROCESS_STARTED = datetime.now(timezone.utc).isoformat()


def _backend_dir(backend_dir: Optional[Path]) -> Path:
    return Path(backend_dir) if backend_dir is not None else Path.cwd()


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def release_version(backend_dir: Optional[Path] = None) -> str:
    """APP_VERSION when set, else the superproject's VERSION, else "unknown"."""
    override = (config.settings.app_version or "").strip()
    if override:
        return override
    text = _read(_backend_dir(backend_dir).parent / "VERSION")
    line = text.splitlines()[0].strip() if text else ""
    return line or UNKNOWN


def _git_dir(work: Path) -> Optional[Path]:
    """The git directory of the checkout at `work`: `.git` itself, or where a `.git` file points."""
    dot = work / ".git"
    if dot.is_dir():
        return dot
    text = _read(dot) if dot.is_file() else None
    if text and text.startswith("gitdir:"):
        target = Path(text[len("gitdir:"):].strip())
        target = target if target.is_absolute() else (work / target)
        return target if target.is_dir() else None
    return None


def _common_dir(gitdir: Path) -> Path:
    text = _read(gitdir / "commondir")
    if not text:
        return gitdir
    common = Path(text)
    return common if common.is_absolute() else (gitdir / common)


def _packed(common: Path) -> tuple:
    """({ref: (sha, peeled sha or None)}, fully_peeled) from packed-refs.

    With the `fully-peeled` trait in its header, a ref with no `^` line is
    known not to be an annotated tag; without it, that is unknown.
    """
    out = {}
    text = _read(common / "packed-refs")
    if not text:
        return out, False
    fully_peeled = False
    last = None
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("#"):
            fully_peeled = fully_peeled or "fully-peeled" in line.split()
            continue
        if line.startswith("^"):
            if last is not None:
                out[last] = (out[last][0], line[1:].strip())
            continue
        parts = line.split()
        if len(parts) == 2 and SHA.match(parts[0]):
            out[parts[1]] = (parts[0], None)
            last = parts[1]
    return out, fully_peeled


def _ref(gitdir: Path, ref: str) -> Optional[str]:
    common = _common_dir(gitdir)
    for base in (gitdir, common):
        sha = _read(base / ref)
        if sha and SHA.match(sha):
            return sha
    packed = _packed(common)[0].get(ref)
    return packed[0] if packed else None


def _head(gitdir: Path) -> Optional[str]:
    text = _read(gitdir / "HEAD")
    if not text:
        return None
    if SHA.match(text):
        return text
    if text.startswith("ref:"):
        return _ref(gitdir, text[len("ref:"):].strip())
    return None


def code_revision(backend_dir: Optional[Path] = None) -> Optional[str]:
    """The backend checkout's HEAD commit, or None when it cannot be read."""
    gitdir = _git_dir(_backend_dir(backend_dir))
    return _head(gitdir) if gitdir is not None else None


def _loose_object(common: Path, sha: str) -> Optional[tuple]:
    """(type, body) of a loose object, or None when it is not loose or cannot be read."""
    try:
        raw = zlib.decompress((common / "objects" / sha[:2] / sha[2:]).read_bytes())
    except (OSError, zlib.error):
        return None
    header, _, body = raw.partition(b"\0")
    return header.split(b" ", 1)[0].decode("ascii", "replace"), body


def _tag_commit(gitdir: Path, name: str) -> Optional[str]:
    """The commit the tag `name` points to, annotated tags peeled; None when that cannot be known.

    A loose tag ref is read through its loose object: a tag object gives its
    `object`, a commit is the commit. A packed one is read from packed-refs:
    its `^` line, or, under the fully-peeled trait, the ref itself. Anything
    else (an object only in a pack, packed-refs without the trait) is None,
    and the checkout counts as untagged.
    """
    common = _common_dir(gitdir)
    ref = f"refs/tags/{name}"
    loose = _read(common / ref)
    if loose and SHA.match(loose):
        obj = _loose_object(common, loose)
        if obj is None:
            return None
        kind, body = obj
        if kind == "commit":
            return loose
        if kind == "tag":
            first = body.split(b"\n", 1)[0].decode("ascii", "replace")
            target = first[len("object "):].strip() if first.startswith("object ") else ""
            return target if SHA.match(target) else None
        return None
    packed, fully_peeled = _packed(common)
    entry = packed.get(ref)
    if entry is None:
        return None
    sha, peeled = entry
    if peeled:
        return peeled
    return sha if fully_peeled else None


def system_version(backend_dir: Optional[Path] = None) -> str:
    """The manifest's systemVersion: `<release>`, or `<release>+g<12-hex backend commit>` off the release tag."""
    backend = _backend_dir(backend_dir)
    release = release_version(backend)
    commit = code_revision(backend)
    if commit is None:
        return release
    super_git = _git_dir(backend.parent)
    if super_git is not None and release != UNKNOWN:
        head = _head(super_git)
        tagged = _tag_commit(super_git, release)
        if head is not None and tagged is not None and head == tagged:
            return release
    return f"{release}+g{commit[:12]}"


def code_identity(backend_dir: Optional[Path] = None) -> str:
    """What the version stop is keyed on: the backend commit and the release, or the process start without a commit."""
    backend = _backend_dir(backend_dir)
    commit = code_revision(backend)
    release = release_version(backend)
    return f"{commit}:{release}" if commit else f"no-revision:{release}:{PROCESS_STARTED}"
