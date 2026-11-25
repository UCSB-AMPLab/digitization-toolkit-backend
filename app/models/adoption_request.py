"""The adoptions a client named with its own id, and how each ended.

POST /collections/{source}/adopt-into/{target} may carry
`client_request_id`, a UUID the client mints for one adoption. A row is
written when the adoption takes ADOPTION_LOCK and completed when the
request ends (app.core.adoption_requests).

- `request_id`: the client's id, in canonical lowercase form; one row per id.
- `source_id`, `target_id`: the volumes the request named, as plain ids:
  a refused request may name a volume that does not exist, and a volume
  adopted away may be deleted afterwards. NULL on a closed id's row.
- `started_at`: when the adoption took ADOPTION_LOCK; NULL on a closed
  id's row. `ended_at`: when the outcome was written, or the id closed;
  NULL until then.
- `outcome`: NULL until the request is settled; `applied` by the
  adoption's own transaction, in its commit; `refused` or `failed` as the
  request answered; `reverted` by a recovery that undid it, or by a lookup
  that found, under ADOPTION_LOCK, no journal of the request in place;
  `closed` when a lookup answered that the unit had no request
  with the id, after which no request with it adopts
  (`check_adoption_request_outcome`).
- `status`, `body`: the HTTP status and body the request answered, which
  a repeated request answers again; NULL for `reverted` and `closed`.
"""

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, Integer, String, UniqueConstraint

from app.core.db import Base

OUTCOMES = ("applied", "refused", "failed", "reverted", "closed")
OUTCOME_CHECK = "outcome IS NULL OR outcome IN ('applied', 'refused', 'failed', 'reverted', 'closed')"


class AdoptionRequest(Base):
    __tablename__ = "adoption_requests"

    id = Column(Integer, primary_key=True)
    request_id = Column(String(36), nullable=False)
    source_id = Column(Integer, nullable=True)
    target_id = Column(Integer, nullable=True)
    started_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    outcome = Column(String(12), nullable=True)
    status = Column(Integer, nullable=True)
    body = Column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("request_id", name="adoption_requests_request_id_key"),
        CheckConstraint(OUTCOME_CHECK, name="check_adoption_request_outcome"),
    )
