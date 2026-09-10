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
