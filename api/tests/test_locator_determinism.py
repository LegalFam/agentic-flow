"""El reanclaje difuso y la auditoria de citas no pueden depender de PYTHONHASHSEED.

En ablacion-v1 la misma cita salia `correct` o `partial` segun el proceso: el fuzzy elegia
anclas por orden de iteracion de un set, un ancla frecuente llenaba el tope de candidatos
y la ventana buena no se llegaba a evaluar.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import corpus
from app import locator as L
from app.config import settings
from app.text_utils import clean_user_text
from eval import corpus_articles

API_DIR = Path(__file__).resolve().parents[1]

# Un token largo y muy frecuente en el snippet: con anclas ordenadas solo por largo era el
# primero en entrar y agotaba los 200 candidatos antes de llegar al articulo real.
FLOODED = (
    "Disposiciones generales del procedimientos.\n" * 250
    + "\nArticulo 10.- La solicitud se tramita conforme a los procedimientos sumarisimos "
    "previstos para alimentos.\n\n"
    "Articulo 11.- El juez resuelve atendiendo las necesidades del beneficiario.\n\n"
    "Articulo 12.- Otra materia sin relacion alguna, sobre registros publicos y notarias.\n"
)
FLOODED_QUERY = (
    "La solicitud se tramita confrome a los procedimientos sumarisimos previstos para "
    "alimentos. Articulo 11.- El juez resuelve atendiendo las necesidades del beneficiario."
)

# Anclas del mismo largo, una de ellas frecuente: el desempate lo decidia el hash.
TIED = (
    "Es procedente.\n" * 250
    + "\nArticulo 10.- La procedente cosa solicitada debe tramitarse por via sumarisimo sin mas.\n\n"
    "Articulo 11.- El juez da la cuota que corresponde al caso concreto.\n\n"
    "Articulo 12.- Otra materia sin relacion alguna, sobre registros publicos y notarias.\n"
)
TIED_QUERY = (
    "La procedente cosa solicitda debe tramitarse por via sumarisimo sin mas. "
    "Articulo 11.- El juez da la cuota que corresponde al caso concreto."
)


def _articles(index: L.DocumentIndex, query: str) -> tuple[str, list[str]]:
    position, strategy = L.find_in_folded(index.folded, query)
    assert position is not None
    start = index.offset_map[position]
    end = index.offset_map[min(position + len(query) - 1, len(index.offset_map) - 1)] + 1
    return strategy, L.articles_in_span(index, start, end)


def test_frequent_anchor_does_not_starve_the_real_window():
    index = L.build_index(FLOODED)
    query = clean_user_text(FLOODED_QUERY)
    strategy, articles = _articles(index, query)
    assert strategy == "fuzzy"
    assert articles == ["Art. 10", "Art. 11"]


def test_fuzzy_window_is_aligned_with_the_snippet_start():
    # Con un margen fijo de 40 caracteres la ventana arrancaba antes del pasaje y podia
    # arrastrar un articulo que la cita no toca.
    index = L.build_index(FLOODED)
    position, _ = L.find_in_folded(index.folded, clean_user_text(FLOODED_QUERY))
    assert position == index.folded.find("la solicitud se tramita")


_PROBE = """
import json, sys
from app import locator
from app.text_utils import clean_user_text
data = json.load(sys.stdin)
index = locator.build_index(data["doc"])
print(json.dumps(locator.find_in_folded(index.folded, clean_user_text(data["query"]))))
"""


@pytest.mark.parametrize("doc,query", [(TIED, TIED_QUERY), (FLOODED, FLOODED_QUERY)])
def test_fuzzy_match_is_the_same_under_every_hash_seed(doc, query):
    payload = json.dumps({"doc": doc, "query": query})
    results = set()
    for seed in ("0", "1", "2", "3", "11", "19", "42", "123"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        completed = subprocess.run(
            [sys.executable, "-c", _PROBE],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=API_DIR,
            env=env,
            check=True,
        )
        results.add(completed.stdout.strip())
    assert len(results) == 1, results
    position, strategy = json.loads(results.pop())
    assert strategy == "fuzzy"
    # Una letra perdida en el snippet corre un caracter las anclas que vienen detras.
    expected = L.build_index(doc).folded.find(clean_user_text(query)[:12].lower())
    assert abs(position - expected) <= 1


@pytest.fixture
def case_corpus(tmp_path, monkeypatch):
    for stem in ("3023-2017-abcdef123456", "588-2016-abcdef123456"):
        (tmp_path / f"{stem}.md").write_text(f"CASACION {stem}\n", encoding="utf-8")
    monkeypatch.setattr(settings, "corpus_dir", str(tmp_path))
    corpus.clear_cache()
    corpus_articles.build_registry.cache_clear()
    corpus_articles._case_index.cache_clear()
    yield tmp_path
    corpus.clear_cache()
    corpus_articles.build_registry.cache_clear()
    corpus_articles._case_index.cache_clear()


def test_citation_with_two_case_numbers_resolves_to_the_first_mentioned(case_corpus):
    path = corpus_articles.find_corpus_path("CASACION 588-2016 LIMA, que reitera la 3023-2017")
    assert path is not None and path.name == "588-2016-abcdef123456.md"
    path = corpus_articles.find_corpus_path("CASACION 3023-2017 LIMA, que reitera la 588-2016")
    assert path is not None and path.name == "3023-2017-abcdef123456.md"
