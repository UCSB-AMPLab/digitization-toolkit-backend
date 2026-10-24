"""Every name the unit uses on Fisqua's side of the exchange (contract 1.2), in one place.

Hosts, paths, query parameters, answer fields, rule ids and status names.
Fisqua's proposal for 1.2 is firm but its final text is still to come, so a
rename there should be an edit here and nowhere else. Juan ruled the token,
the supersede rule, the states and the polling cadence; the rest is
Fisqua's proposal.
"""

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
# The status answer is a list of volumes, or an object holding it under this key.
F_VOLUMES = "volumes"

RULE_ID_REUSED = "id-reused"
# Not Fisqua's: the problem the unit records for a 413 with no problems in it.
RULE_TOO_LARGE = "payload-too-large"

STATES = ("received", "registered", "queued", "running", "review_pending",
          "published", "refused", "superseded", "failed")
# A volume in one of these is not polled again for the same manifest.
FINAL_STATES = ("published", "refused", "superseded")
