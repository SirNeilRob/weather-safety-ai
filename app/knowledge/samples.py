"""Sample alerts for the demo, the integration tests, and the frontend team.

Each entry carries the **expected** classification next to the input. That is
deliberate: the Integration/Testing role can assert on values instead of
eyeballing a screen, and a regression breaks a test rather than a demo.

Two samples are intentionally mislabelled by the feed (heat and thunderstorm)
so the safety-first escalation rule is visible in the output.
"""

from __future__ import annotations

from typing import Any

SAMPLES: list[dict[str, Any]] = [
    {
        "name": "tornado_emergency",
        "label": "Tornado emergency (Extreme)",
        "expected": {
            "hazard": "tornado",
            "severity": "extreme",
            "product": "warning",
            "evacuation": False,
            "shelter": True,
        },
        "alert": {
            "identifier": "SB-DEMO-001",
            "senderName": "National Weather Service",
            "sent": "2026-11-07T18:42:00-06:00",
            "event": "Tornado Warning",
            "severity": "Extreme",
            "urgency": "Immediate",
            "certainty": "Observed",
            "onset": "2026-11-07T18:45:00-06:00",
            "expires": "2026-11-07T19:30:00-06:00",
            "areaDesc": "Cedar Rapids, Linn County, Iowa",
            "headline": "Tornado Emergency for Cedar Rapids",
            "description": (
                "A large and destructive tornado is confirmed on the ground near "
                "Cedar Rapids moving northeast at 40 mph. This is a life-threatening "
                "situation. Mobile homes will be destroyed."
            ),
            "instruction": "Take shelter now in a basement or interior room. Do not wait.",
        },
    },
    {
        "name": "flash_flood",
        "label": "Flash flood (Severe)",
        "expected": {
            "hazard": "flood",
            "severity": "severe",
            "product": "warning",
            "evacuation": False,
            "shelter": False,
        },
        "alert": {
            "identifier": "SB-DEMO-002",
            "senderName": "National Weather Service",
            "sent": "2026-11-07T14:10:00-06:00",
            "event": "Flash Flood Warning",
            "severity": "Severe",
            "urgency": "Immediate",
            "certainty": "Observed",
            "expires": "2026-11-07T18:00:00-06:00",
            "areaDesc": "Cedar Rapids, Linn County",
            "headline": "Flash flooding reported in Cedar Rapids",
            "description": (
                "Four inches of rain have fallen in the last three hours. "
                "Flash flooding is occurring on low-lying roads and underpasses. "
                "Water is rising quickly near the river."
            ),
            "instruction": "Move to higher ground. Do not drive through flood water.",
        },
    },
    {
        "name": "heat_understated",
        "label": "Excessive heat, understated severity (escalates)",
        "expected": {
            "hazard": "heat",
            "severity": "extreme",
            "product": "warning",
            "escalated": True,
        },
        "alert": {
            "identifier": "SB-DEMO-003",
            "senderName": "County Emergency Management",
            "sent": "2026-11-06T11:00:00-06:00",
            "event": "Excessive Heat Warning",
            "severity": "Moderate",
            "urgency": "Expected",
            "certainty": "Likely",
            "expires": "2026-11-06T20:00:00-06:00",
            "areaDesc": "Polk County, Iowa",
            "headline": "Dangerous heat continues",
            "description": (
                "Heat index values up to 112 F are expected this afternoon. "
                "Overnight lows will remain above 80 F. Heat illness is likely "
                "for anyone without air conditioning."
            ),
            "instruction": "Drink water often and check on older neighbors.",
        },
    },
    {
        "name": "winter_storm",
        "label": "Winter storm (escalates on wind chill)",
        "expected": {
            "hazard": "winter",
            "severity": "extreme",
            "product": "warning",
        },
        "alert": {
            "identifier": "SB-DEMO-004",
            "senderName": "National Weather Service",
            "sent": "2026-11-05T05:00:00-06:00",
            "event": "Winter Storm Warning",
            "severity": "Severe",
            "urgency": "Expected",
            "certainty": "Likely",
            "expires": "2026-11-06T12:00:00-06:00",
            "areaDesc": "Story County, Iowa",
            "headline": "Heavy snow and dangerous wind chill",
            "description": (
                "Eight inches of snow are expected. Wind chill values as low as "
                "-20 F are likely overnight. Frostbite can occur in 30 minutes."
            ),
            "instruction": "Avoid travel. Keep blankets and water in your vehicle.",
        },
    },
    {
        "name": "wildfire_evacuation",
        "label": "Wildfire with evacuation warning (Severe)",
        "expected": {
            "hazard": "wildfire",
            "severity": "severe",
            "product": "warning",
            "evacuation": True,
        },
        "alert": {
            "identifier": "SB-DEMO-005",
            "senderName": "County Sheriff",
            "sent": "2026-11-04T16:20:00-07:00",
            "event": "Evacuation Warning",
            "severity": "Severe",
            "urgency": "Immediate",
            "certainty": "Observed",
            "expires": "2026-11-05T08:00:00-07:00",
            "areaDesc": "Boulder County foothills, Colorado",
            "headline": "Brush fire moving toward homes",
            "description": (
                "A fast-moving brush fire is burning near the foothills. "
                "An evacuation order has been issued for zones 3 and 4. "
                "Smoke is reducing visibility on the highway."
            ),
            "instruction": "Leave immediately if you are in zone 3 or 4.",
        },
    },
    {
        "name": "thunderstorm_minor",
        "label": "Thunderstorm, understated severity (warning floor lifts it to moderate)",
        "expected": {
            "hazard": "thunderstorm",
            "severity": "moderate",
            "product": "warning",
            "escalated": True,
        },
        "alert": {
            "identifier": "SB-DEMO-006",
            "senderName": "National Weather Service",
            "sent": "2026-11-03T17:05:00-06:00",
            "event": "Severe Thunderstorm Warning",
            "severity": "Minor",
            "urgency": "Immediate",
            "certainty": "Radar Indicated",
            "expires": "2026-11-03T18:00:00-06:00",
            "areaDesc": "Johnson County, Iowa",
            "headline": "Storms with gusty winds",
            "description": (
                "Storms are moving through the county with 45 mph wind gusts and "
                "frequent lightning. Heavy rain may cause brief ponding on roads."
            ),
            "instruction": "Move indoors and stay away from windows.",
        },
    },
    {
        "name": "earthquake",
        "label": "Earthquake (Severe)",
        "expected": {
            "hazard": "earthquake",
            "severity": "severe",
            "product": None,
        },
        "alert": {
            "identifier": "SB-DEMO-007",
            "senderName": "USGS",
            "sent": "2026-11-02T22:14:00Z",
            "event": "Earthquake",
            "severity": "Severe",
            "urgency": "Immediate",
            "certainty": "Observed",
            "areaDesc": "Central Oklahoma",
            "headline": "Magnitude 6.4 earthquake",
            "description": (
                "A magnitude 6.4 earthquake occurred 12 miles northeast of the city. "
                "Aftershocks are expected for several hours. Damage to older "
                "buildings has been reported."
            ),
            "instruction": "Drop, cover, and hold on. Expect aftershocks.",
        },
    },
    {
        "name": "air_quality",
        "label": "Air quality alert (Moderate)",
        "expected": {
            "hazard": "air_quality",
            "severity": "moderate",
            "product": "alert",
        },
        "alert": {
            "identifier": "SB-DEMO-008",
            "senderName": "State Air Quality Board",
            "sent": "2026-11-01T09:00:00-06:00",
            "event": "Air Quality Alert",
            "severity": "Moderate",
            "urgency": "Expected",
            "certainty": "Likely",
            "areaDesc": "Des Moines metro",
            "headline": "Unhealthy air for sensitive groups",
            "description": (
                "Fine particulate levels are elevated across the metro. "
                "People with asthma or heart conditions should limit time outdoors."
            ),
            "instruction": "Keep windows closed and avoid outdoor exercise.",
        },
    },
    {
        "name": "unclassified_event",
        "label": "Unclassified alert, no severity stated",
        "expected": {
            "hazard": "other",
            "severity": "unknown",
            "product": None,
            "assumed": True,
        },
        "alert": {
            "identifier": "SB-DEMO-009",
            "senderName": "County Emergency Management",
            "event": "Local Notice",
            "urgency": "Unknown",
            "certainty": "Unknown",
            "areaDesc": "Downtown district",
            "description": (
                "Local authorities have issued an emergency notice for the downtown "
                "area. Details are still being confirmed."
            ),
        },
    },
    {
        "name": "spanish_flood",
        "label": "Aviso en español (flood, Severe)",
        "expected": {
            "hazard": "flood",
            "severity": "severe",
            "language": "es",
        },
        "alert": {
            "identifier": "SB-DEMO-010",
            "senderName": "Servicio Nacional de Meteorología",
            "event": "Aviso de inundación",
            "severity": "Severe",
            "urgency": "Immediate",
            "certainty": "Observed",
            "areaDesc": "San Juan, Puerto Rico",
            "language": "es",
            "description": (
                "El río se ha desbordado y el agua sube rápidamente por las calles "
                "del centro. Varias vías están cerradas."
            ),
            "instruction": "Vaya a terreno elevado de inmediato.",
        },
    },
]
