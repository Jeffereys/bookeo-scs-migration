"""
Bookeo -> SCS Gateway field-transform layer.

Pure functions that turn Bookeo booking/customer shapes (see
bookeo/bookeo_api.py) into the field names/formats SCS's Reservations
Gateway expects (see Reserve/reservations.py). Nothing below the "Read-only
demo" section calls either API -- run_sample() pulls live SCS field options
to validate the static maps against, and prints transformed rows, but makes
no writes to either system.

Findings this encodes (full writeup in ../Project_details.txt):
  - Bookeo's startTime/endTime already carry a venue-local UTC offset; SCS
    wants reservationDate ("MM/DD/YYYY") and time ("h:mm AM/PM") as
    separate strings, no timezone conversion needed.
  - Bookeo splits headcount by category (adults/children/...); SCS wants a
    single partySize int. We sum for partySize and keep the breakdown in
    comments since SCS has nowhere else to put it.
  - Bookeo's per-booking options[] (add-ons: shoe sizes, pizza, drinks,
    game cards) don't correspond to any SCS serviceOptions/serviceDetails
    configured for this site -- folded into comments instead of dropped.
  - SCS's reservationTypes/assetDetailTypes/paymentMethods/paymentTypes
    were empty for the Alley Cats Burleson site as of 2026-08-20 -- until
    someone populates them in SCS, REQUEST_MAP below is the only mapping
    with anywhere to land. map_requests() validates against a fresh pull
    rather than trusting the static map blindly.
  - Bookeo doesn't capture a cancellation reason; SCS requires one from a
    fixed list -- DEFAULT_CANCELLATION_REASON below.
  - Duplicate Bookeo customer records for the same person are common
    (confirmed against live data) -- dedupe_customers() groups by
    (email, phone) before contact import.

Not handled here (out of scope for the transform layer itself): the
Bookeo-id <-> SCS-confirmationNumber correlation table needed once bookings
actually get pushed, and any SCS-side admin config work.
"""

from dataclasses import dataclass
from datetime import datetime


# ---------------------------------------------------------------------
# Venues
# ---------------------------------------------------------------------
# One Bookeo account per Alley Cats venue (confirmed: the Burleson account's
# /settings/business is "Alley Cats Burleson", one resource, no venue field on
# bookings). Each venue is pulled from its own Bookeo credentials and pushed
# to its own SCS site. Everything else -- the "Bookeo Import" Event Type, the
# "Online Bookings" salesperson, the OPTION_HOLD_5 status, "Miscellaneous"
# function type -- is shared (all Master List / cross-site in SCS, verified
# via a live GetEventLocationOptions pull on all three sites).
@dataclass(frozen=True)
class Venue:
    key: str                       # CLI argument / dict key
    scs_site_name: str             # function.event.site
    event_location: str            # function.locations
    bookeo_env_prefix: str         # {prefix}_API_KEY / {prefix}_SECRET_KEY in the environment
    marker_prefix: str             # function.event.interfaceAccountId prefix -- MUST be venue-unique
    id_map_filename: str           # per-venue idempotency cache (committed, next to this file)
    salesperson_username: str = "bookings"  # shared "Online Bookings" SCS user for now


VENUES = {
    "burleson": Venue(
        key="burleson",
        scs_site_name="Alley Cats Entertainment, Burleson",
        event_location="Bowling Lanes",
        bookeo_env_prefix="BOOKEO",              # existing BOOKEO_API_KEY / BOOKEO_SECRET_KEY
        marker_prefix="BKO:",                    # unchanged -- the ~78+ live Events carry this
        id_map_filename="event_id_map.burleson.json",
    ),
    "hurst": Venue(
        key="hurst",
        scs_site_name="Alley Cats Entertainment, Hurst",
        # Hurst has no "Bowling Lanes" Location -- options are Lower Lanes /
        # Upper Lanes / Party Room / Conference Room / Other. Function type
        # stays "Miscellaneous" (no asset binding), so this is only a label.
        # TODO: confirm the right one with Alley Cats before enabling Hurst.
        event_location="Lower Lanes",
        bookeo_env_prefix="BOOKEO_HURST",
        marker_prefix="BKO:ACEH:",
        id_map_filename="event_id_map.hurst.json",
    ),
    "arlington": Venue(
        key="arlington",
        scs_site_name="Alley Cats Entertainment, Arlington",
        event_location="Bowling Lanes",
        bookeo_env_prefix="BOOKEO_ARLINGTON",
        marker_prefix="BKO:ARL:",
        id_map_filename="event_id_map.arlington.json",
    ),
}

DEFAULT_VENUE = VENUES["burleson"]


def get_venue(key):
    """Look up a Venue by key, with a clear error for a bad CLI argument."""
    try:
        return VENUES[key]
    except KeyError:
        raise ValueError(f"unknown venue {key!r}; expected one of {sorted(VENUES)}")


SITE_NAME = DEFAULT_VENUE.scs_site_name  # kept for push.py (Reservations, Burleson-only)

# Bookeo productName (matched as a lowercase substring) -> SCS `requests`
# values. Only "Bowling" is configured on the SCS side for this site as of
# 2026-08-20, so everything currently falls through to it.
REQUEST_MAP = {
    "bowling": ["Bowling"],
}
DEFAULT_REQUEST = ["Bowling"]

DEFAULT_CANCELLATION_REASON = "Other"

# ---------------------------------------------------------------------
# Events (see transform_booking_to_event) -- the company doesn't use the
# Reservations calendar operationally, only the Events calendar, so
# push_events.py pushes bookings here instead of as Reservations.
# ---------------------------------------------------------------------
EVENT_SITE_NAME = SITE_NAME  # legacy alias; use venue.scs_site_name

# Placeholder: confirmed via a live GetEventLocationOptions pull on
# 2026-08-26 that this account has a "Bowling Lanes" Location (up to 24
# concurrent events) but NO Function Type scoped to it or to the "Bowling"
# Activity Type -- every Function Type configured (Bar Service, Catering,
# Group Package, Meeting, Miscellaneous, ...) has activityTypeUniqueId=null
# and locationUniqueIds=[]. Until an SCS admin creates a "Bowling" Function
# Type tied to the "Bowling Lanes" Location, pushed bookings will show up
# under "Miscellaneous" rather than anything bowling-specific.
EVENT_LOCATION = DEFAULT_VENUE.event_location  # legacy alias; use venue.event_location
EVENT_FUNCTION_TYPE = "Miscellaneous"  # shared across venues

# Event Status for pushed bookings (function.event.lifecycleState.stateType).
# This is the state's *stateType* (the UPPER_SNAKE enum name from
# GetEventLifecycleModels), NOT the display name -- the gateway rejects
# "Definite" but accepts "DEFINITE", same as "OPTION_HOLD_5" before it. The
# state must also exist in the lifecycle model the gateway's events use (the
# Burleson Standard Lifecycle model). Run push in the default mode="test"
# after any change here -- a bad value comes back as a Failed row, not a bad
# write.
#
# History:
#   - "New" (the model's first state) through 2026-08-31.
#   - "OPTION_HOLD_5" 2026-08-31..2026-09-09: a Prospect-phase, 0%-probability
#     state an SCS admin added to the Burleson Standard Lifecycle model, to
#     give migrated events a distinct calendar color (#DACCEC lavender), since
#     the Events calendar colors by lifecycle status, not by Event Type.
#   - "DEFINITE" briefly on 2026-09-09: modeled the bookings as the confirmed
#     bookings they are and stopped closes reading as "lost business" -- but
#     Definite renders green on the calendar, same as staff-created events,
#     losing the visual cue.
#   - back to "OPTION_HOLD_5" from 2026-09-09 to keep the lavender cue. The
#     lost-business-on-close prompt is being addressed another way (mailing
#     address on the contact / Manage Task Defaults) rather than by moving
#     off the colored status.
EVENT_STATUS = "OPTION_HOLD_5"

# Event Type for pushed bookings (function.event.eventType). A dedicated type
# ("Bookeo Import") created under Settings > Events > Event Types -- the clean
# hook for staff and reports to filter migrated bookings apart from
# hand-entered events (the calendar color itself comes from EVENT_STATUS, not
# from the type). Must exist in SCS before a push -- an unknown type comes
# back as a Failed row.
EVENT_TYPE = "Bookeo Import"

# Salesperson for pushed bookings. SCS correspondence (confirmations, close
# notices, ...) sends *from* the event's salesperson. With owner/salesperson
# omitted the gateway assigns its own agent, which has no real mailbox -- so
# those emails can't go out and staff are forced to clear the
# send-correspondence box on every close. "Online Bookings" is a dedicated
# SCS user (Burleson) for exactly this. Salesperson only -- owner still
# defaults to the gateway agent, whose Ownership Group (Level 3A) is what
# gives staff edit access to these events.
#
# Matched by username, not email: the gateway rejects an email that resolves
# to more than one User ("Multiple salespersons found."), and this user's
# monitored inbox is shared with a real person. Field is
# function.event.salesperson.username on the create path,
# event.salesperson.username on the update path (both confirmed against
# Manage Event ... Gateway Put Requests). A gateway default salesperson is
# also set under Manage Event and Function Gateway Put Request Settings as a
# backstop.
EVENT_SALESPERSON_USERNAME = "bookings"

# Preference order when picking the one mobilePhone SCS wants out of
# Bookeo's typed phoneNumbers[] list.
PHONE_TYPE_PRIORITY = ["mobile", "cell", "home", "work", "other"]

# The Bookeo booking number is stamped onto every migrated Event in
# function.event.interfaceAccountId, prefixed per venue (see Venue.marker_prefix),
# so the migration can ask SCS "did I already import this booking?" instead of
# trusting a local file. interfaceAccountId was verified unused on this account
# (null on all ~12k events, 2026-08-31) and its value round-trips through the
# BookeoMigration_Lookup Event Gateway Get Request. That Get Request can't
# filter on interfaceAccountId server-side, so the read-back sweeps a startDate
# window and matches the venue prefix locally -- see migration/event_lookup.py.
# Each venue has its own prefix because booking numbers from different Bookeo
# accounts could collide: Burleson "BKO:123", Hurst "BKO:ACEH:123".
BOOKEO_MARKER_PREFIX = DEFAULT_VENUE.marker_prefix  # legacy alias


def bookeo_marker(bookeo_booking_number, venue=DEFAULT_VENUE):
    """The function.event.interfaceAccountId value for a Bookeo booking."""
    return f"{venue.marker_prefix}{bookeo_booking_number}"


def parse_bookeo_marker(interface_account_id, venue=DEFAULT_VENUE):
    """Inverse of bookeo_marker(): the Bookeo booking number out of an
    interfaceAccountId value, or None if it isn't one of `venue`'s markers.
    A booking number is all digits, so a longer-prefixed marker from another
    venue (e.g. Hurst's "BKO:ACEH:123" read with Burleson's "BKO:" prefix)
    leaves a non-numeric remainder and is correctly rejected."""
    if not interface_account_id or not interface_account_id.startswith(venue.marker_prefix):
        return None
    rest = interface_account_id[len(venue.marker_prefix):]
    return rest if rest.isdigit() else None


# ---------------------------------------------------------------------
# Date / time
# ---------------------------------------------------------------------
def split_bookeo_datetime(iso_ts):
    """
    '2026-07-21T12:00:00-05:00' -> ('07/21/2026', '12:00 PM')

    Bookeo's timestamp already carries the venue-local UTC offset, so no
    timezone conversion is needed here -- just parse and reformat.
    """
    dt = datetime.fromisoformat(iso_ts)
    return dt.strftime("%m/%d/%Y"), dt.strftime("%I:%M %p").lstrip("0")


# ---------------------------------------------------------------------
# Party size
# ---------------------------------------------------------------------
def aggregate_party_size(participants):
    """
    Bookeo's participants.numbers is category-split
    ([{"peopleCategoryId": "Cadults", "number": 2}, ...]); SCS wants one
    partySize int. Returns the sum across categories.
    """
    numbers = (participants or {}).get("numbers", [])
    return sum(n.get("number", 0) for n in numbers)


def format_party_breakdown(participants):
    """Human-readable category breakdown, for comments since SCS's
    partySize can't hold the adult/child split."""
    numbers = (participants or {}).get("numbers", [])
    if not numbers:
        return ""
    return ", ".join(f"{n.get('number')} {n.get('peopleCategoryId')}" for n in numbers)


# ---------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------
def pick_mobile_phone(phone_numbers):
    """Pick one phone string from Bookeo's typed phoneNumbers[] list,
    preferring mobile/cell, falling back to whatever's first."""
    if not phone_numbers:
        return None
    by_type = {p.get("type"): p.get("number") for p in phone_numbers if p.get("number")}
    for t in PHONE_TYPE_PRIORITY:
        if t in by_type:
            return by_type[t]
    return phone_numbers[0].get("number")


def dedupe_customers(customers):
    """
    Collapse Bookeo customer records that share (email, phone) -- Bookeo is
    known to create duplicate customer records for the same real person
    (confirmed against live data 2026-08-20). Keeps the record with the
    most bookings; ties keep whichever has the earliest creationTime.
    """
    groups = {}
    for c in customers:
        email = (c.get("emailAddress") or "").strip().lower()
        phone = pick_mobile_phone(c.get("phoneNumbers")) or ""
        groups.setdefault((email, phone), []).append(c)

    return [
        max(group, key=lambda c: (c.get("numBookings", 0), c.get("creationTime", "")))
        for group in groups.values()
    ]


# Bookeo customer.streetAddress sub-key -> SCS
# function.event.contact.mailingAddress.* field reference (from Settings >
# Events > Manage Event and Function Gateway Put Requests, Contact group).
# SCS uses the mailing address for *printed* correspondence -- without it,
# closing an Event that has a correspondence task can't produce the letter.
# Bookeo's streetAddress is {address1, address2, city, state, postcode,
# countryCode}, any subset, and is often absent entirely for phone / walk-in
# bookings. If a real Bookeo customer with an address still comes through
# with an empty SCS address, check these Bookeo key names against a live
# get_customer() response -- a wrong key here fails silently (no address),
# not loudly.
_CONTACT_ADDRESS_MAP = {
    "address1": "function.event.contact.mailingAddress.address1",
    "address2": "function.event.contact.mailingAddress.address2",
    "city": "function.event.contact.mailingAddress.city",
    "state": "function.event.contact.mailingAddress.state",
    "postcode": "function.event.contact.mailingAddress.zipCode",
    "countryCode": "function.event.contact.mailingAddress.country",
}


def contact_address_fields(customer):
    """
    SCS function.event.contact.mailingAddress.* fields from a Bookeo
    customer's streetAddress. Returns only the parts actually present -- an
    empty dict if the customer has no address. Never raises; a missing
    address is normal and just means no mailing address on the SCS contact.
    """
    address = customer.get("streetAddress") or {}
    return {
        scs_field: str(address[bookeo_key]).strip()
        for bookeo_key, scs_field in _CONTACT_ADDRESS_MAP.items()
        if str(address.get(bookeo_key) or "").strip()
    }


# ---------------------------------------------------------------------
# Add-ons / notes
# ---------------------------------------------------------------------
def format_addons_comment(options):
    """
    Bookeo's per-booking options[] (free-text add-on answers) have no
    matching SCS serviceOptions/serviceDetails for this site -- fold them
    into reservation.comments as readable text instead of dropping them.
    """
    if not options:
        return ""
    lines = [f"{o.get('name')}: {o.get('value')}" for o in options if o.get("value")]
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Reservation "type" / requests
# ---------------------------------------------------------------------
def map_requests(product_name, available_requests=None):
    """
    Bookeo's productName -> SCS `requests` (asset attributes). Case-
    insensitive substring match against REQUEST_MAP; falls back to
    DEFAULT_REQUEST. If available_requests (a fresh
    get_reservation_field_options() pull) is given, raises if the mapped
    value isn't actually configured on the SCS site -- don't trust the
    static map blindly, SCS's own docs say these lists can change.
    """
    name = (product_name or "").lower()
    mapped = DEFAULT_REQUEST
    for key, value in REQUEST_MAP.items():
        if key in name:
            mapped = value
            break

    if available_requests is not None:
        missing = [v for v in mapped if v not in available_requests]
        if missing:
            raise ValueError(
                f"map_requests({product_name!r}) -> {mapped}, but {missing} "
                f"is not in SCS's configured requests {available_requests}. "
                "Either fix REQUEST_MAP or configure the value in SCS."
            )
    return mapped


# ---------------------------------------------------------------------
# Full booking -> SCS field transform
# ---------------------------------------------------------------------
def transform_booking(booking, customer, available_requests=None):
    """
    booking: one item from BookeoAPI.get_bookings()['data']
    customer: the matching item from BookeoAPI.get_customers()['data']
              (matched by booking['customerId'] == customer['id'])
    available_requests: optional list from a fresh
        get_reservation_field_options() pull, to validate map_requests()
        against instead of trusting the static REQUEST_MAP blindly.

    Returns a dict with the fields needed to call
    ReservationsAPI.check_availability() (to get a reservationCode) and
    then book_reservation() with that code. Doesn't call either API itself
    -- this only prepares the Bookeo-side data.
    """
    reservation_date, desired_time_from = split_bookeo_datetime(booking["startTime"])
    party_size = aggregate_party_size(booking.get("participants"))
    breakdown = format_party_breakdown(booking.get("participants"))
    addons = format_addons_comment(booking.get("options"))

    comments = "\n\n".join(
        part for part in (f"Party: {breakdown}" if breakdown else "", addons) if part
    )

    phone = pick_mobile_phone(customer.get("phoneNumbers"))
    if not phone:
        raise ValueError(f"Bookeo customer {customer.get('id')} has no phone number")

    return {
        "bookeo_booking_number": booking["bookingNumber"],
        "bookeo_customer_id": customer.get("id"),
        "reservation_date": reservation_date,
        "desired_time_from": desired_time_from,
        "party_size": party_size,
        "requests": map_requests(booking.get("productName"), available_requests),
        "first_name": customer.get("firstName", ""),
        "last_name": customer.get("lastName", ""),
        "email": customer.get("emailAddress", ""),
        "mobile_phone": phone,
        "comments": comments,
    }


# ---------------------------------------------------------------------
# Full booking -> SCS Event field transform
# ---------------------------------------------------------------------
def transform_booking_to_event(booking, customer, venue=None):
    """
    booking/customer: same shapes as transform_booking().
    venue: a Venue (see VENUES); defaults to Burleson. Sets
        function.event.site, function.locations, the salesperson, and the
        interfaceAccountId marker prefix.

    Returns {"bookeo_booking_number", "bookeo_customer_id", "fields"} where
    `fields` is a dict of EventFunctionImport Field Reference -> value,
    ready for events.EventsAPI.create_event(). Field names are this
    account's actual configured names (see EVENT_* constants' comments and
    events.py's module docstring for how/when they were confirmed) -- do
    not reuse them for a different SCS account without re-checking Settings
    > Events > Manage Event and Function Gateway Put Requests.

    Unlike transform_booking() (Reservations), there's no `available_requests`-
    style live validation here: EVENT_FUNCTION_TYPE is a hardcoded
    placeholder ("Miscellaneous") since this account has no Function Type
    scoped to Bowling yet. Owner/salesperson fields are omitted deliberately
    -- per Infor's Events and Functions doc an omitted owner/salesperson
    (no site default) is assigned to the Gateway Agent making the request,
    i.e. the `api_key` agent. That agent's Ownership Group was moved
    Level 1 -> Level 3A on 2026-09-01 so ordinary event staff can edit these
    Events (SCS computes edit access from the owner's live hierarchy
    position, so it applied to all existing Events too). Do NOT re-raise the
    agent's Ownership Group, and don't add owner.* fields here without a
    reason -- gateway EventFunctionImport only takes owner as
    emailAddress/firstName/lastName and needs a real SCS user.

    function.event.interfaceAccountId carries the venue-prefixed marker (see
    bookeo_marker) -- the migration's idempotency key, read back via
    migration/event_lookup.py.
    """
    venue = venue or DEFAULT_VENUE
    start_date, start_time = split_bookeo_datetime(booking["startTime"])
    _, end_time = split_bookeo_datetime(booking["endTime"])
    party_size = aggregate_party_size(booking.get("participants"))
    breakdown = format_party_breakdown(booking.get("participants"))
    addons = format_addons_comment(booking.get("options"))

    notes = "\n\n".join(
        part for part in (f"Party: {breakdown}" if breakdown else "", addons) if part
    )

    first_name = customer.get("firstName", "")
    last_name = customer.get("lastName", "")
    if not last_name:
        raise ValueError(f"Bookeo customer {customer.get('id')} has no last name")

    phone = pick_mobile_phone(customer.get("phoneNumbers"))
    if not phone:
        raise ValueError(f"Bookeo customer {customer.get('id')} has no phone number")

    fields = {
        "function.event.interfaceAccountId": bookeo_marker(booking["bookingNumber"], venue),
        "function.event.site": venue.scs_site_name,
        "function.event.name": f"{first_name} {last_name}".strip(),
        "function.event.lifecycleState.stateType": EVENT_STATUS,
        "function.event.eventType": EVENT_TYPE,
        "function.event.salesperson.username": venue.salesperson_username,
        "function.event.estimatedAttendance": str(party_size),
        "function.event.contact.firstName": first_name,
        "function.event.contact.lastName": last_name,
        "function.event.contact.email": customer.get("emailAddress", ""),
        "function.event.contact.mobilePhone": phone,
        "function.startDate": start_date,
        "function.startTime": start_time,
        "function.endTime": end_time,
        "function.functionType": EVENT_FUNCTION_TYPE,
        "function.locations": venue.event_location,
        "function.estimatedAttendance": str(party_size),
    }
    fields.update(contact_address_fields(customer))
    if notes:
        fields["function.event.notes"] = notes

    return {
        "bookeo_booking_number": booking["bookingNumber"],
        "bookeo_customer_id": customer.get("id"),
        "fields": fields,
    }


# ---------------------------------------------------------------------
# Read-only demo / validation
# ---------------------------------------------------------------------
def run_sample():
    """
    Pulls a small, real (but read-only) sample from both APIs and runs it
    through transform_booking(), printing the result for manual review.
    Makes NO writes to either system -- see Project_details.txt's Testing
    section for how to actually push a transformed row into SCS safely
    (QA env / mode="test" / temporary_hold=True).
    """
    import sys
    import os

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bookeo"))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Reserve"))
    from bookeo_api import BookeoAPI
    from scs_gateway import SCSGatewayClient
    from reservations import ReservationsAPI

    bookeo = BookeoAPI()
    bookings = bookeo.get_bookings(
        start_time="2026-07-21T00:00:00Z", end_time="2026-08-01T00:00:00Z"
    )["data"][:5]

    scs = SCSGatewayClient()
    reservations = ReservationsAPI(scs)
    options = reservations.get_reservation_field_options(site_names=[SITE_NAME])
    available_requests = options["results"][0]["requests"]

    for booking in bookings:
        customer = bookeo.get_customer(booking["customerId"])
        print(transform_booking(booking, customer, available_requests))


if __name__ == "__main__":
    run_sample()
