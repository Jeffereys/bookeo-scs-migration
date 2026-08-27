"""
Bookeo -> SCS Events push.

Creates one SCS Event+Function per Bookeo booking via EventFunctionImport
(see Reserve/events.py), using transform.transform_booking_to_event(). Built
because the company doesn't use the Reservations calendar operationally --
only the Events calendar -- so this pushes bookings as Events instead of
Reservations (see push.py for the original Reservations pipeline).

Critical difference from push.py: Events have NO Temporary Hold mechanism.
mode="apply" creates a REAL, immediately staff-visible Event -- there's no
safe intermediate state to verify-then-commit the way Reservations'
temporary_hold=True works. This module defaults every entry point to
mode="test" (SCS's server-side dry-run validation, creates nothing); a
caller must explicitly opt into mode="apply" per call.

There's also no availability-check endpoint for Events (nothing analogous
to ReservationCheckAvailability), so this cannot detect or avoid
double-booking the "Bowling Lanes" Location -- SCS's own maxConcurrentEvents
cap on that Location (24, per a live pull on 2026-08-26) is the only
backstop.

Idempotency uses its own JSON-file id map (event_id_map.json, gitignored,
separate from migration/id_map.json's Reservations-side confirmationNumbers)
keyed by Bookeo bookingNumber, storing the created Function/Event uniqueIds.
Reuses id_map.IdMap's field names loosely (confirmation_number holds the
created Function's uniqueId, site_unique_id holds the Event's uniqueId) --
not a perfect semantic fit, but IdMap's atomic-write JSON store is already
proven and there's no reason to duplicate it.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bookeo"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Reserve"))

from transform import transform_booking_to_event
from id_map import IdMap

DEFAULT_EVENT_ID_MAP_PATH = os.path.join(os.path.dirname(__file__), "event_id_map.json")


class EventPushError(Exception):
    """Raised for expected push failures (e.g. SCS rejected the row)."""


def push_event_booking(events_api, id_map, transformed, mode="test"):
    """
    Push one transformed Bookeo booking into SCS as an Event+Function.
    Idempotent: returns None without calling SCS if id_map already has a
    mapping for this booking. mode="test" (the default) validates without
    creating anything and never touches id_map, since there's nothing real
    to record; mode="apply" creates a real Event and records it immediately.
    """
    booking_number = transformed["bookeo_booking_number"]
    if id_map.is_migrated(booking_number):
        return None

    resp = events_api.create_event(transformed["fields"], mode=mode)
    result = resp["results"][0]

    if result["status"] == "Failed":
        raise EventPushError(
            f"EventFunctionImport failed for booking {booking_number}: {result.get('messages')}"
        )

    if mode == "test":
        return result

    unique_ids = result.get("uniqueIds") or {}
    id_map.record(
        booking_number,
        confirmation_number=unique_ids.get("uniqueId"),
        site_unique_id=unique_ids.get("event.uniqueId"),
        status=result["status"].lower(),
        bookeo_customer_id=transformed.get("bookeo_customer_id"),
    )
    return result


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
def run_push_events(start_time, end_time, limit=5, mode="test"):
    """
    End-to-end driver: pull up to `limit` real Bookeo bookings in
    [start_time, end_time) (max 31 days apart, per Bookeo's own limit),
    transform each, and push as SCS Events (skipping anything
    event_id_map.json already has). mode="test" (the default) validates
    every row without creating anything; only pass mode="apply" once every
    row comes back clean under mode="test".
    """
    from bookeo_api import BookeoAPI
    from scs_gateway import SCSGatewayClient, SCSGatewayError
    from events import EventsAPI

    bookeo = BookeoAPI()
    events_api = EventsAPI(SCSGatewayClient())
    id_map = IdMap(path=DEFAULT_EVENT_ID_MAP_PATH)

    bookings = bookeo.get_bookings(start_time=start_time, end_time=end_time)["data"][:limit]

    for booking in bookings:
        booking_number = booking["bookingNumber"]

        if id_map.is_migrated(booking_number):
            existing = id_map.get(booking_number)
            print(f"skip {booking_number}: already migrated -> {existing['confirmation_number']}")
            continue

        if booking.get("canceled"):
            print(f"skip {booking_number}: canceled in Bookeo")
            continue

        customer = bookeo.get_customer(booking["customerId"])
        try:
            transformed = transform_booking_to_event(booking, customer)
            result = push_event_booking(events_api, id_map, transformed, mode=mode)
        except (EventPushError, ValueError, SCSGatewayError) as exc:
            print(f"FAILED {booking_number}: {exc}")
            continue

        if mode == "test":
            print(f"test-validated {booking_number}: {result}")
        else:
            print(f"pushed {booking_number} -> {result}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    push_cmd = sub.add_parser("push", help="Pull Bookeo bookings in a window and push them as SCS Events")
    push_cmd.add_argument("start_time", help="Bookeo ISO8601 window start, e.g. 2026-08-21T00:00:00Z")
    push_cmd.add_argument("end_time", help="Bookeo ISO8601 window end (max 31 days after start)")
    push_cmd.add_argument("--limit", type=int, default=5)
    push_cmd.add_argument(
        "--apply",
        action="store_true",
        help="Actually create real, permanent Events (default is mode=test, which creates nothing)",
    )

    args = parser.parse_args()
    if args.command == "push":
        run_push_events(args.start_time, args.end_time, limit=args.limit, mode="apply" if args.apply else "test")
