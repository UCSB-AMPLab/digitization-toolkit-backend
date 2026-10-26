"""What the unit sends Fisqua, and what Fisqua last said about each volume.

`fisqua_outbox` has one row per capture manifest to deliver. A handover of
a volume (POST /collections/{id}/promote) adds one when Fisqua is configured;
the worker (app.core.fisqua_outbox) builds the manifest once every object of
the volume is verified in the bucket and hands it to the transport. States:

- `waiting_for_upload`: handed over, not yet received in the bucket. No
  manifest yet; `manifest_id` is reserved, and the manifest built later
  carries it as its `id`.
- `ready`: the manifest is built (`manifest`) and waits for the transport.
- `sending`: the worker has claimed it and is calling the transport. A row
  found in this state when a run begins was left by a crash, and is retried.
- `delivered`: Fisqua accepted it (`delivered_at`, `response`, and in
  `external_id` the volumeId Fisqua gave it). The status poll asks by the
  volume's reference code (its recorded external_id, else its stem), not
  by this id.
- `refused`: final, with `refusal` saying by whom. `not_exportable`: the unit
  could not build the manifest, and `problems` lists the capture_manifest
  problems, as GET /collections/{id}/capture-manifest gives them.
  `fisqua`: Fisqua refused it, with its answer in `response` and any
  problems it named in `problems`.
- `failed`: the last attempt ended in a transient error (`last_error`); the
  worker tries again at `next_attempt_at`. A poll that finds Fisqua holding
  this manifest id marks it `delivered`.
- `superseded`: a later handover of the volume queued a newer manifest
  before this one was sent.
- `withdrawn`: the handover was taken back before this one was sent.

`fisqua_status` holds, per volume, the latest status Fisqua gave for its
latest delivered manifest. A table of its own rather than columns on
collections: the poll writes it without touching the volume's row, which
PATCH, the stem guards and the handover paths lock, and the Fisqua fields
stay out of CollectionRead and the volume listings.

Version: 0.1.0-beta
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.core.db import Base

STATES = ("waiting_for_upload", "ready", "sending", "delivered", "refused", "failed", "superseded", "withdrawn")
REFUSALS = ("not_exportable", "fisqua")

_STATE_LIST = ", ".join(f"'{s}'" for s in STATES)
_REFUSAL_LIST = ", ".join(f"'{r}'" for r in REFUSALS)
STATE_CHECK = f"state IN ({_STATE_LIST})"
REFUSAL_CHECK = (f"(state = 'refused' AND refusal IS NOT NULL AND refusal IN ({_REFUSAL_LIST})) OR "
                 "(state <> 'refused' AND refusal IS NULL)")
DELIVERED_CHECK = ("(state = 'delivered' AND delivered_at IS NOT NULL) OR "
                   "(state <> 'delivered' AND delivered_at IS NULL)")
ATTEMPTS_CHECK = "attempts >= 0"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FisquaOutbox(Base):
    __tablename__ = "fisqua_outbox"

    id = Column(Integer, primary_key=True, index=True)
    collection_id = Column(Integer, ForeignKey("collections.id", ondelete="CASCADE"), nullable=False, index=True)
    manifest_id = Column(String(36), nullable=False)
    stem = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    state = Column(String(20), nullable=False)
    attempts = Column(Integer, default=0, server_default="0", nullable=False)
    next_attempt_at = Column(DateTime, nullable=True)
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    refusal = Column(String(20), nullable=True)
    problems = Column(JSON, nullable=True)
    manifest = Column(JSON, nullable=True)
    response = Column(JSON, nullable=True)
    external_id = Column(String(255), nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    # The reference code the manifest was sent under (its
    # volumes[0].externalId), written when the manifest is built. Status
    # and decisions are asked for by it, not by the volume's code as it is
    # now, which a first external_id set afterwards would change.
    reference_code = Column(String(255), nullable=True)
    # Set by a take-back (`withdrawn`) or a newer handover (`superseded`)
    # while the row is `sending`, which they cannot change under the worker.
    # The worker takes it as the row's state when the send ends in anything
    # but delivered or refused, rather than scheduling a retry.
    cancel_requested = Column(String(20), nullable=True)

    __table_args__ = (
        UniqueConstraint("manifest_id", name="fisqua_outbox_manifest_id_key"),
        CheckConstraint(STATE_CHECK, name="check_fisqua_outbox_state"),
        CheckConstraint(REFUSAL_CHECK, name="check_fisqua_outbox_refusal"),
        CheckConstraint(DELIVERED_CHECK, name="check_fisqua_outbox_delivered"),
        CheckConstraint(ATTEMPTS_CHECK, name="check_fisqua_outbox_attempts"),
        CheckConstraint("cancel_requested IS NULL OR cancel_requested IN ('withdrawn', 'superseded')",
                        name="check_fisqua_outbox_cancel_requested"),
    )


class FisquaStatus(Base):
    __tablename__ = "fisqua_status"

    collection_id = Column(Integer, ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True)
    # The delivered manifest this status was asked for.
    manifest_id = Column(String(36), nullable=True)
    # Fisqua's own name for the state, as it gave it.
    status = Column(Text, nullable=True)
    # When Fisqua says the volume reached that state, if it says.
    status_at = Column(DateTime, nullable=True)
    # When the unit last had an answer.
    status_checked_at = Column(DateTime, nullable=False)
    problems = Column(JSON, nullable=True)
    # QC decisions (contract 1.2 §9.5, app.core.fisqua_decisions): the last
    # set applied, when Fisqua decided it and the unit applied it, for which
    # manifest, and what applying it did (counts, and each record or
    # rotation skipped with why). `decisions_stale_id` is the last set seen
    # for another manifest, kept so it is not fetched again.
    decisions_id = Column(String(255), nullable=True)
    decisions_decided_at = Column(DateTime, nullable=True)
    decisions_applied_at = Column(DateTime, nullable=True)
    decisions_manifest_id = Column(String(36), nullable=True)
    decisions_report = Column(JSON, nullable=True)
    decisions_stale_id = Column(String(255), nullable=True)
    # The last decisionsId a status entry offered. While it is neither
    # applied nor stale, the volume is polled even in a final state, so a
    # set offered with the final status is not lost to a failed fetch.
    decisions_offered_id = Column(String(255), nullable=True)
