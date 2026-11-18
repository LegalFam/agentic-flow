# Actualización de normas desde El Peruano

> Memoria del proyecto. Cómo se mantienen al día las normas del corpus de LegalFam con lo
> que publica el Diario Oficial El Peruano: qué hace cada pieza, por qué está hecha así, qué
> garantías da y cómo se opera. Si vas a tocar este flujo, lee primero
> [Garantías](#garantías-lo-que-nunca-debe-romperse).

## Qué problema resuelve

El RAG responde con el texto de las normas indexadas en Gemini File Search (store
**FamilyLaw**). Cuando una ley se modifica, se deroga o se declara inconstitucional, nuestra
copia queda desactualizada y el asistente cita texto que ya no rige, sin aviso.

Este flujo detecta esos cambios en El Peruano, propone la actualización de nuestro markdown,
la somete a revisión humana y recién entonces cambia el store, con copia de seguridad y
rollback.

## Decisiones de fondo (y por qué)

| Decisión | Motivo |
|---|---|
| **La fuente es El Peruano** (`busquedas.elperuano.pe`), no SPIJ | SPIJ (MINJUS) y `defensoria.gob.pe` cortan la conexión desde IPs fuera del Perú o de nube. Cloud Run no está en el Perú (no hay región GCP allí). El Peruano sí responde desde fuera, comprobado desde n8n en Cloud Run. |
| **No se re-descarga el "link original"** de cada documento | Se revisaron los 60 links del store: los de `gob.pe` son PDFs fijos (el id de archivo en la URL identifica UNA edición) y no se actualizan. El único que se actualiza es la ficha de SPIJ, inaccesible desde el servidor. |
| **El cambio se aplica sobre nuestro markdown** con la convención del SPIJ | Nuestros markdowns ya traen las reformas como notas `- (*) Artículo modificado por el Artículo 1 de la Ley Nº …, publicada el …, cuyo texto es el siguiente:` + el texto entre comillas. Se mantiene esa convención: nada se borra, todo queda trazable. |
| **Gemini nunca escribe el documento** | Gemini devuelve datos (tipo de efecto, línea, texto nuevo) y el **código** compone el bloque. En la primera prueba, dejar que Gemini reescribiera el artículo duplicó un párrafo. |
| **SPIJ solo lo usa la persona que revisa** | El formulario de revisión muestra el link SPIJ de la norma: quien revisa (desde el Perú) contrasta antes de aprobar. |
| **Revisión humana obligatoria** | En File Search un borrado no se deshace. Ningún cambio llega al store sin una decisión explícita (`skip` por defecto). |
| **Copia de seguridad verificada antes de todo cambio** | Pedido explícito: guardar copia local de lo que se va a cambiar y poder volver atrás. |

## Arquitectura

```
                        ┌──────────────────────────── processing-api (FastAPI) ───────────────────────────┐
  n8n                   │                                                                                 │
  ─────────────────     │  elperuano.py          amendments.py                           store_documents.py│
  Scan (diario 07:40) ──┼─▶ listar / buscar ──▶ prefiltro ─▶ Gemini clasifica ─▶ código   swap_document   │
                        │   descargar fichas     (mención +    (efecto, artículo,   inserta  (sube, borra,  │
                        │                         verbo de      evidencia literal)  la nota   sync corpus)  │
                        │                         cambio)            │                 │          ▲         │
                        │                                    Gemini ubica línea        │          │         │
                        │                                    y texto nuevo     verificaciones     │         │
                        │                                                      deterministas      │         │
                        │                                                             │           │         │
                        │                                              propuesta en disco          │         │
  Review (manual) ──────┼─▶ GET propuestas ─▶ formulario ─▶ POST decide ─▶ copia de seguridad ────┘         │
  Rollback (manual) ────┼─▶ GET copias ─────▶ formulario ─▶ POST rollback ─▶ copia de lo actual ─▶ restaura │
                        └─────────────────────────────────────────────────────────────────────────────────┘
```

### Archivos

| Archivo | Qué hace |
|---|---|
| `api/app/elperuano.py` | Cliente del buscador de El Peruano: listar normas por fecha, buscar por frase, leer una ficha. Falla con `ElPeruanoFormatError` si el HTML cambia. |
| `api/app/amendments.py` | Todo el flujo: registro, detección, Gemini, inserción, verificación, propuestas, copias, decisión, rollback, historial. |
| `api/app/store_documents.py` | `get_store_document` (documento con su metadata cruda) y `swap_document` (subir una versión y borrar otras, con el corpus detrás). |
| `api/app/gemini_client.py` | `upload_document(..., raw_custom_metadata=...)`: sube con la metadata copiada tal cual. |
| `api/app/corpus.py` | `remove_documents` (derogación total) y exclusión de la carpeta de amendments del corpus. |
| `api/app/data/normas_vigiladas.seed.json` | Semilla del registro de normas vigiladas. |
| `api/tests/test_amendments.py` + `api/tests/fixtures/elperuano/` | Tests con fichas reales de El Peruano y un extracto real de la Ley 30364. |
| `n8n/workflows/Scan El Peruano For Norm Updates.json` | Escaneo diario (07:40 Lima) y manual. |
| `n8n/workflows/Review Norm Updates.json` | Formulario de revisión por propuesta pendiente. |
| `n8n/workflows/Rollback Norm Update.json` | Formulario de rollback. |

## El flujo paso a paso

### 1. Registro de normas vigiladas

Solo se vigilan **normas**; la jurisprudencia (casaciones del PJ) no cambia. Cada entrada:

```json
{
  "document": "ley3036-erradicarviolencia-0eeaf4de148c.md",   // display_name en el store
  "identificador": "Ley N° 30364",
  "aliases": ["Ley para prevenir, sancionar y erradicar la violencia contra las mujeres ..."],
  "version_hasta": "2018-10-25",                               // hasta dónde está al día
  "spij_url": null,                                            // para quien revisa
  "notas": "..."
}
```

- `version_hasta` se fijó con la **fecha de la última nota `(*)`** de cada markdown indexado,
  no a ojo.
- Un protocolo sin número propio se vigila **solo por nombre** (aliases).
- El registro vivo se guarda junto a las copias (ver [Dónde vive cada cosa](#dónde-vive-cada-cosa));
  si no existe se usa la semilla. Al aplicar un cambio, el registro se actualiza solo: nuevo
  `document` y nuevo `version_hasta`.

### 2. Búsqueda de candidatas (`collect_candidates`)

- **Rango corto (≤ 7 días, la corrida diaria):** se listan **todas** las Normas Legales
  publicadas (~40–75 por día) y se descarga cada ficha. No depende del buscador del sitio.
- **Rango largo (puesta al día):** leer todo serían decenas de miles de fichas, así que se usa
  el buscador con **frases exactas entre comillas** (`"Ley 30364"`, `"Ley N° 30364"`,
  `"Ley Nº 30364"`, `"Código Civil"`…). Sin comillas el buscador une palabras con OR
  (`Decreto Legislativo N° 1297` devolvía 507 normas). Si un rango pasa de 1000 resultados se
  parte en dos recursivamente (nunca se trunca en silencio). Solo se descarga la ficha de
  leyes, decretos legislativos, de urgencia y supremos, o de normas cuya sumilla anuncia un
  cambio (`worth_fetching`).

### 3. Lectura de la ficha (`elperuano.parse_dispositivo`)

El texto oficial viene como un documento HTML embebido en `#visor-html`. Se toma **solo su
`<body>`**: el `<title>` de ese documento a veces trae la sumilla de *otra* norma (bug del
sitio, visto en fichas reales). Si falta la fecha o el visor, es `ElPeruanoFormatError`.

### 4. Prefiltro sin costo (`dispositive_part` + `mentions_with_change`)

1. Se separa la **parte dispositiva** (desde "SE RESUELVE", "DECRETA:", "Ha dado la Ley
   siguiente", "HA RESUELTO", "FALLA:"…). Los considerandos citan derogaciones ajenas
   ("la Ley 27293 … ha sido derogada por el D.L. 1252") que no son efecto de la norma.
2. Debe aparecer nuestra norma **con un verbo de cambio a ≤ 350 caracteres**
   (`modific|derog|incorpor|sustitu|sin efecto|inconstitucional|texto único ordenado|…`).
   Una mención sola es base legal. En la prueba real, 23 de 25 normas que citaban la Ley
   30364 en su parte dispositiva eran de ese tipo.
3. Menciones a "**el Reglamento de** la Ley X" o "**el TUO de** la Ley X" no cuentan (son otro
   texto), salvo "**aprueba** el Texto Único Ordenado de la Ley X", que sí nos afecta.
4. Los números se normalizan: `N°/Nº/N.º`, ceros a la izquierda (`000050-2014-MC` =
   `050-2014-MC`) y el sufijo del sector se conserva (`100-2021-MIMP` ≠ `100-2021-MINSA`).

### 5. Clasificación con Gemini (`classify`)

Gemini recibe el **encabezado** (para nombrar la norma) y la **parte dispositiva**, y devuelve
efectos: `modifica`, `incorpora`, `deroga`, `deroga_total`, `fe_de_erratas`,
`inconstitucional`, `nuevo_tuo` o `ninguno`, con `afecta_a`, `articulos`, `disposicion` y
`evidencia`. El código descarta:

- efectos cuyo `afecta_a` no es `"norma"` (reglamento, TUO, otra norma);
- efectos cuya **evidencia no está literal** en la parte dispositiva (alucinación o
  considerandos).

### 6. Ubicación e inserción (`apply_effect_to_article`)

Por cada artículo afectado:

1. El código encuentra el artículo en nuestro markdown (`find_article`).
2. Si el artículo ya tiene una nota `(*)` de esta norma, o ya contiene el texto nuevo →
   **`ya_aplicado`** (idempotencia: la puesta al día puede pisar rangos ya cubiertos).
3. Gemini recibe el artículo con **líneas numeradas** (`L12| …`) y devuelve
   `alcance`, `ancla_linea` (un número) y `texto_nuevo`. Se usan números porque copiar la línea
   fallaba con párrafos que el PDF partió en dos.
4. El código decide dónde va:
   - cambio al **artículo completo** → al final del artículo;
   - si no, después del **párrafo completo** de la línea indicada (`_end_of_paragraph`
     no corta oraciones partidas por el PDF);
   - artículo **incorporado** que no existe (p. ej. 46-F) → después del último artículo de
     número menor o igual.
5. El código arma el bloque (`build_note_block`):

   ```
   - (*) Párrafo incorporado por el Artículo 1 de la Ley N° 32298, publicada el 11 abril 2025, cuyo texto es el siguiente:

   " La Policía Nacional del Perú coordina con el Programa Nacional ... "
   ```

   El texto nuevo se limpia de `[...]` y del cierre `".` de la norma modificatoria.

Si algo no cuadra (artículo inexistente, línea fuera de rango, texto no literal), el cambio
queda como **`aviso`** para revisión manual; nunca se fuerza.

### 7. Verificaciones deterministas (`verify_proposal`)

Toda propuesta, la arme quien la arme, tiene que pasar:

1. **Solo inserciones**: ninguna línea original cambia ni desaparece (`difflib`).
2. **Todo texto agregado es literal de El Peruano** (las líneas de nota `- (*)` las escribe el
   código).
3. **Ningún párrafo agregado dos veces.**

Si falla, la propuesta queda `blocked`: solo se puede omitir.

### 8. Propuesta

Una propuesta por documento, con todas las normas pendientes **en orden cronológico**
aplicadas sobre el mismo markdown. Se guarda en `proposals/<id>/` (`proposal.json`,
`propuesto.md`, `cambios.diff`) con `base_sha256` del markdown de partida. Acciones:
`revise` (subir versión nueva), `delete` (derogación total), `none` (nada que cambiar en el
texto), `blocked`.

Un escaneo nuevo **recalcula** las propuestas pendientes del mismo documento junto con las
normas nuevas (la anterior queda `superseded`). Las normas ya decididas (`applied`/`skipped`)
no se vuelven a proponer.

### 9. Revisión (workflow `Review Norm Updates`)

Por cada propuesta pendiente, un formulario con: acción, normas con link a El Peruano, link
SPIJ, cambios, problemas de verificación y el diff (solo debe haber líneas `+`). Campos:
`decidedBy` (obligatorio) y `decision` (`skip` por defecto | `apply`).

### 10. Aplicar (`decide`)

1. Se rechaza si la propuesta no está `pending`, está `blocked`, o si el markdown cambió desde
   que se armó (`base_sha256`).
2. **Copia de seguridad** (`backup_document`): markdown exacto + `custom_metadata` crudo del
   store. **Solo si el markdown local mide exactamente lo mismo que el documento indexado**;
   si no, `BackupError` y no se toca nada.
3. `swap_document`: sube la versión nueva con nombre `<stem>-<sha256[:12]>.md` y **la misma
   metadata cruda** (incluida la `fuente` que cargó el administrador), luego borra la vieja,
   luego sincroniza el corpus del locator. Mismo orden que el reemplazo existente: un store sin
   ninguna versión es peor que uno con dos por unos segundos.
4. Historial, propuesta `applied`, registro actualizado.

Derogación total: misma copia de seguridad, se borra del store y del corpus.

### 11. Rollback (workflow `Rollback Norm Update` o `POST /amendments/rollback`)

1. Verifica el `sha256` de la copia.
2. **Respalda también la versión que se va a sacar** (el rollback se puede deshacer).
3. Sube la versión guardada con su nombre y metadata originales y borra la que la reemplazó.
4. Corpus y registro vuelven al estado anterior; queda en el historial.

## Garantías (lo que nunca debe romperse)

1. Nada se borra ni se reescribe del texto existente: solo se agregan notas.
2. Todo texto legal agregado es copia literal de El Peruano.
3. Nada llega al store sin decisión humana explícita.
4. Nada cambia el store sin una copia de seguridad que coincida byte a byte con lo indexado.
5. Todo cambio tiene rollback, y todo rollback también.
6. La metadata del documento viaja intacta.
7. Un cambio de formato de El Peruano es un **error** (`502 SOURCE_FORMAT_CHANGED`), nunca un
   "no hubo normas".

Cada una tiene su test en `api/tests/test_amendments.py`.

## Dónde vive cada cosa

| | Local (docker compose) | Cloud Run |
|---|---|---|
| Corpus | `/work/corpus` (`./work`) | `/corpus` (bucket GCS) |
| Amendments (`AMENDMENTS_DIR`) | `/work/amendments` | `/corpus/_amendments` |
| Registro (`AMENDMENTS_REGISTRY_PATH`) | `/work/normas_vigiladas.json` | `/corpus/_amendments/normas_vigiladas.json` |

En Cloud Run el bucket es el único almacenamiento que sobrevive al contenedor, y de eso depende
el rollback. Las copias son markdowns con el mismo nombre que los indexados, así que
`corpus.iter_corpus_files` **excluye** la carpeta de amendments: si el locator las viera,
resolvería citas contra una versión vieja.

Dentro de `AMENDMENTS_DIR`:

```
proposals/<id>/proposal.json | propuesto.md | cambios.diff
backups/<AAAAMMDDTHHMMSS-xxxxxx>/backup.json | <documento>.md
history.jsonl          # applied / skipped / rollback, con quién y cuándo
```

## API

| Método | Ruta | Uso |
|---|---|---|
| GET | `/amendments/registry` | Normas vigiladas. |
| POST | `/amendments/scan` | `{fecha_ini?, fecha_fin?, documents?}` (ISO). Sin fechas: ayer y hoy. |
| GET | `/amendments/proposals?status=pending` | Propuestas (`pending`, `applied`, `skipped`, `superseded`; vacío = todas). |
| GET | `/amendments/proposals/{id}` | Una propuesta con su diff. |
| POST | `/amendments/decide` | `{proposal_id, decision: apply|skip, decided_by, file_search_store_name}`. |
| GET/POST | `/amendments/backups` | Listar / crear copias a pedido (por defecto, todas las vigiladas). |
| POST | `/amendments/rollback` | `{backup_id, decided_by, file_search_store_name}`. |
| GET | `/amendments/history` | Historial. |

Errores: `409 AMENDMENT_REFUSED` (negativa deliberada), `409 BACKUP_FAILED` (no hay copia
garantizada), `502 SOURCE_FORMAT_CHANGED` (El Peruano cambió), `502 SOURCE_UNAVAILABLE`.

## Operación

- **Diario:** `Scan El Peruano For Norm Updates` corre a las 07:40 (America/Lima). Para que
  corra solo, su id (`ScanElPeruanoNrm1`) tiene que estar en `N8N_PUBLISH_WORKFLOW_IDS`.
- **Revisar:** ejecutar `Review Norm Updates` y abrir los formularios.
- **Volver atrás:** `Rollback Norm Update`, elegir el `backup_id` de la lista.
- **Copia preventiva de todo:** `POST /amendments/backups` con `{}`.
- **Agregar una norma vigilada:** agregarla al registro vivo (o a la semilla si aún no existe
  el vivo) con su `version_hasta` = fecha de su última nota `(*)`, y correr un escaneo desde
  esa fecha.
- **Si el escaneo falla con `SOURCE_FORMAT_CHANGED`:** El Peruano cambió el HTML. Ajustar
  `elperuano.py` con una ficha nueva como fixture. No asumir que no hubo normas.

## Pruebas hechas contra producción

- **Store real (solo lectura):** las 9 normas del registro tienen copia local que coincide
  **byte a byte** con FamilyLaw, así que la copia de seguridad garantizada es posible para
  todas.
- **Punta a punta con APIs reales, en un store temporal** (FamilyLaw no se escribió): copia de
  la Ley 30364 con su metadata real de producción → escaneo real de El Peruano (abril–junio
  2025) → Gemini real → propuesta con las Leyes 32298 (incorpora párrafo al art. 10) y 32351
  (modifica el art. 46), verificada sin problemas → aplicar → el store quedó solo con la versión
  nueva, metadata idéntica, corpus sincronizado → rollback → versión original restaurada
  **byte a byte** → store temporal borrado.
- La misma prueba sacó a la luz y quedaron corregidos: ancla por texto que fallaba con
  párrafos partidos, nota de artículo completo al inicio en vez del final, número de norma sin
  encabezado, sufijo de sector perdido (`100-2021-MIMP`), y 23 normas que solo citaban la ley
  y pasaban a Gemini.

## Limitaciones conocidas

- **El Peruano no tiene API**: se lee su HTML. Un rediseño rompe el parser (falla ruidosa, no
  silenciosa).
- **Puesta al día con buscador:** en rangos largos solo se descargan fichas de normas con rango
  o con sumilla de cambio. Una derogación escondida en una resolución menor con sumilla neutra
  solo se detecta en la corrida diaria, que lee todo.
- **Derogaciones genéricas** ("derógase toda norma que se oponga") no nombran la norma: no se
  detectan.
- **Nuevo TUO:** se detecta como `aviso`; reemplazar el documento por el TUO es manual (flujo
  `Replace Document In Gemini File Search`).
- **Normas antes de ~2010:** el buscador de El Peruano no las devuelve.
- **El texto anterior queda en el documento** (como en SPIJ, con la nota debajo). El RAG puede
  recuperar el párrafo viejo; la nota inmediatamente posterior indica que fue modificado.
