"""Activity totals for the dashboard: where each collection's records stand,
and how much was captured and reviewed on each recent day.

Both endpoints count in the database or over a bounded set of timestamp
columns. Neither loads ORM objects, because a unit holds thousands of records
and the dashboard calls them on every visit.
"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.api.auth import RoleChecker
from app.api.deps import get_db_dependency
from app.models.collection import Collection
from app.models.record import Record, RecordImage, RecordStatusChange
from app.models.user import User
from app.schemas.activity import CollectionActivity, DailyActivity, StatusCounts

router = APIRouter()

allow_read_only = RoleChecker(["admin", "operator", "reviewer"])

STATUSES = ("captured", "in_review", "rejected", "approved")
REVIEW_OUTCOMES = ("approved", "rejected")


def _collections_in_scope(db: Session, project_id: Optional[int]) -> List[int]:
    """Every collection id, or those belonging to a project at any depth.

    A nested collection has no project_id of its own, only a parent, so the
    project's collections are found by walking down from its top-level ones.
    The walk runs over one query of the whole (small) collections table rather
    than one query per level, which is what
    app.core.storage_ops.collection_ids_under_project does.
    """
    rows = db.query(
        Collection.id, Collection.project_id, Collection.parent_collection_id
    ).order_by(Collection.id).all()
    if project_id is None:
        return [cid for cid, _, _ in rows]

    children: dict[int, list[int]] = defaultdict(list)
    for cid, _, parent in rows:
        if parent is not None:
            children[parent].append(cid)
    found: set[int] = set()
    stack = [cid for cid, pid, _ in rows if pid == project_id]
    while stack:
        cid = stack.pop()
        if cid in found:  # a malformed cyclic tree would otherwise loop
            continue
        found.add(cid)
        stack.extend(children[cid])
    return [cid for cid, _, _ in rows if cid in found]


@router.get("/collections", response_model=List[CollectionActivity])
def collection_activity(
    project_id: Optional[int] = Query(None, description="Only this project's collections, at any depth"),
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency),
):
    """Status counts and the time of the latest status change, per collection.

    Every collection in scope is listed, those with no records with zeros and
    a null time. A live record counts under its current collection, and so do
    its status changes; a deleted record's changes (record_id null) count under
    the collection stored on each change row, as in
    GET /collections/{id}/status-changes.
    """
    ids = _collections_in_scope(db, project_id)
    if not ids:
        return []

    counts: dict[int, dict[str, int]] = defaultdict(dict)
    for cid, status, n in (
        db.query(Record.collection_id, Record.status, func.count(Record.id))
        .filter(Record.collection_id.in_(ids))
        .group_by(Record.collection_id, Record.status)
        .all()
    ):
        counts[cid][status] = n

    # The collection a change row belongs to: the record's current one while
    # the record exists, the stored snapshot once it has been deleted.
    owner = case(
        (RecordStatusChange.record_id.is_(None), RecordStatusChange.collection_id),
        else_=Record.collection_id,
    )
    last_change: dict[int, datetime] = {
        cid: _as_naive_utc(t)
        for cid, t in db.query(owner, func.max(RecordStatusChange.changed_at))
        .outerjoin(Record, RecordStatusChange.record_id == Record.id)
        .filter(owner.in_(ids))
        .group_by(owner)
        .all()
    }

    results = []
    for cid in ids:
        by_status = StatusCounts(**counts.get(cid, {}))
        results.append(CollectionActivity(
            collection_id=cid,
            status_counts=by_status,
            record_count=sum(getattr(by_status, s) for s in STATUSES),
            last_status_change_at=last_change.get(cid),
        ))
    return results


def _utcnow() -> datetime:
    """The current time, naive UTC like the stored timestamps."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(t: datetime) -> datetime:
    """Timestamps are stored naive UTC; normalise one that comes back aware."""
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


@router.get("/daily", response_model=List[DailyActivity])
def daily_activity(
    days: int = Query(182, ge=1, le=400, description="How many local days, ending today"),
    tz_offset_minutes: int = Query(
        0, ge=-840, le=840,
        description="The browser's Date.getTimezoneOffset(): minutes to add to local time to get UTC",
    ),
    current_user: User = Depends(allow_read_only),
    db: Session = Depends(get_db_dependency),
):
    """Images captured and records reviewed on each of the last `days` local
    days, oldest first, with a zero for a day on which nothing was done.

    The local day of a stored naive-UTC timestamp t is the date of
    t - tz_offset_minutes minutes.

    Bucketing is done in Python, not SQL. Shifting a timestamp by a
    parameterised number of minutes and taking its date is written differently
    in SQLite (datetime(t, '-300 minutes')) and Postgres (t - interval), and
    the unit tests run on the one while production runs on the other. Instead
    the window's UTC bounds are computed here and the database returns only
    the timestamp columns inside them, which are bounded by what one unit can
    capture and review in `days` days.
    """
    offset = timedelta(minutes=tz_offset_minutes)
    today = (_utcnow() - offset).date()
    first = today - timedelta(days=days - 1)
    # Local midnight at the start of `first` and after `today`, in UTC.
    start_utc = datetime.combine(first, time.min) + offset
    end_utc = datetime.combine(today + timedelta(days=1), time.min) + offset

    def local_day(t: datetime) -> date:
        return (_as_naive_utc(t) - offset).date()

    captured: dict[date, int] = defaultdict(int)
    for (created_at,) in (
        db.query(RecordImage.created_at)
        .filter(RecordImage.created_at >= start_utc, RecordImage.created_at < end_utc)
        .all()
    ):
        captured[local_day(created_at)] += 1

    # Per day, the distinct live records reviewed, plus one for each row of a
    # deleted record, whose rows can no longer be told apart by record.
    reviewed_ids: dict[date, set[int]] = defaultdict(set)
    reviewed_deleted: dict[date, int] = defaultdict(int)
    for record_id, changed_at in (
        db.query(RecordStatusChange.record_id, RecordStatusChange.changed_at)
        .filter(
            RecordStatusChange.to_status.in_(REVIEW_OUTCOMES),
            RecordStatusChange.changed_at >= start_utc,
            RecordStatusChange.changed_at < end_utc,
        )
        .all()
    ):
        d = local_day(changed_at)
        if record_id is None:
            reviewed_deleted[d] += 1
        else:
            reviewed_ids[d].add(record_id)

    out = []
    for i in range(days):
        d = first + timedelta(days=i)
        out.append(DailyActivity(
            date=d,
            captured_images=captured.get(d, 0),
            reviewed_records=len(reviewed_ids.get(d, ())) + reviewed_deleted.get(d, 0),
        ))
    return out
