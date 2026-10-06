"""Tests for timestamp parsing and normalisation."""

from datetime import datetime, timezone

from src.timeutil import normalise_timestamp, parse_timestamp, to_utc_z


class TestParseTimestamp:
    def test_parses_rfc822_from_feeds(self):
        dt = parse_timestamp("Tue, 14 Jul 2026 00:00:00 GMT")
        assert dt == datetime(2026, 7, 14, tzinfo=timezone.utc)

    def test_parses_iso_with_offset(self):
        dt = parse_timestamp("2026-09-01T22:34:25.031038+00:00")
        assert dt.year == 2026 and dt.tzinfo is not None

    def test_parses_iso_with_z(self):
        assert parse_timestamp("2026-09-01T22:00:00Z") == datetime(
            2026, 9, 1, 22, tzinfo=timezone.utc
        )

    def test_naive_is_assumed_utc(self):
        assert parse_timestamp("2026-09-01T22:00:00").tzinfo == timezone.utc

    def test_non_utc_offset_is_converted(self):
        # 18:00 EDT is 22:00 UTC — the conversion, not the wall clock, is what matters.
        assert parse_timestamp("2026-09-01T18:00:00-04:00") == datetime(
            2026, 9, 1, 22, tzinfo=timezone.utc
        )

    def test_unparseable_returns_none(self):
        assert parse_timestamp("not a date") is None

    def test_empty_and_none_return_none(self):
        assert parse_timestamp("") is None
        assert parse_timestamp(None) is None


class TestOrderingRegression:
    """The bug this module exists to prevent.

    RFC 822 and ISO-8601 are not comparable as strings: 'T' sorts after '2',
    so every RFC 822 value beats every ISO one regardless of the real instant.
    """

    def test_string_max_picks_the_wrong_one(self):
        older_rfc = "Tue, 14 Jul 2026 00:00:00 GMT"
        newer_iso = "2026-09-01T23:00:00+00:00"
        assert max(older_rfc, newer_iso) == older_rfc  # wrong, and why we parse

    def test_parsed_max_picks_the_right_one(self):
        older_rfc = "Tue, 14 Jul 2026 00:00:00 GMT"
        newer_iso = "2026-09-01T23:00:00+00:00"
        assert max(parse_timestamp(older_rfc), parse_timestamp(newer_iso)) == parse_timestamp(
            newer_iso
        )


class TestNormalise:
    def test_rfc822_becomes_z_suffixed_iso(self):
        assert normalise_timestamp("Tue, 14 Jul 2026 06:12:00 GMT") == "2026-07-14T06:12:00Z"

    def test_already_canonical_is_unchanged(self):
        assert normalise_timestamp("2026-07-14T06:12:00Z") == "2026-07-14T06:12:00Z"

    def test_unparseable_becomes_none(self):
        assert normalise_timestamp("garbage") is None

    def test_to_utc_z_format(self):
        assert to_utc_z(datetime(2026, 7, 14, 6, 12, tzinfo=timezone.utc)) == (
            "2026-07-14T06:12:00Z"
        )
