from fastapi import FastAPI
from fastapi.responses import Response, StreamingResponse
from starlette.testclient import TestClient

from app.gateway.trace_middleware import TraceMiddleware
from deerflow.trace_context import TRACE_ID_HEADER, get_current_trace_id


def _make_app(*, enabled: bool) -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceMiddleware, enabled_getter=lambda: enabled)

    @app.get("/plain")
    async def plain() -> dict[str, str | None]:
        return {"trace_id": get_current_trace_id()}

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def body():
            yield f"trace={get_current_trace_id()}".encode()

        return StreamingResponse(body(), media_type="text/plain")

    @app.get("/pre-set")
    async def pre_set() -> Response:
        return Response("ok", headers={TRACE_ID_HEADER: "downstream"})

    return app


def test_trace_header_absent_when_disabled() -> None:
    client = TestClient(_make_app(enabled=False))

    response = client.get("/plain")

    assert TRACE_ID_HEADER not in response.headers
    assert response.json() == {"trace_id": None}


def test_trace_header_inherits_inbound_value_and_binds_context() -> None:
    client = TestClient(_make_app(enabled=True))

    response = client.get("/plain", headers={TRACE_ID_HEADER: "trace-from-upstream"})

    assert response.headers[TRACE_ID_HEADER] == "trace-from-upstream"
    assert response.json() == {"trace_id": "trace-from-upstream"}


def test_trace_header_generated_when_missing() -> None:
    client = TestClient(_make_app(enabled=True))

    response = client.get("/plain")

    trace_id = response.headers[TRACE_ID_HEADER]
    assert trace_id
    assert response.json() == {"trace_id": trace_id}


def test_trace_header_added_to_streaming_response_without_consuming_body() -> None:
    client = TestClient(_make_app(enabled=True))

    response = client.get("/stream", headers={TRACE_ID_HEADER: "stream-trace"})

    assert response.headers[TRACE_ID_HEADER] == "stream-trace"
    assert response.text == "trace=stream-trace"


def test_trace_header_overwrites_duplicate_downstream_value() -> None:
    client = TestClient(_make_app(enabled=True))

    response = client.get("/pre-set", headers={TRACE_ID_HEADER: "canonical-trace"})

    assert response.headers[TRACE_ID_HEADER] == "canonical-trace"
    assert response.headers.get_list(TRACE_ID_HEADER) == ["canonical-trace"]
