"""Regression tests for provisioner request-path K8s IO threading."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from blockbuster import BlockBuster


class _RecordingCoreV1:
    def __init__(self, *, event_loop_thread_id: int) -> None:
        self.event_loop_thread_id = event_loop_thread_id
        self.thread_ids: list[int] = []

    def _record_k8s_call(self) -> None:
        thread_id = threading.get_ident()
        self.thread_ids.append(thread_id)
        time.sleep(0)
        if thread_id == self.event_loop_thread_id:
            raise AssertionError("Kubernetes client call ran on the ASGI event-loop thread")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise AssertionError("Kubernetes client call ran inside an asyncio event loop")

    def read_namespaced_service(self, _name: str, _namespace: str):
        self._record_k8s_call()
        return _service("sandbox-existing")

    def read_namespaced_pod(self, _name: str, _namespace: str):
        self._record_k8s_call()
        return SimpleNamespace(status=SimpleNamespace(phase="Running"))

    def delete_namespaced_service(self, _name: str, _namespace: str) -> None:
        self._record_k8s_call()

    def delete_namespaced_pod(self, _name: str, _namespace: str) -> None:
        self._record_k8s_call()

    def list_namespaced_service(self, _namespace: str, *, label_selector: str):
        self._record_k8s_call()
        assert label_selector == "app=deer-flow-sandbox"
        return SimpleNamespace(items=[_service("sandbox-listed")])


def _service(sandbox_id: str):
    return SimpleNamespace(
        metadata=SimpleNamespace(labels={"sandbox-id": sandbox_id}),
        spec=SimpleNamespace(ports=[SimpleNamespace(name="http", node_port=32123)]),
    )


@contextmanager
def _detect_provisioner_blocking_io(provisioner_module):
    detector = BlockBuster(scanned_modules=[provisioner_module])
    detector.activate()
    try:
        yield
    finally:
        detector.deactivate()


def test_sandbox_business_route_handlers_are_sync(provisioner_module) -> None:
    """FastAPI runs sync handlers in its worker pool, away from the event loop."""
    for handler in (
        provisioner_module.create_sandbox,
        provisioner_module.destroy_sandbox,
        provisioner_module.get_sandbox,
        provisioner_module.list_sandboxes,
    ):
        assert not inspect.iscoroutinefunction(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("POST", "/api/sandboxes", {"sandbox_id": "sandbox-existing", "thread_id": "thread-1", "user_id": "user-1"}),
        ("DELETE", "/api/sandboxes/sandbox-existing", None),
        ("GET", "/api/sandboxes/sandbox-existing", None),
        ("GET", "/api/sandboxes", None),
    ],
    ids=["create-existing", "destroy", "get", "list"],
)
async def test_sandbox_business_routes_run_k8s_client_off_event_loop_thread(
    method: str,
    path: str,
    json_body: dict[str, str] | None,
    monkeypatch: pytest.MonkeyPatch,
    provisioner_module,
) -> None:
    fake_core_v1 = _RecordingCoreV1(event_loop_thread_id=threading.get_ident())
    monkeypatch.setattr(provisioner_module, "core_v1", fake_core_v1)

    with _detect_provisioner_blocking_io(provisioner_module):
        transport = httpx.ASGITransport(app=provisioner_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            if json_body is None:
                response = await client.request(method, path)
            else:
                response = await client.request(method, path, json=json_body)

    assert response.status_code == 200
    assert fake_core_v1.thread_ids
