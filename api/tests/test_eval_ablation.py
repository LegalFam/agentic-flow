"""Que los parches de la ablacion realmente ablacionan, y fallan si dejan de encajar."""

import copy
import json

import pytest

from eval.ablation import arms


@pytest.fixture(scope="module")
def production() -> dict:
    from eval.ablation import build_workflows

    if not build_workflows.SOURCE.exists():
        pytest.skip("no esta el workflow de produccion")
    return json.loads(build_workflows.SOURCE.read_text(encoding="utf-8"))


@pytest.fixture
def workflow(production) -> dict:
    return copy.deepcopy(production)


@pytest.mark.parametrize("arm", sorted(arms.ARMS))
def test_every_arm_builds(workflow, arm):
    built = arms.build(workflow, arm)
    assert built["name"] == f"LegalFam Eval - {arm}"
    assert arms.node(built, "Webhook")["parameters"]["path"] == f"chat-process-eval-{arm}"


@pytest.mark.parametrize("arm", sorted(arms.ARMS))
def test_arms_get_distinct_webhooks(production, arm):
    paths = {
        arms.node(arms.build(copy.deepcopy(production), name), "Webhook")["parameters"]["path"]
        for name in arms.ARMS
    }
    assert len(paths) == len(arms.ARMS)


@pytest.mark.parametrize("arm", sorted(arms.ARMS))
def test_ids_are_stable_across_builds(production, arm):
    first = arms.build(copy.deepcopy(production), arm)
    second = arms.build(copy.deepcopy(production), arm)
    assert [item["id"] for item in first["nodes"]] == [item["id"] for item in second["nodes"]]
    assert first["id"] == second["id"]


@pytest.mark.parametrize("arm", sorted(arms.ARMS))
def test_temperature_is_pinned_everywhere(workflow, arm):
    built = arms.build(workflow, arm)
    models = [item for item in built["nodes"] if item["type"].endswith("lmChatGoogleGemini")]
    assert models, "el workflow deberia tener nodos de modelo"
    assert all(item["parameters"]["options"]["temperature"] == 0 for item in models)


def test_no_rag_removes_the_retrieval_tool(workflow):
    built = arms.build(workflow, "no_rag")
    assert not any(item["name"] == "SearchStore" for item in built["nodes"])
    assert arms._tools_of(built, "RAG Agent") == []
    assert "SearchStore" not in arms.node(built, "RAG Agent")["parameters"]["options"]["systemMessage"]


def test_no_rag_keeps_the_explainability_layer(workflow):
    built = arms.build(workflow, "no_rag")
    assert arms.node(built, "Resolve Locators")
    assert arms.node(built, "Attach Locators")


def test_no_xai_reduces_the_contract_to_the_answer(workflow):
    built = arms.build(workflow, "no_xai")
    schema = json.loads(arms.node(built, "XAI Output Parser")["parameters"]["inputSchema"])
    assert list(schema["properties"]) == ["answer"]
    assert schema["additionalProperties"] is False

    message = arms.node(built, "XAI Agent")["parameters"]["options"]["systemMessage"]
    assert not any(token in message for token in arms.XAI_FORBIDDEN)


def test_no_xai_keeps_the_writer(workflow):
    """La ablacion es de la explicabilidad, no del redactor: si se llevara por delante el
    encargo de redaccion, el brazo mediria prosa y no verificabilidad."""
    built = arms.build(workflow, "no_xai")
    message = arms.node(built, "XAI Agent")["parameters"]["options"]["systemMessage"]
    assert "Formato de answer:" in message
    assert "Trato conversacional:" in message
    assert "Reglas de seguridad:" in message


def test_no_xai_keeps_retrieval(workflow):
    built = arms.build(workflow, "no_xai")
    assert arms._tools_of(built, "RAG Agent") == ["SearchStore"]


def test_no_xai_rewires_the_response_builder(workflow):
    built = arms.build(workflow, "no_xai")
    assert not any(item["name"] == "Resolve Locators" for item in built["nodes"])
    assert built["connections"]["XAI Agent"]["main"][0] == [
        {"node": "Build Response XAI", "type": "main", "index": 0}
    ]


def test_base_ablates_both_axes(workflow):
    built = arms.build(workflow, "base")
    assert arms._tools_of(built, "RAG Agent") == []
    message = arms.node(built, "XAI Agent")["parameters"]["options"]["systemMessage"]
    assert not any(token in message for token in arms.XAI_FORBIDDEN)


def test_inline_control_lets_the_answer_name_its_sources(workflow):
    built = arms.build(workflow, "no_xai_inline")
    message = arms.node(built, "XAI Agent")["parameters"]["options"]["systemMessage"]
    assert "nombra el articulo y la norma dentro de answer" in message
    assert "No incluyas una seccion de fuentes" not in message


def test_patch_fails_loudly_when_the_anchor_moved(workflow):
    """Un parche que no encuentra su ancla no puede seguir: produciria un brazo que dice
    estar ablacionado y no lo esta, y eso no se nota en los resultados."""
    agent = arms.node(workflow, "RAG Agent")
    agent["parameters"]["options"]["systemMessage"] = "prompt reescrito sin las anclas"

    with pytest.raises(arms.PatchError):
        arms.build(workflow, "no_rag")


def test_verify_rejects_an_arm_that_kept_its_tool(workflow):
    built = arms.build(copy.deepcopy(workflow), "full")
    with pytest.raises(arms.PatchError, match="herramientas"):
        arms.verify(built, "no_rag")


def test_dropping_a_missing_node_is_an_error(workflow):
    with pytest.raises(arms.PatchError):
        arms.drop_node(workflow, "Nodo Que No Existe")


# --------------------------------------------------------------------------------------
# Deteccion de brazos desincronizados
#
# Los brazos generados se versionan, y el riesgo de eso no es que ocupen sitio: es que se
# queden viejos en silencio cuando alguien toca un prompt de produccion, y que la corrida
# siguiente compare contra una version del sistema que ya no existe.


def _write_arms(production: dict, out) -> list[tuple[str, dict]]:
    from eval.ablation import build_workflows

    variants = [(arm, arms.build(copy.deepcopy(production), arm)) for arm in arms.FACTORIAL]
    for arm, variant in variants:
        build_workflows.serialize(out / f"legalfam-eval-{arm}.json", variant)
    return variants


def test_check_passes_when_arms_are_current(production, tmp_path):
    from eval.ablation import build_workflows

    variants = _write_arms(production, tmp_path)
    assert build_workflows.check(variants, tmp_path) == 0


def test_check_fails_when_production_moved_on(production, tmp_path):
    from eval.ablation import build_workflows

    _write_arms(production, tmp_path)

    moved = copy.deepcopy(production)
    agent = arms.node(moved, "XAI Agent")
    agent["parameters"]["options"]["systemMessage"] += "\n- Una regla nueva."
    regenerated = [(arm, arms.build(copy.deepcopy(moved), arm)) for arm in arms.FACTORIAL]

    assert build_workflows.check(regenerated, tmp_path) == 2


def test_check_fails_when_an_arm_was_never_generated(production, tmp_path):
    from eval.ablation import build_workflows

    variants = _write_arms(production, tmp_path)
    (tmp_path / "legalfam-eval-no_rag.json").unlink()

    assert build_workflows.check(variants, tmp_path) == 2
