"""The unit's version: the release in the superproject's VERSION, and the code's revision (app.core.version).

Every repository here is built in a temporary directory: a superproject
with VERSION and a .git directory, and a backend inside it whose .git is a
directory or, as on a unit, a file pointing into the superproject's
.git/modules.

    python -m pytest tests/unit/test_version.py

Version: 0.1.0-beta
"""

import zlib
from pathlib import Path

import pytest

SUPER_TAGGED = "1" * 40
SUPER_OTHER = "2" * 40
BACKEND = "abcdef0123456789abcdef0123456789abcdef01"
TAG_OBJECT = "3" * 40


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _superproject(tmp_path, *, version="0.1.0", head=SUPER_TAGGED, backend_gitfile=True,
                  backend_head=BACKEND):
    """<tmp>/super with VERSION and .git, and <tmp>/super/backend; returns the backend directory."""
    root = tmp_path / "super"
    if version is not None:
        _write(root / "VERSION", version + "\n")
    _write(root / ".git" / "HEAD", head + "\n")
    backend = root / "backend"
    backend.mkdir(parents=True)
    if backend_gitfile:
        _write(backend / ".git", "gitdir: ../.git/modules/backend\n")
        gitdir = root / ".git" / "modules" / "backend"
    else:
        gitdir = backend / ".git"
    if backend_head is not None:
        _write(gitdir / "HEAD", backend_head + "\n")
    return backend


def _loose_tag(backend, name, sha):
    _write(backend.parent / ".git" / "refs" / "tags" / name, sha + "\n")


def _loose_object(backend, sha, kind, body: bytes):
    obj = kind.encode() + b" " + str(len(body)).encode() + b"\0" + body
    path = backend.parent / ".git" / "objects" / sha[:2] / sha[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zlib.compress(obj))


def _packed_tag(backend, name, sha, peeled=None, fully_peeled=True):
    header = "# pack-refs with: peeled fully-peeled sorted\n" if fully_peeled else "# pack-refs with: peeled\n"
    _write(backend.parent / ".git" / "packed-refs",
           header + f"{sha} refs/tags/{name}\n" + (f"^{peeled}\n" if peeled else ""))


@pytest.fixture(autouse=True)
def no_app_version(monkeypatch):
    from app.core import config
    monkeypatch.setattr(config.settings, "app_version", None)


# ---------------------------------------------------------------------------
# The release
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_release_is_read_from_the_superprojects_version_file(tmp_path):
    from app.core.version import release_version
    assert release_version(_superproject(tmp_path)) == "0.1.0"


@pytest.mark.unit
def test_app_version_overrides_the_version_file(tmp_path, monkeypatch):
    from app.core import config
    from app.core.version import release_version, system_version
    backend = _superproject(tmp_path)
    monkeypatch.setattr(config.settings, "app_version", "0.1.0-rc1")
    assert release_version(backend) == "0.1.0-rc1"
    assert system_version(backend) == f"0.1.0-rc1+g{BACKEND[:12]}"


@pytest.mark.unit
def test_a_missing_version_file_is_unknown(tmp_path):
    from app.core.version import release_version, system_version
    backend = _superproject(tmp_path, version=None)
    assert release_version(backend) == "unknown"
    assert system_version(backend) == f"unknown+g{BACKEND[:12]}"


@pytest.mark.unit
def test_the_version_file_is_found_from_the_working_directory_as_env_is(tmp_path, monkeypatch):
    """config reads ../.env from the working directory; VERSION is read the same way."""
    from app.core.version import release_version
    backend = _superproject(tmp_path, version="0.2.0")
    monkeypatch.chdir(backend)
    assert release_version() == "0.2.0"


# ---------------------------------------------------------------------------
# systemVersion: tagged, untagged, no git
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_tagged_release_is_the_bare_release(tmp_path):
    """A lightweight tag in a clone's packed-refs (fully peeled, no ^ line)."""
    from app.core.version import system_version
    backend = _superproject(tmp_path, head=SUPER_TAGGED)
    _packed_tag(backend, "0.1.0", SUPER_TAGGED)
    assert system_version(backend) == "0.1.0"


@pytest.mark.unit
def test_a_loose_lightweight_tag_is_read_through_its_commit_object(tmp_path):
    from app.core.version import system_version
    backend = _superproject(tmp_path, head=SUPER_TAGGED)
    _loose_tag(backend, "0.1.0", SUPER_TAGGED)
    _loose_object(backend, SUPER_TAGGED, "commit", b"tree " + b"0" * 40 + b"\n")
    assert system_version(backend) == "0.1.0"


@pytest.mark.unit
def test_packed_refs_without_the_fully_peeled_trait_is_untagged(tmp_path):
    from app.core.version import system_version
    backend = _superproject(tmp_path, head=SUPER_TAGGED)
    _packed_tag(backend, "0.1.0", SUPER_TAGGED, fully_peeled=False)
    assert system_version(backend) == "0.1.0+gabcdef012345"


@pytest.mark.unit
def test_a_checkout_off_the_tag_carries_the_backend_commit(tmp_path):
    from app.core.version import system_version
    backend = _superproject(tmp_path, head=SUPER_OTHER)
    _packed_tag(backend, "0.1.0", SUPER_TAGGED)
    assert system_version(backend) == "0.1.0+gabcdef012345"


@pytest.mark.unit
def test_no_tag_at_all_is_untagged(tmp_path):
    from app.core.version import system_version
    assert system_version(_superproject(tmp_path)) == "0.1.0+gabcdef012345"


@pytest.mark.unit
def test_no_git_drops_the_revision(tmp_path):
    from app.core.version import code_revision, system_version
    root = tmp_path / "super"
    _write(root / "VERSION", "0.1.0\n")
    backend = root / "backend"
    backend.mkdir()
    assert code_revision(backend) is None
    assert system_version(backend) == "0.1.0"


@pytest.mark.unit
def test_a_backend_with_its_own_git_directory_is_read(tmp_path):
    from app.core.version import code_revision
    assert code_revision(_superproject(tmp_path, backend_gitfile=False)) == BACKEND


@pytest.mark.unit
def test_a_submodule_gitfile_is_followed(tmp_path):
    from app.core.version import code_revision
    backend = _superproject(tmp_path, backend_gitfile=True)
    assert (backend / ".git").is_file()
    assert code_revision(backend) == BACKEND


@pytest.mark.unit
def test_a_symbolic_head_is_resolved_through_a_loose_ref(tmp_path):
    from app.core.version import code_revision
    backend = _superproject(tmp_path, backend_head="ref: refs/heads/dev")
    _write(backend.parent / ".git" / "modules" / "backend" / "refs" / "heads" / "dev", BACKEND + "\n")
    assert code_revision(backend) == BACKEND


@pytest.mark.unit
def test_a_symbolic_head_is_resolved_through_packed_refs(tmp_path):
    from app.core.version import code_revision
    backend = _superproject(tmp_path, backend_head="ref: refs/heads/dev")
    _write(backend.parent / ".git" / "modules" / "backend" / "packed-refs",
           "# pack-refs with: peeled fully-peeled sorted\n"
           f"{'9' * 40} refs/heads/main\n{BACKEND} refs/heads/dev\n")
    assert code_revision(backend) == BACKEND


@pytest.mark.unit
def test_a_worktree_reads_its_refs_from_the_common_dir(tmp_path):
    """A linked worktree's gitdir holds HEAD; refs are in the directory `commondir` names."""
    from app.core.version import code_revision
    root = tmp_path / "repo"
    common = root / ".git"
    _write(common / "refs" / "heads" / "feature", BACKEND + "\n")
    wt_gitdir = common / "worktrees" / "wt"
    _write(wt_gitdir / "HEAD", "ref: refs/heads/feature\n")
    _write(wt_gitdir / "commondir", "../..\n")
    backend = tmp_path / "wt"
    _write(backend / ".git", f"gitdir: {wt_gitdir}\n")
    assert code_revision(backend) == BACKEND


@pytest.mark.unit
def test_an_annotated_tag_in_packed_refs_is_peeled(tmp_path):
    from app.core.version import system_version
    backend = _superproject(tmp_path, head=SUPER_TAGGED)
    _packed_tag(backend, "0.1.0", TAG_OBJECT, peeled=SUPER_TAGGED)
    assert system_version(backend) == "0.1.0"


@pytest.mark.unit
def test_an_annotated_loose_tag_is_peeled_from_its_object(tmp_path):
    from app.core.version import system_version
    backend = _superproject(tmp_path, head=SUPER_TAGGED)
    _loose_tag(backend, "0.1.0", TAG_OBJECT)
    body = f"object {SUPER_TAGGED}\ntype commit\ntag 0.1.0\ntagger x <x@x> 0 +0000\n\nrelease\n".encode()
    _loose_object(backend, TAG_OBJECT, "tag", body)
    assert system_version(backend) == "0.1.0"


@pytest.mark.unit
def test_an_annotated_tag_whose_object_cannot_be_read_is_untagged(tmp_path):
    """A tag object inside a pack is not read; the checkout counts as untagged."""
    from app.core.version import system_version
    backend = _superproject(tmp_path, head=SUPER_TAGGED)
    _loose_tag(backend, "0.1.0", TAG_OBJECT)
    assert system_version(backend) == "0.1.0+gabcdef012345"


# ---------------------------------------------------------------------------
# The version-stop fingerprint
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_code_identity_changes_with_the_backend_commit_and_the_release(tmp_path):
    from app.core.version import code_identity
    a = code_identity(_superproject(tmp_path / "a"))
    b = code_identity(_superproject(tmp_path / "b", backend_head="f" * 40))
    c = code_identity(_superproject(tmp_path / "c", version="0.1.1"))
    assert len({a, b, c}) == 3
    assert BACKEND in a and "0.1.0" in a


@pytest.mark.unit
def test_with_no_revision_the_identity_carries_the_process_start(tmp_path, monkeypatch):
    """Without a revision, the stop clears on every restart rather than never."""
    from app.core import version
    root = tmp_path / "super"
    _write(root / "VERSION", "0.1.0\n")
    backend = root / "backend"
    backend.mkdir()
    first = version.code_identity(backend)
    assert version.PROCESS_STARTED in first
    monkeypatch.setattr(version, "PROCESS_STARTED", "another-start")
    assert version.code_identity(backend) != first


@pytest.mark.unit
def test_the_fisqua_fingerprint_follows_the_code_identity(monkeypatch):
    from app.core import fisqua_outbox, version
    monkeypatch.setattr(version, "code_identity", lambda backend_dir=None: "one")
    a = fisqua_outbox.config_fingerprint()
    monkeypatch.setattr(version, "code_identity", lambda backend_dir=None: "two")
    assert fisqua_outbox.config_fingerprint() != a


@pytest.mark.unit
def test_the_capture_manifest_carries_the_system_version(monkeypatch):
    import inspect

    from app.core import capture_manifest
    assert "system_version()" in inspect.getsource(capture_manifest.build_capture_manifest)
