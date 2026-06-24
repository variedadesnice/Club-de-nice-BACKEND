"""
Two patches that eliminate a recurring Windows-only failure mode in outbound Supabase
calls: WinError 10035 (WSAEWOULDBLOCK), Winsock's documented signal that a non-blocking
socket just wasn't ready yet — not a real failure (see Microsoft/Winsock docs). It shows
up here because supabase-py's postgrest/storage/auth sub-clients each build their own
internal httpx.Client and reuse pooled keep-alive connections across the many worker
threads FastAPI spawns for concurrent sync requests; reusing a pooled connection from a
different thread is exactly the scenario that triggers this race on Windows. None of
those sub-clients expose a hook to change this, so both patches apply at the one shared
choke point all of them go through: httpx.Client itself.

1. install_no_keepalive_pooling(): every new httpx.Client defaults to
   max_keepalive_connections=0, so each request opens a fresh connection instead of
   reusing one from a shared pool. This removes the root cause instead of papering over
   it — connections are never reused across threads, so the race can't happen.

2. install_retry_wrapper(): defense in depth for the (now rare) remaining transient
   errors — e.g. a connection attempt that needed a second try. Connection-phase errors
   (ConnectError/ConnectTimeout) are safe to retry for any HTTP method, since httpx
   guarantees the request was never sent. Errors that can happen after the request was
   sent (ReadError, RemoteProtocolError, ...) are only retried for GET/HEAD/OPTIONS, to
   avoid silently double-applying a write that may have already reached the server.
"""
import logging
import time

import httpx

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 0.2
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_NO_KEEPALIVE_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=0)

_original_send = httpx.Client.send
_original_init = httpx.Client.__init__


def _resilient_send(self: httpx.Client, request: httpx.Request, **kwargs):
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return _original_send(self, request, **kwargs)
        except httpx.TransportError as exc:
            is_connect_phase = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
            can_retry = is_connect_phase or request.method in _SAFE_METHODS
            if attempt == _MAX_ATTEMPTS or not can_retry:
                raise
            logger.warning(
                "[http_resilience] transient %s on %s %s - retry %s/%s",
                type(exc).__name__, request.method, request.url, attempt, _MAX_ATTEMPTS,
            )
            time.sleep(_BACKOFF_SECONDS * attempt)


def _no_keepalive_init(self: httpx.Client, *args, **kwargs):
    # Only applies when the caller didn't explicitly pass limits/transport of its own.
    kwargs.setdefault("limits", _NO_KEEPALIVE_LIMITS)
    _original_init(self, *args, **kwargs)


def install() -> None:
    """Patches httpx.Client process-wide. Idempotent — safe to call more than once."""
    if httpx.Client.send is not _resilient_send:
        httpx.Client.send = _resilient_send
        logger.info("[http_resilience] installed retry wrapper around httpx.Client.send")
    if httpx.Client.__init__ is not _no_keepalive_init:
        httpx.Client.__init__ = _no_keepalive_init
        logger.info("[http_resilience] disabled keep-alive connection reuse for new httpx.Client instances")
