# Flujo n8n: PDF legal a Markdown + metadata

Implementacion base para procesar PDFs legales desde Google Drive, convertirlos a Markdown fiel al documento original, extraer metadata juridica en JSON y preparar la subida opcional a Gemini File Search.

## Componentes

- `docker-compose.yml`: levanta n8n, PostgreSQL y la API de procesamiento.
- `api/`: microservicio FastAPI para conversion, OCR, metadata y subida opcional.
- `n8n/workflows/legal_pdf_to_gemini_file_search.json`: workflow importable para convertir PDF a Markdown + metadata.
- `n8n/workflows/upload_markdown_metadata_to_gemini_file_search.json`: workflow manual que resuelve/crea el File Search Store, empareja Markdown + metadata, pausa en un formulario de revision y continua con la subida o el log de omitido.
- `n8n/workflows/LegalFam Message Flow.json`: workflow de chat que clasifica la consulta del usuario y usa Gemini File Search con hints de metadata para mejorar la recuperacion RAG.
- `n8n/workflows/Move Reviewed PDFs To Proccessed.json`: workflow manual simple que mueve PDFs de `input/pdf` a `proccessed/pdf` cuando existe un `.review.json` cuyo campo `filename` coincide exactamente con el nombre del PDF.
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
- `POST /rag-search`: busqueda con grounding. Cada cita incluye `locator`, `breadcrumb`, `page` y `locator_source`.
- `GET /corpus/status`: cuantos markdowns ve el locator y si hay manifest.
- `POST /corpus/reload`: limpia el cache de indices sin reiniciar el contenedor.
- `POST /locator/probe`: diagnostico. Recibe `{title, snippet}` y devuelve la ubicacion resuelta y la estrategia usada.

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

### Degradacion

La feature es aditiva y nunca rompe una busqueda:

- Sin documentos en el corpus, o si el archivo no casa, se aplica un regex de
  `Articulo N` sobre el propio snippet (`locator_source: "snippet_regex"`).
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
