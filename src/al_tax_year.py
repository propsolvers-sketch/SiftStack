"""Compute the current Alabama tax year for property + tax-delinquent lookups.

Alabama statute § 40-10-15 ties the annual tax-lien auction to the
first Tuesday of May. The "current" delinquent roster is the prior tax
year up until that date; after the auction, queries shift to the new
year. Hardcoded `year=2025` defaults across the county adapters were
correct on 2026-04-30 but rot silently every May.

Usage:

    from al_tax_year import current_al_tax_year

    def fetch_delinquent_parcels(year: int | None = None) -> list[...]:
        if year is None:
            year = current_al_tax_year()
        ...

The helper is split out into its own module (rather than living in
config.py or tax_distress_pipeline.py) so the four county adapter files
can import it without dragging the broader config / pipeline graph.
"""
from __future__ import annotations

from datetime import date, timedelta


def _first_tuesday_of_may(year: int) -> date:
    """First Tuesday of May for the given year (the AL tax-sale auction)."""
    may1 = date(year, 5, 1)
    # Monday=0, Tuesday=1, ..., Sunday=6
    days_until_tuesday = (1 - may1.weekday()) % 7
    return may1 + timedelta(days=days_until_tuesday)


def current_al_tax_year(today: date | None = None) -> int:
    """Return the tax year that applies to today's queries.

    Before the first Tuesday of May, the active delinquent roster is
    *last* year's tax bill (e.g. on 2026-04-30 the active year is 2025).
    On or after the first Tuesday of May, the new auction has happened
    and queries roll forward to the current calendar year (e.g. on
    2026-05-06 the active year is 2026).

    The result is the year value that callers should pass to county tax
    APIs as their `year` parameter.
    """
    if today is None:
        today = date.today()
    auction = _first_tuesday_of_may(today.year)
    if today < auction:
        return today.year - 1
    return today.year
