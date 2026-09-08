"""Que normas del corpus se pueden citar, y como se las nombra en una respuesta.

Es la tabla que une tres vocabularios que no coinciden entre si:

- el nombre del fichero en `work/corpus/` ("codigo-de-los-ni-os-y-adolescentes-<hash>.md",
  con los acentos ya perdidos al convertir el PDF),
- la clave corta y estable que usa el dataset ("codigo_ninos_adolescentes"),
- y como el modelo escribe la norma en la respuesta ("Codigo de los Ninos y Adolescentes",
  "CNA", "Ley 30364", ...).

Sin este tercer vocabulario no se puede decidir si un "articulo 481" que aparece en el
texto es del Codigo Civil o de otra norma, y sin eso no se puede medir alucinacion.
"""

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Norm:
    key: str
    display: str
    # Fragmento del nombre de fichero en el corpus, ya sin acentos y en minusculas.
    stem_contains: str
    # Como puede aparecer escrita en una respuesta. Se comparan sobre texto plegado
    # (sin acentos, en minusculas), asi que aca van sin acentos.
    aliases: tuple[str, ...] = field(default_factory=tuple)
    # Normas sin articulado propio citable (jurisprudencia, protocolos): existen en el
    # corpus y pueden respaldar una cita, pero no se les exige registro de articulos.
    articulated: bool = True


NORMS: tuple[Norm, ...] = (
    Norm(
        key="codigo_civil",
        display="Codigo Civil",
        stem_contains="codigo-civil",
        aliases=("codigo civil", "c.c.", "cc peruano", "del cc"),
    ),
    Norm(
        key="codigo_ninos_adolescentes",
        display="Codigo de los Ninos y Adolescentes",
        stem_contains="ni-os-y-adolescentes",
        aliases=(
            "codigo de los ninos y adolescentes",
            "codigo del nino y adolescente",
            "codigo de los ninos y los adolescentes",
            "c.n.a.",
            "cna",
        ),
    ),
    Norm(
        key="codigo_procesal_civil",
        display="Codigo Procesal Civil",
        stem_contains="procesal-civil",
        aliases=("codigo procesal civil", "c.p.c.", "cpc"),
    ),
    Norm(
        key="ley_30364",
        display="Ley 30364",
        stem_contains="ley3036",
        aliases=("ley 30364", "ley n 30364", "ley no 30364", "ley 30.364"),
    ),
    Norm(
        key="ley_26872",
        display="Ley 26872 de Conciliacion",
        stem_contains="ley-26872",
        aliases=("ley 26872", "ley de conciliacion", "ley n 26872"),
    ),
    Norm(
        key="dl_1297",
        display="Decreto Legislativo 1297",
        stem_contains="1297",
        aliases=("decreto legislativo 1297", "d.l. 1297", "dl 1297", "decreto legislativo n 1297"),
    ),
    Norm(
        key="tercer_pleno_casatorio",
        display="Tercer Pleno Casatorio Civil",
        stem_contains="tercer-pleno-casatorio",
        aliases=("tercer pleno casatorio",),
        articulated=False,
    ),
    Norm(
        key="octavo_pleno_casatorio",
        display="Octavo Pleno Casatorio Civil",
        stem_contains="octavo-pleno-casatorio",
        aliases=("octavo pleno casatorio",),
        articulated=False,
    ),
    Norm(
        key="protocolo_cem",
        display="Protocolo de atencion del Centro Emergencia Mujer",
        stem_contains="centro-emergencia-mujer",
        aliases=("protocolo del cem", "protocolo de atencion del centro emergencia mujer"),
        articulated=False,
    ),
    Norm(
        key="protocolo_pbac",
        display="Protocolo Base de Actuacion Conjunta",
        stem_contains="protocolo-base-de-actuacion",
        aliases=("protocolo base de actuacion conjunta", "pbac"),
        articulated=False,
    ),
)

BY_KEY = {norm.key: norm for norm in NORMS}

# Las normas cuyo articulado se puede verificar numero por numero contra el corpus.
ARTICULATED_KEYS = tuple(norm.key for norm in NORMS if norm.articulated)


def normalize_article(raw: str) -> str:
    """"Art. 4-A", "articulo 4 a", "4°" -> "4A". Un numero de articulo, una forma."""
    cleaned = raw.strip().strip("*_# ").upper()
    cleaned = cleaned.replace("ART.", "").replace("ARTICULO", "").replace("ARTÍCULO", "")
    cleaned = re.sub(r"[°º]", "", cleaned)
    cleaned = re.sub(r"[\s\-–—.]+", "", cleaned)
    return cleaned
