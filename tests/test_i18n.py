from __future__ import annotations

import pytest

from app.i18n.terms import Translation, detect_language, translate, translate_to_spanish


def test_product_phrase_translates_as_a_unit() -> None:
    result = translate_to_spanish("Tornado Warning")
    assert result.text == "Aviso de tornado"
    assert result.coverage == 1.0


def test_longest_phrase_wins_over_its_own_words() -> None:
    result = translate_to_spanish("Flash Flood Warning")
    assert result.text == "Aviso de inundación repentina"
    assert "flash" not in result.text.lower()


def test_translation_does_not_rewrite_its_own_output() -> None:
    # Regression: sequential substitution used to feed Spanish back through the
    # English table, turning "vaya a terreno elevado" into "vaya un terreno".
    result = translate_to_spanish("Move to higher ground.")
    assert "terreno elevado" in result.text
    assert "un terreno" not in result.text


def test_safety_imperatives_survive_phrasebook_gaps() -> None:
    result = translate_to_spanish(
        "Take shelter now in a basement or interior room. Do not wait."
    )
    assert "habitación interior" in result.text
    assert "no espere" in result.text.lower()
    assert "interior room" not in result.text
    assert "wait" not in result.text.lower()


def test_units_are_converted_for_a_spanish_audience() -> None:
    assert "km/h" in translate_to_spanish("winds at 40 mph").text
    assert "°C" in translate_to_spanish("heat index 112 F").text
    assert "mm" in translate_to_spanish("4 inches of rain").text


def test_unknown_terms_are_reported_instead_of_invented() -> None:
    result = translate_to_spanish("Tornado Warning for the pallet factory")
    assert "pallet" in result.unknown_terms
    assert result.coverage < 1.0
    assert result.method == "phrasebook"


def test_coverage_counts_words_inside_multiword_phrases() -> None:
    result = translate_to_spanish("Flash Flood Warning in Linn County")
    assert result.coverage == 1.0
    assert result.unknown_terms == []


def test_spanish_source_is_detected_and_passed_through_untouched() -> None:
    text = "Vaya a terreno elevado de inmediato."
    assert detect_language(text) == "es"
    result = translate(text, "es")
    assert result.method == "none"
    assert result.text == text
    assert result.coverage == 1.0


def test_reverse_translation_returns_readable_english() -> None:
    result = translate("Aviso de inundación repentina", "en")
    assert "flood" in result.text.lower()
    assert result.coverage > 0


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_text_is_not_an_error(text) -> None:
    result = translate_to_spanish(text)
    assert isinstance(result, Translation)
    assert result.text == ""
    assert result.coverage == 1.0
