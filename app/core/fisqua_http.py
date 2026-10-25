"""The HTTPS transport to Fisqua (contract 1.2), and how each of its answers is read.

One httpx client, TLS verified, no redirects followed (the token would go
to whatever host a redirect named), explicit timeouts. The token goes in
the Authorization header only and is never part of a URL, an error text or
a result. The names come from app.core.fisqua_contract.

Answers to POST of a manifest (the codes are in fisqua_contract):

    2xx with a volumeId          delivered (Fisqua's state kept)
    401, 403, 404, any 3xx       config_error: the unit stops sending and polling
    400, 413, 422                refused, final, with Fisqua's problems
                                 (400, 413 without: a local bad-request,
                                 payload-too-large problem)
    409 with problems            refused, final, with Fisqua's problems
    409 without problems         error, retried
    429, 5xx                     error, retried; Retry-After is a floor
    anything else                error, retried
    timeout, connection error    error, retried

Answers to GET of the status: 2xx with the volumes, the same configuration
errors, the rest error. Answers to GET of a volume's QC decisions (§9.5):
2xx with an object, 404 with rule no-decisions as nothing to apply yet
(read again at the next poll), the same configuration errors otherwise,
the rest error.
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Optional

import logging

import httpx

from app.core import fisqua_contract as fc
from app.core.fisqua_outbox import TransportResult

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10.0


def retry_after_seconds(value: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    """A Retry-After header as seconds from now: delta-seconds or an HTTP date. None if absent or unreadable."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    try:
        return max(0.0, float(int(value)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (when - now).total_seconds())


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _problems(body: Any) -> Optional[list]:
    """The body's `problems` array when it is a non-empty list, else None."""
    if isinstance(body, dict) and isinstance(body.get(fc.F_PROBLEMS), list) and body[fc.F_PROBLEMS]:
        return body[fc.F_PROBLEMS]
    return None


def _config_error(code: int, body: Any) -> Optional[TransportResult]:
    """A configuration error for 401, 403, 404 and every 3xx; None for any other code."""
    if code in fc.CONFIG_ERROR:
        reason = fc.CONFIG_ERROR_REASONS[code]
    elif 300 <= code < 400:
        reason = fc.REDIRECT_REASON
    else:
        return None
    text = {"token": "Fisqua refused this unit's token",
            "not_found": "Fisqua has no such endpoint for this unit",
            fc.REDIRECT_REASON: "Fisqua answered with a redirect, which is not followed"}[reason]
    return TransportResult(outcome="config_error", response=body, status_code=code,
                           reason=reason, error=f"{text} (HTTP {code})")


def _transient(response: httpx.Response, body: Any) -> TransportResult:
    return TransportResult(outcome="error", response=body, status_code=response.status_code,
                           error=f"Fisqua answered HTTP {response.status_code}",
                           retry_after=retry_after_seconds(response.headers.get(fc.RETRY_AFTER_HEADER)))


def read_send_answer(response: httpx.Response, sent_id: Optional[str] = None) -> TransportResult:
    """The answer to a POST of the manifest whose id is `sent_id`.

    A 2xx must name that manifest: one naming another id, or none, is
    retried (and logged), since the unit cannot tell what Fisqua accepted.
    """
    code = response.status_code
    body = _json(response)
    if 200 <= code < 300:
        volume_id = body.get(fc.F_VOLUME_ID) if isinstance(body, dict) else None
        if volume_id in (None, ""):
            return TransportResult(outcome="error", response=body, status_code=code,
                                   error=f"Fisqua answered HTTP {code} without a {fc.F_VOLUME_ID}")
        answered = body.get(fc.F_MANIFEST_ID)
        if sent_id is not None and answered != sent_id:
            # Not the value Fisqua gave: it is unredacted here.
            logger.warning("Fisqua answered HTTP %s naming another manifest id, or none, to the POST "
                           "of manifest %s; retried", code, sent_id)
            return TransportResult(outcome="error", response=body, status_code=code,
                                   error=(f"Fisqua answered HTTP {code} with {fc.F_MANIFEST_ID} {answered!r}, "
                                          f"not the {sent_id} sent"))
        state = body.get(fc.F_STATE)
        return TransportResult(outcome="delivered", response=body, status_code=code,
                               external_id=str(volume_id),
                               status=state if isinstance(state, str) else None)
    stop = _config_error(code, body)
    if stop is not None:
        return stop
    if code == 413:
        return TransportResult(outcome="refused", response=body, status_code=code,
                               problems=_problems(body) or [{fc.F_RULE: fc.RULE_TOO_LARGE,
                                                             "message": "Fisqua refused the manifest as too large."}])
    if code == 400:
        return TransportResult(outcome="refused", response=body, status_code=code,
                               problems=_problems(body) or [{fc.F_RULE: fc.RULE_BAD_REQUEST,
                                                             "message": "Fisqua refused the manifest as malformed."}])
    if code in fc.REFUSED_ALWAYS:
        return TransportResult(outcome="refused", response=body, status_code=code, problems=_problems(body))
    if code in fc.REFUSED_WITH_PROBLEMS and _problems(body) is not None:
        return TransportResult(outcome="refused", response=body, status_code=code, problems=_problems(body))
    return _transient(response, body)


def read_status_answer(response: httpx.Response) -> TransportResult:
    code = response.status_code
    body = _json(response)
    if 200 <= code < 300:
        volumes = body.get(fc.F_VOLUMES) if isinstance(body, dict) else body
        if not isinstance(volumes, list):
            return TransportResult(outcome="error", response=body, status_code=code,
                                   error=f"Fisqua answered HTTP {code} without a list of volumes")
        return TransportResult(outcome="status", response=body, status_code=code,
                               volumes=[v for v in volumes if isinstance(v, dict)])
    stop = _config_error(code, body)
    if stop is not None:
        return stop
    return _transient(response, body)


def _has_rule(body: Any, rule: str) -> bool:
    """Whether the body names `rule`, at its top level or in its problems."""
    if not isinstance(body, dict):
        return False
    if body.get(fc.F_RULE) == rule:
        return True
    return any(isinstance(p, dict) and p.get(fc.F_RULE) == rule for p in body.get(fc.F_PROBLEMS) or [])


def read_decisions_answer(response: httpx.Response) -> TransportResult:
    code = response.status_code
    body = _json(response)
    if 200 <= code < 300:
        if not isinstance(body, dict):
            return TransportResult(outcome="error", response=body, status_code=code,
                                   error=f"Fisqua answered HTTP {code} without a decisions object")
        return TransportResult(outcome="decisions", response=body, status_code=code)
    if code == 404 and _has_rule(body, fc.RULE_NO_DECISIONS):
        # Nothing decided yet, or not yet visible: not a configuration error.
        return TransportResult(outcome="no_decisions", response=body, status_code=code)
    stop = _config_error(code, body)
    if stop is not None:
        return stop
    return _transient(response, body)


def _failed(exc: Exception) -> TransportResult:
    kind = "timeout" if isinstance(exc, httpx.TimeoutException) else type(exc).__name__
    return TransportResult(outcome="error", error=f"Fisqua could not be reached: {kind}: {exc}")


class HttpFisquaTransport:
    def __init__(self, base_url: str, token: str, *, timeout: float = 60.0,
                 client: Optional[httpx.Client] = None):
        self._base = base_url.rstrip("/")
        self._token = token
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_SECONDS),
            verify=True, follow_redirects=False)

    def __repr__(self) -> str:
        return f"HttpFisquaTransport({self._base!r})"

    def _headers(self) -> dict:
        return {fc.AUTH_HEADER: f"{fc.AUTH_SCHEME} {self._token}", "Accept": "application/json"}

    def send_manifest(self, manifest: dict) -> TransportResult:
        try:
            response = self._client.post(self._base + fc.MANIFESTS_PATH, json=manifest, headers=self._headers())
        except httpx.HTTPError as exc:
            return _failed(exc)
        return read_send_answer(response, sent_id=manifest.get("id") if isinstance(manifest, dict) else None)

    def get_status(self, refs: Iterable[str]) -> TransportResult:
        refs = list(refs)
        if len(refs) > fc.MAX_REFS_PER_CALL:
            raise ValueError(f"at most {fc.MAX_REFS_PER_CALL} refs per call, not {len(refs)}")
        try:
            response = self._client.get(self._base + fc.STATUS_PATH, headers=self._headers(),
                                        params={fc.STATUS_REFS_PARAM: fc.STATUS_REFS_SEPARATOR.join(refs)})
        except httpx.HTTPError as exc:
            return _failed(exc)
        return read_status_answer(response)

    def get_decisions(self, ref: str) -> TransportResult:
        from urllib.parse import quote

        path = fc.DECISIONS_PATH.format(ref=quote(ref, safe=""))
        try:
            response = self._client.get(self._base + path, headers=self._headers())
        except httpx.HTTPError as exc:
            return _failed(exc)
        return read_decisions_answer(response)

    def close(self) -> None:
        self._client.close()
