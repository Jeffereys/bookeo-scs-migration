"""
Bookeo -> SCS push/orchestration using Temporary Holds.

Ties transform.py (Bookeo -> SCS field shapes) and id_map.py (dedup/
correlation) into the actual write step: for each Bookeo booking, find a
matching SCS slot via check_availability() and book it as a Temporary Hold
(reservations_api.book_reservation(..., temporary_hold=True)) -- invisible
to SCS staff, blocks real inventory, and can only be converted/cancelled by
this gateway. This is deliberately the ONLY write mode this script
supports; see Project_details.txt's Testing section for why (QA env /
mode="test" / temporary_hold=True staging before anything ever gets
mode="apply"'d or converted to a permanent, non-hold reservation).

Cleanup is not optional: SCS puts the responsibility for converting or
cancelling every temporary hold entirely on the sender (see reservations.py's
module docstring). push_booking() records the id_map mapping the instant a
hold is created (and cancels the hold if that record fails), and
cleanup_all_holds() sweeps every temporary_hold entry left in id_map --
run it after every test batch, per the Testing section.

NOTE: SCS's check_availability() only returns currently-open FUTURE slots
-- it can't find a slot for a Bookeo booking whose startTime is already in
the past. This script validates the push mechanism end-to-end (point it at
a near-future Bookeo booking); it does not retroactively recreate
already-passed bookings in SCS.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bookeo"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Reserve"))

from transform import transform_booking, SITE_NAME
from id_map import IdMap


class PushError(Exception):
    """Raised for expected push failures (e.g. no matching SCS slot)."""


def find_reservation_code(reservations_api, transformed, max_results=20, max_assets_to_try=8):
    """
    Look up a live SCS reservationCode for a transformed booking's target
    date/desired time/party size/requests via check_availability(). Takes
    the first result -- exact start-time matching against the returned
    slots isn't implemented since check_availability()'s result shape for
    startTime hasn't been verified against live data yet; if that turns
    out to matter, refine this to prefer the closest startTime instead of
    just slots[0].

    SCS's default (unspecified number_of_assets) search only auto-combines
    up to 2 assets (e.g. 2 lanes) -- confirmed live on 2026-08-20: a party
    of 12 gets a normal 2-lane result, a party of 13 gets zero results
    with no other parameters changed. Larger parties need
    number_of_assets set explicitly. Rather than hardcode a per-lane
    capacity (not a documented SCS setting), retry with increasing
    number_of_assets (3, 4, ... up to max_assets_to_try) whenever the
    plain search comes back empty, and use whichever is the first to find
    a slot. Raises PushError if nothing is available at any asset count.
    """
    for number_of_assets in [None] + list(range(3, max_assets_to_try + 1)):
        kwargs = {}
        if number_of_assets is not None:
            kwargs["number_of_assets"] = number_of_assets
        slots = reservations_api.check_availability(
            reservation_date=transformed["reservation_date"],
            desired_time_from=transformed["desired_time_from"],
            party_size=transformed["party_size"],
            site_names=[SITE_NAME],
            requests=transformed["requests"],
            max_results=max_results,
            **kwargs,
        )
        results = slots.get("results", [])
        if results:
            return results[0]["reservationCode"]

    raise PushError(
        f"No SCS availability for {transformed['reservation_date']} "
        f"{transformed['desired_time_from']} (booking "
        f"{transformed['bookeo_booking_number']}), tried default and "
        f"number_of_assets 3-{max_assets_to_try}"
    )


def push_booking(reservations_api, id_map, transformed):
    """
    Push one transformed Bookeo booking into SCS as a Temporary Hold.
    Idempotent: returns None without calling SCS if id_map already has a
    live (non-cancelled) mapping for this booking. If the id_map write
    fails after a hold was successfully created, cancels the hold before
    re-raising -- never leaves an orphaned hold with no local record of
    it, since that's exactly what would let it linger forever (SCS puts
    cleanup entirely on the sender).
    """
    booking_number = transformed["bookeo_booking_number"]
    if id_map.is_migrated(booking_number):
        return None

    reservation_code = find_reservation_code(reservations_api, transformed)

    extra = {}
    if transformed.get("comments"):
        extra["comments"] = transformed["comments"]

    booked = reservations_api.book_reservation(
        reservation_code=reservation_code,
        first_name=transformed["first_name"],
        last_name=transformed["last_name"],
        email=transformed["email"],
        mobile_phone=transformed["mobile_phone"],
        temporary_hold=True,
        **extra,
    )

    try:
        id_map.record(
            booking_number,
            confirmation_number=booked["confirmationNumber"],
            unique_id=booked.get("uniqueId"),
            site_unique_id=booked.get("siteUniqueId"),
            status="temporary_hold",
            bookeo_customer_id=transformed.get("bookeo_customer_id"),
        )
    except Exception:
        reservations_api.cancel_reservation(
            booked["confirmationNumber"], cancellation_reason="Other"
        )
        raise

    return booked


def cleanup_all_holds(reservations_api, id_map):
    """
    Cancel every temporary_hold entry currently in id_map -- the sweep
    step recommended in Project_details.txt's Testing section after a
    test batch, so nothing lingers as blocked inventory. Returns the list
    of Bookeo booking numbers that were cancelled.
    """
    cancelled = []
    for booking_number, entry in id_map.all().items():
        if entry.get("status") != "temporary_hold":
            continue
        reservations_api.cancel_reservation(
            entry["confirmation_number"], cancellation_reason="Other"
        )
        id_map.update_status(booking_number, "cancelled")
        cancelled.append(booking_number)
    return cancelled


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
def run_push(start_time, end_time, limit=5, cleanup_after=False):
    """
    End-to-end driver: pull up to `limit` real Bookeo bookings in
    [start_time, end_time) (max 31 days apart, per Bookeo's API), transform
    each, push as SCS temporary holds (skipping anything id_map already
    has), print results. If cleanup_after=True, cancels every hold this
    run just created before exiting.
    """
    from bookeo_api import BookeoAPI
    from scs_gateway import SCSGatewayClient
    from reservations import ReservationsAPI

    bookeo = BookeoAPI()
    reservations = ReservationsAPI(SCSGatewayClient())
    id_map = IdMap()

    options = reservations.get_reservation_field_options(site_names=[SITE_NAME])
    available_requests = options["results"][0]["requests"]

    bookings = bookeo.get_bookings(start_time=start_time, end_time=end_time)["data"][:limit]

    pushed_this_run = []
    for booking in bookings:
        booking_number = booking["bookingNumber"]
        if id_map.is_migrated(booking_number):
            existing = id_map.get(booking_number)
            print(f"skip {booking_number}: already migrated -> {existing['confirmation_number']}")
            continue

        customer = bookeo.get_customer(booking["customerId"])
        try:
            transformed = transform_booking(booking, customer, available_requests)
            booked = push_booking(reservations, id_map, transformed)
        except (PushError, ValueError) as exc:
            print(f"FAILED {booking_number}: {exc}")
            continue

        print(f"pushed {booking_number} -> {booked['confirmationNumber']} (temporary hold)")
        pushed_this_run.append(booking_number)

    if cleanup_after and pushed_this_run:
        print(f"cleaning up {len(pushed_this_run)} temporary hold(s) from this run...")
        for booking_number in pushed_this_run:
            entry = id_map.get(booking_number)
            reservations.cancel_reservation(entry["confirmation_number"], cancellation_reason="Other")
            id_map.update_status(booking_number, "cancelled")
        print("done.")


def run_cleanup():
    """CLI entry for `python push.py cleanup` -- sweep every temporary_hold
    left in id_map, regardless of which run created it."""
    from scs_gateway import SCSGatewayClient
    from reservations import ReservationsAPI

    reservations = ReservationsAPI(SCSGatewayClient())
    id_map = IdMap()
    cancelled = cleanup_all_holds(reservations, id_map)
    if not cancelled:
        print("nothing to clean up.")
    for booking_number in cancelled:
        print(f"cancelled hold for {booking_number}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    push_cmd = sub.add_parser("push", help="Pull Bookeo bookings in a window and push them as SCS temporary holds")
    push_cmd.add_argument("start_time", help="Bookeo ISO8601 window start, e.g. 2026-08-21T00:00:00Z")
    push_cmd.add_argument("end_time", help="Bookeo ISO8601 window end (max 31 days after start)")
    push_cmd.add_argument("--limit", type=int, default=5)
    push_cmd.add_argument("--cleanup-after", action="store_true", help="Cancel every hold this run creates before exiting")

    sub.add_parser("cleanup", help="Cancel every temporary_hold currently recorded in id_map")

    args = parser.parse_args()
    if args.command == "push":
        run_push(args.start_time, args.end_time, limit=args.limit, cleanup_after=args.cleanup_after)
    elif args.command == "cleanup":
        run_cleanup()
