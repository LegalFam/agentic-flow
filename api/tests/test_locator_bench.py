from dataclasses import replace

from app import locator
from eval import locator_bench

DOC = """## Artículo 167º

## Requisitos de la demanda

La demanda se presenta por escrito y contendrá los requisitos del Código Procesal Civil.

**Aplicación supletoria del artículo 433**

## Artículo 167-A (*)

## Contenido del auto admisorio

El auto admisorio debe contener el apercibimiento de declararse la rebeldía del demandado.
"""


def test_heading_number_is_read_from_the_original_line():
    index = locator.build_index(DOC)
    numbers, disagreements = locator_bench.heading_numbers(index)
    assert sorted(numbers.values()) == ["167", "167A"]
    assert disagreements == []


def test_label_that_drops_the_letter_is_reported_and_scored_wrong():
    index = locator.build_index(DOC)
    mislabeled = [replace(heading, label="Art. 167") for heading in index.articles]
    index = replace(index, articles=mislabeled, headings=mislabeled)
    numbers, disagreements = locator_bench.heading_numbers(index)
    assert [item["read"] for item in disagreements] == ["167A"]

    start = DOC.index("El auto admisorio")
    truth = locator_bench.truth_articles(index, start, start + 60, numbers)
    assert truth == {"167A"}
    assert locator_bench.classify(locator.Locator(label="Art. 167"), truth) == "wrong"
