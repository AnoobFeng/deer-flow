"""Gateway request trace middleware."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from deerflow.trace_context import TRACE_ID_HEADER, request_trace_context

logger = logging.getLogger(__name__)

TraceEnabledGetter = Callable[[], bool]


class TraceMiddleware:
    """Bind a request-level trace id and write it to HTTP response headers."""

    def __init__(self, app: ASGIApp, *, enabled_getter: TraceEnabledGetter):
        self.app = app
        self.enabled_getter = enabled_getter

    def _enabled(self) -> bool:
        try:
            return bool(self.enabled_getter())
        except Exception:
            logger.debug("Trace middleware disabled because config lookup failed", exc_info=True)
            return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._enabled():
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        incoming_trace_id = headers.get(TRACE_ID_HEADER)

        with request_trace_context(incoming_trace_id) as trace_id:

            async def send_with_trace(message: Message) -> None:
                if message["type"] == "http.response.start":
                    response_headers = MutableHeaders(scope=message)
                    response_headers[TRACE_ID_HEADER] = trace_id
                await send(message)

            await self.app(scope, receive, send_with_trace)


def make_trace_enabled_getter(config_getter: Callable[[], Any]) -> TraceEnabledGetter:
    """Return a lazy getter for ``logging.enhance.enabled``."""

    def _enabled() -> bool:
        config = config_getter()
        logging_config = getattr(config, "logging", None)
        enhance = getattr(logging_config, "enhance", None)
        return bool(getattr(enhance, "enabled", False))

    return _enabled
