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
