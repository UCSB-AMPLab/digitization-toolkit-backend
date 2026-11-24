"""A colour card change that waited for an adoption's lock is refused when the adoption left its journal, on PostgreSQL.

app.core.colour_card._locked_volume reads the adoption journal again once it
holds the volume's row lock. Here one session holds the volume's lock, as
an adoption does from its lock step to its commit, while a second opens a
colour card set: it passes the first, early read of the journal (there is
none yet) and waits for the lock. The holder then writes a journal naming
the volume, as an adoption that committed and could not append the target's
lines leaves it, and commits. The waiting request must answer 409
`adoption_unfinished`, and no set may be written. SQLite takes no row lock,
so only a real PostgreSQL shows the wait
(tests/unit/test_adopt_colour_card.py checks the order with a patched lock).

Opt-in, as tests/integration/test_pg_retake_reads_images_after_lock.py:
skipped unless DATABASE_HOST is set and DATABASE_* reach a PostgreSQL
server; the tables are created in a schema of their own, dropped
afterwards. The waiting session is detected in pg_locks, never by time.
"""
import json
import os
import uuid

import pytest


if "DATABASE_HOST" not in os.environ:
    pytest.skip("PostgreSQL lock tests are opt-in: set DATABASE_* to a PostgreSQL server",
                allow_module_level=True)

from tests.integration.test_pg_retake_reads_images_after_lock import (  # noqa: E402
    PG_LIMITS, _blocked_by, _in_a_thread, _joined, _pid, _until)


@pytest.fixture
def pg(monkeypatch, tmp_path):
    """A throwaway schema with one stemmed volume, and a data directory of the test's own for the journal."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import sessionmaker

    from app.core import colour_card, config
    from app.core.db import Base, DATABASE_URL
    # Every table, so that create_all makes the ones a volume's commit reads (offsite_objects, the colour card's).
    from app.models import (camera, capture_request, colour_card as _cc, fisqua, offsite_object,  # noqa: F401
                            project_member, record, system_log, unit_settings, user)
    from app.models.collection import Collection
    from app.models.project import Project

    schema = f"captua_cardguard_{uuid.uuid4().hex[:12]}"
    admin = create_engine(DATABASE_URL, connect_args={"options": PG_LIMITS})
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    except OperationalError as exc:
        admin.dispose()
        pytest.skip(f"DATABASE_* does not reach a PostgreSQL server: {exc.orig}")

    engine = create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={schema} {PG_LIMITS}"})
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config.settings, "DATA_DIR", str(data))
    # Whether the unit asks for shots is not what is tested here.
    monkeypatch.setattr(colour_card, "_refuse_unless_asked", lambda db, volume: None)
    try:
        Base.metadata.create_all(engine)
        with Session() as db:
            p = Project(name="P")
            db.add(p)
            db.commit()
            v = Collection(name="V", project_id=p.id, filename_stem="co-test-card")
            db.add(v)
            db.commit()
            volume_id = v.id
        yield {"Session": Session, "engine": engine, "volume": volume_id, "data": data}
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.integration
def test_pg_a_colour_card_set_opened_while_an_adoption_held_the_volume_is_refused(pg):
    from app.core import colour_card
    from app.core.adopt_journal import journal_file
    from app.core.volume_stem import locked_volume_query
    from app.models.colour_card import ColourCardSet
    from app.models.user import User

    holder = pg["Session"]()
    locked_volume_query(holder, pg["volume"]).first()
    waiter = pg["Session"]()
    thread = None
    try:
        holder_pid, waiter_pid = _pid(holder), _pid(waiter)
        thread, done, out = _in_a_thread(
            lambda: colour_card.open_set(waiter, pg["volume"], User(username="op", role="operator")))
        _until(lambda: _blocked_by(pg["engine"], waiter_pid, holder_pid) or done.is_set(),
               "the colour card request waiting for the volume's lock")
        assert not done.is_set(), f"the request did not wait for the lock: {out}"

        # The adoption commits and keeps its journal (its target lines could not be appended).
        journal_file().write_text(json.dumps({"version": 1, "run_id": "held", "phase": "committed",
                                              "source_id": -1, "target_id": pg["volume"]}))
        holder.commit()
        _until(done.is_set, "the request to answer once the lock was released")

        error = out.get("error")
        assert error is not None and getattr(error, "status_code", None) == 409, out
        assert error.detail["code"] == "adoption_unfinished"
        with pg["Session"]() as db:
            assert db.query(ColourCardSet).filter(ColourCardSet.collection_id == pg["volume"]).count() == 0
    finally:
        holder.rollback()
        holder.close()
        if thread is not None:
            _joined(thread)
        waiter.rollback()
        waiter.close()
