"""El registro es lo que impide que un LLM invente un numero de articulo.

Los agentes solo transportan el `citation_id`; el locator sale de aca. La propiedad
critica que fijan estos tests es la degradacion: un id desconocido o vencido devuelve
vacio, nunca algo parecido ni algo inventado.
"""

import pytest

from app import locator_registry
from app.config import settings

FIELDS = {
    "locator": "Art. 333",
    "breadcrumb": "Libro III > Art. 333",
    "page": None,
    "locator_source": "exact",
}


@pytest.fixture(autouse=True)
def clean_registry():
    locator_registry.clear()
    yield
    locator_registry.clear()


def test_registers_and_resolves():
    locator_registry.register("abc123", FIELDS)
    assert locator_registry.resolve("abc123") == FIELDS


def test_unknown_id_returns_none():
    assert locator_registry.resolve("no-existe") is None


def test_empty_id_is_ignored():
    locator_registry.register("", FIELDS)
    assert locator_registry.resolve("") is None
    assert locator_registry.size() == 0


def test_entry_expires(monkeypatch):
    monkeypatch.setattr(settings, "locator_registry_ttl_seconds", 0)
    locator_registry.register("abc123", FIELDS)
    assert locator_registry.resolve("abc123") is None


def test_returns_a_copy_so_callers_cannot_mutate_the_registry():
    locator_registry.register("abc123", FIELDS)
    first = locator_registry.resolve("abc123")
    first["locator"] = "Art. 999"
    assert locator_registry.resolve("abc123")["locator"] == "Art. 333"


def test_evicts_when_over_capacity(monkeypatch):
    monkeypatch.setattr(settings, "locator_registry_max_entries", 10)
    for index in range(40):
        locator_registry.register(f"id-{index}", FIELDS)
    assert locator_registry.size() <= 10
    # Lo ultimo registrado debe sobrevivir: es lo que una busqueda en curso va a pedir.
    assert locator_registry.resolve("id-39") is not None


def test_re_registering_the_same_id_refreshes_it():
    locator_registry.register("abc123", FIELDS)
    locator_registry.register("abc123", {**FIELDS, "locator": "Art. 481"})
    assert locator_registry.resolve("abc123")["locator"] == "Art. 481"
    assert locator_registry.size() == 1
