"""Registro de que articulos existen realmente en el corpus, y detector de los que se citan.

Dos usos, y el mismo registro sirve a los dos:

1. Verificar el ground truth. Un dataset cuyos `expected_articles` no existen en el corpus
   mide el error del dataset, no el del sistema. `--verify-dataset` sale distinto de cero
   si alguno no existe.
2. Medir alucinacion normativa. Una respuesta que dice "articulo 1481 del Codigo Civil"
   se puede contrastar contra el articulado real: si el numero no existe, esta inventado.
   Es la metrica que separa el brazo con recuperacion del que responde de memoria.

    python -m eval.corpus_articles                       # resumen del registro
    python -m eval.corpus_articles --list codigo_civil   # articulos de una norma
    python -m eval.corpus_articles --verify-dataset eval/dataset/family_law_v1.jsonl
"""

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app import corpus, locator
from eval.norms import NORMS, Norm, normalize_article


def deaccent(text: str) -> str:
    """Para comparar contra los alias: el corpus perdio acentos al convertir el PDF y el
    modelo los escribe o no segun el dia."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


@dataclass
class NormIndex:
    norm: Norm
    path: Path
    index: locator.DocumentIndex
    # articulo normalizado -> (offset de inicio, offset de fin) en coordenadas base
    articles: dict[str, tuple[int, int]]

    def has(self, article: str) -> bool:
        return normalize_article(article) in self.articles

    def text_of(self, article: str) -> str:
        span = self.articles.get(normalize_article(article))
        if span is None:
            return ""
        return self.index.base[span[0] : span[1]]


def _article_spans(index: locator.DocumentIndex) -> dict[str, tuple[int, int]]:
    """Cada articulo va desde su encabezado hasta el siguiente encabezado, del nivel que sea.

    Se corta en cualquier encabezado estructural y no solo en el siguiente articulo: el
    texto que sigue a un "TITULO III" ya no pertenece al ultimo articulo del titulo
    anterior, y darselo ensancharia el tramo hasta el absurdo.
    """
    boundaries = sorted(
        heading.offset
        for heading in index.headings
        if heading.kind == "articulo" or heading.level <= locator.LEVEL_ARTICULO
    )
    spans: dict[str, tuple[int, int]] = {}
    for heading in index.headings:
        if heading.kind != "articulo":
            continue
        key = normalize_article(heading.label)
        if key in spans:
            # Un articulo repetido (el indice del codigo al inicio del documento, o un
            # encabezado duplicado por el OCR): manda la primera aparicion.
            continue
        end = next((offset for offset in boundaries if offset > heading.offset), len(index.base))
        spans[key] = (heading.offset, end)
    return spans


@lru_cache(maxsize=1)
def build_registry() -> dict[str, NormIndex]:
    """Norma -> su articulado. Solo las normas que la tabla `NORMS` sabe nombrar."""
    files = corpus.iter_corpus_files()
    registry: dict[str, NormIndex] = {}

    for norm in NORMS:
        matches = [path for path in files if norm.stem_contains in path.stem.lower()]
        if not matches:
            continue
        # Si hay mas de uno (una version vieja que no se borro), gana el mas grande: el
        # truncado por un fallo de conversion nunca es el bueno.
        path = max(matches, key=lambda candidate: candidate.stat().st_size)
        index = corpus.load_index(path)
        if index is None:
            continue
        registry[norm.key] = NormIndex(
            norm=norm, path=path, index=index, articles=_article_spans(index)
        )

    return registry


# "articulo 481", "art. 4-A", "arts. 481, 482 y 483". El grupo de numeros se captura
# entero para poder desplegar las enumeraciones despues.
_MENTION_RE = re.compile(
    r"\bart(?:iculos?|s?\.|\b)\s*"
    r"((?:\d+(?:\s?[-–]\s?[a-z])?[°º]?)"
    r"(?:\s*(?:,|;|\sy\s|\se\s)\s*\d+(?:\s?[-–]\s?[a-z])?[°º]?)*)",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d+(?:\s?[-–]\s?[a-z])?[°º]?", re.IGNORECASE)

# Cuanto texto se mira despues de la mencion para encontrar la norma ("... del Codigo
# Civil"), y cuanto hacia atras si ahi no aparece.
_LOOKAHEAD = 90
_LOOKBEHIND = 320


@dataclass(frozen=True)
class Mention:
    article: str
    norm_key: str | None
    raw: str
    position: int
    # `explicit`: la norma va pegada a la cita ("art. 481 del Codigo Civil").
    # `inferred` : se arrastro de la frase anterior ("Segun el Codigo Civil, el art. 481").
    # `none`     : el texto nunca dijo de que norma habla.
    attribution: str = "none"


def _nearest_norm(folded: str, start: int, end: int, direction: str) -> str | None:
    """El alias de norma mas cercano a la cita dentro de la ventana `[start, end)`.

    Gana el mas cercano y no el ultimo del tramo. Con "el ultimo" una cita se llevaba la
    norma de la frase siguiente: en "art. 92 del Codigo de los Ninos y Adolescentes. El
    art. 9999 del Codigo Civil" la ventana de la primera cita alcanzaba a "Codigo Civil"
    y le atribuia el 92 al codigo equivocado, que es exactamente el error que este modulo
    tiene que detectar y no cometer.
    """
    window = folded[start:end]
    if not window:
        return None

    best: tuple[int, str] | None = None
    for norm in NORMS:
        for alias in norm.aliases:
            # Hacia adelante interesa donde empieza el alias; hacia atras, donde termina.
            position = window.find(alias) if direction == "after" else window.rfind(alias)
            if position < 0:
                continue
            distance = position if direction == "after" else len(window) - (position + len(alias))
            if best is None or distance < best[0]:
                best = (distance, norm.key)

    return best[1] if best else None


def detect_mentions(text: str) -> list[Mention]:
    """Articulos citados en el texto de una respuesta, con la norma a la que se atribuyen.

    La norma se busca primero hacia adelante ("articulo 481 del Codigo Civil") y, si no
    aparece, hacia atras, que es como queda en una enumeracion ("Segun el Codigo Civil,
    los articulos 481 y 482..."). Las dos ventanas se cortan en la cita vecina: el texto
    que ya pertenece a otra cita no puede prestarle su norma a esta.

    Sin norma, la mencion queda con `norm_key=None`; con norma arrastrada de atras, queda
    marcada como `inferred`. Las tres clases se cuentan por separado porque solo la
    explicita permite afirmar sin discusion que un articulo esta inventado.
    """
    folded = deaccent(text)
    matches = list(_MENTION_RE.finditer(folded))
    mentions: list[Mention] = []

    for position, match in enumerate(matches):
        previous_end = matches[position - 1].end() if position else 0
        next_start = matches[position + 1].start() if position + 1 < len(matches) else len(folded)

        norm_key = _nearest_norm(
            folded, match.end(), min(next_start, match.end() + _LOOKAHEAD), "after"
        )
        attribution = "explicit" if norm_key else "none"
        if norm_key is None:
            norm_key = _nearest_norm(
                folded, max(previous_end, match.start() - _LOOKBEHIND), match.start(), "before"
            )
            attribution = "inferred" if norm_key else "none"

        for number in _NUMBER_RE.finditer(match.group(1)):
            mentions.append(
                Mention(
                    article=normalize_article(number.group(0)),
                    norm_key=norm_key,
                    raw=number.group(0).strip(),
                    position=match.start(),
                    attribution=attribution,
                )
            )

    return mentions


def norm_of_document(file_name: str, file_url: str = "") -> str | None:
    """A que norma corresponde el documento que la cita dice haber usado.

    Hace falta porque una cita trae el nombre del documento, no la clave de la norma, y
    sin esa traduccion no se puede comparar "Art. 481" de una cita contra el ground truth.
    Se prueba por alias (como lo escribe el metadato del corpus) y por el nombre de
    fichero, que es lo unico estable cuando el titulo viene con los acentos rotos.
    """
    haystack = deaccent(f"{file_name} {file_url}")
    for norm in NORMS:
        if norm.stem_contains.replace("-", " ") in haystack.replace("-", " "):
            return norm.key
        if any(alias in haystack for alias in norm.aliases):
            return norm.key
    return None


# "CASACION N° 3496 - 2016", "1189-2018", "000588-2016", "CASACION N° 588 2016".
# El año se acota a 19xx/20xx porque el separador tambien puede ser un espacio: con
# `\d{4}` a secas, un "articulo 472 2016" cualquiera habria pasado por expediente.
_CASE_RE = re.compile(r"\b(\d{1,6})\s*(?:[-–]\s*|\s+)((?:19|20)\d{2})\b")


def case_numbers(text: str) -> set[str]:
    """Expedientes citados en un texto, normalizados a `numero-año` sin ceros a la izquierda.

    El corpus guarda la jurisprudencia por numero de expediente ("3023-2017-<hash>.md",
    "resolucion-001532-2013-patty-<hash>.md") mientras que la cita la nombra por su
    caratula ("CASACION 3023-2017 LIMA TENENCIA Y CUSTODIA"). El numero es lo unico que
    aparece igual en los dos sitios.
    """
    found = set()
    for number, year in _CASE_RE.findall(text or ""):
        found.add(f"{number.lstrip('0') or '0'}-{year}")
    return found


@lru_cache(maxsize=1)
def _case_index() -> dict[str, Path]:
    """Expediente -> fichero del corpus. Solo para la jurisprudencia."""
    index: dict[str, Path] = {}
    for path in corpus.iter_corpus_files():
        for case in case_numbers(path.stem):
            index.setdefault(case, path)
    return index


def find_corpus_path(file_name: str, file_url: str = "") -> Path | None:
    """El fichero del corpus que respalda una cita, sea norma o jurisprudencia.

    Sin esto, toda cita a una casacion quedaba sin documento y por tanto sin verificar:
    eran 29 de 157 citas marcadas como no verificables por una carencia del evaluador, no
    por un fallo del sistema evaluado.
    """
    norm_key = norm_of_document(file_name, file_url)
    if norm_key:
        entry = build_registry().get(norm_key)
        if entry is not None:
            return entry.path

    # En el orden en que aparecen en la cita: recorrer el set de `case_numbers` dejaba la
    # eleccion entre dos expedientes al orden de hash, distinto en cada proceso.
    index = _case_index()
    for number, year in _CASE_RE.findall(f"{file_name} {file_url}"):
        case = f"{number.lstrip('0') or '0'}-{year}"
        if case in index:
            return index[case]
    return None


def articles_in_locator(label: str) -> list[str]:
    """`"Arts. 562 y 563"` -> `["562", "563"]`. Un locator combinado cubre varios."""
    if not label:
        return []
    return [normalize_article(number) for number in _NUMBER_RE.findall(label)]


def classify_mention(mention: Mention, registry: dict[str, NormIndex]) -> str:
    """`exists` | `hallucinated` | `unattributed` | `unknown_norm`.

    `unattributed` no es un acierto ni un fallo: el texto cito un articulo sin decir de
    que norma, y sin norma no hay nada contra que verificarlo. Se reporta aparte porque
    su tasa es en si misma un sintoma: una respuesta que nunca nombra la norma no es
    verificable por el usuario.
    """
    if mention.norm_key is None:
        return "unattributed"
    entry = registry.get(mention.norm_key)
    if entry is None or not entry.norm.articulated:
        return "unknown_norm"
    return "exists" if entry.has(mention.article) else "hallucinated"


def verify_dataset(path: Path) -> int:
    registry = build_registry()
    problems: list[str] = []
    items = 0
    articles = 0

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"linea {line_number}: JSON invalido ({exc})")
            continue
        items += 1

        for expected in item.get("expected_articles", []):
            articles += 1
            norm_key = expected.get("norm")
            article = expected.get("article", "")
            entry = registry.get(norm_key)
            if entry is None:
                problems.append(f"{item.get('id')}: norma ausente del corpus: {norm_key}")
            elif not entry.has(article):
                problems.append(f"{item.get('id')}: {norm_key} no tiene articulo {article}")

        for norm_key in item.get("expected_norms", []):
            if norm_key not in registry:
                problems.append(
                    f"{item.get('id')}: expected_norms cita {norm_key}, que no esta en el corpus"
                )

    print(f"dataset   : {path}")
    print(f"preguntas : {items}")
    print(f"articulos : {articles}")

    if problems:
        print(f"\nProblemas ({len(problems)}):")
        for problem in problems:
            print(f"  {problem}")
        print("\n-> corrige el ground truth: un articulo que no existe en el corpus mide")
        print("   el error del dataset y no el del sistema.")
        return 2

    print("\nTodo el ground truth existe en el corpus.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Registro de articulos del corpus")
    parser.add_argument("--list", metavar="NORMA", help="Lista los articulos de una norma")
    parser.add_argument("--verify-dataset", metavar="RUTA", type=Path)
    parser.add_argument("--detect", metavar="TEXTO", help="Prueba el detector de menciones")
    args = parser.parse_args(argv)

    if args.verify_dataset:
        return verify_dataset(args.verify_dataset)

    registry = build_registry()

    if args.detect:
        for mention in detect_mentions(args.detect):
            verdict = classify_mention(mention, registry)
            print(f"  Art. {mention.article:<8} {str(mention.norm_key):<28} {mention.attribution:<10} {verdict}")
        return 0

    if args.list:
        entry = registry.get(args.list)
        if entry is None:
            print(f"norma desconocida: {args.list}")
            print(f"disponibles: {', '.join(sorted(registry))}")
            return 2
        print(f"{entry.norm.display}  ({entry.path.name})")
        print(f"articulos: {len(entry.articles)}")
        print("  " + ", ".join(sorted(entry.articles, key=lambda value: (len(value), value))))
        return 0

    print(f"corpus : {corpus.corpus_path()}")
    print(f"normas reconocidas: {len(registry)} de {len(NORMS)}\n")
    for norm in NORMS:
        entry = registry.get(norm.key)
        if entry is None:
            print(f"  {norm.key:<28} AUSENTE DEL CORPUS  (stem ~ {norm.stem_contains})")
            continue
        kind = "articulos" if norm.articulated else "sin articulado"
        count = len(entry.articles) if norm.articulated else ""
        print(f"  {norm.key:<28} {str(count):>5} {kind:<14} {entry.path.name}")

    missing = [norm.key for norm in NORMS if norm.key not in registry]
    return 2 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
