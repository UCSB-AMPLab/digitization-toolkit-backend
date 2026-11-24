"""The adoption's journal, and the startup recovery that finishes or undoes an adoption a killed process left.

adopt_captures (app.core.adopt_apply) writes a journal into the data
directory before its first rename and removes it once the target's lines
are appended. recover_adoption_at_startup (app.core.adopt_journal) reads it
at the next start and decides by the database: rows holding the new paths
mean the commit landed, and the adoption is finished; rows holding the old
paths mean it did not, and every file is put back. Anything else changes
nothing and keeps the journal, and every adoption is refused meanwhile.

A kill is simulated by raising Killed, a BaseException of these tests' own,
at a chosen point. The adoption does not clean up after a BaseException: the
session is rolled back, as a dead process's transaction is, and nothing on
disk is touched, so what is left is what a kill leaves. Recovery then runs
on a fresh session. Every file is inventoried by sha256 before and after,
so a file overwritten or lost shows.

    python -m pytest tests/unit/test_adopt_journal.py

Version: 0.3.3
"""

import ast
import hashlib
import json
from pathlib import Path

import pytest

from tests.unit.test_adopt_apply import NOW, _adopt, _audit, _journal, _manifest, _setup, _snapshot
from tests.unit.test_adopt_captures_plan import SRC, TGT, _plan, _record, own_data_dir, tenant  # noqa: F401


class Killed(BaseException):
    """The process dies here."""


@pytest.fixture
def kill():
    """A MonkeyPatch for the kill points alone, so undoing it leaves the other fixtures' patches in place."""
    with pytest.MonkeyPatch.context() as patch:
        yield patch


def _inventory(root: Path) -> list:
    """The sha256 of every file under the root but the two manifests, sorted: what must neither be lost nor overwritten."""
    return sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*")
                  if p.is_file() and p.name != "manifest.jsonl")


def _recover():
    from app.core.adopt_journal import recover_adoption_at_startup
    from app.core.db import SessionLocal

    fresh = SessionLocal()
    try:
        return recover_adoption_at_startup(fresh)
    finally:
        fresh.close()


def _kill_at_phase(kill, phase):
    """Kill just after the journal is created (`renaming`), or after the commit, before the journal says so."""
    from app.core import adopt_journal

    if phase == "renaming":
        real = adopt_journal.create_journal

        def create(payload):
            real(payload)
            raise Killed()

        kill.setattr(adopt_journal, "create_journal", create)
        return

    def update(payload):
        raise Killed()

    kill.setattr(adopt_journal, "update_journal", update)


def _kill_after_renames(kill, k):
    from app.core import adopt_apply

    real = adopt_apply._rename
    done = []

    def rename(src, dst):
        real(src, dst)
        done.append(dst)
        if len(done) == k:
            raise Killed()

    kill.setattr(adopt_apply, "_rename", rename)


def _kill_during_append(kill):
    from app.core import adopt_apply

    real = adopt_apply._append_target_lines

    def half(volume_root, lines):
        real(volume_root, lines[:3])
        raise Killed()

    kill.setattr(adopt_apply, "_append_target_lines", half)


def _assert_as_before(db, root, before, inventory, source_manifest):
    rows, files = _snapshot(db, root)
    assert (rows, files) == before
    assert _inventory(root) == inventory
    assert _manifest(root, SRC).read_bytes().startswith(source_manifest)
    assert not _manifest(root, TGT).exists() or _manifest(root, TGT).read_bytes() == b""
    assert not _journal().exists()


def _assert_adopted(db, root, plan, tgt, inventory, target_lines_count=7):
    from app.core import capture_manifest
    from app.core.export_service import _filtered_manifest_blob
    from app.core.integrity import build_fixity_index
    from app.core.paths import preview_sidecar_of
    from app.models.record import Record, RecordImage

    db.expire_all()
    assert _inventory(root) == inventory
    for r in plan.renames:
        assert r.new_path.is_file() and not r.old_path.exists()
        assert db.get(RecordImage, r.image_id).file_path == str(r.new_path)
    lines = [json.loads(l) for l in _manifest(root, TGT).read_text().splitlines() if l.strip()]
    assert len(lines) == target_lines_count
    index = build_fixity_index(root.resolve())
    for r in plan.renames:
        img = db.get(RecordImage, r.image_id)
        master = capture_manifest._resolved(img.file_path)
        sha = hashlib.sha256(master.read_bytes()).hexdigest()
        assert index.expected(r.capture_id, None, master) == sha
        assert capture_manifest._digest(index, r.capture_id, r.image_id, master, r.new_name) == sha
        if r.new_preview_path is not None:
            preview = preview_sidecar_of(master)
            assert capture_manifest._digest(index, r.capture_id, None, preview, preview.name) == \
                hashlib.sha256(preview.read_bytes()).hexdigest()
    records = db.query(Record).filter(Record.collection_id == tgt.id).all()
    bag = [json.loads(l) for l in _filtered_manifest_blob(db, tgt, records).decode().splitlines()]
    assert bag and all(f"{TGT}-img_" in f["relative_path"] for line in bag for f in line["files"])
    assert not _journal().exists()


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_journal_names_everything_the_recovery_needs(db_session, override_projects_root, tenant, kill):
    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _kill_after_renames(kill, 1)

    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)

    journal = json.loads(_journal().read_text())
    assert journal["version"] == 1 and journal["phase"] == "renaming"
    assert (journal["source_id"], journal["source_stem"], journal["target_id"], journal["target_stem"]) == \
        (src.id, SRC, tgt.id, TGT)
    assert journal["actor"] == "admin" and journal["started_at"] == NOW.isoformat()
    assert len(journal["moves"]) == 23
    assert journal["moves"][0] == {"kind": "master", "image_id": plan.renames[0].image_id,
                                   "old": str(plan.renames[0].old_path), "new": str(plan.renames[0].new_path)}
    assert sorted(i["id"] for i in journal["images"]) == sorted(r.image_id for r in plan.renames)
    assert sorted(journal["record_ids"]) == sorted({r.record_id for r in plan.renames})
    assert len(journal["retired_row_ids"]) == 14 and len(journal["target_lines"]) == 7
    assert journal["target_root"] == str(root / TGT)


@pytest.mark.unit
def test_a_successful_adoption_leaves_no_journal(db_session, override_projects_root, tenant):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)

    _adopt(db_session, src, tgt)

    assert not _journal().exists()


# ---------------------------------------------------------------------------
# A kill at each point, then the next start
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("point", ["journal_written", "after_first_rename", "after_middle_rename",
                                   "after_last_rename"])
def test_a_kill_before_the_commit_is_undone_at_the_next_start(db_session, override_projects_root, tenant, kill, point):
    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    before, inventory = _snapshot(db_session, root), _inventory(root)
    source_manifest = _manifest(root, SRC).read_bytes()
    if point == "journal_written":
        _kill_at_phase(kill, "renaming")
    else:
        _kill_after_renames(kill, {"after_first_rename": 1, "after_middle_rename": 10,
                                          "after_last_rename": 23}[point])

    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    assert _journal().exists()

    report = _recover()

    assert report["outcome"] == "reverted", report
    _assert_as_before(db_session, root, before, inventory, source_manifest)
    assert _plan(db_session, src, tgt).refusals == []
    assert _audit(db_session, "volume_captures_adoption_reverted_at_startup")


@pytest.mark.unit
@pytest.mark.parametrize("point", ["after_commit", "during_append"])
def test_a_kill_after_the_commit_is_finished_at_the_next_start(db_session, override_projects_root, tenant, kill, point):
    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    inventory = _inventory(root)
    if point == "after_commit":
        _kill_at_phase(kill, "committed")
    else:
        _kill_during_append(kill)

    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    assert _journal().exists()

    report = _recover()

    assert report["outcome"] == "completed", report
    _assert_adopted(db_session, root, plan, tgt, inventory)
    assert _audit(db_session, "volume_captures_adoption_completed_at_startup")


@pytest.mark.unit
def test_recovery_run_twice_is_recovery_run_once(db_session, override_projects_root, tenant, kill):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    _kill_after_renames(kill, 10)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()

    _recover()
    once = (_snapshot(db_session, root), _manifest(root, SRC).read_bytes())
    assert _recover() is None
    assert (_snapshot(db_session, root), _manifest(root, SRC).read_bytes()) == once


@pytest.mark.unit
def test_a_kill_during_a_recovery_that_puts_files_back_is_recovered_by_the_next_start(db_session, override_projects_root, tenant, kill):
    from app.core import adopt_journal

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before, inventory = _snapshot(db_session, root), _inventory(root)
    source_manifest = _manifest(root, SRC).read_bytes()
    _kill_after_renames(kill, 15)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()

    real = adopt_journal._rename
    done = []

    def rename(a, b):
        real(a, b)
        done.append(b)
        if len(done) == 4:
            raise Killed()

    kill.setattr(adopt_journal, "_rename", rename)
    with pytest.raises(Killed):
        _recover()
    kill.undo()
    assert _journal().exists()

    assert _recover()["outcome"] == "reverted"
    _assert_as_before(db_session, root, before, inventory, source_manifest)


@pytest.mark.unit
def test_a_kill_during_a_recovery_that_appends_the_lines_is_recovered_by_the_next_start(db_session, override_projects_root, tenant, kill):
    from app.core import adopt_journal

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    inventory = _inventory(root)
    _kill_at_phase(kill, "committed")
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()

    real = adopt_journal._append_lines

    def half(volume_root, lines):
        real(volume_root, lines[:2])
        raise Killed()

    kill.setattr(adopt_journal, "_append_lines", half)
    with pytest.raises(Killed):
        _recover()
    kill.undo()

    assert _recover()["outcome"] == "completed"
    _assert_adopted(db_session, root, plan, tgt, inventory)


# ---------------------------------------------------------------------------
# What recovery will not decide
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("case", ["file_at_both", "file_at_neither", "rows_disagree"])
def test_an_ambiguous_state_changes_nothing_keeps_the_journal_and_refuses(db_session, override_projects_root, tenant, kill, case):
    from app.core.adopt_apply import AdoptRefused
    from app.models.record import RecordImage

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _kill_after_renames(kill, 10)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    moved = plan.renames[0]
    if case == "file_at_both":
        moved.old_path.write_bytes(b"a copy put back by hand")
    elif case == "file_at_neither":
        moved.new_path.unlink()
    else:
        db_session.get(RecordImage, moved.image_id).file_path = str(moved.new_path)
        db_session.commit()
    state = (_snapshot(db_session, root), _inventory(root), _journal().read_bytes())

    report = _recover()

    assert report["outcome"] == "unfinished", report
    assert (_snapshot(db_session, root), _inventory(root), _journal().read_bytes()) == state
    [event] = _audit(db_session, "volume_captures_adoption_unfinished")
    assert event.level == "ERR" and "adopt-captures.journal" in event.detail
    assert _plan(db_session, src, tgt).refusals[0] == "adoption_unfinished"
    with pytest.raises(AdoptRefused) as refused:
        _adopt(db_session, src, tgt)
    assert refused.value.codes[0] == "adoption_unfinished"
    assert _journal().read_bytes() == state[2]


# ---------------------------------------------------------------------------
# J4: the target's lines pending after the commit
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_lines_that_could_not_be_appended_are_appended_at_the_next_start(db_session, override_projects_root, tenant, kill):
    import errno

    from app.core import adopt_apply

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    inventory = _inventory(root)

    def disk_full(volume_root, lines):
        raise OSError(errno.ENOSPC, "No space left on device")

    kill.setattr(adopt_apply, "_append_target_lines", disk_full)
    result = _adopt(db_session, src, tgt)
    kill.undo()
    assert result["target_lines_pending"] is True

    assert _recover()["outcome"] == "completed"
    _assert_adopted(db_session, root, plan, tgt, inventory)


@pytest.mark.unit
def test_recovery_runs_before_the_startup_manifest_repair(db_session, override_projects_root, tenant, kill):
    """Run in the order main.py runs them, the repair finds the target's records already there and copies nothing."""
    from app.core.volume_layout import migrate_volume_layout

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    inventory = _inventory(root)
    _kill_at_phase(kill, "committed")
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()

    _recover()
    report = migrate_volume_layout(db_session)

    assert report["manifests_repaired"] == {}
    _assert_adopted(db_session, root, plan, tgt, inventory)
    lines = [json.loads(l) for l in _manifest(root, TGT).read_text().splitlines() if l.strip()]
    assert not any(f["relative_path"].startswith("..") for line in lines for f in line.get("files", []))


@pytest.mark.unit
def test_main_calls_the_recovery_before_the_volume_layout_migration():
    source = (Path(__file__).resolve().parents[2] / "app" / "main.py").read_text()
    calls = [node.func.id for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert "recover_adoption_at_startup" in calls
    assert calls.index("recover_adoption_at_startup") < calls.index("migrate_volume_layout_at_startup")


# ---------------------------------------------------------------------------
# J2: capture into a volume an unfinished adoption names is refused
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_capture_into_a_volume_an_unfinished_adoption_names_is_refused(db_session, override_projects_root, tenant, kill):
    from fastapi import HTTPException

    from app.api.cameras import _refuse_capture_without_volume_or_stem
    from tests.unit.test_adopt_captures_plan import _volume

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    other = _volume(db_session, "Otro", "xx-other-001")
    _kill_after_renames(kill, 1)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()

    for vol in (src, tgt):
        with pytest.raises(HTTPException) as refused:
            _refuse_capture_without_volume_or_stem(db_session, vol.id, None)
        assert refused.value.status_code == 409 and refused.value.detail["code"] == "adoption_unfinished"
    _refuse_capture_without_volume_or_stem(db_session, other.id, None)

    _recover()
    _refuse_capture_without_volume_or_stem(db_session, tgt.id, None)


# ---------------------------------------------------------------------------
# E1: one adoption at a time, and the journal as its claim
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_two_adoptions_at_once_run_one_at_a_time(db_session, override_projects_root, tenant, kill):
    import threading

    from app.core import adopt_apply
    from app.core.adopt_apply import AdoptRefused, adopt_captures
    from app.core.db import SessionLocal
    from tests.unit.test_adopt_captures_plan import _volume

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    other_src = _volume(db_session, "Otra", "xx-other-001")
    other_tgt = _volume(db_session, "Otra asignada", "xx-other-002", external_id="xx-other-002")
    src_id, tgt_id, other = src.id, tgt.id, (other_src.id, other_tgt.id)
    paused, go = threading.Event(), threading.Event()
    real = adopt_apply._rename

    def rename(a, b):
        if not paused.is_set():
            paused.set()
            go.wait(20)
        real(a, b)

    kill.setattr(adopt_apply, "_rename", rename)
    outcome = {}

    def first():
        session = SessionLocal()
        try:
            outcome["first"] = adopt_captures(session, src_id, tgt_id, actor="admin", now=NOW,
                                              list_target_prefix=lambda t, s: [])
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcome["first"] = exc
        finally:
            session.close()

    thread = threading.Thread(target=first)
    thread.start()
    assert paused.wait(20)
    journal = _journal().read_bytes()
    before = _snapshot(db_session, root)

    second = SessionLocal()
    try:
        with pytest.raises(AdoptRefused) as refused:
            adopt_captures(second, *other, actor="admin", now=NOW, list_target_prefix=lambda t, s: [])
    finally:
        second.close()

    assert (refused.value.status_code, refused.value.codes) == (409, ["adoption_in_progress"])
    assert _journal().read_bytes() == journal
    assert _snapshot(db_session, root)[1] == before[1]
    go.set()
    thread.join(30)
    assert isinstance(outcome["first"], dict) and outcome["first"]["images"] == len(plan.renames)
    assert not _journal().exists()


@pytest.mark.unit
def test_a_journal_another_process_wrote_meanwhile_is_neither_overwritten_nor_removed(db_session, override_projects_root, tenant, kill):
    from app.core import volume_stem
    from app.core.adopt_apply import AdoptRefused

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before = _snapshot(db_session, root)
    real = volume_stem.locked_volume_query
    foreign = b'{"version": 1, "run_id": "another process", "source_id": 98, "target_id": 99}'

    def appear(db, collection_id):
        if not _journal().exists():
            _journal().write_bytes(foreign)
        return real(db, collection_id)

    kill.setattr(volume_stem, "locked_volume_query", appear)
    with pytest.raises(AdoptRefused) as refused:
        _adopt(db_session, src, tgt)

    assert refused.value.codes == ["adoption_unfinished"]
    assert _journal().read_bytes() == foreign
    assert _snapshot(db_session, root) == before


@pytest.mark.unit
def test_the_journal_is_created_exclusively_and_rewritten_only_by_its_own_run(override_projects_root):
    from app.core import adopt_journal

    adopt_journal.create_journal({"run_id": "a", "phase": "renaming"})
    with pytest.raises(adopt_journal.JournalTaken):
        adopt_journal.create_journal({"run_id": "b", "phase": "renaming"})
    with pytest.raises(adopt_journal.JournalTaken):
        adopt_journal.update_journal({"run_id": "b", "phase": "committed"})
    with pytest.raises(adopt_journal.JournalTaken):
        adopt_journal.remove_journal("b")
    adopt_journal.update_journal({"run_id": "a", "phase": "committed"})
    assert json.loads(_journal().read_text())["phase"] == "committed"
    adopt_journal.remove_journal("a")
    assert not _journal().exists()


# ---------------------------------------------------------------------------
# G1: the directories made durable are a function of the journal
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_the_directories_to_sync_follow_from_the_journal_alone(tmp_path):
    from app.core.adopt_journal import directories_to_sync

    root = tmp_path / "projects"
    src, tgt = root / "src-stem", root / "tgt-stem"
    journal = {
        "source_root": str(src), "target_root": str(tgt),
        "moves": [{"old": str(src / "captures" / "a.CR2"), "new": str(tgt / "captures" / "b.CR2")},
                  {"old": str(src / "thumbnails" / "t.jpg"), "new": str(tgt / "thumbnails" / "t.jpg")}],
        "created_dirs": [str(tgt / "metadata"), str(tgt / "captures"), str(tgt / "thumbnails"), str(tgt)],
    }

    dirs = directories_to_sync(journal)

    assert set(dirs) == {str(p) for p in (
        src / "captures", src / "thumbnails", src / "metadata", src,
        tgt / "captures", tgt / "thumbnails", tgt / "metadata", tgt, root)}
    depth = [len(Path(d).parts) for d in dirs]
    assert depth == sorted(depth, reverse=True)   # deepest first, the projects root last
    assert dirs[-1] == str(root)


def _record_order(kill, db, order):
    from app.core import adopt_apply, adopt_journal

    real_sync = adopt_journal.sync_journal_directories
    real_remove = adopt_journal.remove_journal
    real_commit = db.commit
    real_append = adopt_apply._append_target_lines
    real_recovery_append = adopt_journal._append_lines

    def sync(journal):
        order.append("sync")
        real_sync(journal)

    def remove(run_id=None):
        order.append("remove")
        real_remove(run_id)

    def commit():
        if _journal().exists():
            order.append("commit")
        real_commit()

    def append(volume_root, lines):
        order.append("append")
        real_append(volume_root, lines)

    def recovery_append(volume_root, lines):
        order.append("append")
        real_recovery_append(volume_root, lines)

    kill.setattr(adopt_journal, "sync_journal_directories", sync)
    kill.setattr(adopt_journal, "remove_journal", remove)
    kill.setattr(adopt_apply, "_append_target_lines", append)
    kill.setattr(adopt_journal, "_append_lines", recovery_append)
    kill.setattr(db, "commit", commit)


@pytest.mark.unit
def test_apply_syncs_before_the_commit_and_again_before_the_journal_goes(db_session, override_projects_root, tenant, kill):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    order = []
    _record_order(kill, db_session, order)

    _adopt(db_session, src, tgt)

    assert order == ["sync", "commit", "append", "sync", "remove"]


@pytest.mark.unit
def test_an_undo_syncs_before_the_journal_goes(db_session, override_projects_root, tenant, kill):
    from app.core.adopt_apply import AdoptFailed
    from tests.unit.test_adopt_apply import _failing_rename

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    _failing_rename(kill, 10)
    order = []
    _record_order(kill, db_session, order)

    with pytest.raises(AdoptFailed):
        _adopt(db_session, src, tgt)

    assert order == ["sync", "remove"]


@pytest.mark.unit
@pytest.mark.parametrize("way", ["forward", "back"])
def test_recovery_syncs_before_it_appends_and_before_it_removes(db_session, override_projects_root, tenant, kill, way):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    if way == "forward":
        _kill_at_phase(kill, "committed")
    else:
        _kill_after_renames(kill, 10)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    order = []
    _record_order(kill, db_session, order)

    _recover()

    assert order == (["sync", "append", "sync", "remove"] if way == "forward" else ["sync", "remove"])


@pytest.mark.unit
def test_a_recovery_killed_after_its_last_rename_back_still_syncs_every_directory_at_the_next_start(db_session, override_projects_root, tenant, kill):
    from app.core import adopt_journal

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    _kill_after_renames(kill, 23)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()

    def die(journal):
        raise Killed()

    kill.setattr(adopt_journal, "sync_journal_directories", die)
    with pytest.raises(Killed):
        _recover()   # every file is back at its old path; nothing made durable yet
    kill.undo()
    assert all(r.old_path.exists() for r in plan.renames)
    journal = json.loads(_journal().read_text())
    synced = []
    real = adopt_journal._fsync_dir
    kill.setattr(adopt_journal, "_fsync_dir", lambda d: (synced.append(str(d)), real(d)))
    real_remove = adopt_journal.remove_journal
    removed_after = []
    kill.setattr(adopt_journal, "remove_journal",
                 lambda run_id=None: (removed_after.append(list(synced)), real_remove(run_id)))

    assert _recover()["outcome"] == "reverted"

    existing = [d for d in adopt_journal.directories_to_sync(journal) if Path(d).is_dir()]
    assert existing and set(existing) <= set(removed_after[0])


# ---------------------------------------------------------------------------
# G2: nothing is renamed outside the storage
# ---------------------------------------------------------------------------

def _outside_captures(root, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / TGT).mkdir(exist_ok=True)
    (root / TGT / "captures").symlink_to(outside, target_is_directory=True)
    return outside


@pytest.mark.unit
def test_a_target_directory_that_leads_outside_the_storage_is_refused(db_session, override_projects_root, tenant, tmp_path):
    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    outside = _outside_captures(root, tmp_path)

    plan = _plan(db_session, src, tgt)

    assert plan.refusals == ["target_outside_storage"]
    assert not list(outside.iterdir())


@pytest.mark.unit
def test_a_target_directory_made_a_link_outside_after_the_plan_is_refused_before_any_rename(db_session, override_projects_root, tenant, tmp_path, kill):
    from app.core import adopt_journal
    from app.core.adopt_apply import AdoptRefused

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before, inventory = _snapshot(db_session, root), _inventory(root)
    real = adopt_journal.create_journal
    made = []

    def create(payload):
        real(payload)
        made.append(_outside_captures(root, tmp_path))

    kill.setattr(adopt_journal, "create_journal", create)
    with pytest.raises(AdoptRefused) as refused:
        _adopt(db_session, src, tgt)

    assert refused.value.codes == ["target_outside_storage"]
    assert not list(made[0].iterdir())
    assert _snapshot(db_session, root)[0] == before[0] and _inventory(root) == inventory
    assert not _journal().exists()


# ---------------------------------------------------------------------------
# G3: the journal exists complete or not at all
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_journal_that_cannot_be_written_leaves_none_and_a_later_adoption_runs(db_session, override_projects_root, tenant, kill):
    import errno

    from app.core import adopt_journal
    from app.core.adopt_apply import AdoptFailed

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    before = _snapshot(db_session, root)

    def disk_full(fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    kill.setattr(adopt_journal, "_write_all", disk_full)
    with pytest.raises(AdoptFailed) as failed:
        _adopt(db_session, src, tgt)
    kill.undo()

    assert failed.value.restored is True
    assert not _journal().exists() and not list(_journal().parent.glob("*.tmp"))
    assert _snapshot(db_session, root) == before
    assert _adopt(db_session, src, tgt)["images"] == len(plan.renames)


@pytest.mark.unit
def test_two_threads_creating_the_journal_at_once_one_wins(override_projects_root):
    import threading

    from app.core import adopt_journal

    barrier = threading.Barrier(2)
    results = []

    def claim(run_id):
        barrier.wait(5)
        try:
            adopt_journal.create_journal({"run_id": run_id, "phase": "renaming"})
            results.append(run_id)
        except adopt_journal.JournalTaken:
            results.append(None)

    threads = [threading.Thread(target=claim, args=(r,)) for r in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    winners = [r for r in results if r]
    assert len(winners) == 1 and json.loads(_journal().read_text())["run_id"] == winners[0]
    assert not list(_journal().parent.glob("*.tmp"))


@pytest.mark.unit
def test_without_hard_links_the_journal_is_created_exclusively_all_the_same(override_projects_root, kill):
    import errno

    from app.core import adopt_journal

    def no_links(a, b):
        raise OSError(errno.EPERM, "Operation not permitted")

    kill.setattr(adopt_journal.os, "link", no_links)
    adopt_journal.create_journal({"run_id": "a", "phase": "renaming"})
    with pytest.raises(adopt_journal.JournalTaken):
        adopt_journal.create_journal({"run_id": "b", "phase": "renaming"})
    assert json.loads(_journal().read_text())["run_id"] == "a"
    assert not list(_journal().parent.glob("*.tmp"))


@pytest.mark.unit
def test_an_unreadable_journal_is_kept_reported_and_named(db_session, override_projects_root, tenant):
    root = override_projects_root
    _setup(db_session, root)
    _journal().write_text('{"version": 1, "run_id": "torn')

    report = _recover()

    assert report["outcome"] == "unfinished" and str(_journal()) in report["reason"]
    assert _journal().read_text() == '{"version": 1, "run_id": "torn'
    [event] = _audit(db_session, "volume_captures_adoption_unfinished")
    assert str(_journal()) in event.detail


# ---------------------------------------------------------------------------
# G4: a manifest line is identified by its whole content
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_two_upload_records_of_one_capture_id_are_both_carried_and_appended_once(db_session, override_projects_root, tenant, kill):
    from capture.manifestHandler import append_upload_record

    from app.core import adopt_apply
    from app.core.export_service import _filtered_manifest_blob
    from app.core.integrity import build_fixity_index
    from app.models.record import Record, RecordImage
    from tests.unit.test_adopt_manifest import _offsite, _row, _write

    root = override_projects_root
    src, tgt, images, _ = _setup(db_session, root)
    shared = images[(5, 0)].capture_id
    rec = _record(db_session, src, "f. 7", mode="single")
    master = _write(root / SRC / "captures" / f"{SRC}-img_0006_c0.jpg", b"second upload")
    append_upload_record(root / SRC, capture_id=shared, relative_path=f"captures/{master.name}",
                         size=master.stat().st_size, mimetype="image/jpeg",
                         sha256=hashlib.sha256(master.read_bytes()).hexdigest())
    second = _row(db_session, rec, master, shared, f"{SRC}-img_0006", "single", True, root)
    _offsite(db_session, second, "capture", f"captua-test/{SRC}/captures/{master.name}", master.read_bytes())
    src.capture_counter = 6
    db_session.commit()
    plan = _plan(db_session, src, tgt)
    assert plan.refusals == [], plan.details
    uploads = [line for line in json.loads(json.dumps(_journal_lines_for(db_session, plan, root)))
               if line.get("record_type") == "upload"]
    assert len(uploads) == 2 and {u["capture_id"] for u in uploads} == {shared}

    real = adopt_apply._append_target_lines

    def first_upload_only(volume_root, lines):
        cut = next(i for i, line in enumerate(lines) if line.get("record_type") == "upload") + 1
        real(volume_root, lines[:cut])
        raise Killed()

    kill.setattr(adopt_apply, "_append_target_lines", first_upload_only)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()

    assert _recover()["outcome"] == "completed"

    target = [json.dumps(json.loads(l), sort_keys=True) for l in _manifest(root, TGT).read_text().splitlines()
              if l.strip()]
    assert len(target) == len(set(target)) == 8
    assert sum(1 for l in target if '"record_type": "upload"' in l) == 2
    db_session.expire_all()
    index = build_fixity_index(root.resolve())
    for r in plan.renames:
        img = db_session.get(RecordImage, r.image_id)
        assert index.expected(img.capture_id, None, Path(img.file_path).resolve()) == \
            hashlib.sha256(Path(img.file_path).read_bytes()).hexdigest()
    records = db_session.query(Record).filter(Record.collection_id == tgt.id).all()
    bag = [json.loads(l) for l in _filtered_manifest_blob(db_session, tgt, records).decode().splitlines()]
    assert sum(1 for line in bag if line.get("record_type") == "upload") == 2


def _journal_lines_for(db, plan, root):
    from app.core.adopt_manifest import adopt_manifest_lines

    return adopt_manifest_lines(plan, [json.loads(l) for l in _manifest(root, SRC).read_text().splitlines()
                                       if l.strip()], adopted_at=NOW.isoformat())[0]


# ---------------------------------------------------------------------------
# E4: the records decide
# ---------------------------------------------------------------------------

def _with_placeholder(db, root):
    from tests.unit.test_adopt_captures_plan import _record

    src, tgt, images, _ = _setup(db, root)
    placeholder = _record(db, src, "missing opening")
    plan = _plan(db, src, tgt)
    assert plan.refusals == [] and plan.records == 5
    return src, tgt, placeholder, plan


@pytest.mark.unit
def test_a_kill_before_the_commit_with_an_imageless_record_is_undone(db_session, override_projects_root, tenant, kill):
    from app.models.record import Record

    root = override_projects_root
    src, tgt, placeholder, plan = _with_placeholder(db_session, root)
    inventory = _inventory(root)
    _kill_after_renames(kill, 10)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()

    assert _recover()["outcome"] == "reverted"
    db_session.expire_all()
    assert db_session.get(Record, placeholder.id).collection_id == src.id
    assert _inventory(root) == inventory and not _journal().exists()


@pytest.mark.unit
def test_a_kill_after_the_commit_with_an_imageless_record_is_finished(db_session, override_projects_root, tenant, kill):
    from app.models.record import Record

    root = override_projects_root
    src, tgt, placeholder, plan = _with_placeholder(db_session, root)
    inventory = _inventory(root)
    _kill_at_phase(kill, "committed")
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()

    assert _recover()["outcome"] == "completed"
    db_session.expire_all()
    assert db_session.get(Record, placeholder.id).collection_id == tgt.id
    _assert_adopted(db_session, root, plan, tgt, inventory)


@pytest.mark.unit
def test_records_split_between_the_volumes_are_unfinished(db_session, override_projects_root, tenant, kill):
    from app.models.record import Record

    root = override_projects_root
    src, tgt, placeholder, plan = _with_placeholder(db_session, root)
    _kill_at_phase(kill, "committed")
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    db_session.get(Record, placeholder.id).collection_id = src.id
    db_session.commit()

    assert _recover()["outcome"] == "unfinished"
    assert _journal().exists()


@pytest.mark.unit
def test_image_rows_that_contradict_the_records_are_unfinished(db_session, override_projects_root, tenant, kill):
    from app.models.record import RecordImage

    root = override_projects_root
    src, tgt, _, plan = _with_placeholder(db_session, root)
    _kill_at_phase(kill, "committed")
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    first = plan.renames[0]
    db_session.get(RecordImage, first.image_id).file_path = str(first.old_path)
    db_session.commit()

    assert _recover()["outcome"] == "unfinished"
    assert _journal().exists()


# ---------------------------------------------------------------------------
# The three limits
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_journal_write_that_fails_after_the_commit_is_still_a_success(db_session, override_projects_root, tenant, kill):
    import errno

    from app.core import adopt_journal

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    inventory = _inventory(root)
    real = adopt_journal.update_journal

    def update(payload):
        if payload["phase"] == "committed":
            raise OSError(errno.EIO, "input/output error")
        real(payload)

    kill.setattr(adopt_journal, "update_journal", update)
    result = _adopt(db_session, src, tgt)
    kill.undo()

    assert result["images"] == 8 and result["recovery_pending"] is True
    [event] = _audit(db_session, "volume_captures_adoption_finish_pending")
    assert event.level == "WARN"
    assert _recover()["outcome"] == "completed"
    _assert_adopted(db_session, root, plan, tgt, inventory)


@pytest.mark.unit
def test_a_journal_removal_that_fails_after_the_commit_is_still_a_success(db_session, override_projects_root, tenant, kill):
    import errno

    from app.core import adopt_journal

    root = override_projects_root
    src, tgt, _, plan = _setup(db_session, root)
    inventory = _inventory(root)

    def remove(run_id=None):
        raise OSError(errno.EIO, "input/output error")

    kill.setattr(adopt_journal, "remove_journal", remove)
    result = _adopt(db_session, src, tgt)
    kill.undo()

    assert result["recovery_pending"] is True and _journal().exists()
    assert _recover()["outcome"] == "completed"
    _assert_adopted(db_session, root, plan, tgt, inventory)


@pytest.mark.unit
def test_after_an_undo_that_failed_the_next_start_puts_the_file_back(db_session, override_projects_root, tenant, kill):
    import errno

    from app.core import adopt_apply
    from app.core.adopt_apply import AdoptFailed
    from tests.unit.test_adopt_apply import _failing_rename

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    before, inventory = _snapshot(db_session, root), _inventory(root)
    source_manifest = _manifest(root, SRC).read_bytes()
    _failing_rename(kill, 10)
    real_back = adopt_apply._rename_back
    stuck = []

    def back(new, old):
        if not stuck:
            stuck.append(new)
            raise OSError(errno.EACCES, "Permission denied")
        real_back(new, old)

    kill.setattr(adopt_apply, "_rename_back", back)
    with pytest.raises(AdoptFailed):
        _adopt(db_session, src, tgt)
    kill.undo()

    assert _recover()["outcome"] == "reverted"
    _assert_as_before(db_session, root, before, inventory, source_manifest)


@pytest.mark.unit
def test_a_recovery_that_raises_keeps_the_journal_refuses_and_is_logged(db_session, override_projects_root, tenant, kill):
    from app.core import adopt_journal

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    _kill_after_renames(kill, 3)
    with pytest.raises(Killed):
        _adopt(db_session, src, tgt)
    kill.undo()
    journal = _journal().read_bytes()

    def broken(db, journal):
        raise RuntimeError("disk vanished")

    kill.setattr(adopt_journal, "_recover", broken)
    report = _recover()

    assert report["outcome"] == "unfinished" and "disk vanished" in report["reason"]
    assert _journal().read_bytes() == journal
    assert _audit(db_session, "volume_captures_adoption_unfinished")
    assert _plan(db_session, src, tgt).refusals[0] == "adoption_unfinished"


# ---------------------------------------------------------------------------
# K2: create_journal raises with no journal of its run left, and returns with one complete and durable
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_directory_sync_that_fails_after_the_link_leaves_no_journal(override_projects_root, kill):
    import errno

    from app.core import adopt_journal

    def eio(directory):
        raise OSError(errno.EIO, "input/output error")

    kill.setattr(adopt_journal, "_fsync_dir", eio)
    with pytest.raises(OSError):
        adopt_journal.create_journal({"run_id": "a", "phase": "renaming"})
    kill.undo()

    assert not _journal().exists() and not list(_journal().parent.glob("*.tmp"))
    adopt_journal.create_journal({"run_id": "b", "phase": "renaming"})


@pytest.mark.unit
def test_a_temporary_file_that_cannot_be_unlinked_after_the_link_leaves_no_journal(override_projects_root, kill):
    import errno

    from app.core import adopt_journal

    real = adopt_journal._unlink
    failed = []

    def unlink(path):
        if path.name.endswith(".tmp") and not failed:
            failed.append(path)
            raise OSError(errno.EIO, "input/output error")
        real(path)

    kill.setattr(adopt_journal, "_unlink", unlink)
    with pytest.raises(OSError):
        adopt_journal.create_journal({"run_id": "a", "phase": "renaming"})

    assert failed and not _journal().exists()


@pytest.mark.unit
def test_a_competing_journal_is_never_removed_by_a_failed_creation(override_projects_root, kill):
    import errno

    from app.core import adopt_journal

    competing = b'{"version": 1, "run_id": "other run"}'

    def replaced_then_eio(directory):
        # Another process removed this run's journal and wrote its own in its place.
        _journal().unlink()
        _journal().write_bytes(competing)
        raise OSError(errno.EIO, "input/output error")

    kill.setattr(adopt_journal, "_fsync_dir", replaced_then_eio)
    with pytest.raises(OSError):
        adopt_journal.create_journal({"run_id": "a", "phase": "renaming"})
    kill.undo()

    assert _journal().read_bytes() == competing


@pytest.mark.unit
def test_a_removal_whose_directory_sync_fails_has_still_removed_the_journal(override_projects_root, kill):
    import errno

    from app.core import adopt_journal

    adopt_journal.create_journal({"run_id": "a", "phase": "renaming"})

    def eio(directory):
        raise OSError(errno.EIO, "input/output error")

    kill.setattr(adopt_journal, "_fsync_dir", eio)
    adopt_journal.remove_journal("a")   # returns: the journal is gone, its removal's sync was best effort

    assert not _journal().exists()


@pytest.mark.unit
def test_an_update_that_raises_leaves_a_complete_journal_of_this_run(override_projects_root, kill):
    import errno

    import capture.utils as utils
    from app.core import adopt_journal

    adopt_journal.create_journal({"run_id": "a", "phase": "renaming"})

    def eio(directory):
        raise OSError(errno.EIO, "input/output error")

    kill.setattr(utils, "_fsync_dir", eio)
    with pytest.raises(OSError):
        adopt_journal.update_journal({"run_id": "a", "phase": "committed"})
    kill.undo()

    journal = json.loads(_journal().read_text())
    assert journal["run_id"] == "a" and journal["phase"] in ("renaming", "committed")


@pytest.mark.unit
def test_an_adoption_whose_journal_removal_sync_fails_reports_nothing_pending(db_session, override_projects_root, tenant, kill):
    import errno

    from app.core import adopt_journal

    root = override_projects_root
    src, tgt, _, _ = _setup(db_session, root)
    real = adopt_journal._fsync_dir

    def eio_after_removal(directory):
        if Path(directory) == _journal().parent and not _journal().exists():
            raise OSError(errno.EIO, "input/output error")
        real(directory)

    kill.setattr(adopt_journal, "_fsync_dir", eio_after_removal)
    result = _adopt(db_session, src, tgt)

    assert result["recovery_pending"] is False and not _journal().exists()
