"""Traccar client provisioning, in particular device grouping.

Traccar scopes the device list per user through ``tc_user_device``, and being an
administrator does not bypass it. Putting every auto-created device in one group
is what makes admin visibility a single share per user rather than one per
device, so these tests pin the group down to the request level.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from gtfs_zone_rt_api.traccar import TraccarClient

Handler = Callable[[httpx.Request], httpx.Response]
Call = tuple[str, str, dict | None]


def _client(handler: Handler, **kwargs: object) -> TraccarClient:
    return TraccarClient(
        "http://traccar:8082",
        email="a@b.c",
        password="pw",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _recorder(*, groups: list[dict], device_id: int = 99) -> tuple[Handler, list[Call]]:
    """Handler that fakes /api/groups and /api/devices, recording every call."""
    calls: list[Call] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/api/groups":
            if request.method == "GET":
                return httpx.Response(200, json=groups)
            created = {"id": len(groups) + 1, "name": body["name"]}
            groups.append(created)
            return httpx.Response(200, json=created)
        if request.url.path == "/api/devices":
            if request.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"id": device_id, **body})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    return handler, calls


async def test_ensure_device_creates_and_joins_the_group() -> None:
    handler, calls = _recorder(groups=[])
    client = _client(handler, device_group="All Vehicles")

    device = await client.ensure_device("west", "safely-vast-marten")

    assert device["groupId"] == 1
    created_group = [c for c in calls if c[:2] == ("POST", "/api/groups")]
    assert created_group == [("POST", "/api/groups", {"name": "All Vehicles"})]
    assert (
        "POST",
        "/api/devices",
        {
            "name": "west",
            "uniqueId": "safely-vast-marten",
            "groupId": 1,
        },
    ) in calls


async def test_existing_group_is_reused_not_recreated() -> None:
    handler, calls = _recorder(groups=[{"id": 7, "name": "All Vehicles"}])
    client = _client(handler, device_group="All Vehicles")

    device = await client.ensure_device("west", "safely-vast-marten")

    assert device["groupId"] == 7
    assert not [c for c in calls if c[:2] == ("POST", "/api/groups")]


async def test_group_is_resolved_once_across_devices() -> None:
    handler, calls = _recorder(groups=[{"id": 7, "name": "All Vehicles"}])
    client = _client(handler, device_group="All Vehicles")

    await client.ensure_device("a", "one")
    await client.ensure_device("b", "two")

    # Memoised: the second provision must not re-list groups.
    assert len([c for c in calls if c[:2] == ("GET", "/api/groups")]) == 1


async def test_no_group_configured_creates_ungrouped_device() -> None:
    handler, calls = _recorder(groups=[])
    client = _client(handler, device_group="")

    device = await client.ensure_device("west", "safely-vast-marten")

    assert "groupId" not in device
    assert not [c for c in calls if c[1] == "/api/groups"]


async def test_group_failure_still_provisions_the_device() -> None:
    """Grouping is a visibility convenience, never a provisioning blocker."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/groups":
            return httpx.Response(500, text="boom")
        if request.method == "GET":
            return httpx.Response(200, json=[])
        body = json.loads(request.content)
        return httpx.Response(200, json={"id": 99, **body})

    client = _client(handler, device_group="All Vehicles")

    device = await client.ensure_device("west", "safely-vast-marten")

    assert device["id"] == 99
    assert "groupId" not in device


async def test_existing_device_is_not_regrouped() -> None:
    """An already-provisioned device is returned untouched, group or no group."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/devices" and request.method == "GET":
            return httpx.Response(
                200, json=[{"id": 5, "uniqueId": "safely-vast-marten"}]
            )
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    client = _client(handler, device_group="All Vehicles")

    device = await client.ensure_device("west", "safely-vast-marten")

    assert device == {"id": 5, "uniqueId": "safely-vast-marten"}
