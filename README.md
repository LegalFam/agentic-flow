# Flujo n8n: PDF legal a Markdown + metadata

Implementacion base para procesar PDFs legales desde Google Drive, convertirlos a Markdown fiel al documento original, extraer metadata juridica en JSON y preparar la subida opcional a Gemini File Search.

## Componentes

- `docker-compose.yml`: levanta n8n, PostgreSQL y la API de procesamiento.
- `api/`: microservicio FastAPI para conversion, OCR, metadata y subida opcional.
- `n8n/workflows/legal_pdf_to_gemini_file_search.json`: workflow importable para convertir PDF a Markdown + metadata.
- `n8n/workflows/upload_markdown_metadata_to_gemini_file_search.json`: workflow manual que resuelve/crea el File Search Store, empareja Markdown + metadata, pausa en un formulario de revision y continua con la subida o el log de omitido.
- `n8n/workflows/LegalFam Message Flow.json`: workflow de chat que clasifica la consulta del usuario y usa Gemini File Search con hints de metadata para mejorar la recuperacion RAG.
- `n8n/workflows/Move Reviewed PDFs To Proccessed.json`: workflow manual simple que mueve PDFs de `input/pdf` a `proccessed/pdf` cuando existe un `.review.json` cuyo campo `filename` coincide exactamente con el nombre del PDF.
- `n8n/workflows/Replace Document In Gemini File Search.json`: workflow manual que actualiza un documento ya indexado a partir del PDF nuevo. Convierte, extrae metadata, muestra en un formulario que version se va a borrar y recien entonces reemplaza.
- `.env.example`: variables requeridas.

## Puesta en marcha

1. Copiar `.env.example` a `.env` y completar credenciales.
2. Levantar servicios:

```powershell
docker compose up --build
```

3. Abrir n8n en `http://localhost:5678`.
4. Importar `n8n/workflows/legal_pdf_to_gemini_file_search.json`.
5. Configurar credenciales de Google Drive en n8n.
6. Ajustar los IDs de carpetas de Google Drive en los nodos `Set Drive Folder IDs`.
7. Importar el workflow de subida a Gemini solo cuando existan Markdown y JSON listos para revision.

## Carpetas esperadas en Google Drive

- `input/pdf`: PDFs fuente.
- `output/markdown`: archivos `.md`.
- `output/metadata`: archivos `.metadata.json`.
- `output/review`: paquete de revision manual del workflow de conversion.
- `output/errors`: errores por documento.
- `proccessed/pdf`: PDFs ya revisados y movidos fuera de `input/pdf`.

## API local

- `GET /health`: estado del servicio.
- `POST /convert`: PDF binario a Markdown.
- `POST /extract-metadata`: Markdown a metadata JSON validada.
- `POST /file-search-stores/resolve`: busca o crea el File Search Store por nombre visible y guarda su id en `/work/file_search_stores.json`.
- `POST /upload-gemini-file-search`: subida a Gemini File Search desde el workflow separado.
- `POST /file-search-stores/documents`: lista los documentos indexados en un store.
- `POST /file-search-stores/documents/plan-replace`: dice que version quedaria reemplazada por un nombre dado, sin tocar el store.
- `POST /file-search-stores/documents/replace`: sube la revision nueva, borra la vieja y sincroniza `work/corpus`.
- `POST /file-search-stores/documents/delete`: borra un documento del store sin subir nada. Es la limpieza para un reemplazo que quedo a medias.
- `POST /rag-search`: busqueda con grounding. Cada cita incluye `locator`, `breadcrumb`, `page` y `locator_source`.
- `GET /corpus/status`: cuantos markdowns ve el locator y si hay manifest.
- `POST /corpus/reload`: limpia el cache de indices sin reiniciar el contenedor.
- `POST /locator/probe`: diagnostico. Recibe `{title, snippet}` y devuelve la ubicacion resuelta y la estrategia usada.
- `POST /resolve-locators`: cambia los `citation_id` que traen los agentes por el locator autoritativo. Acepta ademas el `original_snippet` que declara el agente XAI.

## Actualizar un documento ya indexado

Gemini File Search **no tiene update in-place**. Un documento solo se puede borrar y
volver a subir. Si la version nueva entra sin sacar la vieja, el store queda con las dos
y el RAG recupera texto contradictorio del mismo cuerpo legal sin forma de saber cual
rige. Y el borrado no se deshace: no hay version anterior a la que volver.

La version vieja tampoco se encuentra por igualdad de nombre. `build_document_id` mete
el `sha256` del PDF en el nombre, asi que el PDF actualizado produce un `display_name`
distinto. Se encuentra por la misma llave que usa el locator para casar corpus y store:
el stem normalizado sin el hash.

Esa llave puede casar con varios documentos o con ninguno. En los dos casos el reemplazo
se **niega** con `409 REPLACE_NEEDS_DECISION` en vez de elegir, porque borrar el
documento equivocado no se deshace. La salida es indicar el nombre exacto en
`supersedes`.

El borrado va con `force`. Un documento ya indexado tiene chunks, y la API rechaza
borrarlo sin `force` con `400 FAILED_PRECONDITION: Cannot delete non-empty Document`, o sea
que falla exactamente en el caso normal. Si aun asi un borrado falla, el reemplazo **no**
revierte la subida (dejar el store sin ninguna version seria peor): responde con
`delete_failed` y un warning, y la version vieja se saca despues con
`/file-search-stores/documents/delete` o con `corpus_replace --delete <doc> --apply`.

### El corpus del locator tiene que moverse con el store

Es la parte que no perdona el olvido, y el dano es peor que perder la ubicacion de una
cita. Si el store apunta al nombre nuevo y el corpus todavia tiene el markdown viejo,
`resolve_document_path` no se queda sin respuesta: en su ultimo intento cae al stem sin el
hash, que es justamente lo que empareja las dos revisiones, y **casa con el archivo
viejo**. El locator entonces busca el snippet nuevo dentro del texto anterior, y con
`LOCATOR_FUZZY_THRESHOLD` en 0.82 un articulo que sobrevivio la modificatoria pero cambio
de numero casa igual. La cita sale con el numero anterior, con seguridad y sin aviso.

De ahi el orden, que no es negociable: **el markdown entra al corpus antes de reemplazar
en el store, y el viejo se borra despues**. Mientras el store apunta al nombre viejo nadie
consulta el nuevo, asi que el archivo nuevo no molesta; y en el instante en que el store
cambia, el corpus ya lo tiene y gana el match exacto. La ventana de citas mal atribuidas
es cero.

Ese movimiento, en los dos entornos, lo hace la API con `sync_corpus: true`:
`/file-search-stores/documents/replace` escribe el markdown nuevo, borra el viejo, poda el
manifest y limpia el cache del locator. En local sobre el bind mount `./work:/work`; en
Cloud Run sobre el volumen de Cloud Storage montado en `/corpus`, que va **con escritura**
y necesita `roles/storage.objectUser` en la runtime SA.

Ese volumen fue de solo lectura hasta que aparecio este flujo, y lo natural habria sido
que n8n escribiera el bucket por la API de GCS para no perder esa propiedad. No se puede:
esa ruta exige una credencial de service account con llave, y la organizacion las prohibe
por `iam.disableServiceAccountKeyCreation`. La runtime SA de Cloud Run, en cambio, saca
tokens del metadata server y no necesita llave.

Si el corpus no se puede escribir igual (un mount de solo lectura, un bucket sin permiso),
la respuesta sale con `corpus.synced: false` y un aviso en vez de romper: el reemplazo en
el store ya ocurrio y una excepcion ahi no ayudaria a nadie.

### Desde n8n

`Replace Document In Gemini File Search` toma los PDFs de una carpeta de Drive aparte de
`input/pdf` (para que el flujo de alta no los trate como documentos nuevos), los
convierte, extrae la metadata, y pausa en un formulario que muestra el nombre y el tamano
exactos de lo que se va a borrar. El desplegable de decision viene en `skip`, y el campo
`supersedes` solo viene precargado cuando hay una sola version que reemplazar: en el caso
ambiguo va vacio, para que aceptar sin leer no borre los dos candidatos.

Al cerrar, el flujo deja Drive coherente con lo que quedo indexado: sube el markdown y la
metadata nuevos a `processed/markdown` y `processed/metadata`, y **archiva los de la
revision anterior en `derogated`**. Eso no es cosmetico: `processed/markdown` es de donde
esta guia dice repoblar el corpus, asi que dejarlo con la version vieja significa que un
`rsync` de mantenimiento reintroduce el texto desactualizado y vuelve el caso silencioso
descrito arriba.

Ese tramo es limpieza y corre despues del reemplazo y del log. Si falla, el RAG queda
correcto y Drive atrasado, que es exactamente lo que `corpus_diff` reporta. Los dos nodos
que archivan llevan `alwaysOutputData` porque un markdown viejo que no este en Drive (un
documento indexado antes de que existiera este flujo) haria salir vacia la busqueda, y una
cadena cortada dejaria el loop sin arrancar el PDF siguiente.

Antes de la primera corrida hay que completar en `Edit Replace Config` los ids marcados
como `REPLACE_WITH_..._FOLDER_ID`: la carpeta de PDFs actualizados, la de logs de
reemplazo y `derogated`. `processed/pdf`, `processed/markdown` y `processed/metadata` ya
vienen con los mismos ids que usa `Upload To Gemini File Search`.

El reemplazo va siempre con `sync_corpus: true`: la sincronizacion del corpus la hace la
API por el mount, sin nodos de por medio.

Hubo una version de este flujo con dos nodos `GCS - *` que escribian el bucket desde n8n
antes de reemplazar. Se sacaron: necesitan una credencial de service account con llave, la
organizacion las prohibe, y un nodo con credencial que n8n no puede resolver muestra error
en cada corrida aunque nunca se ejecute — indistinguible de un fallo real. El orden sigue
garantizado, solo que dentro de `sync_replacement` en vez de en el canvas.

Los folder ids son distintos: son parametros del nodo `Edit Replace Config` y no tienen
resolucion por nombre, asi que el valor durable es el del JSON. Se pueden cambiar en la UI
para probar, pero el deploy corre `n8n import:workflow`, que hace upsert por id de
workflow, y eso pisa el contenido con el del repo. Vale para cualquier push que toque
`api/**`, `n8n/workflows/**`, `Dockerfile.n8n`, `scripts/sync-n8n-workflows.sh` o el propio
deploy: no hace falta que el cambio sea del workflow.

Lo que si hace falta en Cloud Run es que la runtime SA tenga `roles/storage.objectUser`
en el bucket, no solo `objectViewer`, porque ahora es ella la que escribe el corpus.

Si la subida al bucket falla, el flujo **no** sigue al reemplazo: se registra el error y
pasa al siguiente PDF. Es el punto del orden, y perderlo dejaria el store apuntando a un
markdown que el corpus no tiene. Si lo que falla es el borrado del objeto viejo, el store
ya quedo bien y la resolucion tambien (el nombre nuevo gana por match exacto), pero el
bucket queda con un archivo de mas: eso se registra como error para que alguien lo mire, y
`corpus_diff` lo reporta como sobrante.

### Desde la linea de comandos

```bash
docker compose run --rm processing-api python -m app.corpus_replace --store "FamilyLaw" --list
docker compose run --rm processing-api python -m app.corpus_replace --store "FamilyLaw" --plan codigo-civil-<hash>.md
```

El reemplazo pide el markdown ya convertido y su metadata, y sin `--apply` es dry-run:

```bash
docker compose run --rm processing-api python -m app.corpus_replace \
  --store "FamilyLaw" \
  --markdown /work/nuevo/codigo-civil-<hash>.md \
  --metadata /work/nuevo/codigo-civil-<hash>.metadata.json \
  --apply
```

Codigos de salida: 0 hecho, 2 hace falta una decision, 3 se aplico pero algo quedo a
medias (tipicamente el borrado o la sincronizacion del corpus), 1 error.

### Lo que este flujo no resuelve

- **Solo la metadata.** El `custom_metadata` se fija al indexar y no se puede editar. Un
  cambio de categorias o de fuente obliga a reemplazar el documento igual, con el mismo
  texto.
- **El PDF renombrado entre revisiones.** `build_document_id` pierde los acentos en vez
  de normalizarlos (`Codigo Procesal Civil.pdf` quedo indexado como `c-digo-...`), asi
  que mientras el PDF conserve su nombre las dos revisiones se manglean igual y casan. Si
  alguien lo renombra, el plan sale `new` y hay que pasar `supersedes` a mano.
- **El `citation_id` de una respuesta en vuelo.** El registro del locator apunta al texto
  viejo hasta que expira (`LOCATOR_REGISTRY_TTL_SECONDS`, 15 minutos por defecto). Una
  cita emitida justo antes del reemplazo puede resolver a una ubicacion que ya no existe.

## Ubicacion de las citas (citation locator)

Gemini File Search no devuelve en que parte del documento estaba el fragmento recuperado:
solo entrega el texto del chunk y la metadata del archivo completo. Para que una cita diga
`Art. 333` y no solo `Codigo Civil`, la API busca ese texto dentro del markdown original y
camina hacia atras por la jerarquia de encabezados (`LIBRO`, `SECCION`, `TITULO`,
`CAPITULO`, `Articulo N`, y los marcadores `## Pagina N` que agrega el OCR).

### Agregar documentos

Deben ser **los markdown ya procesados**, no los PDFs originales. El locator busca el
snippet dentro del texto que se indexo; reconvertir los PDFs produciria un texto distinto,
porque docling y el OCR no son deterministas entre corridas, y los matches exactos se
degradarian a difusos o a nada. El nombre debe
corresponder al `display_name` con el que se indexaron, aunque el matcher tolera
diferencias de mayusculas, acentos, separadores, extension y el hash de `document_id`
(ver abajo). Verifica siempre con `corpus_diff` antes de copiar.

- **Local**: copiar los `.md` a `work/corpus/`. Ya esta montado como `/work/corpus`.
  Son los mismos archivos de `processed/markdown` en Drive, que ya llevan el nombre con
  `document_id` y por lo tanto coinciden exactamente con el `display_name` del store.
- **Cloud Run**: subirlos al bucket configurado en la variable `CORPUS_BUCKET`. Se monta
  como `/corpus`, de solo lectura. No requiere volver a desplegar.

Verificar que la API los reconoce:

```bash
curl localhost:8000/corpus/status
```

### Verificar los nombres antes de copiar

`corpus_diff` cruza lo que hay en el store contra lo que hay en el corpus:

```bash
python -m app.corpus_diff --list-stores
python -m app.corpus_diff --store "fileSearchStores/familylaw-k3uggnk7czvu"
```

El store que usa el chat esta fijado en el nodo `Get FileStore Name1` del
`LegalFam Message Flow`, no en `GEMINI_FILE_SEARCH_STORE`, que suele estar vacio.

Reporta cuantos casan, cuales no, y que markdowns del corpus no reclama nadie. Ademas
compara el tamano de cada archivo local contra el `size_bytes` del markdown indexado, que
es la forma de confirmar que es la misma revision y no solo el mismo nombre. Distingue una
diferencia real de contenido de un simple cambio de saltos de linea.

Codigos de salida: 0 todo bien, 2 quedo algo sin casar, 3 casan todos pero alguno tiene
contenido distinto al indexado. Con `--write-manifest` guarda las sugerencias, que conviene
revisar a mano antes de confiar en ellas.

Un archivo con contenido distinto al indexado es el peor caso silencioso: el nombre casa,
el locator lo indexa, pero el snippet que devuelve Gemini no existe en ese texto y todo
cae al fallback sin explicacion aparente.

#### El hash del document_id

Los documentos no se indexaron con su nombre original: `build_document_id` en
`converter.py` los sube como `<stem>-<sha256(pdf)[:12]>.md`, por ejemplo
`codigo-civil-97fe57ba02fb.md`. El hash se calcula sobre los bytes del PDF y no se puede
recomputar desde el markdown, asi que el matcher lo ignora en ambas direcciones. No hace
falta renombrar nada por este motivo.

Lo que si rompe el match son los acentos: ese mismo `build_document_id` reemplaza todo lo
que no sea `[a-z0-9]` por un guion, de modo que `Código Procesal Civil.pdf` quedo indexado
como `c-digo-procesal-civil-...`, con la vocal perdida. Esos casos si necesitan manifest.

### Si los nombres no coinciden

Crear `work/corpus/corpus_manifest.json` con el mapeo, sin renombrar nada:

```json
{
  "Codigo Civil - Libro III.md": "codigo-civil-libro-iii.md"
}
```

La resolucion intenta, en orden: manifest, nombre exacto, nombre sin extension, stem
normalizado (ignora acentos, mayusculas y separadores), y stem normalizado sin el hash de
`document_id`. Cada intento se prueba con el `display_name` y con el `identificador` de la
metadata. Si nada casa devuelve `None` y la cita cae al fallback por regex: nunca adivina
un documento parecido, porque una cita atribuida al documento equivocado es peor que una
cita sin ubicacion.

### El fragmento que uso el agente (`original_snippet`)

Un chunk de File Search no respeta el articulado: puede arrancar a media frase del
`Art. 561` y terminar dentro del `Art. 563`. El locator del chunk se queda con el primero,
que no tiene por que ser el que sustenta la respuesta. Ese fue el caso real: la cita salia
bien redactada y atribuida al articulo equivocado.

Por eso el agente XAI devuelve dos textos por cita, y solo esos dos:

- `original_snippet`: copia literal del pasaje del chunk en el que se apoyo.
- `summary_snippet`: el resumen que lee el usuario (el backend lo persiste como
  `source_snippet`).

No hay un tercer campo con el mismo texto: `summary_snippet` es el unico nombre del
resumen de punta a punta.

`/resolve-locators` trata `original_snippet` como puntero, no como ubicacion: lo busca
dentro del chunk que se guardo al recuperarlo y, si aparece, recalcula la ubicacion sobre
el markdown a partir de esa posicion. Un texto que el modelo invento no esta en el chunk y
se descarta sin mas.

El documento de la cita (`file_name`, `file_url`) tampoco viaja por el prompt: se guarda
en el registro junto al locator y `/resolve-locators` lo devuelve a partir del
`citation_id`. Antes dependia de que el agente lo copiara, y una cita sin `file_url` se
caia entera en el backend.

#### Un excerpt tambien puede cruzar articulos

El corte del excerpt lo elige el modelo, asi que puede arrancar al final del `Art. 562` y
seguir dentro del `Art. 563`. Ubicarlo en el primero reproduce el mismo error un nivel mas
abajo. Cuando eso pasa se citan todos los articulos que cubre (`Arts. 562 y 563`), siempre
que cuelguen del mismo padre y no sean mas de `LOCATOR_MAX_COMBINED_ARTICLES` (3 por
defecto). Un tramo que cruza de un titulo a otro ya no ubica nada util: sale sin ubicacion.

`locator_scope` dice de donde salio cada ubicacion:

- `excerpt`: del pasaje citado, que cae dentro de un solo articulo. Es el caso bueno.
- `excerpt_multi`: del pasaje citado, que cruza articulos y se cita con todos ellos.
- `chunk`: del chunk completo, porque no hubo `original_snippet` verificable y el chunk
  cubre un solo articulo, asi que no hay ambiguedad que resolver.
- `ambiguous`: se cubrian varios articulos que no se pudieron combinar, o el chunk cubria
  varios y no hubo `original_snippet` verificable. La cita sale sin ubicacion; elegir el
  primer articulo seria adivinar. El caso del chunk se apaga con
  `LOCATOR_REQUIRE_EXCERPT_WHEN_AMBIGUOUS=false`.
- `unknown`: el `citation_id` no esta en el registro (alterado por un agente o vencido).

`LOCATOR_MIN_EXCERPT_CHARS` (25 por defecto) descarta pasajes tan cortos que casarian en
cualquier articulo.

### Degradacion

La feature es aditiva y nunca rompe una busqueda:

- Sin documentos en el corpus, o si el archivo no casa, se aplica un regex de
  `Articulo N` sobre el propio snippet (`locator_source: "snippet_regex"`). Si el texto
  nombra mas de un articulo, el regex no elige: sin indice no hay forma de saber donde
  termina uno y empieza el otro.
- Si tampoco hay referencia normativa, los campos quedan vacios y la cita sale igual que
  antes de esta feature.
- Cualquier excepcion del locator se traga: `ENABLE_CITATION_LOCATOR=false` la desactiva
  por completo.

### Medir cobertura

Antes de propagar el locator al chat conviene saber que porcentaje del corpus resuelve:

```bash
docker compose run --rm processing-api python -m app.locator_coverage
```

Reporta el desglose por estrategia (`exact`, `prefix`, `fuzzy`, `snippet_regex`, `none`),
los documentos mas flojos y cuales quedaron sin encabezados legales detectados, que suele
indicar que el markdown perdio la estructura al convertir el PDF. Sale con codigo 0 si
`exact+prefix` supera el 80%, y con 2 si no llega.

### Tests

`tests/` no entra en la imagen para no ensuciar produccion, asi que se montan al correr:

```bash
docker compose run --rm -v ./api:/app processing-api sh -c "pip install -q -r requirements-dev.txt && pytest -q"
```

## Notas

- La metadata no se embebe en el Markdown; se genera como JSON separado.
- Los campos sin evidencia clara deben quedar como `null`.
- Las categorias y subcategorias se validan contra un catalogo cerrado.
- La subida a Gemini File Search corre en un workflow aparte y usa pares `archivo.md` + `archivo.metadata.json`.
- El workflow manual configura el File Search Store en `Edit Upload Config`, resuelve/crea el store y guarda su id desde la API.
- `Loop Over Markdown Files` procesa un documento por vez para que la revision sea secuencial.
- La revision visual ocurre en `Wait - Metadata Review Form`. La ejecucion queda pausada, el admin edita la metadata en el formulario y al enviarlo el mismo workflow continua con ese documento antes de pasar al siguiente.
- Los nodos finales son logs en Google Drive: subida correcta, omitido o error.
- El campo `fuente` sirve como origen/enlace editable por el admin antes de la subida.
- La clasificacion usa `categorias`, un arreglo de una o mas parejas `categoria/subcategoria`.

## Metadata compacta para Gemini File Search

Gemini File Search limita cada `custom_metadata.string_value` a 256 caracteres. Por eso la API no sube el JSON completo de `categorias` como metadata del store. El JSON completo se conserva en Google Drive y en los logs de subida, pero Gemini recibe una version compacta para evitar errores y habilitar busqueda por categoria.

Formato usado al subir:

```json
{
  "categorias_count": "6",
  "categorias_compact": "plra:conciliacion|rpfm:pension_alimentos,tenencia_custodia|vcp:sociedad_gananciales|ppv:violencia_familiar,capacidad_juridica|sh|gen:derecho_familia"
}
```

Codigos de categoria:

```text
vcp  -> Vinculos Conyugales y Patrimoniales
rpfm -> Relaciones Paterno-Filiales y Menores
ppv  -> Proteccion a Personas Vulnerables
sh   -> Sucesiones y Herencia
plra -> Procesos Legales y Resolucion Alternativa
gen  -> Generales
```

Tambien se aplica un limite defensivo a todos los valores de custom metadata. Si `observaciones` es largo, se sube como `observaciones_resumen`.

## Uso en LegalFam Message Flow

`LegalFam Message Flow` primero clasifica el mensaje del usuario con `Parser Agent`. Luego el nodo `Map Retrieval Metadata` convierte esa categoria a:

- categoria canonica del catalogo juridico;
- subcategorias canonicas;
- hints compactos de metadata, por ejemplo `rpfm:pension_alimentos`;
- terminos de busqueda juridicos relacionados.

El tool `SearchStore` recibe una consulta enriquecida con esos datos. Ejemplo:

```text
Consulta del usuario: Como calculo una pension de alimentos?
Categoria legal: Pension de Alimentos
Categoria canonica: Relaciones Paterno-Filiales y Menores
Subcategorias canonicas: Pension de Alimentos
Metadata preferida: rpfm:pension_alimentos
Terminos de busqueda: pension de alimentos, alimentos, obligacion alimentaria
```

Esto reduce ruido en el RAG porque la busqueda no depende solo de la pregunta literal del usuario: tambien incluye la clasificacion legal y los mismos codigos compactos usados al indexar documentos.
