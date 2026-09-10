"""Flight data providers.

See :mod:`app.providers.base` for the interface every source implements and
:mod:`app.providers.registry` for configuration-driven selection and failover.
"""

from app.providers.base import (
    FlightDataProvider,
    NormalizedFlight,
    ProviderError,
    ProviderResult,
)
from app.providers.registry import PROVIDER_FACTORIES, ProviderRegistry

__all__ = [
    "PROVIDER_FACTORIES",
    "FlightDataProvider",
    "NormalizedFlight",
    "ProviderError",
    "ProviderRegistry",
    "ProviderResult",
]
