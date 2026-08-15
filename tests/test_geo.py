"""Geo reference data: regions, provinces, comuni lookups, area matching."""

from poghiamo import geo


def test_regions_and_provinces_counts():
    assert len(geo.regions()) == 20
    assert len(geo.provinces()) == 107  # current ISTAT set, incl. Sud Sardegna


def test_sud_sardegna_present():
    assert geo.region_of_province()["SU"] == "Sardegna"


def test_provinces_by_region():
    toscana = geo.provinces_by_region()["Toscana"]
    sigle = [s for s, _ in toscana]
    assert "FI" in sigle and "LU" in sigle
    assert len(toscana) == 10


def test_comune_lookup():
    assert geo.provinces_of_comune("Firenze") == ["FI"]
    assert geo.provinces_of_comune("  firenze ") == ["FI"]  # normalized
    assert geo.provinces_of_comune("Atlantide") == []


def test_ambiguous_comune_names_return_all_candidates():
    # "Calliano" exists both in AT and TN
    assert sorted(geo.provinces_of_comune("Calliano")) == ["AT", "TN"]


def test_area_matches_semantics():
    # No preferences: everything matches
    assert geo.area_matches([], [], "Toscana", "FI")
    # Whole region selected
    assert geo.area_matches(["Toscana"], [], "Toscana", "FI")
    assert not geo.area_matches(["Toscana"], [], "Lazio", "RM")
    # Single province selected
    assert geo.area_matches([], ["FI"], "Toscana", "FI")
    assert not geo.area_matches([], ["FI"], "Toscana", "LU")
    # Union of both
    assert geo.area_matches(["Lazio"], ["FI"], "Toscana", "FI")
    assert geo.area_matches(["Lazio"], ["FI"], "Lazio", "RM")
    # Unknown location fields
    assert not geo.area_matches(["Toscana"], ["FI"], None, None)
