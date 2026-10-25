"""Every name the unit uses on Fisqua's side of the exchange (contract 1.2), in one place.

Hosts, paths, query parameters, answer fields, rule ids, status names, and
which HTTP answers mean what. A rename in the contract is an edit here and
nowhere else.

The unit's token is 64 lowercase hex characters (TOKEN_PATTERN); beyond
that shape it is opaque to the unit, sent as it is and never parsed.

Contract 1.2 is approved (archiving 5e480b7, §9).
"""

import re

TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# At most this many calls a minute per token; the unit sends at most this
# many manifests per pass, and a pass runs at most every few minutes.
CALLS_PER_MINUTE = 60
MAX_SENDS_PER_PASS = CALLS_PER_MINUTE
# Of those, status batches leave this many for reading QC decisions, so a
# unit with many volumes still reads the decisions its status calls find.
DECISIONS_RESERVED_CALLS = 10

CONTRACT_VERSION = "1.2"

# DTK_FISQUA_ENV -> base URL, with the unit's offsite tenant as `tenant`.
ENVIRONMENTS = {
    "production": "https://{tenant}.fisqua.org",
    "staging": "https://{tenant}.staging.fisqua.org",
}

AUTH_HEADER = "Authorization"
AUTH_SCHEME = "Bearer"
RETRY_AFTER_HEADER = "Retry-After"

MANIFESTS_PATH = "/api/capture/v1/manifests"
STATUS_PATH = "/api/capture/v1/volumes/status"
STATUS_REFS_PARAM = "refs"
STATUS_REFS_SEPARATOR = ","
MAX_REFS_PER_CALL = 100

# Fields of Fisqua's answers.
F_STATE = "state"
F_MANIFEST_ID = "manifestId"
F_VOLUME_ID = "volumeId"
F_PROBLEMS = "problems"
F_RULE = "rule"
F_REF = "ref"
F_ENTERED_AT = "enteredAt"
# The status answer is an object holding the volumes under this key (a bare
# list is also accepted).
F_VOLUMES = "volumes"

# Rules Fisqua names in problems. The unit acts on none of them by name: a
# 400 or 409 with problems is a refusal whatever the rule.
RULE_ID_REUSED = "id-reused"
RULE_REFERENCE_CODE = "reference-code"  # 422: the manifest's volume has no reference code
RULE_RUN_STARTED = "run-started"  # 409: a newer manifest for a volume a run has started on
# 422: an object the manifest names was not in the masters bucket when it
# arrived (§9.2). One problem per missing object: objectKey exactly as the
# manifest's objectKey or previewObjectKey, captureId, role, and kind
# ("capture" or "preview").
RULE_OBJECT_MISSING = "object-missing"
F_OBJECT_KEY = "objectKey"
F_KIND = "kind"
# Not Fisqua's: the problems the unit records for a 413 or a 400 with no problems in it.
RULE_TOO_LARGE = "payload-too-large"
RULE_BAD_REQUEST = "bad-request"

# How the unit reads an HTTP status from Fisqua (app.core.fisqua_http).
# Refused, final, for that manifest id, with or without problems (§9.2:
# 400 is a malformed body).
REFUSED_ALWAYS = (400, 413, 422)
# Refused, final, when the body holds a non-empty `problems` array (the id
# reused, or a newer manifest for a volume a run has started on). Without
# problems: retried.
REFUSED_WITH_PROBLEMS = (409,)
# A refused manifest does not hold the volume: a later one, with a new id,
# is checked afresh (§9.3).
# A configuration error: sending and polling stop for the whole unit until
# the environment, the base URL or the token changes. Fisqua sends no
# redirect, and following one would carry the token elsewhere, so every 3xx
# counts here too.
CONFIG_ERROR = (401, 403, 404)
CONFIG_ERROR_REASONS = {401: "token", 403: "token", 404: "not_found"}
REDIRECT_REASON = "redirect"
# 400 rules (§9.2) that mean the unit's software and Fisqua disagree about
# the manifest itself, not that one volume is wrong: the refusal is final for
# its manifest, and the unit stops sending until its software changes or a
# retest finds that Fisqua reads its manifests (fisqua_outbox._retest_version_stop).
VERSION_RULES = ("version", "envelope", "shape")
VERSION_REASON = "version"

# Fisqua's states for a volume (§9.3, as amended). These are not Captua's
# record statuses: Fisqua's `approved` is a whole volume through its capture
# QC, and is not app.schemas.record's `approved` for one record.
STATES = ("received", "registered", "preparing_review", "in_capture_qc", "retakes_requested",
          "approved", "queued", "running", "review_pending", "published", "refused",
          "superseded", "failed", "unknown")
# A volume in one of these is not polled again for the same manifest.
# `unknown` (Fisqua does not know the volume; usually the manifest was not
# received) is not final: it is shown and polled again.
FINAL_STATES = ("published", "refused", "superseded")
STATE_UNKNOWN = "unknown"
STATE_REFUSED = "refused"

# ---------------------------------------------------------------------------
# QC decisions (§9.5)
# ---------------------------------------------------------------------------

# In a status entry, once Fisqua has decided any of the volume's openings.
F_DECISIONS_ID = "decisionsId"
F_DECIDED_AT = "decidedAt"

# GET, with the volume's reference code quoted into the path.
DECISIONS_PATH = "/api/capture/v1/volumes/{ref}/decisions"

# A 404 on the decisions read with this rule: an unknown reference, or one
# with nothing decided yet. Not a configuration error: the unit reads
# decisions only when status offered a decisionsId, so this is a race, and
# the set is read again at the next poll. A 404 without it, or on another
# path, stays a configuration error.
RULE_NO_DECISIONS = "no-decisions"

# Fields of the decisions answer. captuaRecordId is an integer, as in the
# manifest; decidedBy is the reviewer's display name, or "Fisqua reviewer"
# when none is set, never an email.
F_RECORDS = "records"
F_CAPTUA_RECORD_ID = "captuaRecordId"
F_DECISION = "decision"
F_REASON = "reason"
F_NOTE = "note"
F_DECIDED_BY = "decidedBy"
F_ROTATIONS = "rotations"
F_CAPTURE_ID = "captureId"
F_ROLE = "role"
F_QUARTER_TURNS = "quarterTurns"

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"
ROTATION_ROLES = ("single", "left", "right")
QUARTER_TURNS = (1, 2, 3)

# The rejection reasons are Captua's own list,
# app.schemas.record.PREDEFINED_REJECTION_REASONS, the nine of §8; a
# decision is checked with RecordRejectRequest, as POST /records/{id}/reject
# checks a reviewer's (app.core.fisqua_decisions).
