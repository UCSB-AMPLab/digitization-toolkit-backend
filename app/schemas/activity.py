import datetime as dt
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class StatusCounts(BaseModel):
    """How many of a collection's records are at each QA status. Every key is
    always present, with 0 for a status no record is at."""
    captured: int = 0
    in_review: int = 0
    rejected: int = 0
    approved: int = 0


class CollectionActivity(BaseModel):
    """One collection's place in the capture-and-review pipeline."""
    collection_id: int
    status_counts: StatusCounts
    # The sum of the four status counts.
    record_count: int
    # The newest status change among the collection's records, live and
    # deleted; None when no change has been recorded. Naive UTC.
    last_status_change_at: Optional[datetime] = None


class DailyActivity(BaseModel):
    """What was done on one local calendar day."""
    date: dt.date
    # record_images rows created that day, retakes included.
    captured_images: int
    # Distinct records approved or rejected that day; each status change of a
    # deleted record counts as one.
    reviewed_records: int
