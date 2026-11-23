"""CORS for the kiosk's first-run page: two routes, two origins, no credentials (NEH-336).

The kiosk opens the unit's pages at http://localhost, through nginx, where
every API request reaches the backend from the Docker bridge and is never
local (app.api.auth.request_is_local). The first-run page therefore asks
the backend at its loopback address, http://127.0.0.1:8000, directly: a
cross-origin request. This middleware answers exactly those requests:

* origins FIRST_RUN_ORIGINS (http://localhost, http://127.0.0.1), when the
  deployment's CORS_ORIGINS does not list them already;
* GET /auth/setup/status and POST /auth/register only, each for its own
  method, with the request headers FIRST_RUN_HEADERS;
* never with credentials: no Access-Control-Allow-Credentials, so a
  browser sends no cookie and the page reads no answer to a credentialed
  request. Neither route reads a session to create the first account.

Every other request, and these two from any other origin, goes to the
application's CORSMiddleware unchanged. A request this middleware answers
reaches the application without its Origin header, so CORSMiddleware adds
none of its headers (among them Access-Control-Allow-Credentials) to it.
It is added after CORSMiddleware in app.main, so it runs first.
"""

from __future__ import annotations

from typing import Iterable

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

FIRST_RUN_ORIGINS = ("http://localhost", "http://127.0.0.1")
FIRST_RUN_ROUTES = {"/auth/setup/status": "GET", "/auth/register": "POST"}
FIRST_RUN_HEADERS = ("content-type", "x-requested-with")
MAX_AGE = "600"


class FirstRunCORSMiddleware:
    def __init__(self, app: ASGIApp, configured_origins: Iterable[str]) -> None:
        self.app = app
        configured = set(configured_origins)
        self.origins = frozenset(o for o in FIRST_RUN_ORIGINS if o not in configured)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = FIRST_RUN_ROUTES.get(scope.get("path", ""))
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if method is None or origin not in self.origins:
            await self.app(scope, receive, send)
            return
        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            await self._preflight(method, origin, headers)(scope, receive, send)
            return

        scope = dict(scope)
        scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"origin"]

        async def send_with_origin(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                out["Access-Control-Allow-Origin"] = origin
                out.add_vary_header("Origin")
            await send(message)

        await self.app(scope, receive, send_with_origin)

    @staticmethod
    def _preflight(method: str, origin: str, headers: Headers) -> PlainTextResponse:
        requested = headers["access-control-request-method"].strip().upper()
        asked = {h.strip().lower() for h in headers.get("access-control-request-headers", "").split(",")
                 if h.strip()}
        if requested != method or not asked <= set(FIRST_RUN_HEADERS):
            return PlainTextResponse("Disallowed CORS request", status_code=400, headers={"Vary": "Origin"})
        return PlainTextResponse("OK", status_code=200, headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": method,
            "Access-Control-Allow-Headers": ", ".join(h.title() for h in FIRST_RUN_HEADERS),
            "Access-Control-Max-Age": MAX_AGE,
            "Vary": "Origin",
        })
