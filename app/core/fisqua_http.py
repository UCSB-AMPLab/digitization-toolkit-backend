"""The HTTPS transport to Fisqua (contract 1.2), and how each of its answers is read.

One httpx client, TLS verified, no redirects followed (the token would go
to whatever host a redirect named), explicit timeouts. The token goes in
the Authorization header only and is never part of a URL, an error text or
a result. The names come from app.core.fisqua_contract.

Answers to POST of a manifest:

    2xx with a volumeId          delivered (Fisqua's state kept)
    401, 403                     auth_error: the unit stops sending
    422                          refused, with Fisqua's problems
    409 with rule id-reused      refused
    413                          refused
    429, 5xx                     error, retried; Retry-After is a floor
    anything else                error, retried
    timeout, connection error    error, retried

Answers to GET of the status: 2xx with the volumes, 401/403 auth_error, the
rest error.
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Optional

import httpx

from app.core import fisqua_contract as fc
from app.core.fisqua_outbox import TransportResult

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


def _rules(body: Any) -> set:
    rules = set()
    if isinstance(body, dict):
        if isinstance(body.get(fc.F_RULE), str):
            rules.add(body[fc.F_RULE])
        for problem in body.get(fc.F_PROBLEMS) or []:
            if isinstance(problem, dict) and isinstance(problem.get(fc.F_RULE), str):
                rules.add(problem[fc.F_RULE])
    return rules


def _problems(body: Any) -> Optional[list]:
    if isinstance(body, dict) and isinstance(body.get(fc.F_PROBLEMS), list):
        return body[fc.F_PROBLEMS]
    return None


def _auth(code: int, body: Any) -> TransportResult:
    return TransportResult(outcome="auth_error", response=body, status_code=code,
                           error=f"Fisqua refused this unit's token (HTTP {code})")


def _transient(response: httpx.Response, body: Any) -> TransportResult:
    return TransportResult(outcome="error", response=body, status_code=response.status_code,
                           error=f"Fisqua answered HTTP {response.status_code}",
                           retry_after=retry_after_seconds(response.headers.get(fc.RETRY_AFTER_HEADER)))


def read_send_answer(response: httpx.Response) -> TransportResult:
    code = response.status_code
    body = _json(response)
    if 200 <= code < 300:
        volume_id = body.get(fc.F_VOLUME_ID) if isinstance(body, dict) else None
        if volume_id in (None, ""):
            return TransportResult(outcome="error", response=body, status_code=code,
                                   error=f"Fisqua answered HTTP {code} without a {fc.F_VOLUME_ID}")
        state = body.get(fc.F_STATE)
        return TransportResult(outcome="delivered", response=body, status_code=code,
                               external_id=str(volume_id),
                               status=state if isinstance(state, str) else None)
    if code in (401, 403):
        return _auth(code, body)
    if code == 422:
        return TransportResult(outcome="refused", response=body, status_code=code, problems=_problems(body))
    if code == 409 and fc.RULE_ID_REUSED in _rules(body):
        return TransportResult(outcome="refused", response=body, status_code=code,
                               problems=_problems(body) or [{fc.F_RULE: fc.RULE_ID_REUSED,
                                                             "message": "Fisqua has this manifest id already."}])
    if code == 413:
        return TransportResult(outcome="refused", response=body, status_code=code,
                               problems=_problems(body) or [{fc.F_RULE: fc.RULE_TOO_LARGE,
                                                             "message": "Fisqua refused the manifest as too large."}])
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
    if code in (401, 403):
        return _auth(code, body)
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
        return read_send_answer(response)

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

    def close(self) -> None:
        self._client.close()
