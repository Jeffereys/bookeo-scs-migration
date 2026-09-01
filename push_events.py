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

Idempotency is anchored in SCS, not in a local file. Every Event this
module creates carries function.event.interfaceAccountId = "BKO:<bookingNumber>"
(transform.bookeo_marker); event_lookup.py reads those markers back through
the BookeoMigration_Lookup Get Request. run_push_events() sweeps the target
window up front and skips any booking already present in SCS, so a lost or
stale event_id_map.json can't cause a duplicate -- and event_lookup.rebuild_id_map()
reconstructs the file from SCS. event_id_map.json is kept as a fast-path
cache / audit log (confirmation_number = Event Number, site_unique_id =
Event uniqueId).
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
    Returns None without calling SCS if id_map already has a mapping for
    this booking (fast path -- the authoritative check is the SCS marker
    sweep in run_push_events). mode="test" (the default) validates without
    creating anything and never touches id_map; mode="apply" creates a real
    Event and records it immediately.
    """
    booking_number = transformed["bookeo_booking_number"]
    if id_map.is_migrated(booking_number):
        return None

    resp = events_api.create_event(transformed["fields"], mode=mode)
    try:
        result = resp["results"][0]
        status = result["status"]
    except (KeyError, IndexError, TypeError) as exc:
        raise EventPushError(
            f"EventFunctionImport returned an unparseable response for booking "
            f"{booking_number}: {resp!r}"
        ) from exc

    if status == "Failed":
        raise EventPushError(
            f"EventFunctionImport failed for booking {booking_number}: {result.get('messages')}"
        )

    if mode == "test":
        return result

    unique_ids = result.get("uniqueIds") or {}
    id_map.record(
        booking_number,
        confirmation_number=unique_ids.get("eventNumber") or unique_ids.get("uniqueId"),
        site_unique_id=unique_ids.get("event.uniqueId"),
        status=status.lower(),
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
    transform each, and push as SCS Events. Before pushing anything it
    sweeps SCS for the same window (event_lookup.migrated_booking_numbers)
    and skips every booking already present there -- that's the real
    idempotency guard; event_id_map.json is just a fast-path cache on top.
    mode="test" (the default) validates every row without creating
    anything; only pass mode="apply" once every row comes back clean.
    """
    from bookeo_api import BookeoAPI
    from scs_gateway import SCSGatewayClient, SCSGatewayError
    from events import EventsAPI
    from event_lookup import migrated_booking_numbers

    bookeo = BookeoAPI()
    client = SCSGatewayClient()
    events_api = EventsAPI(client)
    id_map = IdMap(path=DEFAULT_EVENT_ID_MAP_PATH)

    already_in_scs = migrated_booking_numbers(client, start_time, end_time)
    print(f"SCS already has {len(already_in_scs)} migrated booking(s) in this window")

    bookings = bookeo.get_bookings(start_time=start_time, end_time=end_time)["data"][:limit]

    for booking in bookings:
        booking_number = booking["bookingNumber"]

        if booking_number in already_in_scs:
            print(f"skip {booking_number}: already an Event in SCS")
            # keep the cache honest without a second round-trip
            if not id_map.is_migrated(booking_number):
                id_map.record(booking_number, confirmation_number=None,
                              status="migrated", source="scs_sweep")
            continue

        if id_map.is_migrated(booking_number):
            existing = id_map.get(booking_number)
            print(f"skip {booking_number}: in cache -> {existing.get('confirmation_number')}")
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
            print(f"pushed {booking_number} [{result['status']}] -> {result.get('uniqueIds')}")


def rebuild_event_id_map(start, end):
    """CLI helper: reconstruct event_id_map.json from SCS for a date window
    (MM/DD/YYYY or ISO). Use after losing/doubting the local cache. Only
    picks up Events that carry a BKO: marker -- run backfill_interface_markers
    first if any pre-marker Events are still in play."""
    from scs_gateway import SCSGatewayClient
    from event_lookup import rebuild_id_map

    id_map = rebuild_id_map(SCSGatewayClient(), start, end, path=DEFAULT_EVENT_ID_MAP_PATH)
    print(f"event_id_map.json rebuilt from SCS: {len(id_map)} entr(y/ies)")


def backfill_interface_markers(mode="test"):
    """
    One-time: stamp event.interfaceAccountId = "BKO:<bookingNumber>" onto
    the Events that were migrated before the marker existed (every entry in
    event_id_map.json -- the 44+ from the 2026-08-27/30 runs carry no
    marker in SCS, so rebuild_id_map() and the run_push_events sweep can't
    see them). Uses EventUpdate keyed on the cached Event uniqueId.
    Idempotent: EventUpdate returns "Merged" and re-running is harmless.
    mode="test" validates only; pass mode="apply" once test is clean.
    """
    from scs_gateway import SCSGatewayClient, SCSGatewayError
    from events import EventsAPI
    from transform import bookeo_marker

    events_api = EventsAPI(SCSGatewayClient())
    id_map = IdMap(path=DEFAULT_EVENT_ID_MAP_PATH)

    ok = failed = 0
    for booking_number, entry in sorted(id_map.all().items()):
        event_uid = entry.get("site_unique_id")
        if not event_uid:
            print(f"skip {booking_number}: no Event uniqueId in cache")
            failed += 1
            continue
        try:
            r = events_api.update_event(
                event_uid, {"event.interfaceAccountId": bookeo_marker(booking_number)}, mode=mode
            )["results"][0]
        except (SCSGatewayError, KeyError, IndexError) as exc:
            print(f"FAILED {booking_number} ({event_uid}): {exc}")
            failed += 1
            continue
        if r.get("status") == "Failed":
            print(f"FAILED {booking_number}: {r.get('messages')}")
            failed += 1
            continue
        print(f"{booking_number} -> {r.get('status')}")
        ok += 1
    print(f"\n{'(test) ' if mode == 'test' else ''}{ok} ok, {failed} failed")


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

    rebuild_cmd = sub.add_parser(
        "rebuild", help="Reconstruct event_id_map.json from SCS for a date window"
    )
    rebuild_cmd.add_argument("start", help="window start, MM/DD/YYYY or ISO8601")
    rebuild_cmd.add_argument("end", help="window end, MM/DD/YYYY or ISO8601")

    backfill_cmd = sub.add_parser(
        "backfill-markers",
        help="One-time: stamp BKO: markers onto Events migrated before the marker existed",
    )
    backfill_cmd.add_argument("--apply", action="store_true", help="Actually write (default is test)")

    args = parser.parse_args()
    if args.command == "push":
        run_push_events(args.start_time, args.end_time, limit=args.limit, mode="apply" if args.apply else "test")
    elif args.command == "rebuild":
        rebuild_event_id_map(args.start, args.end)
    elif args.command == "backfill-markers":
        backfill_interface_markers(mode="apply" if args.apply else "test")
