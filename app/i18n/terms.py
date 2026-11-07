"""English/Spanish support for the offline path.

Two very different translation problems are handled differently, on purpose:

1. **Safety instructions are natively bilingual.** They come from the curated
   playbook in ``app/knowledge/hazards.py``, written in both languages by hand.
   They are never machine-translated, so the text a user acts on is always
   correct.
2. **Free text (feed headlines, descriptions) uses a phrasebook.** A domain
   phrasebook is honest and offline; a general translator is not available
   without a model. Every translation therefore reports its ``method`` and a
   ``coverage`` score so the UI can show "translated automatically" rather than
   pretending the output is perfect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Curated domain pairs. Order does not matter; matching is longest-first.
PAIRS: tuple[tuple[str, str], ...] = (
    # --- products and severity
    ("tornado emergency", "emergencia de tornado"),
    ("tornado warning", "aviso de tornado"),
    ("tornado watch", "vigilancia de tornado"),
    ("flash flood emergency", "emergencia de inundación repentina"),
    ("flood warning", "aviso de inundación"),
    ("earthquake warning", "aviso de terremoto"),
    ("flash flood warning", "aviso de inundación repentina"),
    ("flash flood watch", "vigilancia de inundación repentina"),
    ("severe thunderstorm warning", "aviso de tormenta eléctrica severa"),
    ("excessive heat warning", "aviso de calor excesivo"),
    ("excessive heat watch", "vigilancia de calor excesivo"),
    ("winter storm warning", "aviso de tormenta invernal"),
    ("winter storm watch", "vigilancia de tormenta invernal"),
    ("hurricane warning", "aviso de huracán"),
    ("hurricane watch", "vigilancia de huracán"),
    ("tropical storm warning", "aviso de tormenta tropical"),
    ("flash flood warning", "aviso de inundación repentina"),
    ("severe thunderstorm", "tormenta eléctrica severa"),
    ("civil emergency", "emergencia civil"),
    ("evacuation order", "orden de evacuación"),
    ("evacuation warning", "aviso de evacuación"),
    ("red flag warning", "aviso de bandera roja"),
    ("air quality alert", "alerta de calidad del aire"),
    ("dense fog advisory", "aviso de niebla densa"),
    ("high wind warning", "aviso de viento fuerte"),
    ("heat advisory", "aviso por calor"),
    ("wind advisory", "aviso por viento"),
    ("flood advisory", "aviso por inundación"),
    ("warning", "aviso"),
    ("watch", "vigilancia"),
    ("advisory", "aviso preventivo"),
    ("emergency", "emergencia"),
    ("alert", "alerta"),
    ("extreme", "extrema"),
    ("severe", "severa"),
    ("moderate", "moderada"),
    ("minor", "menor"),
    ("unknown", "desconocida"),
    ("immediate", "inmediata"),
    ("expected", "prevista"),
    ("observed", "observada"),
    ("likely", "probable"),
    ("possible", "posible"),
    # --- hazards
    ("tornado", "tornado"),
    ("hurricane", "huracán"),
    ("tropical storm", "tormenta tropical"),
    ("typhoon", "tifón"),
    ("thunderstorm", "tormenta eléctrica"),
    ("thunderstorms", "tormentas eléctricas"),
    ("thunder", "trueno"),
    ("lightning", "rayos"),
    ("hail", "granizo"),
    ("flooding", "inundación"),
    ("flood", "inundación"),
    ("flash flood", "inundación repentina"),
    ("rain", "lluvia"),
    ("winds", "vientos"),
    ("ponding", "acumulación de agua"),
    ("visibility", "visibilidad"),
    ("storm surge", "marea de tormenta"),
    ("wildfire", "incendio forestal"),
    ("brush fire", "incendio de matorrales"),
    ("fire", "incendio"),
    ("smoke", "humo"),
    ("earthquake", "terremoto"),
    ("aftershock", "réplica"),
    ("tsunami", "tsunami"),
    ("winter storm", "tormenta invernal"),
    ("blizzard", "ventisca"),
    ("ice storm", "tormenta de hielo"),
    ("freezing rain", "lluvia helada"),
    ("snow", "nieve"),
    ("ice", "hielo"),
    ("extreme heat", "calor extremo"),
    ("heat wave", "ola de calor"),
    ("heat index", "índice de calor"),
    ("high wind", "viento fuerte"),
    ("wind", "viento"),
    ("gust", "ráfaga"),
    ("gusts", "ráfagas"),
    ("dense fog", "niebla densa"),
    ("fog", "niebla"),
    ("air quality", "calidad del aire"),
    # --- geography and time
    ("county", "condado"),
    ("counties", "condados"),
    ("city", "ciudad"),
    ("town", "pueblo"),
    ("village", "aldea"),
    ("state", "estado"),
    ("region", "región"),
    ("area", "zona"),
    ("areas", "zonas"),
    ("coast", "costa"),
    ("coastal", "costero"),
    ("north", "norte"),
    ("south", "sur"),
    ("east", "este"),
    ("west", "oeste"),
    ("central", "central"),
    ("northern", "del norte"),
    ("southern", "del sur"),
    ("eastern", "del este"),
    ("western", "del oeste"),
    ("until", "hasta"),
    ("through", "hasta"),
    ("today", "hoy"),
    ("tonight", "esta noche"),
    ("tomorrow", "mañana"),
    ("morning", "mañana"),
    ("afternoon", "tarde"),
    ("evening", "noche"),
    ("overnight", "durante la noche"),
    ("now", "ahora"),
    ("immediately", "de inmediato"),
    ("minutes", "minutos"),
    ("hours", "horas"),
    ("days", "días"),
    # --- impact language
    ("life-threatening", "que amenaza la vida"),
    ("life threatening", "que amenaza la vida"),
    ("destructive", "destructivo"),
    ("catastrophic", "catastrófico"),
    ("dangerous", "peligroso"),
    ("extremely dangerous", "extremadamente peligroso"),
    ("significant", "significativo"),
    ("major", "importante"),
    ("damaging", "dañino"),
    ("damage", "daños"),
    ("power outage", "corte de electricidad"),
    ("power lines", "líneas eléctricas"),
    ("downed", "caídos"),
    ("roads", "carreteras"),
    ("road", "carretera"),
    ("bridge", "puente"),
    ("bridges", "puentes"),
    ("basement", "sótano"),
    ("shelter", "refugio"),
    ("evacuate", "evacuar"),
    ("evacuation", "evacuación"),
    ("take shelter", "busque refugio"),
    ("stay indoors", "permanezca bajo techo"),
    ("move to higher ground", "vaya a terreno elevado"),
    # --- safety imperatives: whole phrases before word-by-word substitution,
    #     otherwise "Do not wait" degrades to "No wait" in a live alert.
    ("do not drive through flood water", "no conduzca por agua de inundación"),
    ("do not drive through", "no conduzca por"),
    ("do not drive", "no conduzca"),
    ("do not wait", "no espere"),
    ("do not go outside", "no salga al exterior"),
    ("do not touch", "no toque"),
    ("interior room", "habitación interior"),
    ("lowest floor", "piso más bajo"),
    ("mobile homes", "casas móviles"),
    ("mobile home", "casa móvil"),
    ("will be destroyed", "serán destruidas"),
    ("on the ground", "en el suelo"),
    ("underpasses", "pasos subterráneos"),
    ("low-lying", "de baja altura"),
    ("high ground", "terreno elevado"),
    ("wait", "espere"),
    ("drive", "conduzca"),
    ("water", "agua"),
    ("food", "comida"),
    ("medication", "medicamentos"),
    ("medications", "medicamentos"),
    ("children", "niños"),
    ("elderly", "personas mayores"),
    ("pets", "mascotas"),
    ("vehicle", "vehículo"),
    ("vehicles", "vehículos"),
    ("phone", "teléfono"),
    ("flashlight", "linterna"),
    ("radio", "radio"),
    ("blankets", "mantas"),
    ("windows", "ventanas"),
    ("doors", "puertas"),
    ("electricity", "electricidad"),
    ("gas", "gas"),
    ("generator", "generador"),
    ("carbon monoxide", "monóxido de carbono"),
    # --- verbs and connectives
    ("is expected", "se espera"),
    ("are expected", "se esperan"),
    ("is occurring", "está ocurriendo"),
    ("has been reported", "se ha reportado"),
    ("has been issued", "se ha emitido"),
    ("issued", "emitido"),
    ("in effect", "en vigor"),
    ("remains in effect", "sigue en vigor"),
    ("expires", "vence"),
    ("effective", "vigente"),
    ("issued by", "emitido por"),
    ("according to", "según"),
    ("because", "porque"),
    ("however", "sin embargo"),
    ("please", "por favor"),
    ("avoid", "evite"),
    ("do not", "no"),
    ("never", "nunca"),
    ("seek", "busque"),
    ("call", "llame"),
    ("monitor", "siga"),
    ("prepare", "prepárese"),
    ("protect", "proteja"),
    ("close", "cierre"),
    ("stay", "permanezca"),
    ("leave", "salga"),
    ("move", "muévase"),
    ("drink", "beba"),
    ("check", "verifique"),
    ("expect", "espere"),
    ("could", "podría"),
    ("may", "puede"),
    ("will", "va a"),
    ("and", "y"),
    ("or", "o"),
    ("with", "con"),
    ("without", "sin"),
    ("from", "desde"),
    ("near", "cerca de"),
    ("across", "en toda la zona de"),
    ("the", "el"),
    ("a", "un"),
    ("an", "un"),
    ("of", "de"),
    ("in", "en"),
    ("on", "en"),
    ("at", "a las"),
    ("for", "para"),
    ("to", "a"),
    ("by", "por"),
    ("this", "este"),
    ("these", "estos"),
    ("is", "es"),
    ("are", "son"),
)

_WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúñü][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'-]*")
_TEMP_F_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:°\s*|degrees?\s*)?F\b")
_WIND_MPH_RE = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*mph\b", re.IGNORECASE)
_INCH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:inches|inch|in)\b", re.IGNORECASE)

SPANISH_HINTS = frozenset(
    """
    el la los las de del que en y con para por una uno es son está están
    se su sus al como más pero sin sobre entre cuando desde hasta
    """.split()
)
ENGLISH_HINTS = frozenset(
    """
    the and of to in for with is are was were be been will would this that
    from until until near across until
    """.split()
)


_LETTERS = "A-Za-zÁÉÍÓÚÜÑáéíóúñü"

#: Pairs whose two sides are identical are still real vocabulary: they must count
#: as covered (and never as an unknown term) even though they are not rewritten.
_IDENTITY_TERMS = frozenset(en.lower() for en, es in PAIRS if en == es)

#: "Linn County" is "condado de Linn" in Spanish, so the English word order has to
#: be flipped before substitution; doing it after would only reorder Spanish.
_COUNTY_RE = re.compile(r"\b([A-ZÁÉÍÓÚÑ][\w'.-]*)\s+County\b")


def _build_index() -> tuple[re.Pattern[str], dict[str, str]]:
    """Compile one alternation of every English phrase, longest first.

    A single pass is a correctness requirement, not an optimisation. Applying
    the table phrase-by-phrase rewrites text that is already Spanish, so a
    later entry such as ``a -> un`` turns the correct
    ``vaya a terreno elevado`` into ``vaya un terreno elevado``. Matching the
    English source once keeps every replacement out of reach of the next one.
    """
    pairs = [(en, es) for en, es in PAIRS if en != es]
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    alternatives = "|".join(re.escape(en) for en, _ in pairs)
    pattern = re.compile(
        rf"(?<![{_LETTERS}])(?:{alternatives})(?![{_LETTERS}])", re.IGNORECASE
    )
    return pattern, {en.lower(): es for en, es in pairs}


_EN_PATTERN, _EN_LOOKUP = _build_index()


def detect_language(text: str) -> str:
    """Cheap heuristic; good enough to pick which side of the phrasebook to use."""
    lowered = f" {text.lower()} "
    if any(char in lowered for char in "ñ¿¡"):
        return "es"
    tokens = set(_WORD_RE.findall(lowered))
    es_hits = len(tokens & SPANISH_HINTS)
    en_hits = len(tokens & ENGLISH_HINTS)
    if es_hits > en_hits:
        return "es"
    return "en"


def _convert_units(text: str) -> str:
    def fahrenheit(match: re.Match[str]) -> str:
        value = float(match.group(1))
        celsius = round((value - 32) * 5 / 9)
        return f"{celsius} °C"

    def mph(match: re.Match[str]) -> str:
        return f"{round(float(match.group(1)) * 1.609)} km/h"

    def inches(match: re.Match[str]) -> str:
        return f"{round(float(match.group(1)) * 25.4)} mm"

    text = _TEMP_F_RE.sub(fahrenheit, text)
    text = _WIND_MPH_RE.sub(mph, text)
    text = _INCH_RE.sub(inches, text)
    return text


@dataclass(slots=True)
class Translation:
    text: str
    source_language: str
    target_language: str
    method: str = "phrasebook"
    coverage: float = 0.0
    unknown_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "method": self.method,
            "coverage": round(self.coverage, 3),
            "unknown_terms": self.unknown_terms[:10],
        }


def translate_to_spanish(text: str) -> Translation:
    """Phrasebook translation EN -> ES with a coverage score."""
    if not text.strip():
        return Translation("", "en", "es", coverage=1.0)

    # Units first: once the phrasebook has rewritten "in" to "en", the inch
    # conversion can no longer see "four inches".
    source = _convert_units(text)
    counties = _COUNTY_RE.findall(source)
    source = _COUNTY_RE.sub(r"County of \1", source)
    matched_terms: set[str] = set(_IDENTITY_TERMS)
    matched_terms.update(name.lower() for name in counties)

    def _replace(match: re.Match[str]) -> str:
        matched = match.group(0)
        replacement = _EN_LOOKUP[matched.lower()]
        matched_terms.update(word.lower() for word in _WORD_RE.findall(matched))
        if matched[:1].isupper() and replacement[:1].islower():
            return replacement[:1].upper() + replacement[1:]
        return replacement

    output = _EN_PATTERN.sub(_replace, source)

    content = [
        token.lower()
        for token in _WORD_RE.findall(source)
        if len(token) > 2 and token.lower() not in ENGLISH_HINTS
    ]
    covered = [token for token in content if token in matched_terms]
    coverage = (len(covered) / len(content)) if content else 1.0

    unknown = sorted({token for token in content if token not in matched_terms})[:20]
    return Translation(
        text=re.sub(r"\s+", " ", output).strip(),
        source_language="en",
        target_language="es",
        coverage=coverage,
        unknown_terms=unknown,
    )


def translate(text: str, target: str = "es") -> Translation:
    """Translate toward ``target``; no-op when the text is already there."""
    source = detect_language(text)
    if source == target:
        return Translation(text, source, target, method="none", coverage=1.0)
    if target == "es":
        return translate_to_spanish(text)
    # ES -> EN is best effort: the table is designed for English source text.
    reverse = {es: en for en, es in PAIRS if es and es != en}
    output = text
    matched = 0
    for es, en in sorted(reverse.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = re.compile(rf"(?<![A-Za-zÁÉÍÓÚáéíóúñ]){re.escape(es)}(?![A-Za-z])", re.IGNORECASE)
        new_output, count = pattern.subn(en, output)
        if count:
            matched += count
            output = new_output
    content = [token for token in _WORD_RE.findall(text) if len(token) > 2]
    coverage = min(1.0, matched / len(content)) if content else 1.0
    return Translation(
        text=re.sub(r"\s+", " ", output).strip(),
        source_language=source,
        target_language=target,
        coverage=coverage,
    )
