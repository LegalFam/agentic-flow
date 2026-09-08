"""Mide que porcentaje del corpus resuelve a una ubicacion concreta.

Trocea cada markdown en fragmentos del tamano aproximado que devuelve File Search, los
pasa por el resolver y reporta el desglose por estrategia. Es el insumo para decidir si
conviene propagar el locator hasta el usuario o si primero hay que ajustar el corpus.

    python -m app.locator_coverage
    python -m app.locator_coverage --chunk-size 1200 --worst 15
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from app import corpus, locator
from app.config import settings

STRATEGIES = ("exact", "prefix", "fuzzy", "markdown_heading", "snippet_regex", "none")


def chunk_text(collapsed: str, size: int, stride: int) -> list[str]:
    """Fragmentos sobre el texto ya colapsado, cortando en limites de palabra."""
    if not collapsed:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(collapsed):
        end = min(start + size, len(collapsed))
        if end < len(collapsed):
            space = collapsed.rfind(" ", start + size // 2, end)
            if space > start:
                end = space
        piece = collapsed[start:end].strip()
        if len(piece) >= 40:
            chunks.append(piece)
        if end >= len(collapsed):
            break
        start = end + stride
    return chunks


def analyze_document(path: Path, size: int, stride: int) -> tuple[Counter, int]:
    index = corpus.load_index(path)
    if index is None:
        return Counter(), 0

    counts: Counter = Counter()
    for chunk in chunk_text(index.collapsed, size, stride):
        found = locator.resolve(index, chunk)
        counts[found.source if not found.is_empty() else "none"] += 1

    legal_headings = sum(1 for heading in index.headings if heading.level < locator.LEVEL_MARKDOWN)
    return counts, legal_headings


def format_row(name: str, counts: Counter, headings: int | None = None) -> str:
    total = sum(counts.values()) or 1
    resolved = total - counts.get("none", 0)
    parts = [f"{name[:52]:<52} {resolved * 100 // total:>3}%"]
    for strategy in STRATEGIES:
        parts.append(f"{strategy[:4]}={counts.get(strategy, 0):<4}")
    if headings is not None:
        parts.append(f"headings={headings}")
    return " ".join(parts)


# Una linea que "parece" encabezado de articulo, sin importar la decoracion que traiga.
# La cobertura por si sola no detecta un encabezado perdido: el fragmento igual resuelve,
# solo que al articulo anterior. Este chequeo es el que ve ese error.
#
# El juego de caracteres tiene que ser mas ancho que el de `locator._ARTICULO_RE`, o el
# chequeo se vuelve ciego justo a los encabezados que el resolver todavia no toma: las
# comillas y la vineta de lista quedaban fuera de ambos y por eso 631 encabezados reales
# nunca aparecieron en esta lista.
ARTICLE_LIKE = re.compile(r"^[\s*_#>\-–—•\"'“”‘’]{0,12}art[ií]culo\s+\d", re.IGNORECASE)


def undetected_article_headings(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return []
    return [
        line.strip()
        for line in lines
        if ARTICLE_LIKE.match(line) and not locator._ARTICULO_RE.match(line)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cobertura del citation locator sobre el corpus")
    parser.add_argument("--chunk-size", type=int, default=900)
    parser.add_argument("--stride", type=int, default=200, help="Salto entre fragmentos")
    parser.add_argument("--worst", type=int, default=10, help="Cuantos documentos flojos listar")
    args = parser.parse_args(argv)

    files = corpus.iter_corpus_files()
    print(f"corpus_dir : {settings.corpus_dir}")
    print(f"documentos : {len(files)}")

    if not files:
        print("\nNo hay documentos en el corpus. Copia los .md y vuelve a correr.")
        return 1

    totals: Counter = Counter()
    per_document: list[tuple[float, str, Counter, int]] = []

    for path in files:
        counts, headings = analyze_document(path, args.chunk_size, args.stride)
        chunks = sum(counts.values())
        if not chunks:
            continue
        totals.update(counts)
        rate = (chunks - counts.get("none", 0)) / chunks
        per_document.append((rate, path.name, counts, headings))

    grand_total = sum(totals.values())
    if not grand_total:
        print("\nNo se genero ningun fragmento analizable.")
        return 1

    print(f"fragmentos : {grand_total}\n")
    print("Resolucion por estrategia:")
    for strategy in STRATEGIES:
        count = totals.get(strategy, 0)
        print(f"  {strategy:<14} {count:>7}  {count * 100 / grand_total:5.1f}%")

    strong = totals.get("exact", 0) + totals.get("prefix", 0)
    print(f"\n  exact+prefix   {strong:>7}  {strong * 100 / grand_total:5.1f}%   (meta: >80%)")
    # markdown_heading no es una ubicacion juridica: en resoluciones sin articulado
    # devuelve el asunto del caso o ruido del OCR, que la cita ya muestra por titulo.
    weak = totals.get("markdown_heading", 0)
    if weak:
        print(f"  markdown_heading {weak:>7}  {weak * 100 / grand_total:5.1f}%   no es ubicacion juridica:")
        print("  es el asunto del caso o ruido del OCR, tipico de resoluciones sin articulado.")

    per_document.sort()
    worst = [row for row in per_document if row[0] < 1.0][: args.worst]
    if worst:
        print(f"\nDocumentos mas flojos (los {len(worst)} peores):")
        for _, name, counts, headings in worst:
            print("  " + format_row(name, counts, headings))

    sin_headings = [name for _, name, _, headings in per_document if headings == 0]
    if sin_headings:
        print(f"\nSin encabezados legales detectados ({len(sin_headings)}):")
        for name in sin_headings[: args.worst]:
            print(f"  {name}")
        print("  -> revisar si el markdown perdio la estructura al convertir el PDF.")

    suspects: list[tuple[str, str]] = []
    for path in files:
        for line in undetected_article_headings(path):
            suspects.append((path.name, line))

    if suspects:
        print(f"\nLineas tipo 'Articulo N' que NO se toman como encabezado ({len(suspects)}):")
        for name, line in suspects[: args.worst]:
            print(f"  {name[:34]:34} {line[:60]}")
        print("  -> revisar una por una: las referencias en prosa deben quedar fuera, pero")
        print("     un encabezado real perdido atribuye su texto al articulo anterior.")

    return 0 if strong * 100 / grand_total >= 80 else 2


if __name__ == "__main__":
    sys.exit(main())
