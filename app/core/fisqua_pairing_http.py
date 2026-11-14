"""The HTTPS client for pairing a unit with its workspace by a code (contract 1.11, §12), and how each answer is read.

The two routes are called on the environment's main address
(fisqua_contract.PAIRING_HOSTS), with no token: they are the exception to
"a unit with no token never calls Fisqua". This is therefore not
fisqua_http.HttpFisquaTransport, whose every call needs the unit's token
and fisqua mode; whether a unit may pair at all is the caller's gate
(app.core.fisqua_pairing). The rules are the same: TLS verified, no
redirect followed, explicit timeouts. The collect secret, the code and the
payload are never logged and never part of an error text or a repr; only
status codes and outcome names are.

Answers to POST of the codes route (PairingAnswer.outcome):

    201 with code, collectSecret, pollSeconds, expiresInSeconds   code
    201 missing any of them, or of the wrong shape               network
    400 with a rule                                              refused (rule kept)
    429, or 503 with a JSON object                               busy (Retry-After kept)
    405                                                          method
    404 whose body is not a JSON object                          cannot_pair (§12.3)

Answers to POST of the collect route:

    202                                                          waiting
    200 with a JSON object                                       payload (shape not checked here)
    410 expired / collected                                      expired / collected
    404 unknown                                                  unknown
    429, or 503 with a JSON object                               busy
    405                                                          method

On both routes: any other answer whose body is not a JSON object (a
captive portal, a proxy, a redirect, a 404 on the collect route), any
JSON answer of 5xx, and any connection, TLS or timeout error is `network`,
which the session retries. Any other JSON answer is `unexpected`, final.
TLS is verified by httpx, so a certificate that does not verify is a
connection error, never `cannot_pair`: the contract reads a non-JSON 404
as an older Fisqua only over valid TLS.

validate_payload checks the shape of a 200's payload before anything is
done with it.

Which host (pairing_target): a linked unit (Fisqua configured,
fisqua_outbox.fisqua_configured) asks the main address of its own
environment and reports its tenant as currentTenant; a never-linked unit
asks production unless CAPTUA_FISQUA_ENV says staging. The environment of
the target is the one a payload must carry. CAPTUA_FISQUA_PAIRING_BASE_URL
replaces the address, for tests and development, under the rule of
fisqua_outbox._usable_override; it does not change the environment.

Version: 0.3.3
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

# As a module: tests rebuild the Settings object.
from app.core import config
from app.core import fisqua_contract as fc
from app.core.fisqua_http import CONNECT_TIMEOUT_SECONDS, retry_after_seconds

logger = logging.getLogger(__name__)

# Each call's read and write timeout. Short: an admin is waiting at the
# card, and a call in flight holds the session's thread.
TIMEOUT_SECONDS = 15.0
DEFAULT_ENVIRONMENT = "production"

# Every value of a payload is within this class: what provisioning accepts
# in offsite.env and the key file, with no space, quote or newline. Read
# with fullmatch, since `$` would let a trailing newline through.
PAYLOAD_VALUE = re.compile(r"^[A-Za-z0-9_+/=.:-]+$")


@dataclass(repr=False)
class PairingAnswer:
    """One answer of the codes or collect route. Its repr and str name only the outcome, the status and the rule."""

    outcome: str
    status_code: Optional[int] = None
    rule: Optional[str] = None
    retry_after: Optional[float] = None
    code: Optional[str] = None
    collect_secret: Optional[str] = None
    expires_in_seconds: Optional[int] = None
    poll_seconds: Optional[int] = None
    payload: Optional[dict] = None
    error: Optional[str] = None

    def __repr__(self) -> str:
        return f"PairingAnswer(outcome={self.outcome!r}, status_code={self.status_code!r}, rule={self.rule!r})"

    __str__ = __repr__


@dataclass(frozen=True)
class PairingTarget:
    base_url: str
    environment: str
    current_tenant: Optional[str]


def pairing_target() -> PairingTarget:
    """Where this unit asks for a code, the environment a payload must name, and the tenant it reports."""
    from app.core import fisqua_outbox

    linked = fisqua_outbox.fisqua_configured()
    environment = fisqua_outbox.fisqua_environment() or DEFAULT_ENVIRONMENT
    base = fc.PAIRING_HOSTS[environment]
    override = config.settings.FISQUA_PAIRING_BASE_URL.strip()
    if override:
        usable = fisqua_outbox._usable_override(override, "CAPTUA_FISQUA_PAIRING_BASE_URL")
        if usable is not None:
            base = usable
    return PairingTarget(base_url=base, environment=environment,
                         current_tenant=fisqua_outbox._tenant() if linked else None)


def _object(response: httpx.Response) -> Optional[dict]:
    """The body when it is a JSON object, else None."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _common(code: int, body: Optional[dict], response: httpx.Response) -> Optional[PairingAnswer]:
    """The answers both routes read alike; None for the route's own."""
    if body is None:
        return PairingAnswer(outcome="network", status_code=code,
                             error=f"Fisqua's address answered HTTP {code} with a body that is not JSON")
    if code == 429 or code == 503:
        return PairingAnswer(outcome="busy", status_code=code, rule=_text(body.get(fc.F_RULE)),
                             retry_after=retry_after_seconds(response.headers.get(fc.RETRY_AFTER_HEADER)),
                             error=f"Fisqua is busy (HTTP {code})")
    if code == 405:
        return PairingAnswer(outcome="method", status_code=code, rule=_text(body.get(fc.F_RULE)),
                             error="Fisqua refused the request's method (HTTP 405)")
    return None


def _unexpected(code: int, body: dict) -> PairingAnswer:
    if code >= 500:
        return PairingAnswer(outcome="network", status_code=code, error=f"Fisqua answered HTTP {code}")
    return PairingAnswer(outcome="unexpected", status_code=code, rule=_text(body.get(fc.F_RULE)),
                         error=f"Fisqua answered HTTP {code}, which pairing does not expect")


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def read_codes_answer(response: httpx.Response) -> PairingAnswer:
    code = response.status_code
    body = _object(response)
    if code == 404 and body is None:
        return PairingAnswer(outcome="cannot_pair", status_code=code,
                             error="This Fisqua cannot pair units by a code (HTTP 404, not JSON)")
    common = _common(code, body, response)
    if common is not None:
        return common
    if code == 201:
        value = _text(body.get(fc.F_CODE))
        secret = _text(body.get(fc.F_COLLECT_SECRET))
        expires = _positive_int(body.get(fc.F_EXPIRES_IN_SECONDS))
        poll = _positive_int(body.get(fc.F_POLL_SECONDS))
        if not value or secret is None or fc.COLLECT_SECRET_PATTERN.fullmatch(secret) is None \
                or expires is None or poll is None:
            return PairingAnswer(outcome="network", status_code=code,
                                 error="Fisqua answered HTTP 201 without a well-formed code")
        return PairingAnswer(outcome="code", status_code=code, code=value, collect_secret=secret,
                             expires_in_seconds=expires, poll_seconds=poll)
    if code == 400:
        return PairingAnswer(outcome="refused", status_code=code, rule=_text(body.get(fc.F_RULE)),
                             error="Fisqua refused the request for a code (HTTP 400)")
    return _unexpected(code, body)


def read_collect_answer(response: httpx.Response) -> PairingAnswer:
    code = response.status_code
    body = _object(response)
    common = _common(code, body, response)
    if common is not None:
        return common
    state = body.get(fc.F_STATE)
    if code == 202:
        return PairingAnswer(outcome="waiting", status_code=code)
    if code == 200:
        return PairingAnswer(outcome="payload", status_code=code, payload=body)
    if code == 410 and state in (fc.PAIRING_EXPIRED, fc.PAIRING_COLLECTED):
        return PairingAnswer(outcome=state, status_code=code)
    if code == 404 and state == fc.PAIRING_UNKNOWN:
        return PairingAnswer(outcome="unknown", status_code=code)
    return _unexpected(code, body)


def _failed(exc: Exception) -> PairingAnswer:
    # The exception's text is not kept: it is not ours to vouch for.
    kind = "timeout" if isinstance(exc, httpx.TimeoutException) else type(exc).__name__
    return PairingAnswer(outcome="network", error=f"Fisqua could not be reached: {kind}")


class FisquaPairingClient:
    """The two pairing calls to one main address. Nothing else is sent; no token is held."""

    def __init__(self, base_url: str, *, timeout: float = TIMEOUT_SECONDS, client: Optional[httpx.Client] = None):
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_SECONDS),
            verify=True, follow_redirects=False)

    def __repr__(self) -> str:
        return f"FisquaPairingClient({self._base!r})"

    def _post(self, path: str, body: dict, read) -> PairingAnswer:
        try:
            response = self._client.post(self._base + path, json=body, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            answer = _failed(exc)
        else:
            answer = read(response)
        logger.debug("Pairing call %s answered %s (HTTP %s)", path, answer.outcome, answer.status_code)
        return answer

    def request_code(self, unit_name: str, device_id: str, current_tenant: Optional[str]) -> PairingAnswer:
        body = {fc.F_UNIT_NAME: unit_name, fc.F_DEVICE_ID: device_id}
        if current_tenant:
            body[fc.F_CURRENT_TENANT] = current_tenant
        return self._post(fc.PAIRING_CODES_PATH, body, read_codes_answer)

    def collect(self, code: str, collect_secret: str) -> PairingAnswer:
        return self._post(fc.PAIRING_COLLECT_PATH, {fc.F_CODE: code, fc.F_COLLECT_SECRET: collect_secret},
                          read_collect_answer)

    def close(self) -> None:
        self._client.close()


def _bad_value(value: Any) -> bool:
    return not isinstance(value, str) or PAYLOAD_VALUE.fullmatch(value) is None


def validate_payload(payload: Any) -> Optional[str]:
    """None when the payload has every field, each a string within PAYLOAD_VALUE and the token 64 lowercase hex.

    Otherwise the name of the first field that fails (`writeKey.keyId` for
    a nested one, `payload` when it is not an object), never its value.
    Fields the contract does not name are ignored.
    """
    if not isinstance(payload, dict):
        return "payload"
    for name in fc.PAYLOAD_STRINGS:
        if _bad_value(payload.get(name)):
            return name
    if fc.TOKEN_PATTERN.fullmatch(payload[fc.F_TOKEN]) is None:
        return fc.F_TOKEN
    for name in fc.PAYLOAD_KEYS:
        key = payload.get(name)
        if not isinstance(key, dict):
            return name
        for field in fc.PAYLOAD_KEY_FIELDS:
            if _bad_value(key.get(field)):
                return f"{name}.{field}"
    return None
