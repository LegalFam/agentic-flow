import json

import pytest

from app import corpus, store_documents
from app.config import settings


def document(display_name: str, size_bytes: int = 100) -> dict:
    return {
        "name": f"fileSearchStores/s/documents/{display_name.replace('.', '-')}",
        "display_name": display_name,
        "size_bytes": size_bytes,
        "state": "STATE_ACTIVE",
        "mime_type": "text/markdown",
        "create_time": None,
    }


def plan(filename: str, display_names: list[str]) -> dict:
    return store_documents._plan_from_documents(
        "fileSearchStores/s", filename, [document(name) for name in display_names]
    )


def test_replacement_key_ignores_the_document_id_hash():
    # El hash sale del sha256 del PDF, asi que la revision nueva trae otro y por igualdad
    # de nombre no encontraria a la vieja.
    assert store_documents.replacement_key("codigo-civil-97fe57ba02fb.md") == store_documents.replacement_key(
        "codigo-civil-000000000000.md"
    )


def test_replacement_key_ignores_separators_and_case():
    assert store_documents.replacement_key("codigo-civil-1a7a94997c08.md") == (
        store_documents.replacement_key("Codigo_Civil.md")
    )


def test_a_renamed_pdf_does_not_match_and_falls_to_supersedes():
    """`build_document_id` pierde el acento en vez de normalizarlo.

    `Codigo Procesal Civil.pdf` se indexo como `c-digo-...`: la vocal no esta, y de
    `c_digo` no se puede recuperar `codigo` sin adivinar. Mientras el PDF conserve su
    nombre las dos revisiones se manglean igual y casan; si alguien lo renombra entre
    revisiones, el plan sale "new" y hay que pasar `supersedes` a mano.
    """
    assert store_documents.replacement_key("c-digo-procesal-civil-1a7a94997c08.md") != (
        store_documents.replacement_key("codigo-procesal-civil-ffffffffffff.md")
    )
    assert plan("c-digo-procesal-civil-ffffffffffff.md", ["c-digo-procesal-civil-1a7a94997c08.md"])[
        "verdict"
    ] == "replace"


def test_different_documents_do_not_share_a_key():
    assert store_documents.replacement_key("codigo-civil-aaaaaaaaaaaa.md") != (
        store_documents.replacement_key("codigo-procesal-civil-aaaaaaaaaaaa.md")
    )


def test_plan_finds_the_previous_revision():
    result = plan("codigo-civil-ffffffffffff.md", ["codigo-civil-97fe57ba02fb.md", "cas-563-2011-1554e6ea4f6e.md"])
    assert result["verdict"] == "replace"
    assert [item["display_name"] for item in result["superseded"]] == ["codigo-civil-97fe57ba02fb.md"]


def test_plan_reports_nothing_to_replace():
    result = plan("ley-31572-aaaaaaaaaaaa.md", ["codigo-civil-97fe57ba02fb.md"])
    assert result["verdict"] == "new"
    assert result["superseded"] == []


def test_plan_reports_ambiguity_instead_of_picking():
    result = plan(
        "codigo-civil-ffffffffffff.md",
        ["codigo-civil-97fe57ba02fb.md", "Codigo Civil.md"],
    )
    assert result["verdict"] == "ambiguous"
    assert len(result["superseded"]) == 2


def test_plan_detects_the_same_pdf_reuploaded():
    # Mismo display_name = mismo hash = mismo PDF: no hay texto nuevo que indexar.
    result = plan("codigo-civil-97fe57ba02fb.md", ["codigo-civil-97fe57ba02fb.md"])
    assert result["verdict"] == "identical"


def test_nothing_to_replace_is_refused_unless_allowed():
    result = plan("ley-31572-aaaaaaaaaaaa.md", ["codigo-civil-97fe57ba02fb.md"])
    with pytest.raises(store_documents.ReplaceRefused):
        store_documents._decide_targets(result, [], [], allow_new=False, allow_multiple=False)
    assert store_documents._decide_targets(result, [], [], allow_new=True, allow_multiple=False) == []


def test_ambiguity_is_refused_unless_allowed():
    documents = [document("codigo-civil-97fe57ba02fb.md"), document("Codigo Civil.md")]
    result = store_documents._plan_from_documents("fileSearchStores/s", "codigo-civil-ffffffffffff.md", documents)
    with pytest.raises(store_documents.ReplaceRefused):
        store_documents._decide_targets(result, documents, [], allow_new=False, allow_multiple=False)
    targets = store_documents._decide_targets(result, documents, [], allow_new=False, allow_multiple=True)
    assert len(targets) == 2


def test_supersedes_overrides_the_plan():
    documents = [document("codigo-civil-97fe57ba02fb.md"), document("Codigo Civil.md")]
    result = store_documents._plan_from_documents("fileSearchStores/s", "codigo-civil-ffffffffffff.md", documents)
    targets = store_documents._decide_targets(
        result, documents, ["Codigo Civil.md"], allow_new=False, allow_multiple=False
    )
    assert [item["display_name"] for item in targets] == ["Codigo Civil.md"]


def test_supersedes_rejects_a_document_that_is_not_in_the_store():
    documents = [document("codigo-civil-97fe57ba02fb.md")]
    result = store_documents._plan_from_documents("fileSearchStores/s", "codigo-civil-ffffffffffff.md", documents)
    with pytest.raises(store_documents.ReplaceRefused):
        store_documents._decide_targets(
            result, documents, ["no-existe.md"], allow_new=False, allow_multiple=False
        )


@pytest.fixture
def corpus_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_dir", str(tmp_path))
    corpus.clear_cache()
    yield tmp_path
    corpus.clear_cache()


def test_sync_writes_the_new_revision_and_drops_the_old_file(corpus_dir):
    (corpus_dir / "codigo-civil-97fe57ba02fb.md").write_text("viejo", encoding="utf-8")

    result = corpus.sync_replacement(
        "codigo-civil-ffffffffffff.md", "nuevo", ["codigo-civil-97fe57ba02fb.md"]
    )

    assert result["synced"] is True
    assert result["removed"] == ["codigo-civil-97fe57ba02fb.md"]
    assert (corpus_dir / "codigo-civil-ffffffffffff.md").read_text(encoding="utf-8") == "nuevo"
    assert not (corpus_dir / "codigo-civil-97fe57ba02fb.md").exists()


def test_sync_does_not_delete_the_file_it_just_wrote(corpus_dir):
    """El nombre viejo y el nuevo normalizan a la misma llave: es lo que los emparejo.

    Si los viejos se resolvieran despues de escribir el nuevo, `resolve_document_path`
    del nombre viejo devolveria el archivo nuevo por el fallback de stem sin hash, y el
    borrado se llevaria la revision recien subida dejando el corpus vacio.
    """
    result = corpus.sync_replacement(
        "codigo-civil-ffffffffffff.md", "nuevo", ["codigo-civil-97fe57ba02fb.md"]
    )

    assert result["synced"] is True
    assert result["removed"] == []
    assert (corpus_dir / "codigo-civil-ffffffffffff.md").read_text(encoding="utf-8") == "nuevo"


def test_sync_prunes_the_manifest_entry_of_the_removed_file(corpus_dir):
    (corpus_dir / "viejo.md").write_text("viejo", encoding="utf-8")
    (corpus_dir / "otro.md").write_text("otro", encoding="utf-8")
    manifest = corpus_dir / settings.corpus_manifest_filename
    manifest.write_text(
        json.dumps({"Codigo Civil - Libro III.md": "viejo.md", "Otro.md": "otro.md"}),
        encoding="utf-8",
    )

    result = corpus.sync_replacement("codigo-civil-ffffffffffff.md", "nuevo", ["Codigo Civil - Libro III.md"])

    assert result["removed"] == ["viejo.md"]
    assert result["manifest_pruned"] == ["Codigo Civil - Libro III.md"]
    assert corpus.load_manifest() == {"Otro.md": "otro.md"}


def test_sync_reports_a_missing_corpus_directory_instead_of_raising(monkeypatch):
    # En Cloud Run el corpus es un volumen de solo lectura: no poder escribir es un
    # resultado esperado, y el reemplazo en el store ya paso.
    monkeypatch.setattr(settings, "corpus_dir", "/no/existe/aqui")
    result = corpus.sync_replacement("nuevo.md", "texto", [])
    assert result["synced"] is False
    assert "no existe" in result["reason"]


def test_sync_gives_the_new_file_the_md_extension(corpus_dir):
    corpus.sync_replacement("codigo-civil-ffffffffffff", "nuevo", [])
    assert (corpus_dir / "codigo-civil-ffffffffffff.md").exists()


def test_sync_reports_a_read_only_corpus_instead_of_raising(corpus_dir, monkeypatch):
    """Es el caso de Cloud Run: /corpus se monta con readonly=true.

    El reemplazo en el store ya ocurrio cuando se llega aca, asi que una excepcion no
    ayudaria a nadie: lo que hace falta es que la respuesta diga que el corpus quedo
    atrasado para que el log del workflow lo registre.
    """
    (corpus_dir / "codigo-civil-97fe57ba02fb.md").write_text("viejo", encoding="utf-8")

    def read_only(self, *args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr("pathlib.Path.write_text", read_only)
    result = corpus.sync_replacement(
        "codigo-civil-ffffffffffff.md", "nuevo", ["codigo-civil-97fe57ba02fb.md"]
    )

    assert result["synced"] is False
    assert "Read-only file system" in result["reason"]
    # Y sobre todo: no se borro la revision vieja. Si se hubiera borrado sin poder
    # escribir la nueva, el corpus se quedaria sin ninguna de las dos.
    assert (corpus_dir / "codigo-civil-97fe57ba02fb.md").exists()
