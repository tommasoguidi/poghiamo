"""Italian geography reference: regions, provinces, comuni.

Data: src/poghiamo/data/comuni.json, derived from ISTAT open data via
https://github.com/matteocontrini/comuni-json (current provinces, incl.
Sud Sardegna). Regenerate by re-deriving from that repo when ISTAT reshuffles
municipalities.

Area-preference semantics used across the app: a user selects whole regions
and/or single provinces; an event matches if its region is selected OR its
province is selected; no selection at all means "all of Italy".
"""

import json
from functools import lru_cache
from pathlib import Path

_DATA_FILE = Path(__file__).parent / "data" / "comuni.json"


@lru_cache(maxsize=1)
def _data() -> dict:
    with open(_DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def provinces() -> list[tuple[str, str, str]]:
    """All provinces as (sigla, name, region), sorted by sigla."""
    return [tuple(p) for p in _data()["province"]]


@lru_cache(maxsize=1)
def regions() -> list[str]:
    """The 20 region names, alphabetical."""
    return sorted({region for _, _, region in provinces()})


@lru_cache(maxsize=1)
def provinces_by_region() -> dict[str, list[tuple[str, str]]]:
    """region -> [(sigla, province name), ...] sorted by province name."""
    grouped: dict[str, list[tuple[str, str]]] = {}
    for sigla, name, region in provinces():
        grouped.setdefault(region, []).append((sigla, name))
    for entries in grouped.values():
        entries.sort(key=lambda e: e[1])
    return grouped


@lru_cache(maxsize=1)
def region_of_province() -> dict[str, str]:
    """sigla -> region name."""
    return {sigla: region for sigla, _, region in provinces()}


@lru_cache(maxsize=1)
def _comune_index() -> dict[str, list[str]]:
    """normalized comune name -> [sigla, ...] (names can repeat across provinces)."""
    index: dict[str, list[str]] = {}
    for name, sigla in _data()["comuni"]:
        index.setdefault(name.casefold(), []).append(sigla)
    return index


def provinces_of_comune(name: str) -> list[str]:
    """Province sigle for a comune name (usually one; ambiguous names give more).
    Used by phase 3 to map scraped city names to province/region."""
    return _comune_index().get(name.strip().casefold(), [])


def area_matches(user_regions: list[str], user_provinces: list[str], region: str | None, province: str | None) -> bool:
    """True if an event located in (region, province) falls inside the user's
    selected areas. Empty preferences mean all of Italy."""
    if not user_regions and not user_provinces:
        return True
    if region and region in user_regions:
        return True
    if province and province in user_provinces:
        return True
    return False
