# Validación interna por ablación: ¿cuánto aporta el RAG y cuánto el XAI?

Experimento factorial 2×2 que mide el aporte de cada componente del asistente sobre
consultas reales de Derecho de Familia peruano, ejecutado **contra el workflow real de
n8n** y puntuado **contra el corpus normativo**.

| Brazo | RAG | XAI | Qué se quita |
|---|:-:|:-:|---|
| `full` | ✔ | ✔ | nada (workflow de producción) |
| `no_rag` | ✘ | ✔ | la recuperación: el agente responde de memoria |
| `no_xai` | ✔ | ✘ | la capa de explicabilidad: sin citas, localizador, confianza ni pasos |
| `base` | ✘ | ✘ | ambos |
| `no_xai_inline` | ✔ | ✘ | control opcional, fuera del 2×2 (ver *Limitaciones*) |

---

## Runbook

Todo se corre desde `agentic-flow/api/`. Los comandos de Docker van entre paréntesis para
que el `cd` viva sólo dentro del subshell y el directorio de trabajo no cambie.

```bash
cd agentic-flow/api
```

**1. Verificar el ground truth contra el corpus** (no gasta tokens, no necesita n8n)

```bash
(cd .. && docker compose run --rm -v ./api:/app processing-api python -m eval.corpus_articles --verify-dataset eval/dataset/family_law_v1.jsonl)
```

Sale distinto de cero si algún artículo del dataset no existe en `work/corpus/`. Un
ground truth inventado mediría el error del dataset, no el del sistema.

**2. Generar los cuatro workflows y revisar el diff** (no toca n8n)

```bash
python -m eval.ablation.build_workflows --dry-run
```

Sin `--dry-run` escribe en `n8n/workflows/eval/`, que **sí se versiona**: son la evidencia
de qué sistema exacto produjo cada número. Si el workflow de producción cambió y un parche
ya no encaja, esto **falla** en vez de generar un brazo mal ablacionado.

Versionar un artefacto generado tiene un riesgo —que se quede viejo sin que nadie lo
note— y por eso existe el modo que lo convierte en un fallo visible:

```bash
python -m eval.ablation.build_workflows --check
```

Sale distinto de cero si algún brazo en disco ya no coincide con lo que saldría del
workflow de producción actual. Conviene correrlo antes de cada corrida: los ficheros no
son la verdad, el workflow de producción lo es.

Estos brazos **nunca se despliegan**. El deploy empaqueta sólo `n8n/workflows/*.json`
—el patrón del `.dockerignore` no cruza el `/`— y además `n8n/workflows/eval/` está
excluido de forma explícita, porque el coste de equivocarse es publicar en producción un
workflow con la recuperación desactivada.

**3. Levantar el entorno local e importar las variantes**

```bash
(cd .. && docker compose up -d)
```

El servicio `n8n` no monta `./n8n/workflows`, así que los ficheros se copian al contenedor
antes de importarlos:

```bash
(cd .. && docker compose cp n8n/workflows/eval n8n:/tmp/eval)
```

```bash
(cd .. && docker compose exec n8n n8n import:workflow --separate --input=/tmp/eval)
```

Después hay que **activar los cuatro workflows** desde la UI (`http://localhost:5678`):
el webhook de producción sólo responde cuando el workflow está activo. Las credenciales
de Gemini y el header de autenticación se heredan de la instancia; si es una instancia
nueva, hay que crearlas antes con los mismos nombres que usa el workflow de producción.

**4. Humo: una pregunta por brazo**

```bash
python -m eval.run_ablation --limit 1
```

**5. Corrida completa**

```bash
python -m eval.run_ablation --repeat 3 --repeat-sample 10
```

Se puede reanudar: `--run-id <timestamp>` retoma donde se cortó y no repite lo ya
contestado.

**6. Puntuar y reportar**

```bash
(cd .. && docker compose run --rm -v ./api:/app processing-api python -m eval.score --run eval/runs/<timestamp>)
```

```bash
python -m eval.report --run eval/runs/<timestamp>
```

Deja `per_question.jsonl`, `results.json` y `report.md` en el directorio de la corrida.

---

## Cómo se mide

Nada se toma de lo que el sistema declara de sí mismo. Todo se recalcula contra
`work/corpus/` reutilizando `app.locator` y `app.corpus`.

**Eje RAG — exactitud normativa**

- `hallucinated_explicit` — artículos citados en el texto con la norma nombrada al lado
  que **no existen** en el corpus. Es la métrica central del contraste. Los inferidos de
  la frase anterior se cuentan aparte: sólo la atribución explícita permite afirmar sin
  discusión que un número está inventado.
- `article_recall` / `article_precision` frente a los artículos esperados, contando los
  dos canales por los que un artículo puede llegar al usuario (el texto de la respuesta y
  las citas). Se reportan también por separado.
- `must_mention_coverage` — términos jurídicos cuya ausencia hace la respuesta
  objetivamente incompleta. Sustituye al juez LLM para medir completitud.

**Eje XAI — verificabilidad**

- `verbatim_rate` — proporción de `original_snippet` que se localiza literalmente en el
  documento. Mide si la capa puede sostener lo que dice haber usado.
- `locator_correctness_rate` — se recalcula el artículo que contiene el pasaje **desde
  cero** sobre el markdown y se compara con el localizador entregado. Una cita bien
  redactada y mal ubicada cuenta como fallo.
- `traceable` — la respuesta tiene al menos una cita literal y bien ubicada: lo mínimo
  para que el usuario pueda ir a comprobarla.
- Calibración de `confidenceStatus` contra la corrección real.

**Eje XAI — la explicación en sí** (`explainability.py`)

La fidelidad de la cita es sólo una dimensión de la explicabilidad. Estas cuatro cubren
las otras tres que se pueden medir sin humanos:

- `support_density` — **suficiencia**: qué fracción de las afirmaciones normativas de la
  respuesta tiene respaldo, no sólo una. Complementa a `traceable`, que se conforma con
  una cita buena. **Es un proxy declarado**: el enlace afirmación→cita se aproxima por
  solapamiento léxico con el texto del artículo, porque decidir de verdad si un pasaje
  sustenta una afirmación es inferencia y no se automatiza sin juez. Sirve para comparar
  brazos entre sí —el sesgo es el mismo en todos— y no como cifra absoluta.
- `readability_gap` — **comprensibilidad**: cuánto simplifica `summary_snippet` respecto
  al pasaje legal que resume (Szigriszt-Pazos). Es la medida directa del trabajo que la
  capa dice hacer. Un salto cercano a cero significa que el resumen es tan denso como la
  norma. Se acompaña de `answer_readability` sobre la respuesta completa.
- `steps_actionable_rate` — **accionabilidad**: los pasos empiezan por un verbo de acción,
  no repiten una `clarifyingQuestion`, y no derivan a PNP/CEM/DEMUNA sin emergencia que lo
  justifique.
- `stability` — **fidelidad causal**: si entre repeticiones la respuesta se mantiene pero
  las citas cambian, la cita no es lo que produjo la respuesta: la acompaña. Sale gratis
  de `--repeat`. No prueba causalidad; la descarta cuando falla. El test fuerte —quitar el
  documento citado y ver si la respuesta cambia— exigiría un segundo store de File Search
  y queda fuera de alcance.

**Seguridad**

- Precisión y recall de `specialistSupportRecommended` contra la etiqueta de riesgo del
  dataset. El falso negativo —violencia o sustracción de un menor sin derivación a
  PNP/CEM/DEMUNA— es el error grave.

**Contrastes.** Los cuatro brazos ven las mismas preguntas, así que todo es pareado:
McNemar exacto para las métricas binarias, Wilcoxon para las continuas, e intervalos al
95 % por bootstrap sobre preguntas. Sin dependencias externas.

---

## Decisiones de diseño

**Las variantes se generan, no se copian.** `ablation/build_workflows.py` parchea el JSON
de producción. Una copia editada a mano deja de ser comparable en cuanto alguien toca un
prompt, y el experimento pasaría a medir la diferencia entre dos versiones del sistema.
Cada parche está anclado a texto literal y falla si el ancla desaparece.

**`no_xai` conserva el redactor.** No se borra el nodo `XAI Agent`: se le recorta el
contrato de salida a sólo `answer`. Si se borrara entero, el brazo perdería también la
redacción —la salida cruda del RAG Agent es visiblemente peor prosa— y el efecto medido
mezclaría explicabilidad con calidad de escritura.

**`no_rag` conserva la capa XAI.** Se quita la herramienta de búsqueda y sólo la parte del
prompt que la nombra. El resto queda literalmente igual.

**Temperatura 0 en los cuatro brazos.** No hace la corrida determinista, pero sin ella la
diferencia entre brazos incluiría el ruido de muestreo de cinco nodos. `--repeat` sobre un
subconjunto estima la varianza que queda.

**Sesión nueva por pregunta.** El flujo cambia de registro cuando detecta mensajes
previos; reutilizar la sesión haría que la respuesta dependiera del orden del dataset.

---

## Limitaciones

- **El sesgo de formato en `no_xai`.** Ese brazo conserva la instrucción de no nombrar
  fuentes dentro de `answer` —correcta en producción, donde la interfaz las muestra al
  lado— pero se queda sin esa interfaz. Parte de su caída en trazabilidad podría venir de
  la instrucción y no de la ablación. El brazo `no_xai_inline` separa las dos cosas: es
  `no_xai` con permiso explícito para citar en el texto. Correrlo cuando haga falta
  defender el resultado:

  ```bash
  python -m eval.ablation.build_workflows --arm no_xai_inline
  python -m eval.run_ablation --arms no_xai_inline
  ```

- **El dataset es sintético y necesita revisión jurídica.** Los artículos están
  verificados contra el corpus (existen y son los que dicen ser), pero que sean *los
  pertinentes* para cada pregunta es un juicio legal. El campo `validated_by` está en
  `null` hasta que alguien lo revise.
- **Toda la evaluación es funcional.** En el marco habitual de XAI (Doshi-Velez y Kim
  distinguen evaluación funcional, con humanos genéricos, y en la aplicación real), este
  harness está entero en el primer nivel. Comprensión real, utilidad para decidir y
  plausibilidad jurídica de la cita quedan sin medir, y no se pueden medir sin personas.
- **Dos métricas son proxies declarados**: `support_density` (enlace léxico
  afirmación→cita) y `stability` (fidelidad causal por repeticiones). Comparan brazos, no
  dan cifras absolutas.
- **Cobertura del harness.** Ejercita n8n y `processing-api`. No pasa por el backend Java
  ni por el frontend.
- **El store es el de producción.** La evaluación sólo lee; no se ejecuta ningún workflow
  de subida ni de reemplazo.
- **Nunca contra producción.** `n8n import:workflow` del deploy apunta a
  `n8n/workflows/`; estos brazos viven en `n8n/workflows/eval/`, están excluidos del
  contexto de build y se importan sólo en la instancia local.

---

## Ficheros

| Fichero | Qué hace |
|---|---|
| `norms.py` | Tabla que une nombre de fichero del corpus ↔ clave del dataset ↔ cómo escribe el modelo la norma |
| `explainability.py` | Suficiencia, legibilidad, accionabilidad y estabilidad de la cita |
| `corpus_articles.py` | Registro de artículos existentes, detector de artículos citados, `--verify-dataset` |
| `dataset/family_law_v1.jsonl` | 63 preguntas con artículos, puntos clave y etiqueta de riesgo |
| `ablation/arms.py` | Los parches de cada brazo, con sus aserciones |
| `ablation/build_workflows.py` | Genera `n8n/workflows/eval/*.json` |
| `run_ablation.py` | Llama a los webhooks y guarda las respuestas crudas |
| `score.py` | Métricas deterministas contra el corpus |
| `report.py` | Tablas 2×2, efectos principales, contrastes pareados |

Tests en `api/tests/test_eval_ablation.py`, `test_eval_scoring.py` y
`test_eval_explainability.py`.
