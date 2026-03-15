import asyncio

import pytest

from src.services import flow_client as flow_client_module
from src.services.flow_client import FlowClient


def test_create_project_retries_timeout_then_succeeds(monkeypatch):
    client = FlowClient(proxy_manager=None)
    attempts = []
    sleep_calls = []

    async def fake_make_request(**kwargs):
        attempts.append(kwargs["timeout"])
        if len(attempts) < 3:
            raise Exception("Flow API request failed: curl: (28) Connection timed out after 5013 milliseconds")
        return {
            "result": {
                "data": {
                    "json": {
                        "result": {
                            "projectId": "project-123",
                        }
                    }
                }
            }
        }

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(client, "_make_request", fake_make_request)
    monkeypatch.setattr(flow_client_module.asyncio, "sleep", fake_sleep)

    project_id = asyncio.run(client.create_project("st-token", "Retry Test"))

    assert project_id == "project-123"
    assert attempts == [15, 15, 15]
    assert sleep_calls == [1, 1]


def test_create_project_invalid_response_fails_fast(monkeypatch):
    client = FlowClient(proxy_manager=None)
    attempts = []

    async def fake_make_request(**kwargs):
        attempts.append(kwargs["timeout"])
        return {"result": {"data": {"json": {"result": {}}}}}

    monkeypatch.setattr(client, "_make_request", fake_make_request)

    with pytest.raises(Exception, match="missing projectId"):
        asyncio.run(client.create_project("st-token", "Invalid Response"))

    assert attempts == [15]
