"""An adoption of a source with no colour card set writes exactly what it wrote before shots were adopted.

The source of tests/unit/test_adopt_apply.py (pages only) is adopted with
uuid4 made deterministic. The journal as it is created, the adoption's
summary, every row of every table but the system log, every file under the
projects root (path and sha256) and both manifests' lines are compared with
tests/fixtures/adopt_no_shots_golden.json, recorded from the adoption of
pages alone before shots were added to it. Paths are written relative to the
projects root, the journal's run id as `<run>`, and timestamps as `<t>`.

    python -m pytest tests/unit/test_adopt_no_shots_unchanged.py

Version: 0.3.3
"""

import copy
import hashlib
import json
import re
import uuid
from pathlib import Path

import pytest

from tests.unit.test_adopt_apply import NOW, _adopt, _manifest, _setup
from tests.unit.test_adopt_captures_plan import SRC, TGT, own_data_dir, tenant  # noqa: F401

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "adopt_no_shots_golden.json"
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)?")


def _normal(value, root: Path):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    text = text.replace(str(root.resolve()), "<root>").replace(str(root), "<root>")
    return _without_host(json.loads(_STAMP.sub("<t>", text)))


# The capture lines carry the machine that took them; the test's fixtures
# stamp the machine the test runs on, which is not the same from one to the next.
_HOST_KEYS = ("hostname", "machine", "platform")


def _without_host(value):
    if isinstance(value, dict):
        return {k: ("<host>" if k in _HOST_KEYS else _without_host(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_without_host(v) for v in value]
    return value


def capture(db, root, monkeypatch) -> dict:
    """What a pages-only adoption writes, normalised (module header)."""
    from app.core import adopt_journal
    from app.core.db import Base

    counter = iter(range(1, 10 ** 6))
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(counter)))
    src, tgt, _, _ = _setup(db, root)
    journals = []
    real = adopt_journal.create_journal

    def create(payload):
        journals.append({**copy.deepcopy(payload), "run_id": "<run>"})
        real(payload)

    monkeypatch.setattr(adopt_journal, "create_journal", create)
    result = _adopt(db, src, tgt)
    db.expire_all()
    rows = {}
    for table in Base.metadata.sorted_tables:
        if table.name != "system_logs":
            rows[table.name] = sorted([str(v) for v in r] for r in db.execute(table.select()).fetchall())
    files = sorted([str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest()]
                   for p in root.rglob("*") if p.is_file() and p.name != "manifest.jsonl")
    manifests = {stem: [json.loads(l) for l in _manifest(root, stem).read_text().splitlines() if l.strip()]
                 for stem in (SRC, TGT)}
    return _normal({"journal": journals, "result": result, "rows": rows, "files": files,
                    "manifests": manifests, "now": NOW.isoformat()}, root)


@pytest.mark.unit
def test_a_pages_only_adoption_writes_what_it_wrote_before(db_session, override_projects_root, tenant, monkeypatch):
    got = capture(db_session, override_projects_root, monkeypatch)

    want = json.loads(GOLDEN.read_text())
    # The tables are compared by their rows: a table added since is empty here, and is not in the record.
    got_rows = {name: rows for name, rows in got["rows"].items() if rows or name in want["rows"]}
    # The summary's colour card counts came with the shots; they are zero here, and every other key is as before.
    result = dict(got["result"])
    assert (result.pop("colour_card_sets"), result.pop("colour_card_shots")) == (0, 0)
    # The one expected difference in the rows: collections.captures_adopted_at, set on the source when it is
    # closed (a timestamp, `<t>`) and empty on the target. Every other value of every row is as before.
    from app.core.db import Base

    names = [c.name for c in Base.metadata.tables["collections"].columns]
    at, ident = names.index("captures_adopted_at"), names.index("id")
    closed = {}
    for row in got_rows["collections"]:
        closed[row[ident]] = row.pop(at)
    assert closed == {"1": "<t>", "2": "None"}, closed
    assert {**got, "rows": got_rows, "result": result} == want
