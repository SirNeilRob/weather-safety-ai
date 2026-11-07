from __future__ import annotations

import pytest

from app.core.processors.local import (
    LocalProcessor,
    analyze,
    classify,
    entities,
    keywords,
    summarize,
)

SAMPLE = (
    "The generator at the north site failed at 03:12 on 2026-11-01. "
    "Field crew restored power after replacing the fuel pump. "
    "The outage affected 412 homes for 96 minutes. "
    "A follow-up inspection is scheduled for 2026-11-09. "
    "Contact dispatch@example.com or see https://example.com/incident/991."
)


def test_summarize_respects_sentence_budget_and_order() -> None:
    summary = summarize(SAMPLE, max_sentences=2)
    sentences = [s for s in summary.split(". ") if s]
    assert 1 <= len(sentences) <= 2
    # the summary must be drawn from the original text, not invented
    assert summary.split(".")[0].strip()[:20] in SAMPLE


def test_summarize_short_input_is_returned_intact() -> None:
    text = "One sentence only."
    assert summarize(text, max_sentences=3) == text


def test_keywords_drop_stopwords() -> None:
    result = keywords(SAMPLE)
    assert "the" not in result
    assert "generator" in result


def test_entities_find_contacts_and_dates() -> None:
    found = entities(SAMPLE)
    assert "dispatch@example.com" in found["emails"]
    assert any("example.com" in url for url in found["urls"])
    assert "2026-11-01" in found["dates"]


def test_classify_picks_the_matching_label() -> None:
    result = classify(
        "I was charged twice, please refund this invoice",
        {"billing": ["invoice", "refund", "charged"], "shipping": ["parcel", "courier"]},
    )
    assert result["label"] == "billing"
    assert 0.5 < result["confidence"] <= 1.0


def test_classify_without_labels_is_safe() -> None:
    result = classify("anything at all", [])
    assert result == {"label": None, "confidence": 0.0, "scores": {}}


def test_analyze_reports_stats() -> None:
    result = analyze(SAMPLE)
    assert result["stats"]["words"] > 20
    assert result["stats"]["reading_time_seconds"] >= 1


@pytest.mark.anyio
async def test_processor_handles_unknown_kind_without_raising() -> None:
    processor = LocalProcessor()
    result = await processor.run("something_new", {"text": SAMPLE})
    assert "note" in result
    assert result["offline"] is True
    assert result["engine"] == "local:builtin"


@pytest.mark.anyio
async def test_processor_reads_openai_style_messages() -> None:
    processor = LocalProcessor()
    result = await processor.run(
        "summarize", {"messages": [{"role": "user", "content": "Alpha beta. Gamma delta."}]}
    )
    assert "alpha" in result["summary"].lower()


@pytest.mark.anyio
async def test_processor_survives_empty_payload() -> None:
    processor = LocalProcessor()
    result = await processor.run("analyze", {})
    assert result["offline"] is True
