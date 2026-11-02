"""HTTP-level tests: status codes, async processing, queue ordering, rollback visibility."""
from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.models import AuditLog, FlightStatus, MealInventory
from app.services import background_tasks, catering_engine

DELAY = {"flight_id": "QR1001", "event_type": "FLIGHT_DELAY", "delay_minutes": 90}


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


# ------------------------------- flights ----------------------------------- #
async def test_list_flights_pagination_and_filter(client, make_flight):
    for i in range(5):
        await make_flight(f"QR100{i}", minutes_ahead=60 + i)
    await make_flight("QR2000", status=FlightStatus.DELAYED, minutes_ahead=500)

    page = (await client.get("/api/v1/flights/", params={"limit": 2, "offset": 1})).json()
    assert page["total"] == 6 and len(page["items"]) == 2
    assert [f["flight_id"] for f in page["items"]] == ["QR1001", "QR1002"]  # departure order

    delayed = (await client.get("/api/v1/flights/", params={"status": "Delayed"})).json()
    assert delayed["total"] == 1 and delayed["items"][0]["flight_id"] == "QR2000"


async def test_list_flights_validates_query_params(client):
    assert (await client.get("/api/v1/flights/", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/v1/flights/", params={"status": "Nope"})).status_code == 422


async def test_flight_detail_includes_manifest_and_meals(client, make_flight):
    await make_flight(economy=100, business=0, crew=0)
    r = await client.get("/api/v1/flights/QR1001")
    assert r.status_code == 200
    body = r.json()
    assert body["manifest"]["total_passengers"] == 100
    assert len(body["meal_inventories"]) == 5
    assert body["total_meals_required"] == 100
    assert {m["status"] for m in body["meal_inventories"]} == {"Pending"}


async def test_flight_detail_404(client):
    assert (await client.get("/api/v1/flights/QR0000")).status_code == 404


async def test_patch_status_cancel_updates_meals_synchronously(client, make_flight):
    await make_flight()
    r = await client.patch("/api/v1/flights/QR1001/status", json={"status": "Cancelled", "reason": "ATC"})
    assert r.status_code == 200
    assert r.json()["status"] == "Cancelled" and r.json()["total_meals_required"] == 0


async def test_patch_status_404_and_422(client, make_flight):
    await make_flight()
    assert (await client.patch("/api/v1/flights/QR9999/status", json={"status": "Boarding"})).status_code == 404
    assert (await client.patch("/api/v1/flights/bad id/status", json={"status": "Boarding"})).status_code == 404
    assert (await client.patch("/api/v1/flights/QR1001/status", json={"status": "Flying"})).status_code == 422


# ------------------------------ disruptions -------------------------------- #
async def test_disruption_returns_202_and_updates_inventory_async(client, make_flight, session_factory):
    await make_flight(economy=90, business=0, crew=10)
    r = await client.post("/api/v1/events/disruption", json=DELAY)
    assert r.status_code == 202
    assert r.json()["status"] == "accepted" and r.json()["flight_id"] == "QR1001"

    await background_tasks.wait_for_pending(timeout=5)

    detail = (await client.get("/api/v1/flights/QR1001")).json()
    assert detail["status"] == "Delayed" and detail["delay_minutes"] == 90
    assert detail["total_meals_required"] == 105  # 100 + 5% buffer
    logs = (await client.get("/api/v1/kitchen/logs", params={"flight_id": "QR1001"})).json()
    assert logs["total"] == 1 and logs["items"][0]["event_type"] == "FLIGHT_DELAY"


async def test_202_is_returned_before_processing_finishes(client, make_flight, monkeypatch):
    """The endpoint must not wait on the engine: block the engine and still get 202."""
    await make_flight(economy=90, business=0, crew=10)
    gate = asyncio.Event()
    real_apply = catering_engine.apply_event

    async def slow_apply(*args, **kwargs):
        await gate.wait()
        return await real_apply(*args, **kwargs)

    monkeypatch.setattr(catering_engine, "apply_event", slow_apply)

    r = await asyncio.wait_for(client.post("/api/v1/events/disruption", json=DELAY), timeout=2)
    assert r.status_code == 202
    assert background_tasks.pending_count() == 1
    assert (await client.get("/api/v1/flights/QR1001")).json()["total_meals_required"] == 100  # untouched

    gate.set()
    await background_tasks.wait_for_pending(timeout=5)
    assert (await client.get("/api/v1/flights/QR1001")).json()["total_meals_required"] == 105


async def test_passenger_surge_event(client, make_flight):
    await make_flight(economy=100, business=0, crew=0)
    payload = {
        "flight_id": "QR1001",
        "event_type": "PASSENGER_COUNT_CHANGE",
        "passenger_delta": {"economy": 25, "business": 5},
    }
    assert (await client.post("/api/v1/events/disruption", json=payload)).status_code == 202
    await background_tasks.wait_for_pending(timeout=5)
    detail = (await client.get("/api/v1/flights/QR1001")).json()
    assert detail["manifest"]["total_passengers"] == 130
    assert detail["total_meals_required"] == 130


async def test_disruption_unknown_flight_is_404(client):
    r = await client.post("/api/v1/events/disruption", json={**DELAY, "flight_id": "QR9999"})
    assert r.status_code == 404


async def test_disruption_payload_validation(client, make_flight):
    await make_flight()
    bad_payloads = [
        {"flight_id": "QR1001", "event_type": "FLIGHT_DELAY"},  # missing delay_minutes
        {**DELAY, "delay_minutes": -5},
        {**DELAY, "delay_minutes": 99999},
        {"flight_id": "QR1001", "event_type": "PASSENGER_COUNT_CHANGE"},  # missing delta
        {"flight_id": "QR1001", "event_type": "PASSENGER_COUNT_CHANGE", "passenger_delta": {}},  # all zero
        {"flight_id": "QR1001", "event_type": "STATUS_CHANGE"},  # missing new_status
        {"flight_id": "QR1001", "event_type": "EXPLODE"},
        {**DELAY, "flight_id": "not valid"},
    ]
    for payload in bad_payloads:
        r = await client.post("/api/v1/events/disruption", json=payload)
        assert r.status_code == 422, payload
    assert background_tasks.pending_count() == 0


async def test_failed_background_recalc_leaves_state_untouched_and_is_audited(
    client, make_flight, session_factory, monkeypatch
):
    await make_flight(economy=90, business=0, crew=10)
    real_recalc = catering_engine._recalculate_inventories

    def explode(*a, **k):
        real_recalc(*a, **k)
        raise RuntimeError("kaboom")

    monkeypatch.setattr(catering_engine, "_recalculate_inventories", explode)
    assert (await client.post("/api/v1/events/disruption", json=DELAY)).status_code == 202
    await background_tasks.wait_for_pending(timeout=5)

    detail = (await client.get("/api/v1/flights/QR1001")).json()
    assert detail["delay_minutes"] == 0 and detail["total_meals_required"] == 100
    async with session_factory() as s:
        types = (await s.scalars(select(AuditLog.event_type))).all()
    assert types == ["RECALC_FAILED"]


# -------------------------------- kitchen ---------------------------------- #
async def test_kitchen_queue_sorted_by_departure_and_filtered(client, make_flight):
    await make_flight("QR1001", minutes_ahead=180)
    await make_flight("QR1002", minutes_ahead=30)
    await make_flight("QR1003", minutes_ahead=90)
    await make_flight("QR1004", minutes_ahead=10, status=FlightStatus.CANCELLED)  # excluded
    await make_flight("QR1005", minutes_ahead=45, assembled_fraction=1.0)  # nothing left to do
    await make_flight("QR1006", minutes_ahead=-30)  # already departed
    await make_flight("QR1007", minutes_ahead=60 * 30)  # beyond 24h window

    r = await client.get("/api/v1/kitchen/queue")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["flight_id"] for i in items] == ["QR1002", "QR1003", "QR1001"]
    assert [i["rank"] for i in items] == [1, 2, 3]
    assert [i["priority"] for i in items] == ["CRITICAL", "HIGH", "HIGH"]
    assert items[0]["total_remaining"] == items[0]["total_required"] > 0


async def test_kitchen_queue_reflects_delay_reordering(client, make_flight):
    await make_flight("QR1001", minutes_ahead=30)
    await make_flight("QR1002", minutes_ahead=60)
    first = (await client.get("/api/v1/kitchen/queue")).json()["items"]
    assert first[0]["flight_id"] == "QR1001"

    await client.post(
        "/api/v1/events/disruption",
        json={"flight_id": "QR1001", "event_type": "FLIGHT_DELAY", "delay_minutes": 120},
    )
    await background_tasks.wait_for_pending(timeout=5)
    second = (await client.get("/api/v1/kitchen/queue")).json()["items"]
    assert [i["flight_id"] for i in second] == ["QR1002", "QR1001"]


async def test_kitchen_queue_limit_and_validation(client, make_flight):
    for i in range(4):
        await make_flight(f"QR100{i}", minutes_ahead=30 + i)
    assert len((await client.get("/api/v1/kitchen/queue", params={"limit": 2})).json()["items"]) == 2
    assert (await client.get("/api/v1/kitchen/queue", params={"hours_ahead": 0})).status_code == 422


async def test_kitchen_logs_filtering_and_order(client, make_flight):
    await make_flight("QR1001")
    await make_flight("QR1002")
    for fid, minutes in [("QR1001", 10), ("QR1001", 20), ("QR1002", 30)]:
        await client.post(
            "/api/v1/events/disruption",
            json={"flight_id": fid, "event_type": "FLIGHT_DELAY", "delay_minutes": minutes},
        )
        await background_tasks.wait_for_pending(timeout=5)

    everything = (await client.get("/api/v1/kitchen/logs")).json()
    assert everything["total"] == 3
    assert "30" in everything["items"][0]["description"]  # newest first
    one = (await client.get("/api/v1/kitchen/logs", params={"flight_id": "QR1001"})).json()
    assert one["total"] == 2


async def test_database_enforces_foreign_keys(session_factory):
    """Raw inserts that reference a missing flight must be rejected by SQLite."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with session_factory() as s:
            async with s.begin():
                s.add(AuditLog(flight_id="NOPE1", event_type="X", description="orphan"))
