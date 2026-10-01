"""The GTFS-RT alert enumerations, by name.

One definition, because two writers use them: ``routers/ingest.py`` accepts
alerts from a service, and ``api/schemas.py`` accepts them from a person. A
value either app would reject has to be a value the other rejects too, or the
same feed publishes fields that only one half of it believes in.

Names rather than the protobuf's integers: they are what the columns store and
what the admin has always shown.
"""

from __future__ import annotations

from typing import Literal, get_args

AlertCause = Literal[
    "UNKNOWN_CAUSE",
    "OTHER_CAUSE",
    "TECHNICAL_PROBLEM",
    "STRIKE",
    "DEMONSTRATION",
    "ACCIDENT",
    "HOLIDAY",
    "WEATHER",
    "MAINTENANCE",
    "CONSTRUCTION",
    "POLICE_ACTIVITY",
    "MEDICAL_EMERGENCY",
    "SPECIAL_EVENT",
]
AlertEffect = Literal[
    "NO_SERVICE",
    "REDUCED_SERVICE",
    "SIGNIFICANT_DELAYS",
    "DETOUR",
    "ADDITIONAL_SERVICE",
    "MODIFIED_SERVICE",
    "OTHER_EFFECT",
    "UNKNOWN_EFFECT",
    "STOP_MOVED",
    "NO_EFFECT",
    "ACCESSIBILITY_ISSUE",
]
AlertSeverity = Literal["UNKNOWN_SEVERITY", "INFO", "WARNING", "SEVERE"]

ALERT_CAUSES: tuple[str, ...] = get_args(AlertCause)
ALERT_EFFECTS: tuple[str, ...] = get_args(AlertEffect)
ALERT_SEVERITIES: tuple[str, ...] = get_args(AlertSeverity)
