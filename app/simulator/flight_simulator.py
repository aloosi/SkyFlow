"""Live flight disruption simulator.

Every 15-30 seconds it picks a random active flight, creates a delay or a
passenger-count surge, and POSTs it to the running API:

    python -m app.simulator.flight_simulator --base-url http://localhost:8000
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
from typing import Any, Optional

import httpx

logger = logging.getLogger("skyflow.simulator")

EVENTS_PATH = "/api/v1/events/disruption"
FLIGHTS_PATH = "/api/v1/flights/"


def build_payload(flight_id: str, rng: random.Random) -> dict[str, Any]:
    """60% delays, 40% passenger surges (late bookings / connecting-flight arrivals)."""
    if rng.random() < 0.6:
        minutes = rng.choice([15, 20, 30, 45, 60, 75, 90, 120, 150, 180])
        return {
            "flight_id": flight_id,
            "event_type": "FLIGHT_DELAY",
            "delay_minutes": minutes,
            "reason": "Simulated operational delay",
        }
    return {
        "flight_id": flight_id,
        "event_type": "PASSENGER_COUNT_CHANGE",
        "passenger_delta": {
            "economy": rng.randint(3, 40),
            "business": rng.randint(0, 6),
            "first_class": 0,
            "crew": 0,
        },
        "reason": "Simulated late-booking surge",
    }


async def pick_random_flight(client: httpx.AsyncClient, rng: random.Random) -> Optional[str]:
    """Choose a random Scheduled/Delayed flight using the paginated list endpoint."""
    status = rng.choice(["Scheduled", "Scheduled", "Delayed"])
    probe = await client.get(FLIGHTS_PATH, params={"status": status, "limit": 1})
    probe.raise_for_status()
    total = probe.json()["total"]
    if total == 0:
        return None
    pick = await client.get(
        FLIGHTS_PATH, params={"status": status, "limit": 1, "offset": rng.randrange(total)}
    )
    pick.raise_for_status()
    items = pick.json()["items"]
    return items[0]["flight_id"] if items else None


async def run_once(client: httpx.AsyncClient, rng: random.Random) -> Optional[dict[str, Any]]:
    flight_id = await pick_random_flight(client, rng)
    if flight_id is None:
        logger.warning("No eligible flights found; is the database seeded?")
        return None
    payload = build_payload(flight_id, rng)
    response = await client.post(EVENTS_PATH, json=payload)
    response.raise_for_status()
    logger.info("Sent %s for %s -> HTTP %d", payload["event_type"], flight_id, response.status_code)
    return payload


async def run(
    base_url: str,
    min_interval: float,
    max_interval: float,
    iterations: int = 0,
    seed: Optional[int] = None,
) -> None:
    """Run forever (iterations=0) or a fixed number of events."""
    rng = random.Random(seed)
    sent = 0
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        while iterations == 0 or sent < iterations:
            try:
                if await run_once(client, rng):
                    sent += 1
            except httpx.HTTPError as exc:  # API restarting / not ready: keep going
                logger.warning("Request failed (%s); will retry", exc)
            if iterations and sent >= iterations:
                break
            await asyncio.sleep(rng.uniform(min_interval, max_interval))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--base-url", default=os.getenv("SKYFLOW_API_URL", "http://localhost:8000"))
    parser.add_argument("--min-interval", type=float, default=float(os.getenv("SIM_MIN_INTERVAL", 15)))
    parser.add_argument("--max-interval", type=float, default=float(os.getenv("SIM_MAX_INTERVAL", 30)))
    parser.add_argument("--iterations", type=int, default=0, help="stop after N events (0 = forever)")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()
    if args.min_interval > args.max_interval:
        parser.error("--min-interval must be <= --max-interval")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run(args.base_url, args.min_interval, args.max_interval, args.iterations, args.seed))
    except KeyboardInterrupt:
        logger.info("Simulator stopped")


if __name__ == "__main__":
    main()
