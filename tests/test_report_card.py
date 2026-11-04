"""The monthly report card's grades and months."""

from datetime import date

import pytest

from transit.web import report_card


@pytest.mark.parametrize(
    ("ewt", "letter"),
    [
        (-5, "A"),
        (30, "A"),
        (31, "B"),
        (60, "B"),
        (120, "C"),
        (240, "D"),
        (241, "F"),
        (None, None),
    ],
)
def test_grades(ewt, letter):
    assert report_card.grade(ewt) == letter


def test_month_bounds_cross_the_year():
    assert report_card.bounds("2026-10") == (date(2026, 10, 1), date(2026, 11, 1))
    assert report_card.bounds("2026-12") == (date(2026, 12, 1), date(2027, 1, 1))
    assert report_card._month_before(date(2027, 1, 1)) == "2026-12"
    assert report_card._month_before(date(2026, 11, 1)) == "2026-10"


def test_month_format():
    assert report_card.MONTH.fullmatch("2026-10")
    assert not report_card.MONTH.fullmatch("2026-13")
    assert not report_card.MONTH.fullmatch("26-10")
