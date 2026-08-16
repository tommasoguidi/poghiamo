"""Source adapters: parsing real API shapes, Italy-only filtering, rockol parser."""

import datetime as dt
from pathlib import Path

from poghiamo.sources.base import ArtistRef, resolve_area
from poghiamo.sources.dice import DiceAdapter
from poghiamo.sources.rockol import parse_rockol_html
from poghiamo.sources.ticketsms import TicketSmsAdapter, _slug

_FIXTURES = Path(__file__).parent / "fixtures"

ARTIST = ArtistRef(id=1, name="Faccianuvola", name_normalized="faccianuvola")


# --- TicketSms (real JSON shapes captured 2026-08-15) ---


def _tsms_response(payload):
    class R:
        status_code = 200

        def json(self):
            return payload

        def raise_for_status(self):
            pass

    return R()


def test_ticketsms_slug():
    assert _slug("nerissima serpe") == "nerissima-serpe"
    assert _slug("faccianuvola") == "faccianuvola"


def test_ticketsms_parses_real_shapes(monkeypatch):
    routes = {
        "/performers/nerissima-serpe": {"data": {"id": 20, "code": "nerissima-serpe"}},
        "/performers/nerissima-serpe/events?perPage=50": {
            "data": {"items": [{"code": "MT2XJ8", "type": "EVENT"}]}
        },
        "/events/MT2XJ8": {
            "data": {
                "code": "MT2XJ8",
                "name": "Nerissima Serpe - Live",
                "dateEvent": {"date": "2026-10-15T19:00:00+00:00"},
                "locationId": 6924,
            }
        },
        "/locations/6924": {
            "data": {
                "name": "Teatro Concordia",
                "city": "Venaria Reale",
                "province": "TO",
                "latitude": "45.1165723",
                "longitude": " 7.625048",
            }
        },
    }
    adapter = TicketSmsAdapter()
    monkeypatch.setattr(adapter, "_get", lambda path: _tsms_response(routes[path]))

    events = adapter.fetch(ArtistRef(id=2, name="Nerissima Serpe", name_normalized="nerissima serpe"))
    assert len(events) == 1
    e = events[0]
    assert e.source == "ticketsms"
    assert e.date == dt.date(2026, 10, 15)
    assert e.time == dt.time(19, 0)
    assert e.venue == "Teatro Concordia"
    assert e.city == "Venaria Reale"
    assert e.province == "TO"
    assert abs(e.lat - 45.1165723) < 1e-6
    assert abs(e.lon - 7.625048) < 1e-6


def test_ticketsms_unknown_performer_returns_empty(monkeypatch):
    class R404:
        status_code = 404

        def raise_for_status(self):
            pass

    adapter = TicketSmsAdapter()
    monkeypatch.setattr(adapter, "_get", lambda path: R404())
    assert adapter.fetch(ARTIST) == []


# --- DICE (trimmed real __NEXT_DATA__ fixture, one IT + one FR event) ---


def test_dice_parses_next_data_and_filters_to_italy(monkeypatch):
    html = (_FIXTURES / "dice_artist.html").read_text()

    class RGet:
        text = html

        def raise_for_status(self):
            pass

    class RPost:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "sections": [
                    {"items": [{"type": "artist", "artist": {"name": "Faccianuvola", "slug": "faccianuvola-8yaw5"}}]}
                ]
            }

    import poghiamo.sources.dice as dice_mod

    monkeypatch.setattr(dice_mod.requests, "post", lambda *a, **k: RPost())
    monkeypatch.setattr(dice_mod.requests, "get", lambda *a, **k: RGet())

    events = DiceAdapter().fetch(ARTIST)
    # Only the IT event survives the country filter
    assert len(events) == 1
    e = events[0]
    assert e.source == "dice"
    assert e.city == "Milano"
    assert e.venue == "Circolo Magnolia"
    assert e.date == dt.date(2026, 9, 17)
    assert e.lat == 45.45 and e.lon == 9.28


def test_dice_no_slug_match_returns_empty(monkeypatch):
    class RPost:
        def raise_for_status(self):
            pass

        def json(self):
            return {"sections": [{"items": [{"type": "artist", "artist": {"name": "Someone Else", "slug": "x"}}]}]}

    import poghiamo.sources.dice as dice_mod

    monkeypatch.setattr(dice_mod.requests, "post", lambda *a, **k: RPost())
    assert DiceAdapter().fetch(ARTIST) == []


# --- rockol (parser validated against real HTML captured via ZenRows) ---


def test_rockol_parser_extracts_and_dedupes():
    # Real markup captured 2026-08-16: two events, each duplicated desktop/mobile.
    html = (_FIXTURES / "rockol_search.html").read_text()
    events = parse_rockol_html(html)
    assert len(events) == 2  # deduped from 4 anchors by permalink id

    by_city = {e.city: e for e in events}
    roseto = by_city["Roseto degli Abruzzi"]
    assert roseto.date == dt.date(2026, 8, 22)
    assert roseto.venue == "Lungomare Trento"
    assert roseto.source_event_id == "9zdg4n8o4d0"
    assert roseto.ticket_url.startswith("https://www.rockol.it/concerto-")

    verona = by_city["Verona"]  # no province segment in the title
    assert verona.date == dt.date(2026, 9, 2)
    assert verona.venue == "Arena"

    # City names resolve to province/region via the ISTAT table
    assert resolve_area("Roseto degli Abruzzi", None) == ("TE", "Abruzzo")
    assert resolve_area("Verona", None) == ("VR", "Veneto")


def test_rockol_parser_ignores_non_event_anchors():
    html = '<a href="/some/other/page" title="not an event 2026">x</a>'
    assert parse_rockol_html(html) == []


# --- region resolution (shared) ---


def test_resolve_area_from_province_and_city():
    assert resolve_area(None, "MI") == ("MI", "Lombardia")
    assert resolve_area("Firenze", None) == ("FI", "Toscana")
    # Ambiguous comune name resolves to nothing (can't pick a province safely)
    assert resolve_area("Calliano", None) == (None, None)
    assert resolve_area(None, None) == (None, None)
