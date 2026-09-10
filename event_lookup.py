"""
Read-back index of already-migrated Bookeo bookings -- from SCS itself.

push_events.py stamps every Event it creates with
    function.event.interfaceAccountId = "BKO:<bookingNumber>"
(see transform.bookeo_marker). SCS has no server-side filter on that field,
but the `BookeoMigration_Lookup` Event Gateway Get Request returns it as a
column and *is* filterable by startDate. So to learn what's already in SCS
we sweep a startDate window, page through the rows, and pull the "BKO:"
markers out locally.

This makes SCS the source of truth for "has this booking been migrated?":
    migrated_booking_numbers(client, start, end)  -> set[str]   (per-run guard)
    migrated_event_index(client, start, end)      -> {bkNo: {...event fields}}
    rebuild_id_map(client, start, end, path=...)  -> IdMap       (cache rebuild)

event_id_map.json is now only a cache: lose it and rebuild_id_map() brings
it back from SCS.

The Get Request + its `api_key` agent permission were configured 2026-08-31
(Settings > Events > Manage Event Gateway Get Requests). Columns, in header
order:
    interfaceAccountId, uniqueId, eventNumber, name, startDate, startTime,
    lifecycleState.stateType, functions.locations.name
"""

import datetime as _dt

from transform import parse_bookeo_marker, DEFAULT_VENUE

LOOKUP_REQUEST_NAME = "BookeoMigration_Lookup"
_PAGE_SIZE = 100  # SCS hard cap on maxResults


# ---------------------------------------------------------------------
# Date coercion -- callers pass Bookeo ISO timestamps, date objects, or
# already-formatted MM/DD/YYYY strings; the Get Request filter wants
# MM/DD/YYYY.
# ---------------------------------------------------------------------
def to_mmddyyyy(value):
    if isinstance(value, _dt.datetime):
        return value.strftime("%m/%d/%Y")
    if isinstance(value, _dt.date):
        return value.strftime("%m/%d/%Y")
    text = str(value).strip()
    if not text:
        raise ValueError("empty date")
    # already MM/DD/YYYY?
    try:
        return _dt.datetime.strptime(text, "%m/%d/%Y").strftime("%m/%d/%Y")
    except ValueError:
        pass
    # ISO8601, with or without a trailing Z / offset
    iso = text.replace("Z", "+00:00")
    try:
        return _dt.datetime.fromisoformat(iso).strftime("%m/%d/%Y")
    except ValueError:
        pass
    return _dt.datetime.strptime(text[:10], "%Y-%m-%d").strftime("%m/%d/%Y")


def _widen(start_mmddyyyy, end_mmddyyyy, days=1):
    """Pad the window by `days` on each side -- a Bookeo booking's UTC
    window edge can land a venue-local day earlier/later than the SCS
    Event's startDate."""
    s = _dt.datetime.strptime(start_mmddyyyy, "%m/%d/%Y") - _dt.timedelta(days=days)
    e = _dt.datetime.strptime(end_mmddyyyy, "%m/%d/%Y") + _dt.timedelta(days=days)
    return s.strftime("%m/%d/%Y"), e.strftime("%m/%d/%Y")


# ---------------------------------------------------------------------
# Core sweep
# ---------------------------------------------------------------------
def iter_events(client, start, end, venue=DEFAULT_VENUE, widen_days=1):
    """
    Yield each SCS Event (as a header-keyed dict) whose startDate is in
    [start, end] AND whose interfaceAccountId is a Bookeo marker for `venue`.

    `client` is a Reserve.scs_gateway.SCSGatewayClient. `start`/`end` accept
    anything to_mmddyyyy() understands. The Get Request isn't site-filtered,
    so this can see Events from every venue -- the per-venue marker prefix
    (parse_bookeo_marker) is what scopes the result.
    """
    start_s, end_s = _widen(to_mmddyyyy(start), to_mmddyyyy(end), days=widen_days)
    filters = [
        ["startDate", "GREATER_THAN_OR_EQUAL_TO", start_s],
        ["startDate", "LESS_THAN_OR_EQUAL_TO", end_s],
    ]

    first = 0
    seen = 0
    while True:
        resp = client.get_request(
            LOOKUP_REQUEST_NAME,
            filters=filters,
            max_results=_PAGE_SIZE,
            first_result=first,
        )
        header = resp["header"]
        rows = resp.get("results") or []
        for row in rows:
            record = dict(zip(header, row))
            if parse_bookeo_marker(record.get("interfaceAccountId"), venue) is not None:
                yield record

        seen += len(rows)
        total = resp.get("count")
        if len(rows) < _PAGE_SIZE or (total is not None and seen >= total):
            break
        first += _PAGE_SIZE


# ---------------------------------------------------------------------
# Convenience shapes
# ---------------------------------------------------------------------
def migrated_event_index(client, start, end, venue=DEFAULT_VENUE, widen_days=1):
    """{bookeo_booking_number: {event_unique_id, event_number, name,
    start_date, start_time, status, locations}} for the window and venue."""
    index = {}
    for rec in iter_events(client, start, end, venue=venue, widen_days=widen_days):
        booking_number = parse_bookeo_marker(rec["interfaceAccountId"], venue)
        index[booking_number] = {
            "event_unique_id": rec.get("uniqueId"),
            "event_number": rec.get("eventNumber"),
            "name": rec.get("name"),
            "start_date": rec.get("startDate"),
            "start_time": rec.get("startTime"),
            "status": rec.get("lifecycleState.stateType"),
            "locations": rec.get("functions.locations.name"),
        }
    return index


def migrated_booking_numbers(client, start, end, venue=DEFAULT_VENUE, widen_days=1):
    """Set of Bookeo booking numbers already present as Events in SCS for
    the window and venue -- the per-run idempotency guard."""
    return {
        parse_bookeo_marker(rec["interfaceAccountId"], venue)
        for rec in iter_events(client, start, end, venue=venue, widen_days=widen_days)
    }


def rebuild_id_map(client, start, end, venue=DEFAULT_VENUE, path=None, widen_days=1):
    """
    Reconstruct the venue's event_id_map file entirely from SCS for the
    window and return the IdMap. Use when the local cache is lost or suspect.

    Records site_unique_id = the Event uniqueId (authoritative, from SCS),
    confirmation_number = the Event Number, status = the lifecycle state
    lower-cased (e.g. "option hold 5"), plus event_name/start_date for
    eyeballing. Does NOT touch bookings that aren't found in SCS.
    """
    from id_map import IdMap

    if path is None:
        from push_events import id_map_path

        path = id_map_path(venue)
    id_map = IdMap(path=path)
    index = migrated_event_index(client, start, end, venue=venue, widen_days=widen_days)
    for booking_number, info in index.items():
        id_map.record(
            booking_number,
            confirmation_number=info["event_number"],
            site_unique_id=info["event_unique_id"],
            status=(info["status"] or "").lower() or "migrated",
            event_name=info["name"],
            start_date=info["start_date"],
            source="rebuilt_from_scs",
        )
    return id_map
