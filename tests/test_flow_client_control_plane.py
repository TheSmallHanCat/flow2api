import asyncio

from src.services.flow_client import FlowClient


def test_control_plane_timeout_is_capped():
    client = FlowClient(None)

    client.timeout = 120
    assert client._get_control_plane_timeout() == 10

    client.timeout = 8
    assert client._get_control_plane_timeout() == 8

    client.timeout = 3
    assert client._get_control_plane_timeout() == 5


def test_control_plane_calls_use_short_timeouts(monkeypatch):
    client = FlowClient(None)
    client.timeout = 120
    calls = []

    async def fake_make_request(**kwargs):
        calls.append({
            "url": kwargs["url"],
            "timeout": kwargs.get("timeout"),
        })
        url = kwargs["url"]
        if url.endswith("/auth/session"):
            return {"access_token": "at", "user": {"email": "tester@example.com"}}
        if url.endswith("/trpc/project.createProject"):
            return {"result": {"data": {"json": {"result": {"projectId": "project-123"}}}}}
        if url.endswith("/credits"):
            return {"credits": 1000, "userPaygateTier": "PAYGATE_TIER_ONE"}
        return {}

    monkeypatch.setattr(client, "_make_request", fake_make_request)

    async def run():
        await client.st_to_at("st")
        await client.create_project("st", "demo")
        await client.delete_project("st", "project-123")
        await client.get_credits("at")

    asyncio.run(run())

    assert [call["timeout"] for call in calls] == [10, 15, 10, 10]
