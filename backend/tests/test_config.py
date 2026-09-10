"""Configuration parsing and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


class TestListParsing:
    def test_comma_separated_values(self) -> None:
        config = Settings(
            destination_airports="IKA,MHD",
            provider_chain="aerodatabox,flightaware",
            delay_buckets_minutes="15,30,60,120",
        )
        assert config.destination_airports == ["IKA", "MHD"]
        assert config.provider_chain == ["aerodatabox", "flightaware"]
        assert config.delay_buckets_minutes == [15, 30, 60, 120]

    def test_json_array_values(self) -> None:
        assert Settings(provider_chain='["mock"]').provider_chain == ["mock"]

    def test_whitespace_is_tolerated(self) -> None:
        assert Settings(destination_airports=" IKA , MHD ").destination_airports == [
            "IKA",
            "MHD",
        ]

    def test_airport_codes_are_upper_cased(self) -> None:
        config = Settings(origin_airport="ist", destination_airports="ika,mhd")
        assert config.origin_airport == "IST"
        assert config.destination_airports == ["IKA", "MHD"]


class TestDirectionalRoutes:
    """Direction is significant: IST-IKA and IKA-IST are separate routes."""

    def test_explicit_pairs_are_parsed_in_order(self) -> None:
        config = Settings(monitored_routes="IST-IKA,IST-MHD,IKA-IST")
        assert config.routes == [("IST", "IKA"), ("IST", "MHD"), ("IKA", "IST")]

    def test_a_return_leg_is_not_collapsed_into_the_outbound(self) -> None:
        config = Settings(monitored_routes="IST-IKA,IKA-IST")
        assert ("IST", "IKA") in config.routes
        assert ("IKA", "IST") in config.routes
        assert len(config.routes) == 2

    def test_origins_are_distinct_and_ordered(self) -> None:
        config = Settings(monitored_routes="IST-IKA,IST-MHD,IKA-IST,MHD-IST")
        assert config.origins == ["IST", "IKA", "MHD"]

    def test_destinations_are_grouped_per_origin(self) -> None:
        """Filtering per origin is what stops an unconfigured pair being collected."""
        config = Settings(monitored_routes="IST-IKA,IST-MHD,IKA-IST")
        assert config.destinations_by_origin == {
            "IST": ["IKA", "MHD"],
            "IKA": ["IST"],
        }

    def test_lowercase_and_whitespace_are_tolerated(self) -> None:
        config = Settings(monitored_routes=" ist-ika , ika-ist ")
        assert config.routes == [("IST", "IKA"), ("IKA", "IST")]

    def test_duplicates_collapse(self) -> None:
        config = Settings(monitored_routes="IST-IKA,IST-IKA")
        assert config.routes == [("IST", "IKA")]

    def test_route_labels_are_strings(self) -> None:
        assert Settings(monitored_routes="IKA-IST").route_labels() == ["IKA-IST"]

    @pytest.mark.parametrize("bad", ["IST", "IST-", "-IKA", "IST-IST", "ISTANBUL-IKA", "I-IKA"])
    def test_malformed_routes_are_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            Settings(monitored_routes=bad)

    def test_legacy_single_origin_form_still_works(self) -> None:
        """An existing deployment's config must keep working untouched."""
        config = Settings(origin_airport="IST", destination_airports="IKA,MHD")
        assert config.monitored_routes == []
        assert config.routes == [("IST", "IKA"), ("IST", "MHD")]
        assert config.origins == ["IST"]

    def test_explicit_routes_take_precedence_over_the_legacy_form(self) -> None:
        config = Settings(
            monitored_routes="IKA-IST", origin_airport="IST", destination_airports="IKA,MHD"
        )
        assert config.routes == [("IKA", "IST")]


class TestAirportTimezones:
    """AeroDataBox wants each request's window in the ORIGIN airport's local time."""

    def test_each_airport_resolves_its_own_zone(self) -> None:
        from datetime import UTC, datetime

        from app.core.airports import timezone_for

        moment = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        istanbul = moment.astimezone(timezone_for("IST"))
        tehran = moment.astimezone(timezone_for("IKA"))
        # Tehran is UTC+03:30 against Istanbul's UTC+03:00, so a window built in
        # Istanbul time for a Tehran origin is shifted half an hour.
        assert (tehran.hour, tehran.minute) != (istanbul.hour, istanbul.minute)
        assert tehran.utcoffset() != istanbul.utcoffset()

    def test_an_unknown_airport_falls_back_rather_than_raising(self) -> None:
        """A missing entry should degrade, not stop collection."""
        from app.core.airports import timezone_for
        from app.core.config import settings as live

        assert str(timezone_for("XXX")) == live.operational_timezone

    def test_route_labels_name_both_ends_in_order(self) -> None:
        from app.core.airports import route_label

        assert route_label("IST", "IKA") == "Istanbul to Tehran"
        assert route_label("IKA", "IST") == "Tehran to Istanbul"


class TestDerivedValues:
    def test_routes_pair_origin_with_each_destination(self) -> None:
        assert Settings(origin_airport="IST", destination_airports="IKA,MHD").routes == [
            ("IST", "IKA"),
            ("IST", "MHD"),
        ]

    def test_timezone_object_is_usable(self) -> None:
        from datetime import UTC, datetime

        config = Settings(operational_timezone="Europe/Istanbul")
        moment = datetime(2026, 9, 6, 5, 0, tzinfo=UTC).astimezone(config.tz)
        assert moment.hour == 8


class TestValidation:
    def test_unknown_timezone_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(operational_timezone="Mars/Olympus_Mons")

    def test_score_weights_must_sum_to_one(self) -> None:
        with pytest.raises(ValidationError, match="must sum to 1.0"):
            Settings(score_weight_on_time=0.5, score_weight_cancellation=0.5,
                     score_weight_avg_delay=0.5, score_weight_severe_delay=0.5)

    def test_default_weights_are_valid(self) -> None:
        assert round(
            Settings().score_weight_on_time
            + Settings().score_weight_cancellation
            + Settings().score_weight_avg_delay
            + Settings().score_weight_severe_delay,
            6,
        ) == 1.0


class TestStalenessOutlivesTheInterval:
    """A flight must not go stale merely by waiting for the next collection."""

    def test_a_stale_threshold_below_two_intervals_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must exceed twice"):
            Settings(collection_interval_minutes=480, stale_after_minutes=180)

    def test_exactly_two_intervals_is_still_rejected(self) -> None:
        """The threshold has to survive one missed cycle, which is what it is for."""
        with pytest.raises(ValidationError):
            Settings(collection_interval_minutes=60, stale_after_minutes=120)

    def test_a_comfortable_threshold_is_accepted(self) -> None:
        config = Settings(collection_interval_minutes=480, stale_after_minutes=1200)
        assert config.stale_after_minutes == 1200

    def test_the_shipped_defaults_are_consistent(self) -> None:
        config = Settings()
        assert config.stale_after_minutes > config.collection_interval_minutes * 2

    @pytest.mark.parametrize("minutes", [5, 15, 30, 60, 120, 240, 480])
    def test_every_documented_cadence_is_accepted(self, minutes: int) -> None:
        assert (
            Settings(
                collection_interval_minutes=minutes, stale_after_minutes=minutes * 3
            ).collection_interval_minutes
            == minutes
        )

    def test_an_undocumented_cadence_is_still_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(collection_interval_minutes=90, stale_after_minutes=1000)


class TestSecurityDefaults:
    def test_no_credential_has_a_real_default(self) -> None:
        """A shipped default secret is a vulnerability, not a convenience."""
        config = Settings()
        assert config.aerodatabox_api_key is None
        assert config.aviationstack_api_key is None
        assert config.flightaware_api_key is None
        assert config.telegram_bot_token is None
        assert config.telegram_chat_id is None

    def test_mock_provider_is_off_by_default(self) -> None:
        assert Settings().allow_mock_provider is False

    def test_default_chain_uses_a_real_provider(self) -> None:
        assert "mock" not in Settings().provider_chain
