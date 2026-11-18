"""Cliente del buscador oficial de El Peruano (busquedas.elperuano.pe).

No hay API publica: se lee el HTML que el sitio renderiza en el servidor. Por eso cada
parser falla de forma ruidosa (`ElPeruanoFormatError`) cuando la pagina deja de tener la
forma esperada. Un cambio de maquetacion que se tradujera en "cero normas" en silencio
haria creer que no hubo modificaciones, que es justo el error que este modulo existe para
evitar.
"""

import html
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date

BASE_URL = "https://busquedas.elperuano.pe"
PAGE_SIZE = 20
USER_AGENT = "Mozilla/5.0 (compatible; LegalFamMonitor/1.0)"
REQUEST_DELAY_SECONDS = 0.5
MAX_PAGES = 50


class ElPeruanoError(RuntimeError):
    pass


class ElPeruanoFormatError(ElPeruanoError):
    """La pagina respondio, pero no con la estructura que el parser conoce."""


@dataclass
class NormaRef:
    id: str
    entidad: str = ""
    tipo: str = ""
    numero: str = ""
    sumilla: str = ""
    fecha: str = ""

    @property
    def url(self) -> str:
        return dispositivo_url(self.id)


@dataclass
class Dispositivo:
    id: str
    url: str
    fecha_publicacion: str
    texto: str
    pdf_url: str | None = None
    extras: dict = field(default_factory=dict)


def dispositivo_url(norma_id: str) -> str:
    return f"{BASE_URL}/dispositivo/NL/{norma_id}"


def _fetch(url: str, timeout: int = 60, retries: int = 5) -> str:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception as exc:  # red, timeout, 5xx
            last_error = exc
            # El Peruano corta la conexion por unos segundos tras muchas paginas seguidas.
            if attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
    raise ElPeruanoError(f"No se pudo leer {url}: {last_error}")


def _yyyymmdd(value: date) -> str:
    return value.strftime("%Y%m%d")


def listing_url(fecha_ini: date, fecha_fin: date, start: int = 0, query: str | None = None) -> str:
    params = {
        "fechaIni": _yyyymmdd(fecha_ini),
        "fechaFin": _yyyymmdd(fecha_fin),
        "tipoPublicacion": "NL",
        "ci": "ONLY",
        "start": str(start),
    }
    if query:
        params = {"query": query, **params}
    return f"{BASE_URL}/?{urllib.parse.urlencode(params)}"


_CARD_SPLIT = re.compile(r'<div class="rounded-xl border bg-card')
_TAG = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment))).strip()


def parse_listing(page: str) -> list[NormaRef]:
    """Lee las tarjetas de un listado de Normas Legales.

    Cada tarjeta trae la entidad, un enlace con tipo y numero, la sumilla y, al pie, el id
    y la fecha. Una tarjeta sin enlace a /dispositivo/NL/ no es una norma y se ignora.
    """
    refs: list[NormaRef] = []
    seen: set[str] = set()
    for card in _CARD_SPLIT.split(page)[1:]:
        ids = re.findall(r'href="/dispositivo/NL/([0-9]+-[0-9]+)"', card)
        if not ids or ids[0] in seen:
            continue
        norma_id = ids[0]
        seen.add(norma_id)

        entidad = re.search(r'<p class="text-sm font-semibold text-primary">(.*?)</p>', card, re.S)
        cabecera = re.search(
            r'<a [^>]*href="/dispositivo/NL/[^"]+"[^>]*>\s*<p[^>]*>(.*?)</p>\s*<p[^>]*>(.*?)</p>', card, re.S
        )
        sumilla = re.search(r'line-clamp-3" href="/dispositivo/NL/[^"]+"[^>]*>(.*?)</a>', card, re.S)
        fecha = re.search(r"<span>\s*[a-záéíóúñ]+\s+(\d{2}\.\d{2}\.\d{4})\s*</span>", card)

        refs.append(
            NormaRef(
                id=norma_id,
                entidad=_text(entidad.group(1)) if entidad else "",
                tipo=_text(cabecera.group(1)) if cabecera else "",
                numero=_text(cabecera.group(2)) if cabecera else "",
                sumilla=_text(sumilla.group(1)) if sumilla else "",
                fecha=fecha.group(1) if fecha else "",
            )
        )
    return refs


def _looks_like_listing(page: str) -> bool:
    # El formulario de busqueda y el pie del buscador estan en toda pagina de resultados,
    # incluso cuando no hay normas. Si faltan, lo que llego no es el buscador.
    return "Buscador El Peruano" in page or "/assets/" in page


def list_normas(
    fecha_ini: date,
    fecha_fin: date,
    query: str | None = None,
    fetch=_fetch,
    delay: float = REQUEST_DELAY_SECONDS,
) -> list[NormaRef]:
    """Todas las Normas Legales publicadas en el rango, opcionalmente filtradas por texto."""
    refs: list[NormaRef] = []
    seen: set[str] = set()
    for page_number in range(MAX_PAGES):
        start = page_number * PAGE_SIZE
        page = fetch(listing_url(fecha_ini, fecha_fin, start, query))
        if not _looks_like_listing(page):
            raise ElPeruanoFormatError(
                "El buscador de El Peruano respondio con una pagina desconocida. "
                "Revisa si cambio el sitio antes de confiar en que no hubo normas."
            )
        batch = [ref for ref in parse_listing(page) if ref.id not in seen]
        for ref in batch:
            seen.add(ref.id)
        refs.extend(batch)
        has_next = f"start={start + PAGE_SIZE}" in page
        if not batch or not has_next:
            break
        if delay:
            time.sleep(delay)
    else:
        raise ElPeruanoError(
            f"El rango devolvio mas de {MAX_PAGES * PAGE_SIZE} normas; acotalo para no truncar resultados."
        )
    return refs


_BLOCK_END = re.compile(r"</(p|div|h[1-6]|li|tr|section|article)>|<br\s*/?>", re.I)


def _html_to_text(fragment: str) -> str:
    fragment = re.sub(r"<script.*?</script>|<style.*?</style>|<svg.*?</svg>", "", fragment, flags=re.S | re.I)
    fragment = _BLOCK_END.sub("\n", fragment)
    text = html.unescape(_TAG.sub("", fragment))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _embedded_body(page: str) -> str | None:
    """El texto oficial viene como un documento HTML completo dentro de #visor-html.

    Solo se toma su <body>: el <title> de ese documento a veces trae la sumilla de OTRA
    norma (se ve en fichas reales), y leerlo metia esa norma ajena en el texto.
    """
    visor = page.find('id="visor-html"')
    if visor < 0:
        return None
    start = page.find("<body", visor)
    end = page.find("</body>", start)
    if start < 0 or end < 0:
        return None
    return page[page.find(">", start) + 1 : end]


def parse_dispositivo(norma_id: str, page: str) -> Dispositivo:
    fecha = re.search(r"Fecha de publicación:?\s*(?:<!-- -->)?\s*(\d{2}/\d{2}/\d{4})", page)
    if fecha is None:
        raise ElPeruanoFormatError(f"La ficha {norma_id} no tiene 'Fecha de publicación': cambio el formato.")

    body = _embedded_body(page)
    if body is None:
        raise ElPeruanoFormatError(
            f"La ficha {norma_id} no trae el texto en #visor-html: cambio el formato del sitio."
        )
    text = _html_to_text(body)
    if len(text) < 80:
        raise ElPeruanoFormatError(f"La ficha {norma_id} trae un texto vacio o casi vacio.")

    pdf = re.search(r'(api/archivo/file/[^"\\\s]+\.pdf)', page, re.I)
    return Dispositivo(
        id=norma_id,
        url=dispositivo_url(norma_id),
        fecha_publicacion=fecha.group(1),
        texto=text,
        pdf_url=f"{BASE_URL}/{pdf.group(1)}" if pdf else None,
    )


def get_dispositivo(norma_id: str, fetch=_fetch) -> Dispositivo:
    if not re.fullmatch(r"[0-9]+-[0-9]+", norma_id):
        raise ValueError(f"id de norma invalido: {norma_id!r}")
    return parse_dispositivo(norma_id, fetch(dispositivo_url(norma_id)))
