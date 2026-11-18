"""Actualizacion de normas desde El Peruano.

Los textos de El Peruano y el extracto de la Ley 30364 son reales (fichas publicadas y el
markdown indexado). Gemini se reemplaza por respuestas fijas: lo que se prueba es lo que el
codigo hace con ellas, que es donde viven las garantias (nada se reescribe, todo lo agregado
es literal, nada se duplica, y nada toca el store sin copia de seguridad).
"""

import hashlib
import json
import re
from datetime import date
from pathlib import Path

import pytest

from app import amendments, corpus, elperuano, store_documents
from app.config import settings

FIXTURES = Path(__file__).parent / "fixtures" / "elperuano"
DOC = "ley3036-erradicarviolencia-0eeaf4de148c.md"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def dispositivo(norma_id: str) -> elperuano.Dispositivo:
    fecha, texto = fixture_text(f"{norma_id}.txt").split("\n", 1)
    return elperuano.Dispositivo(id=norma_id, url=elperuano.dispositivo_url(norma_id),
                                 fecha_publicacion=fecha, texto=texto)


LEY_32351 = "2403115-1"  # modifica el articulo 46 completo
LEY_32298 = "2389754-3"  # incorpora un parrafo al literal a) del articulo 10, con [...]
RM_413_MC = "2573795-1"  # deroga un articulo de OTRA norma; cita una derogacion en considerandos
DS_249_EF = "2573253-2"  # disposicion complementaria derogatoria de un D.S. ajeno

WATCHED = amendments.WatchedNorm(
    document=DOC,
    identificador="Ley N° 30364",
    aliases=["Ley para prevenir, sancionar y erradicar la violencia contra las mujeres"],
    version_hasta="2019-02-01",
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / DOC).write_text(fixture_text("ley30364_extracto.md"), encoding="utf-8")
    monkeypatch.setattr(settings, "corpus_dir", str(corpus_dir))
    monkeypatch.setattr(settings, "amendments_dir", str(tmp_path / "amendments"))
    monkeypatch.setattr(settings, "amendments_registry_path", str(tmp_path / "normas_vigiladas.json"))
    corpus.clear_cache()
    amendments.save_registry([WATCHED])
    return tmp_path


class FakeGemini:
    """Responde como Gemini segun que norma y que prompt recibe."""

    def __init__(self, classify: dict[str, dict], edits: dict[tuple[str, str], dict]):
        self.classify = classify
        self.edits = edits
        self.calls: list[str] = []

    def __call__(self, prompt: str) -> dict:
        self.calls.append(prompt)
        if "PARTE DISPOSITIVA DE LA NORMA PUBLICADA" in prompt:
            for marker, response in self.classify.items():
                if marker in prompt:
                    return response
            return {"efectos": []}
        for (marker, articulo), response in self.edits.items():
            if marker in prompt and f"NUESTRO ARTICULO {articulo} ===" in prompt:
                return response
        raise AssertionError("prompt inesperado")


ART46_NUEVO = (
    "Los medios de comunicación, en la difusión de informaciones relativas a la violencia contra la mujer "
    "y los integrantes del grupo familiar, garantizan, con la correspondiente objetividad informativa, la "
    "defensa de los derechos humanos, la libertad y dignidad de las mujeres víctimas de violencia, de sus "
    "hijos y de los demás integrantes del grupo familiar. En particular, tienen especial cuidado en el "
    "tratamiento gráfico de las informaciones."
)
ART10_PARRAFO = (
    "La Policía Nacional del Perú coordina con el Programa Nacional para la Prevención y Erradicación de la "
    "Violencia contra las Mujeres e Integrantes del Grupo Familiar (Aurora)"
)


def line_in_article(fixture: str, articulo: str, starts: str) -> int:
    lines = fixture_text(fixture).split("\n")
    start, _ = amendments.find_article(lines, articulo)
    return next(i - start + 1 for i in range(start, len(lines)) if lines[i].startswith(starts))


def fake_for_both_laws(**overrides) -> FakeGemini:
    classify = {
        "LEY Nº 32351": {"norma_publicada": "Ley Nº 32351", "efectos": [{
            "tipo": "modifica", "afecta_a": "norma", "articulos": ["46"], "disposicion": "Artículo único",
            "evidencia": "Se modifica el artículo 46 de la Ley 30364"}]},
        "ley Nº 32298": {"norma_publicada": "Ley Nº 32298", "efectos": [{
            "tipo": "incorpora", "afecta_a": "norma", "articulos": ["10"], "disposicion": "Artículo 1",
            "evidencia": "Se modifica el artículo 10 incorporando un tercer párrafo al literal a)"}]},
    }
    edits = {
        ("Ley Nº 32351", "46"): {"alcance": "Artículo", "ancla_linea": 1, "texto_nuevo": ART46_NUEVO, "articulo_nuevo": False},
        ("Ley Nº 32298", "10"): {
            "alcance": "Párrafo",
            # Primera mitad del parrafo partido en dos lineas por el PDF: el codigo tiene que
            # avanzar hasta el final del parrafo, no cortarlo.
            "ancla_linea": line_in_article("ley30364_extracto.md", "10", '" Es deber de la Policía'),
            "texto_nuevo": re.search(r"La Policía Nacional del Perú coordina.*?expresión oral",
                                     dispositivo(LEY_32298).texto, re.S).group(0),
            "articulo_nuevo": False,
        },
    }
    classify.update(overrides.get("classify", {}))
    edits.update(overrides.get("edits", {}))
    return FakeGemini(classify, edits)


# ----------------------------------------------------------------- El Peruano


def test_listing_parser_reads_real_cards():
    refs = elperuano.parse_listing(fixture_text("listado.html"))
    assert [ref.id for ref in refs] == ["2575335-1", "2575334-1", "2575333-1"]
    assert refs[0].entidad == "DESARROLLO E INCLUSIÓN SOCIAL"
    assert refs[0].numero == "N° 002439-2026-MIDIS/PNCM-DE"
    assert refs[0].fecha == "17.11.2026"


def test_dispositivo_parser_reads_only_the_norm_body():
    parsed = elperuano.parse_dispositivo(LEY_32351, fixture_text("ficha_2403115-1.html"))
    assert parsed.fecha_publicacion == "27/05/2025"
    assert parsed.texto.startswith("LEY Nº 32351")
    # El <title> del documento embebido trae la sumilla de OTRA norma: no debe colarse.
    assert "Negociación Colectiva" not in parsed.texto


def test_unknown_page_is_an_error_not_an_empty_day():
    with pytest.raises(elperuano.ElPeruanoFormatError):
        elperuano.list_normas(date(2026, 1, 1), date(2026, 1, 1), fetch=lambda url: "<html>mantenimiento</html>", delay=0)
    with pytest.raises(elperuano.ElPeruanoFormatError):
        elperuano.parse_dispositivo("1-1", "<html>Fecha de publicación: 01/01/2026</html>")


# ----------------------------------------------------------------- deteccion


def test_number_variants_are_the_same_norm():
    assert amendments.normalize_norm_id("Resolución Ministerial N° 000050-2014-MC") == (
        amendments.normalize_norm_id("RESOLUCION MINISTERIAL Nº 050-2014-MC")
    )
    assert amendments.normalize_norm_id("Ley N.º 30364") == amendments.normalize_norm_id("LEY 30364")


def test_regulation_of_the_law_is_not_the_law():
    assert not amendments.mentions_norm("Decreto Supremo que modifica el Reglamento de la Ley N° 30364", WATCHED)
    assert amendments.mentions_norm("Ley que modifica la Ley 30364, Ley para prevenir", WATCHED)


def test_considerandos_are_not_the_dispositive_part():
    texto = dispositivo(RM_413_MC).texto
    dispositiva, found = amendments.dispositive_part(texto)
    assert found
    assert "Ley N° 27293" in texto and "Ley N° 27293" not in dispositiva
    assert "Derogar el artículo 3 de la Resolución Ministerial N° 050-2014-MC" in dispositiva


def test_effect_without_literal_evidence_in_dispositive_part_is_discarded():
    dispositiva, _ = amendments.dispositive_part(dispositivo(RM_413_MC).texto)
    watched = amendments.WatchedNorm(document="x.md", identificador="Ley N° 27293")
    fake = lambda prompt: {"efectos": [{"tipo": "deroga_total", "afecta_a": "norma", "articulos": [],
                                        "disposicion": "Artículo 1",
                                        "evidencia": "la Ley N° 27293, Ley del Sistema Nacional de Inversión Pública, la cual ha sido derogada"}]}
    _, effects, discarded = amendments.classify(dispositiva, watched, fake)
    assert effects == []
    assert "evidencia" in discarded[0]


# ----------------------------------------------------------------- propuestas


def scan_with(fake, refs_ids, workspace_path):
    refs = [elperuano.NormaRef(id=i, tipo="LEY", numero="N° X", sumilla="") for i in refs_ids]
    return amendments.scan(
        date(2025, 1, 1), date(2025, 12, 31),
        get_dispositivo=dispositivo,
        list_normas=lambda *args, **kwargs: refs,
        generate=fake,
    )


def test_both_real_amendments_become_one_verified_proposal(workspace):
    result = scan_with(fake_for_both_laws(), [LEY_32351, LEY_32298], workspace)
    [proposal] = result["proposals"]
    assert proposal["action"] == "revise"
    assert proposal["verification_problems"] == []
    assert [c["status"] for c in proposal["changes"]] == ["aplicado", "aplicado"]
    # Orden cronologico: la del 11/04 antes que la del 27/05.
    assert [c["norma"] for c in proposal["changes"]] == ["Ley Nº 32298", "Ley Nº 32351"]

    nuevo = (workspace / "amendments" / "proposals" / proposal["id"] / "propuesto.md").read_text(encoding="utf-8")
    original = fixture_text("ley30364_extracto.md")
    assert amendments.verify_only_insertions(original, nuevo) == []
    assert "- (*) Artículo modificado por el Artículo único de la Ley Nº 32351, publicada el 27 mayo 2025" in nuevo
    assert "- (*) Párrafo incorporado por el Artículo 1 de la Ley Nº 32298, publicada el 11 abril 2025" in nuevo
    # El cambio al articulo completo va al final del articulo 46 aunque Gemini apunte a la linea 1.
    assert nuevo.index("Los servicios de radiodifusión") < nuevo.index("Ley Nº 32351") < nuevo.index("## Artículo 47")
    # El texto nuevo se inserta sin el cierre '".' de la norma modificatoria.
    assert "familiar”" not in nuevo
    # El parrafo incorporado aparece una sola vez (en la prueba manual Gemini lo duplicaba).
    assert nuevo.count(ART10_PARRAFO) == 1
    # El parrafo partido en dos lineas por el PDF no queda cortado por la nota.
    assert nuevo.index("víctima,  acerca") < nuevo.index("Ley Nº 32298")
    # Y va despues del literal a), antes del b).
    assert nuevo.index(ART10_PARRAFO) < nuevo.index("## b. Asistencia")


def test_already_applied_amendment_is_not_applied_twice(workspace):
    first = scan_with(fake_for_both_laws(), [LEY_32351], workspace)["proposals"][0]
    nuevo = (workspace / "amendments" / "proposals" / first["id"] / "propuesto.md").read_text(encoding="utf-8")
    (workspace / "corpus" / DOC).write_text(nuevo, encoding="utf-8")

    second = scan_with(fake_for_both_laws(), [LEY_32351], workspace)["proposals"][0]
    assert second["action"] == "none"
    assert second["changes"][0]["status"] == "ya_aplicado"


def test_text_not_literal_from_el_peruano_is_never_inserted(workspace):
    fake = fake_for_both_laws(edits={("Ley Nº 32351", "46"): {
        "alcance": "Artículo", "ancla_linea": 0, "articulo_nuevo": False,
        "texto_nuevo": "Los medios de comunicación deben informar con veracidad."}})
    proposal = scan_with(fake, [LEY_32351], workspace)["proposals"][0]
    assert proposal["action"] == "none"
    assert proposal["changes"][0]["status"] == "aviso"
    assert "literal" in proposal["changes"][0]["detalle"]


def test_line_outside_the_article_becomes_a_warning(workspace):
    texto = re.search(r"La Policía Nacional del Perú coordina.*?expresión oral", dispositivo(LEY_32298).texto, re.S).group(0)
    fake = fake_for_both_laws(edits={("Ley Nº 32298", "10"): {
        "alcance": "Párrafo", "ancla_linea": 999, "articulo_nuevo": False, "texto_nuevo": texto}})
    proposal = scan_with(fake, [LEY_32298], workspace)["proposals"][0]
    assert proposal["action"] == "none"
    assert proposal["changes"][0]["status"] == "aviso"


def test_verification_blocks_rewrites_and_duplicates():
    old = "A\nB\nC"
    assert amendments.verify_only_insertions(old, "A\nB2\nC")
    problems = amendments.verify_proposal(old, "A\nB\n\" X Y Z \"\n\" X Y Z \"\nC", ["texto X Y Z texto"])
    assert any("dos veces" in problem for problem in problems)


def test_skipped_norms_are_not_proposed_again(workspace):
    proposal = scan_with(fake_for_both_laws(), [LEY_32351], workspace)["proposals"][0]
    amendments.decide(proposal["id"], "skip", None, decided_by="test")
    assert scan_with(fake_for_both_laws(), [LEY_32351], workspace)["proposals"] == []


def test_a_new_scan_supersedes_the_pending_proposal_and_keeps_its_norms(workspace):
    first = scan_with(fake_for_both_laws(), [LEY_32298], workspace)["proposals"][0]
    second = scan_with(fake_for_both_laws(), [LEY_32351], workspace)["proposals"][0]
    assert {item["id"] for item in second["normas"]} == {LEY_32298, LEY_32351}
    assert amendments.load_proposal(first["id"])["status"] == "superseded"


# ----------------------------------------------------------------- copias, aplicar y rollback


class FakeStore:
    """Un store en memoria con la misma interfaz que usan amendments y swap_document."""

    def __init__(self, documents: dict[str, dict]):
        self.documents = documents
        self.log: list[tuple] = []

    def get(self, display_name, store):
        doc = self.documents[display_name]
        return {"file_search_store": "fileSearchStores/test", "name": f"docs/{display_name}",
                "display_name": display_name, "size_bytes": doc["size"], "custom_metadata": doc["meta"]}

    def swap(self, upload, markdown, meta, delete, store, **kwargs):
        if upload:
            self.documents[upload] = {"size": len(markdown.encode("utf-8")), "meta": meta}
            corpus.sync_replacement(upload, markdown, delete)
        for name in delete:
            del self.documents[name]
        if not upload:
            corpus.remove_documents(delete)
        self.log.append(("swap", upload, tuple(delete)))
        return {"uploaded": upload, "deleted": list(delete)}


@pytest.fixture
def fake_store(workspace, monkeypatch):
    markdown = fixture_text("ley30364_extracto.md")
    meta = [{"key": "fuente", "string_value": "https://www.defensoria.gob.pe/ley30364.pdf"},
            {"key": "identificador", "string_value": "LEY Nº 30364"}]
    store = FakeStore({DOC: {"size": len(markdown.encode("utf-8")), "meta": meta}})
    monkeypatch.setattr(store_documents, "get_store_document", store.get)
    monkeypatch.setattr(store_documents, "swap_document", store.swap)
    return store


def test_apply_backs_up_first_and_rollback_restores_exactly(workspace, fake_store):
    original = fixture_text("ley30364_extracto.md")
    proposal = scan_with(fake_for_both_laws(), [LEY_32351, LEY_32298], workspace)["proposals"][0]

    result = amendments.decide(proposal["id"], "apply", None, decided_by="test")
    new_document = result["new_document"]
    assert new_document != DOC and new_document.startswith("ley3036-erradicarviolencia-")

    backup = amendments.load_backup(result["backup_id"])
    saved = (workspace / "amendments" / "backups" / result["backup_id"] / DOC).read_text(encoding="utf-8")
    assert saved == original
    assert backup["sha256"] == hashlib.sha256(original.encode("utf-8")).hexdigest()
    # La metadata viaja intacta, incluida la fuente que cargo el administrador.
    assert fake_store.documents[new_document]["meta"] == backup["custom_metadata"]
    assert amendments.load_registry()[0].document == new_document
    assert amendments.load_registry()[0].version_hasta == "2025-05-27"

    undo = amendments.rollback(result["backup_id"], None, decided_by="test")
    assert undo["restored"] == DOC
    assert set(fake_store.documents) == {DOC}
    assert (workspace / "corpus" / DOC).read_text(encoding="utf-8") == original
    assert not (workspace / "corpus" / new_document).exists()
    assert amendments.load_registry()[0].document == DOC
    # El rollback tambien respaldo lo que saco: se puede deshacer.
    assert amendments.load_backup(undo["pre_rollback_backup_id"])["document"] == new_document

    events = [entry["event"] for entry in amendments.load_history()]
    assert events == ["applied", "rollback"]


def test_no_change_without_a_verified_backup(workspace, fake_store):
    fake_store.documents[DOC]["size"] += 1  # la copia local ya no es lo indexado
    proposal = scan_with(fake_for_both_laws(), [LEY_32351], workspace)["proposals"][0]
    with pytest.raises(amendments.BackupError):
        amendments.decide(proposal["id"], "apply", None)
    assert fake_store.log == []


def test_stale_proposal_is_refused(workspace, fake_store):
    proposal = scan_with(fake_for_both_laws(), [LEY_32351], workspace)["proposals"][0]
    (workspace / "corpus" / DOC).write_text(fixture_text("ley30364_extracto.md") + "\nnuevo", encoding="utf-8")
    with pytest.raises(amendments.AmendmentError, match="cambio"):
        amendments.decide(proposal["id"], "apply", None)
    assert fake_store.log == []


def test_backups_inside_the_corpus_are_invisible_to_the_locator(workspace, monkeypatch):
    monkeypatch.setattr(settings, "amendments_dir", str(workspace / "corpus" / "_amendments"))
    hidden = workspace / "corpus" / "_amendments" / "backups" / "x"
    hidden.mkdir(parents=True)
    (hidden / DOC).write_text("version vieja", encoding="utf-8")
    assert [path.name for path in corpus.iter_corpus_files()] == [DOC]
    assert corpus.resolve_document_path(DOC, None) == workspace / "corpus" / DOC


def test_total_repeal_proposes_deletion_and_rollback_brings_it_back(workspace, fake_store):
    fake = FakeGemini({"LEY Nº 32351": {"norma_publicada": "Ley Nº 32351", "efectos": [{
        "tipo": "deroga_total", "afecta_a": "norma", "articulos": [], "disposicion": "Artículo único",
        "evidencia": "Se modifica el artículo 46 de la Ley 30364"}]}}, {})
    proposal = scan_with(fake, [LEY_32351], workspace)["proposals"][0]
    assert proposal["action"] == "delete"
    result = amendments.decide(proposal["id"], "apply", None)
    assert DOC not in fake_store.documents
    assert not (workspace / "corpus" / DOC).exists()
    amendments.rollback(result["backup_id"], None)
    assert DOC in fake_store.documents
    assert (workspace / "corpus" / DOC).read_text(encoding="utf-8") == fixture_text("ley30364_extracto.md")


# ----------------------------------------------------------------- API


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_scan_defaults_to_yesterday_and_today(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(amendments, "scan", lambda ini, fin, docs: seen.update(ini=ini, fin=fin) or {"proposals": []})
    assert client.post("/amendments/scan", json={}).status_code == 200
    assert (seen["fin"] - seen["ini"]).days == 1


def test_source_format_change_is_a_502_not_an_empty_result(client, monkeypatch):
    def broken(*args):
        raise elperuano.ElPeruanoFormatError("cambio el sitio")

    monkeypatch.setattr(amendments, "scan", broken)
    response = client.post("/amendments/scan", json={"fecha_ini": "2026-11-01", "fecha_fin": "2026-11-02"})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "SOURCE_FORMAT_CHANGED"


def test_failed_backup_is_reported_as_such(client, monkeypatch):
    def refuse(*args):
        raise amendments.BackupError("no coincide")

    monkeypatch.setattr(amendments, "decide", refuse)
    response = client.post("/amendments/decide", json={"proposal_id": "0" * 12, "decision": "apply"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "BACKUP_FAILED"


def test_decision_must_be_apply_or_skip(client):
    response = client.post("/amendments/decide", json={"proposal_id": "0" * 12, "decision": "borrar"})
    assert response.status_code == 422


def test_seed_registry_is_used_until_the_live_one_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "amendments_registry_path", str(tmp_path / "no-existe.json"))
    norms = amendments.load_registry()
    assert {norm.document for norm in norms} >= {"ley3036-erradicarviolencia-0eeaf4de148c.md", "codigo-civil-97fe57ba02fb.md"}
    for norm in norms:
        assert norm.aliases or amendments.normalize_norm_id(norm.identificador), norm.document
        date.fromisoformat(norm.version_hasta)


def test_a_protocol_without_number_is_watched_by_name():
    pbac = amendments.WatchedNorm(document="p.md", identificador="Protocolo Base de Actuación Conjunta",
                                  aliases=["Protocolo Base de Actuación Conjunta"])
    assert amendments.mentions_norm("Aprueban la actualización del Protocolo Base de Actuación Conjunta", pbac)
    assert not amendments.mentions_norm("Aprueban otro protocolo", pbac)


def test_sector_suffix_is_part_of_the_number():
    assert amendments.normalize_norm_id("Resolución Ministerial N° 100-2021-MIMP") == "RESOLUCION MINISTERIAL 100-2021-MIMP"
    assert amendments.normalize_norm_id("R.M. ... Resolución Ministerial Nº 100-2021-MINSA") != (
        amendments.normalize_norm_id("Resolución Ministerial N° 100-2021-MIMP")
    )
    assert amendments.normalize_norm_id("Decreto Supremo N° 249-2026-EF") == "DECRETO SUPREMO 249-2026-EF"


def test_mention_alone_is_not_a_change_but_a_nearby_verb_is():
    assert not amendments.mentions_with_change(
        "Artículo 1.- Disponer la implementación de módulos conforme a la Ley N° 30364", WATCHED)
    assert amendments.mentions_with_change("Modifícase el artículo 46 de la Ley 30364", WATCHED)
    assert amendments.mentions_with_change(
        "Apruébase el Texto Único Ordenado de la Ley N° 30364", WATCHED)
    assert not amendments.mentions_with_change(
        "Modifícase el artículo 5 del Reglamento de la Ley N° 30364", WATCHED)


def test_backfill_searches_exact_phrases_and_splits_large_ranges():
    calls = []

    def fake_list(ini, fin, query=None):
        calls.append((ini, fin, query))
        if (fin - ini).days > 40:
            raise elperuano.ElPeruanoError("El rango devolvio mas de 1000 normas; acotalo")
        return [elperuano.NormaRef(id=f"{ini}-{query}", tipo="LEY", numero="N° 1", sumilla="")]

    refs = amendments.collect_candidates([WATCHED], date(2025, 1, 1), date(2025, 6, 30), fake_list)
    assert all(query.startswith('"') for _, _, query in calls)
    assert max((fin - ini).days for ini, fin, _ in calls if (fin - ini).days <= 40) <= 40
    assert refs


def test_backfill_skips_cards_that_cannot_carry_a_change():
    assert amendments.worth_fetching(elperuano.NormaRef(id="1-1", tipo="LEY", sumilla="Ley de presupuesto"))
    assert amendments.worth_fetching(elperuano.NormaRef(id="1-1", tipo="RESOLUCIÓN MINISTERIAL", sumilla="Modifican el Protocolo"))
    assert not amendments.worth_fetching(elperuano.NormaRef(id="1-1", tipo="RESOLUCIÓN ADMINISTRATIVA", sumilla="Designan jueces"))


def test_article_headers_in_the_forms_real_markdowns_use():
    lines = [
        "## Artículo 9º", "## A la libertad de opinión", "texto 9", "",
        "## Artículo 90º", "texto 90", "",
        "## Artículo 3- A", "## Derecho al buen trato", "texto 3-A", "",
        "## Artículo 46. Obligaciones generales", "texto 46", "",
        "## ' Artículo 46-E. Circunstancia agravante", "texto 46-E",
    ]
    assert amendments.find_article(lines, "9") == (0, 3)
    assert amendments.find_article(lines, "90") == (4, 6)
    assert amendments.find_article(lines, "3-A") == (7, 10)
    assert amendments.find_article(lines, "46") == (11, 13)
    assert amendments.find_article(lines, "46-E") == (14, 16)
    assert amendments.find_article(lines, "3") is None


def test_find_article_spans_quoted_versions_under_notes():
    lines = [
        "## Artículo 15. Denuncia",
        "texto original",
        "- (*) Artículo modificado por el Artículo 2 del Decreto Legislativo N° 1386, publicado el 04 septiembre 2018, cuyo texto es el siguiente:",
        "## 'Artículo 15. Denuncia",
        "texto nuevo",
        "## Artículo 16. Plazo",
        "## DISPOSICIONES COMPLEMENTARIAS MODIFICATORIAS",
        "## ' Artículo 15. Del Código Penal citado",
    ]
    assert amendments.find_article(lines, "15") == (0, 5)
    assert amendments._insertion_point_for_new_article(lines, "Sexta Disposición Complementaria Final") is None


def test_note_agrees_with_the_publishing_norm():
    decreto = amendments.build_note_block("Párrafo", "modifica", "SEGUNDA", "Decreto Legislativo N° 1703", "24/01/2026", "x")[1]
    assert "por la Segunda del Decreto Legislativo N° 1703, publicado el" in decreto
    titled = amendments.build_note_block("Párrafo", "modifica", "SEGUNDA. Modificación de la Ley N.° 30364, Ley para prevenir", "Decreto Legislativo N° 1703", "24/01/2026", "x")[1]
    assert "por la Segunda del Decreto Legislativo N° 1703," in titled
    ley = amendments.build_note_block("Artículo", "modifica", "Artículo único", "Ley N° 32351", "27/05/2025", "x")[1]
    assert "por el Artículo único de la Ley N° 32351, publicada el" in ley


def test_find_article_leaves_the_note_introducing_the_next_article():
    lines = [
        "## Artículo 17. Flagrancia",
        "texto 17",
        "",
        "- (*) Artículo incorporado por el Artículo 3 del Decreto Legislativo N° 1386, publicado el 04 septiembre 2018, cuyo texto es el siguiente:",
        "",
        "## ' Artículo 17-A. Medidas",
    ]
    assert amendments.find_article(lines, "17") == (0, 2)


def test_same_heading_in_two_notes_is_not_a_duplicate():
    old = "## Artículo 79. Acreditación\ntexto"
    new = (
        old + "\n- (*) Artículo modificado por el Artículo 19 de la Ley N° 31716, publicada el 22 marzo 2023, cuyo texto es el siguiente:"
        "\nArtículo 79. Acreditación\nuno"
        "\n- (*) Párrafo modificado por el Artículo 1 de la Ley N° 31994, publicada el 2 abril 2024, cuyo texto es el siguiente:"
        "\nArtículo 79. Acreditación\ndos"
    )
    sources = ["Artículo 79. Acreditación\nuno", "Artículo 79. Acreditación\ndos"]
    assert amendments.verify_proposal(old, new, sources) == []
    doubled = old + "\n- (*) nota\nuno\nuno"
    assert any("dos veces" in p for p in amendments.verify_proposal(old, doubled, ["uno"]))
