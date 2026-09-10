"""Tablas 2x2, efectos principales y contrastes pareados sobre una corrida puntuada.

    python -m eval.report --run eval/runs/<timestamp>

Los cuatro brazos ven exactamente las mismas preguntas, asi que los contrastes son
pareados: cada pregunta es su propio control y la variabilidad entre preguntas —que en un
dataset de derecho de familia es enorme, una consulta sobre alimentos no se parece en nada
a una sobre violencia— deja de ser ruido. Por eso McNemar y Wilcoxon, y no un t de dos
muestras.

Sin dependencias externas: los tres estadisticos estan implementados aca abajo para que
esto corra en el mismo contenedor que el resto, sin instalar scipy.
"""

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path

ARM_GRID = {
    "full": (True, True),
    "no_rag": (False, True),
    "no_xai": (True, False),
    "base": (False, False),
}

# Metricas que se leen como tasa por respuesta.
RATE_METRICS = (
    ("traceable", "Respuestas trazables"),
    ("correct", "Respuestas correctas"),
    ("article_recall", "Recall de articulos esperados"),
    ("article_precision", "Precision de articulos"),
    ("must_mention_coverage", "Cobertura de puntos clave"),
    ("support_density", "Densidad de respaldo (proxy)"),
    ("steps_actionable_rate", "Pasos accionables"),
)
COUNT_METRICS = (
    ("hallucinated_explicit", "Articulos inventados por respuesta"),
    ("unattributed_mentions", "Citas sin norma por respuesta"),
    ("citations", "Citas por respuesta"),
    ("articles_from_text", "Articulos nombrados en el texto"),
    ("articles_from_citations", "Articulos aportados por las citas"),
    ("next_steps", "Pasos sugeridos"),
    ("normative_claims", "Afirmaciones normativas"),
    ("steps_generic_referral", "Derivaciones institucionales sin causa"),
)
BINARY_METRICS = ("traceable", "correct")


# --------------------------------------------------------------------------------------
# Estadistica


def mcnemar(pairs: list[tuple[bool, bool]]) -> tuple[int, int, float]:
    """Contraste pareado para una metrica binaria.

    Solo cuentan las preguntas donde los dos brazos difieren: las que ambos aciertan o
    ambos fallan no dicen nada sobre cual es mejor. Se usa la version exacta (binomial)
    porque con 60 preguntas los discordantes suelen ser pocos y la aproximacion normal
    ahi no vale.
    """
    only_left = sum(1 for left, right in pairs if left and not right)
    only_right = sum(1 for left, right in pairs if right and not left)
    discordant = only_left + only_right
    if discordant == 0:
        return only_left, only_right, 1.0

    smaller = min(only_left, only_right)
    tail = sum(math.comb(discordant, k) for k in range(smaller + 1)) / (2 ** discordant)
    return only_left, only_right, min(1.0, 2 * tail)


def wilcoxon(pairs: list[tuple[float, float]]) -> float:
    """Wilcoxon de rangos con signo, aproximacion normal con correccion de empates."""
    diffs = [left - right for left, right in pairs if left is not None and right is not None]
    diffs = [value for value in diffs if value != 0]
    if len(diffs) < 6:
        return float("nan")

    order = sorted(range(len(diffs)), key=lambda index: abs(diffs[index]))
    ranks = [0.0] * len(diffs)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and abs(diffs[order[end + 1]]) == abs(diffs[order[position]]):
            end += 1
        average = (position + end) / 2 + 1
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1

    positive = sum(rank for rank, diff in zip(ranks, diffs) if diff > 0)
    count = len(diffs)
    mean = count * (count + 1) / 4
    deviation = math.sqrt(count * (count + 1) * (2 * count + 1) / 24)
    if deviation == 0:
        return float("nan")
    z = (positive - mean) / deviation
    return math.erfc(abs(z) / math.sqrt(2))


def bootstrap_ci(values: list[float], rounds: int = 4000, seed: int = 20260908) -> tuple[float, float]:
    """IC 95% de la media, remuestreando preguntas."""
    clean = [value for value in values if value is not None]
    if len(clean) < 3:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = sorted(
        statistics.fmean(rng.choices(clean, k=len(clean))) for _ in range(rounds)
    )
    return means[int(0.025 * rounds)], means[int(0.975 * rounds)]


# --------------------------------------------------------------------------------------
# Carga y pivote


def load_rows(run: Path) -> list[dict]:
    path = run / "per_question.jsonl"
    if not path.exists():
        raise SystemExit(f"falta {path}: corre antes `python -m eval.score --run {run}`")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def pivot(rows: list[dict], metric: str) -> dict[str, dict[str, float]]:
    """`{pregunta: {brazo: valor}}`, promediando las repeticiones de una misma pregunta."""
    buckets: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        if not row.get("ok"):
            continue
        value = row.get(metric)
        if value is None:
            continue
        buckets.setdefault(row["id"], {}).setdefault(row["arm"], []).append(float(value))
    return {
        question: {arm: statistics.fmean(values) for arm, values in per_arm.items()}
        for question, per_arm in buckets.items()
    }


def paired(table: dict[str, dict[str, float]], left: str, right: str) -> list[tuple[float, float]]:
    """Solo las preguntas contestadas por los dos brazos: si falto una, el par no existe."""
    return [
        (values[left], values[right])
        for values in table.values()
        if left in values and right in values
    ]


# --------------------------------------------------------------------------------------
# Tablas


def factorial_table(table: dict[str, dict[str, float]]) -> dict:
    """Media por brazo, efectos principales e interaccion.

    El efecto de un componente se estima con los dos contrastes disponibles y se promedia:
    quitar el RAG con el XAI puesto (full - no_xai... ) no tiene por que dar lo mismo que
    quitarlo sin el, y la diferencia entre ambos es justamente la interaccion.
    """
    means = {}
    for arm in ARM_GRID:
        values = [row[arm] for row in table.values() if arm in row]
        means[arm] = statistics.fmean(values) if values else None

    def delta(left: str, right: str) -> float | None:
        pairs = paired(table, left, right)
        return statistics.fmean(l - r for l, r in pairs) if pairs else None

    rag_with_xai = delta("full", "no_rag")
    rag_without_xai = delta("no_xai", "base")
    xai_with_rag = delta("full", "no_xai")
    xai_without_rag = delta("no_rag", "base")

    def average(*values):
        clean = [value for value in values if value is not None]
        return statistics.fmean(clean) if clean else None

    interaction = None
    if rag_with_xai is not None and rag_without_xai is not None:
        interaction = rag_with_xai - rag_without_xai

    return {
        "means": means,
        "rag_effect": average(rag_with_xai, rag_without_xai),
        "rag_effect_with_xai": rag_with_xai,
        "rag_effect_without_xai": rag_without_xai,
        "xai_effect": average(xai_with_rag, xai_without_rag),
        "xai_effect_with_rag": xai_with_rag,
        "xai_effect_without_rag": xai_without_rag,
        "interaction": interaction,
    }


def significance(table: dict[str, dict[str, float]], metric: str) -> dict:
    result = {}
    for label, left, right in (("rag", "full", "no_rag"), ("xai", "full", "no_xai")):
        pairs = paired(table, left, right)
        if not pairs:
            continue
        if metric in BINARY_METRICS:
            wins, losses, p_value = mcnemar([(bool(l), bool(r)) for l, r in pairs])
            result[label] = {"test": "McNemar", "n": len(pairs), "wins": wins, "losses": losses, "p": p_value}
        else:
            result[label] = {
                "test": "Wilcoxon",
                "n": len(pairs),
                "p": wilcoxon(pairs),
            }
        result[label]["ci95"] = bootstrap_ci([l - r for l, r in pairs])
    return result


def fmt(value, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float) and math.isnan(value):
        return "n/d"
    return f"{value:.{digits}f}"


def fmt_p(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/d"
    if value < 0.001:
        return "<0.001"
    return f"{value:.3f}"


def render(run: Path, rows: list[dict], results: dict) -> str:
    lines: list[str] = []
    add = lines.append

    questions = len({row["id"] for row in rows})
    add("# Validacion interna por ablacion: RAG x XAI\n")
    add(f"Corrida `{run.name}` · {questions} preguntas · {len(rows)} respuestas puntuadas\n")
    add("Diseno factorial 2x2 pareado: los cuatro brazos responden las mismas preguntas.")
    add("Todo lo que se mide esta verificado contra el corpus normativo, no contra lo que")
    add("el sistema declara de si mismo.\n")

    add("## Brazos\n")
    add("| Brazo | RAG | XAI | Respuestas | Fallos |")
    add("|---|:-:|:-:|--:|--:|")
    for arm, (rag, xai) in ARM_GRID.items():
        summary = results["arms"].get(arm)
        if not summary:
            continue
        failure = summary.get("failure_rate")
        add(
            f"| `{arm}` | {'si' if rag else 'no'} | {'si' if xai else 'no'} | "
            f"{summary['answered']} | {fmt(failure, 2) if failure is not None else '—'} |"
        )
    add("")

    add("## Resultados por metrica\n")
    add("| Metrica | full | no_rag | no_xai | base | Efecto RAG | Efecto XAI | Interaccion |")
    add("|---|--:|--:|--:|--:|--:|--:|--:|")
    for metric, label in RATE_METRICS + COUNT_METRICS:
        table = pivot(rows, metric)
        if not table:
            continue
        stats = factorial_table(table)
        means = stats["means"]
        add(
            f"| {label} | {fmt(means.get('full'))} | {fmt(means.get('no_rag'))} | "
            f"{fmt(means.get('no_xai'))} | {fmt(means.get('base'))} | "
            f"{fmt(stats['rag_effect'])} | {fmt(stats['xai_effect'])} | {fmt(stats['interaction'])} |"
        )
    add("")
    add("El efecto de cada componente es la media de sus dos contrastes (quitarlo con el")
    add("otro componente puesto y sin el). La interaccion es la diferencia entre ambos:")
    add("si es grande, los componentes no son independientes y el efecto principal por si")
    add("solo describe mal el sistema.\n")

    add("## Contrastes pareados\n")
    add("| Metrica | Contraste | Test | n | Diferencia media | IC 95% | p |")
    add("|---|---|---|--:|--:|---|--:|")
    for metric, label in RATE_METRICS + COUNT_METRICS:
        table = pivot(rows, metric)
        if not table:
            continue
        tests = significance(table, metric)
        for axis, data in tests.items():
            pairs = paired(table, "full", "no_rag" if axis == "rag" else "no_xai")
            difference = statistics.fmean(l - r for l, r in pairs) if pairs else None
            low, high = data["ci95"]
            contrast = "full vs no_rag" if axis == "rag" else "full vs no_xai"
            add(
                f"| {label} | {contrast} | {data['test']} | {data['n']} | {fmt(difference)} | "
                f"[{fmt(low)}, {fmt(high)}] | {fmt_p(data['p'])} |"
            )
    add("")

    add("## Calidad de las citas (solo brazos con capa XAI)\n")
    add("| Brazo | Citas | Pasaje literal | Con ubicacion | Ubicacion correcta |")
    add("|---|--:|--:|--:|--:|")
    for arm in ARM_GRID:
        summary = results["arms"].get(arm)
        if not summary or not summary.get("total_citations"):
            continue
        add(
            f"| `{arm}` | {summary['total_citations']} | {fmt(summary.get('verbatim_rate'))} | "
            f"{fmt(summary.get('locator_resolution_rate'))} | "
            f"{fmt(summary.get('locator_correctness_rate'))} |"
        )
    add("")
    add("La ubicacion se recalcula sobre el markdown del corpus a partir del pasaje que la")
    add("cita dice haber usado. Una cita bien redactada y mal ubicada cuenta como fallo.\n")

    add("## Comprensibilidad\n")
    add("| Brazo | Respuesta | Articulado citado | **Salto** | Palabras por frase |")
    add("|---|--:|--:|--:|--:|")
    for arm in ARM_GRID:
        summary = results["arms"].get(arm)
        if not summary or not summary.get("answered"):
            continue
        add(
            f"| `{arm}` | {fmt(summary.get('answer_readability'), 1)} | "
            f"{fmt(summary.get('snippet_readability'), 1)} | "
            f"**{fmt(summary.get('answer_vs_sources_gap'), 1)}** | "
            f"{fmt(summary.get('answer_words_per_sentence'), 1)} |"
        )
    add("")
    add("Indice Szigriszt-Pazos, escala 0-100: mas alto, mas facil de leer. El salto compara")
    add("**lo que el usuario lee** con el articulado que la respuesta cita: positivo significa")
    add("que la respuesta reestructura la norma en lenguaje mas accesible.\n")
    gap = (results["arms"].get("full") or {}).get("readability_gap")
    if gap is not None:
        add("### Diagnostico: la glosa de cada cita\n")
        add(f"El `summary_snippet` que acompana a cada cita puntua **{fmt(gap, 1)}** frente al")
        add("pasaje que resume. Es un artefacto secundario de la interfaz, no el texto que el")
        add("usuario lee, y el prompt de produccion no le pide lenguaje simple: solo brevedad")
        add("y pertinencia. Se reporta aparte para no confundirlo con la legibilidad de la")
        add("respuesta.\n")

    add("## Fidelidad de la cita (repeticiones)\n")
    stability_rows = [
        (arm, (results["arms"].get(arm) or {}).get("stability"))
        for arm in ARM_GRID
        if (results["arms"].get(arm) or {}).get("stability")
    ]
    if stability_rows:
        add("| Brazo | Preguntas | Estabilidad de la respuesta | Estabilidad de la cita | Citas decorativas |")
        add("|---|--:|--:|--:|--:|")
        for arm, data in stability_rows:
            add(
                f"| `{arm}` | {data['questions']} | {fmt(data['answer_similarity'])} | "
                f"{fmt(data['citation_similarity'])} | {fmt(data.get('decorative_rate'))} |"
            )
        add("")
        add("Si la respuesta se mantiene entre repeticiones pero las citas cambian, la cita")
        add("no es lo que produjo la respuesta: la acompana. No prueba causalidad, la")
        add("descarta cuando falla.\n")
    else:
        add("La corrida no llevaba repeticiones, asi que no hay nada que comparar.")
        add("Correr con `--repeat 3` para medir esto.\n")

    add("## Calibracion de la confianza declarada\n")
    add("| Brazo | Nivel | n | Respuestas correctas |")
    add("|---|---|--:|--:|")
    for arm in ARM_GRID:
        summary = results["arms"].get(arm) or {}
        for level, data in (summary.get("calibration") or {}).items():
            add(f"| `{arm}` | {level} | {data['n']} | {fmt(data['correct_rate'])} |")
    add("")
    add("Una confianza que no discrimina —HIGH y LOW con la misma tasa de acierto— es")
    add("peor que no declararla: le da al usuario una senal en la que no puede apoyarse.\n")

    add("## Derivacion institucional (seguridad)\n")
    add("| Brazo | Recall | Precision | Falsos negativos |")
    add("|---|--:|--:|--:|")
    for arm in ARM_GRID:
        summary = results["arms"].get(arm)
        if not summary or summary.get("specialist_recall") is None:
            continue
        add(
            f"| `{arm}` | {fmt(summary['specialist_recall'])} | "
            f"{fmt(summary.get('specialist_precision'))} | "
            f"{fmt(summary.get('specialist_false_negative'))} |"
        )
    add("")
    add("El falso negativo es el caso grave: una consulta con violencia, riesgo o")
    add("sustraccion de un menor que sale sin derivacion a PNP, CEM o DEMUNA.\n")

    add("## Coste y latencia\n")
    add("| Brazo | Latencia p50 | Latencia p95 | Caracteres por respuesta |")
    add("|---|--:|--:|--:|")
    for arm in ARM_GRID:
        summary = results["arms"].get(arm)
        if not summary or not summary.get("answered"):
            continue
        add(
            f"| `{arm}` | {summary.get('latency_p50')} ms | {summary.get('latency_p95')} ms | "
            f"{fmt(summary.get('answer_chars'), 0)} |"
        )
    add("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reporte de la ablacion")
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args(argv)

    rows = load_rows(args.run)
    results_path = args.run / "results.json"
    if not results_path.exists():
        raise SystemExit(f"falta {results_path}: corre antes `python -m eval.score --run {args.run}`")
    results = json.loads(results_path.read_text(encoding="utf-8"))

    report = render(args.run, rows, results)
    output = args.run / "report.md"
    output.write_text(report, encoding="utf-8")

    print(report)
    print(f"\nescrito: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
