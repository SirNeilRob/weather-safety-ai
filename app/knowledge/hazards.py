"""Hazard taxonomy, severity escalation rules, and bilingual safety playbooks.

This is the offline brain. Everything here is data plus small pure functions, so
the Weather / Safety / Communication agents can run with the network cable
pulled and still produce the same answer twice in a row.

Design decisions worth defending in a demo:

* **Severity can only escalate from text evidence.** If a feed says "Minor" but
  the description says "catastrophic flooding", the result is Extreme with the
  conflict recorded. Safety-first, and never silently downgrades.
* **Missing severity is not "safe".** Unknown collapses to a Moderate safety
  tier, because sending no instructions is worse than sending cautious ones.
* **Units are parsed, not guessed.** Wind, rainfall, temperature, hail, and the
  EF scale are read with regexes and mapped through explicit thresholds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------- severity
SEVERITY_ORDER: tuple[str, ...] = ("unknown", "minor", "moderate", "severe", "extreme")
RANK: dict[str, int] = {name: index for index, name in enumerate(SEVERITY_ORDER)}

SEVERITY_LABELS: dict[str, dict[str, object]] = {
    "unknown": {
        "en": "Unknown",
        "es": "Desconocida",
        "color": "#4b5563",
        "rank": 0,
        "advice": "Severity not stated; act with caution.",
        "consejo": "No se indica la severidad; actúe con precaución.",
    },
    "minor": {
        "en": "Minor",
        "es": "Menor",
        "color": "#ca8a04",
        "rank": 1,
        "advice": "Be aware and monitor updates.",
        "consejo": "Manténgase informado y siga las actualizaciones.",
    },
    "moderate": {
        "en": "Moderate",
        "es": "Moderada",
        "color": "#d97706",
        "rank": 2,
        "advice": "Prepare now and be ready to act.",
        "consejo": "Prepárese ahora y esté listo para actuar.",
    },
    "severe": {
        "en": "Severe",
        "es": "Severa",
        "color": "#b91c1c",
        "rank": 3,
        "advice": "Act now; significant impact is likely.",
        "consejo": "Actúe ahora; es probable un impacto significativo.",
    },
    "extreme": {
        "en": "Extreme",
        "es": "Extrema",
        "color": "#6b0f0f",
        "rank": 4,
        "advice": "Act immediately; this is a threat to life.",
        "consejo": "Actúe de inmediato; esto es una amenaza para la vida.",
    },
}

URGENCY_LABELS = {
    "immediate": {"en": "Immediate", "es": "Inmediata"},
    "expected": {"en": "Expected", "es": "Prevista"},
    "future": {"en": "Future", "es": "Futura"},
    "past": {"en": "Past", "es": "Pasada"},
    "unknown": {"en": "Unknown", "es": "Desconocida"},
}

CERTAINTY_LABELS = {
    "observed": {"en": "Observed", "es": "Observada"},
    "likely": {"en": "Likely", "es": "Probable"},
    "possible": {"en": "Possible", "es": "Posible"},
    "unlikely": {"en": "Unlikely", "es": "Improbable"},
    "unknown": {"en": "Unknown", "es": "Desconocida"},
}

# --------------------------------------------------------------------------- hazards
@dataclass(frozen=True)
class Hazard:
    key: str
    en: str
    es: str
    icon: str


HAZARDS: dict[str, Hazard] = {
    "tornado": Hazard("tornado", "Tornado", "Tornado", "tornado"),
    "hurricane": Hazard("hurricane", "Hurricane or tropical storm", "Huracán o tormenta tropical", "hurricane"),
    "flood": Hazard("flood", "Flood", "Inundación", "flood"),
    "wildfire": Hazard("wildfire", "Wildfire", "Incendio forestal", "wildfire"),
    "heat": Hazard("heat", "Extreme heat", "Calor extremo", "heat"),
    "winter": Hazard("winter", "Winter storm or extreme cold", "Tormenta invernal o frío extremo", "winter"),
    "thunderstorm": Hazard("thunderstorm", "Thunderstorm", "Tormenta eléctrica", "thunderstorm"),
    "earthquake": Hazard("earthquake", "Earthquake", "Terremoto", "earthquake"),
    "tsunami": Hazard("tsunami", "Tsunami", "Tsunami", "tsunami"),
    "wind": Hazard("wind", "High wind", "Viento fuerte", "wind"),
    "fog": Hazard("fog", "Dense fog", "Niebla densa", "fog"),
    "air_quality": Hazard("air_quality", "Air quality", "Calidad del aire", "air_quality"),
    "other": Hazard("other", "Other hazard", "Otro peligro", "warning"),
}

#: Weighted keyword evidence. Higher weight = more specific to that hazard.
HAZARD_KEYWORDS: dict[str, dict[str, float]] = {
    "tornado": {
        "tornado": 6.0, "funnel cloud": 5.0, "tornado emergency": 8.0,
        "twister": 4.0, "wall cloud": 3.5, "rotation": 1.5, "supercell": 2.5,
    },
    "hurricane": {
        "hurricane": 6.0, "typhoon": 6.0, "tropical storm": 5.0, "cyclone": 5.0,
        "tropical depression": 3.0, "storm surge": 4.0, "landfall": 3.0,
    },
    "flood": {
        "flash flood": 7.0, "flood": 5.0, "flooding": 5.0, "river flood": 5.0,
        "dam failure": 6.0, "levee": 3.0, "water level": 2.0, "storm surge": 2.0,
    },
    "wildfire": {
        "wildfire": 6.0, "brush fire": 6.0, "forest fire": 6.0, "fire weather": 4.5,
        "red flag warning": 5.0, "evacuation order": 3.0, "smoke": 1.5,
    },
    "heat": {
        "excessive heat": 6.0, "heat wave": 5.5, "heat advisory": 5.0,
        "heat index": 4.0, "triple digit": 3.0, "hyperthermia": 3.0,
    },
    "winter": {
        "winter storm": 6.0, "blizzard": 6.0, "ice storm": 6.0, "freezing rain": 5.0,
        "wind chill": 4.5, "extreme cold": 6.0, "frostbite": 3.0, "snow": 3.0,
    },
    "thunderstorm": {
        "severe thunderstorm": 6.0, "thunderstorm": 4.0, "lightning": 4.0,
        "hail": 3.0, "microburst": 4.0, "downburst": 4.0, "damaging winds": 3.0,
    },
    "earthquake": {
        "earthquake": 6.0, "aftershock": 5.0, "seismic": 4.0,
        "magnitude": 2.5, "richter": 3.0,
    },
    "tsunami": {
        "tsunami": 8.0, "tidal wave": 5.0, "coastal evacuation": 3.0,
    },
    "wind": {
        "high wind": 5.0, "wind advisory": 5.0, "damaging winds": 4.0,
        "gale": 4.0, "wind gust": 3.0, "gust": 1.5,
    },
    "fog": {
        "dense fog": 7.0, "fog": 4.0, "visibility": 1.5,
    },
    "air_quality": {
        "air quality": 7.0, "particulate": 4.5, "ozone": 3.5,
        "pm2.5": 4.0, "haze": 2.0, "smoke": 2.0,
    },
}

#: Exact CAP event strings, used as strong evidence and to raise confidence.
CAP_EVENT_HAZARD: dict[str, str] = {
    "tornado warning": "tornado",
    "tornado watch": "tornado",
    "tornado emergency": "tornado",
    "hurricane warning": "hurricane",
    "hurricane watch": "hurricane",
    "hurricane local statement": "hurricane",
    "tropical storm warning": "hurricane",
    "flash flood warning": "flood",
    "flash flood watch": "flood",
    "flood warning": "flood",
    "flood advisory": "flood",
    "coastal flood warning": "flood",
    "wildfire warning": "wildfire",
    "evacuation immediate": "wildfire",
    "fire weather watch": "wildfire",
    "red flag warning": "wildfire",
    "excessive heat warning": "heat",
    "excessive heat watch": "heat",
    "heat advisory": "heat",
    "winter storm warning": "winter",
    "winter storm watch": "winter",
    "blizzard warning": "winter",
    "ice storm warning": "winter",
    "extreme cold warning": "winter",
    "severe thunderstorm warning": "thunderstorm",
    "severe thunderstorm watch": "thunderstorm",
    "earthquake warning": "earthquake",
    "tsunami warning": "tsunami",
    "tsunami watch": "tsunami",
    "high wind warning": "wind",
    "high wind watch": "wind",
    "wind advisory": "wind",
    "dense fog advisory": "fog",
    "air quality alert": "air_quality",
    "air quality advisory": "air_quality",
}

#: Product type -> (urgency, severity floor). A Warning is not just a louder Watch.
PRODUCT_TYPES: dict[str, dict[str, str]] = {
    "warning": {"urgency": "immediate", "floor": "moderate", "en": "Warning", "es": "Aviso"},
    "emergency": {"urgency": "immediate", "floor": "severe", "en": "Emergency", "es": "Emergencia"},
    "watch": {"urgency": "expected", "floor": "minor", "en": "Watch", "es": "Vigilancia"},
    "advisory": {"urgency": "expected", "floor": "minor", "en": "Advisory", "es": "Aviso preventivo"},
    "statement": {"urgency": "expected", "floor": "minor", "en": "Statement", "es": "Comunicado"},
    "alert": {"urgency": "expected", "floor": "minor", "en": "Alert", "es": "Alerta"},
}


@dataclass(frozen=True)
class SeveritySignal:
    pattern: re.Pattern[str]
    severity: str
    reason: str


def _sig(pattern: str, severity: str, reason: str) -> SeveritySignal:
    return SeveritySignal(re.compile(pattern, re.IGNORECASE), severity, reason)


#: Global impact language. Text evidence may only raise severity, never lower it.
SEVERITY_SIGNALS: tuple[SeveritySignal, ...] = (
    _sig(r"\b(tornado|flash flood|earthquake|tsunami) emergency\b", "extreme", "emergency-level wording"),
    _sig(r"\bcatastrophic\b", "extreme", "catastrophic impact language"),
    _sig(r"\bextremely dangerous\b", "extreme", "extremely dangerous impact language"),
    _sig(r"\bunsurvivable\b", "extreme", "unsurvivable impact language"),
    _sig(r"\btake shelter (now|immediately)\b", "extreme", "immediate shelter instruction"),
    _sig(r"\bevacuate (now|immediately)\b", "extreme", "immediate evacuation instruction"),
    _sig(r"\blife[- ]threatening\b", "severe", "life-threatening impact language"),
    _sig(r"\bdestructive\b", "severe", "destructive impact language"),
    _sig(r"\bconfirmed (tornado|on the ground)\b", "severe", "confirmed ground truth"),
    _sig(r"\bmajor\b", "severe", "major impact language"),
    _sig(r"\bsignificant\b", "moderate", "significant impact language"),
    _sig(r"\bradar indicated\b", "moderate", "radar-indicated detection"),
    _sig(r"\bobserved\b", "moderate", "observed conditions"),
)

# --------------------------------------------------------------------------- numeric evidence
_WIND_RE = re.compile(r"(\d{2,3}(?:\.\d+)?)\s*(?:mph|miles per hour)\b", re.IGNORECASE)
_WIND_KMH_RE = re.compile(r"(\d{2,3}(?:\.\d+)?)\s*(?:km/h|kph)\b", re.IGNORECASE)
_TEMP_F_RE = re.compile(r"(-?\d{2,3}(?:\.\d+)?)\s*(?:°|degrees?\s*)?f\b", re.IGNORECASE)
_TEMP_C_RE = re.compile(r"(-?\d{2,3}(?:\.\d+)?)\s*(?:°|degrees?\s*)?c\b", re.IGNORECASE)
_RAIN_IN_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*(?:inches|inch|in)\b", re.IGNORECASE)
_RAIN_MM_RE = re.compile(r"(\d{1,4}(?:\.\d+)?)\s*mm\b", re.IGNORECASE)
_HAIL_RE = re.compile(r"(\d(?:\.\d+)?)\s*(?:inches|inch)\s*(?:of\s*)?hail", re.IGNORECASE)
_EF_RE = re.compile(r"\bef[\s-]?(\d)\b", re.IGNORECASE)

WIND_HAZARDS = {"tornado", "hurricane", "thunderstorm", "wind"}
TEMP_HEAT = {"heat"}
TEMP_COLD = {"winter"}
RAIN_HAZARDS = {"flood", "hurricane"}
HAIL_HAZARDS = {"thunderstorm", "tornado"}


def _wind_mph(value: float, hazard: str) -> tuple[str, str] | None:
    if hazard not in WIND_HAZARDS:
        return None
    if value >= 130:
        return "extreme", f"wind {value:.0f} mph"
    if value >= 110:
        return "severe", f"wind {value:.0f} mph"
    if value >= 90:
        return "moderate", f"wind {value:.0f} mph"
    if value >= 58:
        return "minor", f"wind {value:.0f} mph"
    return None


def numeric_evidence(text: str, hazard: str) -> list[tuple[str, str]]:
    """Map measured values to a severity floor. Hazard-gated to avoid nonsense."""
    found: list[tuple[str, str]] = []

    for match in _WIND_RE.finditer(text):
        result = _wind_mph(float(match.group(1)), hazard)
        if result:
            found.append(result)
            break

    if hazard in WIND_HAZARDS:
        for match in _WIND_KMH_RE.finditer(text):
            result = _wind_mph(float(match.group(1)) / 1.609, hazard)
            if result:
                found.append(result)
                break

    if hazard in TEMP_HEAT:
        for match in _TEMP_F_RE.finditer(text):
            value = float(match.group(1))
            if value >= 104:
                found.append(("extreme", f"heat index {value:.0f} F"))
            elif value >= 100:
                found.append(("severe", f"heat index {value:.0f} F"))
            elif value >= 95:
                found.append(("moderate", f"heat index {value:.0f} F"))
            break
        for match in _TEMP_C_RE.finditer(text):
            value = float(match.group(1))
            if value >= 40:
                found.append(("extreme", f"temperature {value:.0f} C"))
            elif value >= 38:
                found.append(("severe", f"temperature {value:.0f} C"))
            elif value >= 35:
                found.append(("moderate", f"temperature {value:.0f} C"))
            break

    if hazard in TEMP_COLD:
        for match in _TEMP_F_RE.finditer(text):
            value = float(match.group(1))
            if value <= -13:
                found.append(("extreme", f"wind chill {value:.0f} F"))
            elif value <= 0:
                found.append(("severe", f"wind chill {value:.0f} F"))
            elif value <= 14:
                found.append(("moderate", f"wind chill {value:.0f} F"))
            break

    if hazard in RAIN_HAZARDS:
        for match in _RAIN_IN_RE.finditer(text):
            value = float(match.group(1))
            if value >= 6:
                found.append(("extreme", f"rainfall {value:.0f} in"))
            elif value >= 3:
                found.append(("severe", f"rainfall {value:.0f} in"))
            elif value >= 1:
                found.append(("moderate", f"rainfall {value:.0f} in"))
            break
        for match in _RAIN_MM_RE.finditer(text):
            value = float(match.group(1)) / 25.4
            if value >= 6:
                found.append(("extreme", f"rainfall {value:.0f} in"))
            elif value >= 3:
                found.append(("severe", f"rainfall {value:.0f} in"))
            elif value >= 1:
                found.append(("moderate", f"rainfall {value:.0f} in"))
            break

    if hazard in HAIL_HAZARDS:
        for match in _HAIL_RE.finditer(text):
            value = float(match.group(1))
            if value >= 2.5:
                found.append(("extreme", f"hail {value:g} in"))
            elif value >= 1.5:
                found.append(("severe", f"hail {value:g} in"))
            elif value >= 1:
                found.append(("moderate", f"hail {value:g} in"))
            break

    if hazard == "tornado":
        for match in _EF_RE.finditer(text):
            value = int(match.group(1))
            if value >= 4:
                found.append(("extreme", f"EF{value} rating"))
            elif value >= 2:
                found.append(("severe", f"EF{value} rating"))
            elif value >= 0:
                found.append(("minor", f"EF{value} rating"))
            break

    return found


# --------------------------------------------------------------------------- playbooks
@dataclass(frozen=True)
class Action:
    hazard: str
    phase: str  # immediate | avoid | prepare
    min_severity: str
    en: str
    es: str
    weight: int = 0

    @property
    def rank(self) -> int:
        return RANK[self.min_severity]


ACTIONS: tuple[Action, ...] = (
    # ---------------------------------------------------------------- tornado
    Action("tornado", "immediate", "minor",
           "Move to a basement or an interior room on the lowest floor.",
           "Vaya al sótano o a una habitación interior en el piso más bajo.", 10),
    Action("tornado", "immediate", "moderate",
           "Stay away from windows and exterior walls.",
           "Aléjese de las ventanas y de las paredes exteriores.", 9),
    Action("tornado", "immediate", "moderate",
           "Protect your head and neck with your arms or a helmet.",
           "Protéjase la cabeza y el cuello con los brazos o un casco.", 8),
    Action("tornado", "avoid", "minor",
           "Do not try to outrun a tornado in a vehicle.",
           "No intente escapar de un tornado en un vehículo.", 7),
    Action("tornado", "avoid", "moderate",
           "Avoid mobile homes; they offer little protection.",
           "Evite las casas móviles; ofrecen poca protección.", 6),
    Action("tornado", "prepare", "minor",
           "Keep a flashlight, water, and a battery radio within reach.",
           "Tenga cerca una linterna, agua y una radio de baterías.", 5),
    # ---------------------------------------------------------------- hurricane
    Action("hurricane", "immediate", "minor",
           "Secure outdoor objects that could become projectiles.",
           "Asegure los objetos exteriores que puedan convertirse en proyectiles.", 10),
    Action("hurricane", "immediate", "moderate",
           "Board up windows and reinforce exterior doors.",
           "Cubra las ventanas y refuerce las puertas exteriores.", 9),
    Action("hurricane", "immediate", "severe",
           "Evacuate if local officials order it; do not delay.",
           "Evacúe si las autoridades locales lo ordenan; no se demore.", 10),
    Action("hurricane", "avoid", "severe",
           "Do not go outside during the calm of the eye.",
           "No salga durante la calma del ojo del huracán.", 8),
    Action("hurricane", "prepare", "moderate",
           "Store at least three days of water, food, and medication.",
           "Guarde al menos tres días de agua, comida y medicamentos.", 7),
    # ---------------------------------------------------------------- flood
    Action("flood", "immediate", "moderate",
           "Move to higher ground immediately; do not wait for instructions.",
           "Vaya a terreno elevado de inmediato; no espere instrucciones.", 10),
    Action("flood", "immediate", "minor",
           "Turn off electricity at the breaker if water is entering.",
           "Corte la electricidad en el interruptor si entra agua.", 8),
    Action("flood", "avoid", "minor",
           "Do not walk, swim, or drive through flood water.",
           "No camine, nade ni conduzca por el agua de la inundación.", 10),
    Action("flood", "avoid", "moderate",
           "Avoid underpasses, basements, and low-lying roads.",
           "Evite los pasos subterráneos, sótanos y caminos bajos.", 7),
    Action("flood", "prepare", "minor",
           "Keep documents and medication in waterproof bags.",
           "Guarde documentos y medicamentos en bolsas impermeables.", 5),
    # ---------------------------------------------------------------- wildfire
    Action("wildfire", "immediate", "moderate",
           "Leave immediately if an evacuation order is issued.",
           "Salga de inmediato si se emite una orden de evacuación.", 10),
    Action("wildfire", "immediate", "minor",
           "Close windows, vents, and outside doors to keep smoke out.",
           "Cierre ventanas, rejillas y puertas exteriores para evitar el humo.", 7),
    Action("wildfire", "avoid", "minor",
           "Do not return until officials say it is safe.",
           "No regrese hasta que las autoridades indiquen que es seguro.", 6),
    Action("wildfire", "prepare", "minor",
           "Pack medication, identification, a phone charger, and water.",
           "Prepare medicamentos, identificación, un cargador y agua.", 8),
    Action("wildfire", "prepare", "moderate",
           "Wear an N95 mask outdoors if you must leave.",
           "Use una mascarilla N95 al aire libre si debe salir.", 5),
    # ---------------------------------------------------------------- heat
    Action("heat", "immediate", "moderate",
           "Drink water before you feel thirsty.",
           "Beba agua antes de tener sed.", 9),
    Action("heat", "immediate", "minor",
           "Move to a cool place with air conditioning or shade.",
           "Vaya a un lugar fresco con aire acondicionado o sombra.", 8),
    Action("heat", "avoid", "minor",
           "Avoid outdoor work between 10 a.m. and 4 p.m.",
           "Evite trabajar al aire libre entre las 10 a. m. y las 4 p. m.", 7),
    Action("heat", "avoid", "moderate",
           "Never leave children or pets in a parked vehicle.",
           "Nunca deje a niños o mascotas en un vehículo estacionado.", 9),
    Action("heat", "prepare", "moderate",
           "Check on older neighbors and anyone without cooling.",
           "Verifique a los vecinos mayores y a quienes no tengan refrigeración.", 6),
    # ---------------------------------------------------------------- winter
    Action("winter", "immediate", "moderate",
           "Stay indoors and keep dry.",
           "Permanezca bajo techo y manténgase seco.", 8),
    Action("winter", "avoid", "minor",
           "Avoid travel; roads may be icy and untreated.",
           "Evite viajar; las carreteras pueden estar heladas y sin tratar.", 9),
    Action("winter", "avoid", "severe",
           "Do not use generators or charcoal indoors; carbon monoxide kills.",
           "No use generadores ni carbón dentro de casa; el monóxido de carbono mata.", 10),
    Action("winter", "prepare", "minor",
           "Keep blankets, water, and a charged phone in the car if you must drive.",
           "Lleve mantas, agua y un teléfono cargado en el auto si debe conducir.", 7),
    Action("winter", "prepare", "moderate",
           "Let faucets drip to prevent pipes from freezing.",
           "Deje gotear las llaves para evitar que se congelen las tuberías.", 5),
    # ---------------------------------------------------------------- thunderstorm
    Action("thunderstorm", "immediate", "minor",
           "Go indoors and stay there until 30 minutes after the last thunder.",
           "Entre a un edificio y permanezca allí hasta 30 minutos después del último trueno.", 10),
    Action("thunderstorm", "avoid", "minor",
           "Stay off open water and away from tall isolated trees.",
           "Aléjese del agua abierta y de los árboles altos y aislados.", 8),
    Action("thunderstorm", "avoid", "moderate",
           "Do not use corded phones or plumbing during the storm.",
           "No use teléfonos con cable ni la plomería durante la tormenta.", 6),
    Action("thunderstorm", "prepare", "minor",
           "Move patio furniture and loose objects inside.",
           "Mueva los muebles de patio y los objetos sueltos hacia adentro.", 5),
    # ---------------------------------------------------------------- earthquake
    Action("earthquake", "immediate", "minor",
           "Drop, cover, and hold on under sturdy furniture.",
           "Agáchese, cúbrase y agárrese debajo de un mueble resistente.", 10),
    Action("earthquake", "immediate", "moderate",
           "If outdoors, move away from buildings and power lines.",
           "Si está al aire libre, aléjese de edificios y líneas eléctricas.", 8),
    Action("earthquake", "avoid", "minor",
           "Do not use elevators; use stairs only if they are safe.",
           "No use ascensores; use las escaleras solo si son seguras.", 7),
    Action("earthquake", "avoid", "moderate",
           "Expect aftershocks and stay out of damaged buildings.",
           "Espere réplicas y no entre en edificios dañados.", 6),
    Action("earthquake", "prepare", "minor",
           "Keep shoes and a flashlight next to your bed.",
           "Deje zapatos y una linterna junto a la cama.", 5),
    # ---------------------------------------------------------------- tsunami
    Action("tsunami", "immediate", "moderate",
           "Move inland and uphill immediately.",
           "Vaya tierra adentro y hacia terrenos altos de inmediato.", 10),
    Action("tsunami", "immediate", "moderate",
           "Climb to the third floor or higher of a sturdy building.",
           "Suba al tercer piso o más alto de un edificio resistente.", 9),
    Action("tsunami", "avoid", "minor",
           "Do not go to the beach to watch the wave.",
           "No vaya a la playa a ver la ola.", 8),
    Action("tsunami", "avoid", "moderate",
           "Stay away from the coast until officials lift the warning.",
           "Aléjese de la costa hasta que las autoridades levanten la advertencia.", 7),
    # ---------------------------------------------------------------- wind
    Action("wind", "immediate", "minor",
           "Secure doors, windows, and loose outdoor objects.",
           "Asegure puertas, ventanas y objetos sueltos al aire libre.", 8),
    Action("wind", "avoid", "minor",
           "Avoid driving high-profile vehicles.",
           "Evite conducir vehículos de gran altura.", 6),
    Action("wind", "avoid", "moderate",
           "Stay clear of trees and power lines.",
           "Aléjese de árboles y líneas eléctricas.", 7),
    # ---------------------------------------------------------------- fog
    Action("fog", "immediate", "minor",
           "Slow down and use low-beam headlights.",
           "Reduzca la velocidad y use las luces bajas.", 8),
    Action("fog", "avoid", "minor",
           "Avoid passing on two-lane roads.",
           "Evite rebasar en carreteras de dos carriles.", 6),
    # ---------------------------------------------------------------- air quality
    Action("air_quality", "immediate", "minor",
           "Keep windows closed and run a filter indoors.",
           "Mantenga las ventanas cerradas y use un filtro adentro.", 8),
    Action("air_quality", "avoid", "minor",
           "Limit outdoor exercise, especially in the afternoon.",
           "Limite el ejercicio al aire libre, sobre todo por la tarde.", 6),
    Action("air_quality", "prepare", "moderate",
           "Keep asthma or COPD medication within reach.",
           "Tenga a mano los medicamentos para el asma o la EPOC.", 5),
    # ---------------------------------------------------------------- other
    Action("other", "immediate", "minor",
           "Follow instructions from local officials.",
           "Siga las instrucciones de las autoridades locales.", 9),
    Action("other", "avoid", "minor",
           "Avoid the affected area until it is declared safe.",
           "Evite la zona afectada hasta que se declare segura.", 7),
    Action("other", "prepare", "minor",
           "Keep water, medication, and a charged phone ready.",
           "Tenga listos agua, medicamentos y un teléfono cargado.", 6),
)

#: Hazards where leaving the area is part of the official playbook, and the
#: severity at which that advice starts. A flash flood is "move to higher
#: ground" first; telling everyone to evacuate at Severe would be wrong advice,
#: so flood only reaches the evacuation banner at Extreme.
EVACUATION_THRESHOLDS: dict[str, str] = {
    "hurricane": "severe",
    "wildfire": "severe",
    "tsunami": "moderate",
    "flood": "extreme",
}
SHELTER_HAZARDS = frozenset({"tornado", "thunderstorm", "winter", "earthquake", "tsunami"})


def severity_at_least(hazard: str, severity: str) -> str:
    """Safety floor: unknown collapses to moderate so instructions are never empty."""
    if severity not in RANK or severity == "unknown":
        return "moderate"
    return severity


def playbook(hazard: str, severity: str) -> dict[str, list[dict[str, object]]]:
    """Return the phase-grouped, severity-filtered action set for a hazard."""
    tier = severity_at_least(hazard, severity)
    tier_rank = RANK.get(tier, 2)
    known = hazard if hazard in HAZARDS else "other"

    def collect(phase: str) -> list[dict[str, object]]:
        chosen = [
            action
            for action in ACTIONS
            if action.hazard == known and action.phase == phase and action.rank <= tier_rank
        ]
        if not chosen and known != "other":
            chosen = [
                action
                for action in ACTIONS
                if action.hazard == "other" and action.phase == phase and action.rank <= max(tier_rank, 2)
            ]
        # Weight first: "move to a basement" must outrank "avoid windows"
        # even though both are gated at different severity levels.
        chosen.sort(key=lambda action: (-action.weight, -action.rank))
        return [
            {"en": action.en, "es": action.es, "phase": action.phase, "min_severity": action.min_severity}
            for action in chosen
        ]

    return {
        "tier": tier,
        "immediate": collect("immediate"),
        "avoid": collect("avoid"),
        "prepare": collect("prepare"),
        "evacuation_recommended": (
            known in EVACUATION_THRESHOLDS
            and tier_rank >= RANK[EVACUATION_THRESHOLDS[known]]
        ),
        "shelter_recommended": known in SHELTER_HAZARDS and tier_rank >= RANK["moderate"],
    }


def hazard_label(hazard: str) -> dict[str, str]:
    known = HAZARDS.get(hazard, HAZARDS["other"])
    return {"en": known.en, "es": known.es}


def detected_product_type(event: str) -> str | None:
    lowered = (event or "").lower()
    # "alert" is checked last: a "Tornado Warning" must stay a warning, while
    # an "Air Quality Alert" has no stronger product word of its own.
    for key in ("emergency", "warning", "watch", "advisory", "statement", "alert"):
        if key in lowered:
            return key
    return None
