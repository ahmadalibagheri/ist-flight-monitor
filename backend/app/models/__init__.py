"""SQLAlchemy models.

Import order matters only for relationship resolution; importing this package
registers every table on ``Base.metadata`` for Alembic.
"""

from app.models.airline import Airline
from app.models.airport import Airport
from app.models.collection import CollectionRun, ProviderHealth
from app.models.flight import Flight, FlightEvent, FlightObservation
from app.models.notification import SentAlert
from app.models.statistics import DailyStatistic, HourlyStatistic

__all__ = [
    "Airline",
    "Airport",
    "CollectionRun",
    "DailyStatistic",
    "Flight",
    "FlightEvent",
    "FlightObservation",
    "HourlyStatistic",
    "ProviderHealth",
    "SentAlert",
]
