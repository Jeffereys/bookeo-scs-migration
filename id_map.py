"""
Bookeo <-> SCS Gateway ID correlation table.

Bookeo's bookingNumber/customerId and SCS's confirmationNumber/uniqueId are
unrelated ID spaces (see ../Project_details.txt) -- nothing about either
system lets you ask "did I already push this Bookeo booking to SCS?". This
module is that missing lookup: a small JSON-file-backed store keyed by
Bookeo bookingNumber, so a migration run (including a crashed/retried one)
can check before booking and never create a duplicate SCS reservation for
the same Bookeo booking.

Writes are atomic (write to a temp file, then os.replace) and every
mutating call saves immediately -- losing an already-recorded mapping is
exactly the failure mode this file exists to prevent (lost mapping -> next
run thinks the booking was never migrated -> re-books it -> duplicate
reservation in SCS).

Usage:
    from id_map import IdMap

    id_map = IdMap()  # defaults to id_map.json next to this file
    if id_map.is_migrated(booking["bookingNumber"]):
        continue  # already pushed, skip

    booked = reservations.book_reservation(...)
    id_map.record(
        booking["bookingNumber"],
        confirmation_number=booked["confirmationNumber"],
        unique_id=booked.get("uniqueId"),
        site_unique_id=booked.get("siteUniqueId"),
        status=booked.get("status", "booked"),
        bookeo_customer_id=customer["id"],
    )
"""

import json
import os
import tempfile
from datetime import datetime, timezone

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "id_map.json")


class IdMap:
    def __init__(self, path=None):
        self.path = path or DEFAULT_PATH
        self._entries = self._load()

    # ------------------------------------------------------------------
    def _load(self):
        if not os.path.exists(self.path):
            return {}
        with open(self.path) as f:
            return json.load(f)

    def _save(self):
        directory = os.path.dirname(self.path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".id_map_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._entries, f, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    # ------------------------------------------------------------------
    def is_migrated(self, bookeo_booking_number):
        """True if this Bookeo booking already has a live (non-cancelled)
        SCS reservation recorded."""
        entry = self._entries.get(bookeo_booking_number)
        return entry is not None and entry.get("status") != "cancelled"

    def get(self, bookeo_booking_number):
        return self._entries.get(bookeo_booking_number)

    def record(
        self,
        bookeo_booking_number,
        confirmation_number,
        unique_id=None,
        site_unique_id=None,
        status="booked",
        bookeo_customer_id=None,
        **extra,
    ):
        """Save/overwrite the mapping for one Bookeo booking and persist
        immediately."""
        self._entries[bookeo_booking_number] = {
            "confirmation_number": confirmation_number,
            "unique_id": unique_id,
            "site_unique_id": site_unique_id,
            "status": status,
            "bookeo_customer_id": bookeo_customer_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **extra,
        }
        self._save()

    def update_status(self, bookeo_booking_number, status):
        """E.g. mark a temporary hold 'booked' once converted, or
        'cancelled' after cancel_reservation()."""
        entry = self._entries.get(bookeo_booking_number)
        if entry is None:
            raise KeyError(f"No mapping for Bookeo booking {bookeo_booking_number!r}")
        entry["status"] = status
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def all(self):
        return dict(self._entries)

    def __len__(self):
        return len(self._entries)

    def __contains__(self, bookeo_booking_number):
        return bookeo_booking_number in self._entries


# ---------------------------------------------------------------------
# CLI: inspect the map without writing a throwaway script each time
# ---------------------------------------------------------------------
def _main():
    import sys

    id_map = IdMap()
    args = sys.argv[1:]

    if not args or args[0] == "list":
        if not len(id_map):
            print(f"{id_map.path}: empty (no mappings recorded yet)")
        for booking_number, entry in sorted(id_map.all().items()):
            print(f"{booking_number} -> {entry['confirmation_number']} [{entry['status']}]")
    elif args[0] == "show" and len(args) == 2:
        entry = id_map.get(args[1])
        if entry is None:
            print(f"No mapping for Bookeo booking {args[1]!r}")
            sys.exit(1)
        print(json.dumps(entry, indent=2))
    else:
        print("Usage: python id_map.py [list | show <bookeo_booking_number>]")
        sys.exit(1)


if __name__ == "__main__":
    _main()
