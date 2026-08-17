"""Event source adapters.

Each adapter is a self-contained unit: it knows its own endpoint, rate limit,
timeout and Italy-only filtering, and turns one artist into a list of validated
ScrapedEvent objects. The pipeline fans out to every ENABLED adapter in parallel
and never lets one source's failure break the scan.
"""

from poghiamo.sources.base import ScrapedEvent, SourceAdapter
from poghiamo.sources.dice import DiceAdapter
from poghiamo.sources.rockol import RockolAdapter
from poghiamo.sources.ticketmaster import TicketmasterAdapter

# Registry, in priority order. Instantiated once; each reads its own config to
# decide `enabled`. Adding a source = appending one adapter here.
#
# TicketSms dropped 2026-08-18: its v4 API has no search endpoint, performers are
# reachable only by a slug that isn't derivable from the artist name (the backend
# key differs, events live under per-event URLs), and across the whole catalog it
# yielded exactly one non-unique event (Rares/Scorzè, already covered by rockol).
# The adapter is kept in sources/ticketsms.py for revival if an event-URL path is
# ever built.
ALL_ADAPTERS: list[SourceAdapter] = [
    DiceAdapter(),
    TicketmasterAdapter(),
    RockolAdapter(),
]


def enabled_adapters() -> list[SourceAdapter]:
    return [a for a in ALL_ADAPTERS if a.enabled]


__all__ = ["ScrapedEvent", "SourceAdapter", "ALL_ADAPTERS", "enabled_adapters"]
