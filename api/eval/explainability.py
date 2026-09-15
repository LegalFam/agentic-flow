import difflib
import re
import unicodedata

from eval.corpus_articles import deaccent, normalize_article


_VOWELS = "aeiouáéíóúüïAEIOU"
_STRONG = set("aeoáéó")
_ACCENTED_WEAK = set("íú")


def _syllables(word: str) -> int:
    normalized = unicodedata.normalize("NFC", word.lower())
    letters = [char for char in normalized if char.isalpha()]
    if not letters:
        return 0

    count = 0
    previous_vowel = ""
    for char in letters:
        is_vowel = char in _VOWELS
        if not is_vowel:
            previous_vowel = ""
            continue
        if not previous_vowel:
            count += 1
        elif (previous_vowel in _STRONG and char in _STRONG) or (
            previous_vowel in _ACCENTED_WEAK or char in _ACCENTED_WEAK
        ):
            count += 1
        previous_vowel = char

    return max(1, count)


_SENTENCE_END = re.compile(r"[.!?;:\n]+")
_WORD = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+")

MIN_WORDS_FOR_READABILITY = 15


def readability(text: str) -> dict | None:
    plain = re.sub(r"[*_#`>\[\]()]", " ", text or "")
    words = _WORD.findall(plain)
    if len(words) < MIN_WORDS_FOR_READABILITY:
        return None

    sentences = [piece for piece in _SENTENCE_END.split(plain) if _WORD.search(piece)]
    sentence_count = max(1, len(sentences))
    syllables = sum(_syllables(word) for word in words)

    syllables_per_word = syllables / len(words)
    words_per_sentence = len(words) / sentence_count

    fernandez_huerta = 206.84 - 0.60 * (syllables * 100 / len(words)) - 1.02 * words_per_sentence
    szigriszt = 206.835 - 62.3 * syllables_per_word - words_per_sentence

    return {
        "fernandez_huerta": round(max(0.0, min(100.0, fernandez_huerta)), 1),
        "szigriszt": round(max(0.0, min(100.0, szigriszt)), 1),
        "words": len(words),
        "words_per_sentence": round(words_per_sentence, 1),
        "syllables_per_word": round(syllables_per_word, 2),
    }


def answer_vs_sources(answer: str, citations: list[dict]) -> float | None:
    target = readability(answer)
    if not target:
        return None
    fuentes = [
        r["szigriszt"]
        for r in (readability(c.get("original_snippet") or "") for c in citations or [])
        if r
    ]
    if not fuentes:
        return None
    return round(target["szigriszt"] - sum(fuentes) / len(fuentes), 1)


def readability_gap(citations: list[dict]) -> dict | None:
    pairs = []
    for citation in citations:
        original = readability(citation.get("original_snippet") or "")
        summary = readability(citation.get("summary_snippet") or "")
        if original and summary:
            pairs.append((original["szigriszt"], summary["szigriszt"]))

    if not pairs:
        return None

    return {
        "pairs": len(pairs),
        "original": round(sum(original for original, _ in pairs) / len(pairs), 1),
        "summary": round(sum(summary for _, summary in pairs) / len(pairs), 1),
        "gap": round(sum(summary - original for original, summary in pairs) / len(pairs), 1),
    }



_DEONTIC = (
    "debe", "deben", "debera", "deberan", "deberia",
    "puede", "pueden", "podra", "podran",
    "corresponde", "correspondera",
    "tiene derecho", "tienen derecho",
    "esta obligado", "estan obligados", "obligacion",
    "se fija", "se regula", "se determina", "se presume",
    "se extingue", "se suspende", "procede", "requiere", "exige",
    "el plazo", "es competente", "tiene competencia",
)

_LEGAL_TERMS = (
    "alimento", "pension", "tenencia", "custodia", "visita", "patria potestad",
    "juez", "juzgado", "demanda", "proceso", "articulo", "ley", "codigo",
    "medida de proteccion", "conciliacion", "divorcio", "separacion", "filiacion",
    "paternidad", "matrimonio", "union de hecho", "ganancial", "curatela", "tutela",
    "denuncia", "sentencia", "menor", "conyuge", "heredero", "bien propio", "bien social",
)

SUPPORT_OVERLAP = 0.30

_STOPWORDS = frozenset(
    """de la que el en y a los del se las por un para con no una su al lo como mas pero
    sus le ya o este si porque esta entre cuando muy sin sobre tambien me hasta hay donde
    quien desde todo nos durante todos uno les ni contra otros ese eso ante ellos e esto
    mi antes algunos que unos yo otro otras otra tanto esa estos mucho quienes nada muchos
    cual poco ella estar estas algunas algo nosotros su ser son fue han sido puede debe
    """.split()
)


def normative_claims(message: str) -> list[str]:
    plain = re.sub(r"[*_#`>]", "", message or "")
    claims = []
    for piece in re.split(r"(?<=[.!?])\s+|\n+", plain):
        sentence = piece.strip(" -•\t")
        if len(_WORD.findall(sentence)) < 6:
            continue
        folded = deaccent(sentence)
        if any(marker in folded for marker in _DEONTIC) and any(
            term in folded for term in _LEGAL_TERMS
        ):
            claims.append(sentence)
    return claims


def _content_words(text: str) -> set[str]:
    return {
        word
        for word in deaccent(text).split()
        if len(word) >= 5 and word not in _STOPWORDS and word.isalpha()
    }


def support_density(message: str, citations: list[dict], article_texts: list[str]) -> dict:
    claims = normative_claims(message)
    if not claims:
        return {"claims": 0, "supported": 0, "density": None}

    evidence = [_content_words(text) for text in article_texts if text]
    supported = 0
    for claim in claims:
        words = _content_words(claim)
        if not words:
            continue
        if any(len(words & source) / len(words) >= SUPPORT_OVERLAP for source in evidence):
            supported += 1

    return {
        "claims": len(claims),
        "supported": supported,
        "density": supported / len(claims),
        "citations": len(citations),
    }



_ACTION_VERBS = (
    "reune", "reunir", "ordena", "ordenar", "solicita", "solicitar", "presenta", "presentar",
    "acude", "acudir", "conserva", "conservar", "prepara", "preparar", "identifica",
    "identificar", "intenta", "intentar", "consulta", "consultar", "guarda", "guardar",
    "registra", "registrar", "obten", "obtener", "verifica", "verificar", "lleva", "llevar",
    "anota", "anotar", "recopila", "recopilar", "revisa", "revisar", "inicia", "iniciar",
    "documenta", "documentar", "calcula", "calcular", "confirma", "confirmar", "busca",
    "buscar", "define", "definir", "evalua", "evaluar", "tramita", "tramitar",
)

_INSTITUTIONS = ("pnp", "cem", "demuna", "comisaria", "policia")


def actionability(next_steps: list[str], clarifying: list[str], expects_referral: bool) -> dict:
    steps = [str(step or "").strip() for step in next_steps or []]
    steps = [step for step in steps if step]
    if not steps:
        return {"steps": 0, "actionable": 0, "actionable_rate": None, "duplicated": 0, "generic_referral": 0}

    questions = [_content_words(str(item or "")) for item in clarifying or []]

    actionable = 0
    duplicated = 0
    referrals = 0
    for step in steps:
        folded = deaccent(step).lstrip("-•* ")
        first = folded.split()[0] if folded.split() else ""
        if first in _ACTION_VERBS or first.endswith(("ar", "er", "ir")):
            actionable += 1

        words = _content_words(step)
        if words and any(
            question and len(words & question) / len(words) >= 0.6 for question in questions
        ):
            duplicated += 1

        if not expects_referral and any(name in folded for name in _INSTITUTIONS):
            referrals += 1

    return {
        "steps": len(steps),
        "actionable": actionable,
        "actionable_rate": actionable / len(steps),
        "duplicated": duplicated,
        "generic_referral": referrals,
    }



ANSWER_STABLE = 0.80
CITATIONS_UNSTABLE = 0.50


def _citation_keys(citations: list[dict]) -> set[str]:
    keys = set()
    for citation in citations or []:
        document = deaccent(str(citation.get("file_name") or citation.get("file_url") or ""))
        locator = normalize_article(str(citation.get("locator") or ""))
        if document or locator:
            keys.add(f"{document}|{locator}")
    return keys


def _jaccard(left: set, right: set) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def stability(responses: list[dict]) -> dict | None:
    if len(responses) < 2:
        return None

    answers = [str(item.get("message") or "") for item in responses]
    citations = [_citation_keys(item.get("citations") or []) for item in responses]

    answer_scores = []
    citation_scores = []
    for left in range(len(responses)):
        for right in range(left + 1, len(responses)):
            answer_scores.append(
                difflib.SequenceMatcher(None, answers[left], answers[right]).ratio()
            )
            citation_scores.append(_jaccard(citations[left], citations[right]))

    answer_similarity = sum(answer_scores) / len(answer_scores)
    citation_similarity = sum(citation_scores) / len(citation_scores)
    has_citations = any(citations)

    return {
        "repeats": len(responses),
        "answer_similarity": round(answer_similarity, 3),
        "citation_similarity": round(citation_similarity, 3),
        "decorative": bool(
            has_citations
            and answer_similarity >= ANSWER_STABLE
            and citation_similarity <= CITATIONS_UNSTABLE
        ),
    }
