from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import unquote, urlsplit

# Termos de release que não ajudam a identificar a obra.
_NOISE = {
    "baixar", "download", "torrent", "filme", "filmes", "serie", "series",
    "temporada", "completa", "completo", "dublado", "dublada", "legendado",
    "legendada", "dual", "audio", "pt", "br", "web", "dl", "webrip",
    "bluray", "blu", "ray", "remux", "hdr", "dv", "dolby", "vision",
    "x264", "x265", "h264", "h265", "hevc", "aac", "atmos", "imax",
}


def normalize_release_title(value: str) -> str:
    """Normaliza título/slug para comparação de relevância."""
    value = unquote(value or "")
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    tokens = re.findall(r"[a-z0-9]+", value.lower())

    cleaned: list[str] = []
    for token in tokens:
        if token in _NOISE:
            continue
        if re.fullmatch(r"(?:19|20)\d{2}", token):
            continue
        if re.fullmatch(r"\d{3,4}p", token):
            continue
        if re.fullmatch(r"s\d{1,2}(?:e\d{1,3})?", token):
            continue
        if re.fullmatch(r"e\d{1,3}", token):
            continue
        cleaned.append(token)

    return " ".join(cleaned)


# Numerais romanos usados como marcador de sequencia. "v" fica de fora de
# proposito: aparece como "versus" ("Batman v Superman") e como letra solta,
# entao incluir criaria falso negativo mais caro que o falso positivo que
# evitaria.
_NUMERAIS_ROMANOS = {"ii", "iii", "iv", "vi", "vii", "viii", "ix", "x"}

# Palavras que marcam continuacao logo apos o titulo base.
_MARCADORES_DE_SEQUENCIA = {
    "part", "parte", "chapter", "capitulo", "episode", "episodio",
    "volume", "vol",
}

_ANO = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def _caminho_da_url(url: str) -> str:
    """
    Extrai apenas o path da URL para a comparacao de relevancia.

    Antes a funcao concatenava a URL INTEIRA ao titulo e normalizava tudo
    junto, entao host e query string injetavam tokens:

        query="Up"       url="https://up.example/superman-returns/"
            -> "up" vinha do DOMINIO e o Superman era aceito

        query="Titanic"  url="https://site/avatar?busca=titanic"
            -> "titanic" vinha da QUERY STRING e o Avatar era aceito

    O slug do path e evidencia do titulo. Host e query string nao sao —
    ambos sao controlados por quem monta a URL, nao pelo conteudo.
    """
    if not url:
        return ""
    try:
        return urlsplit(url).path
    except ValueError:
        return ""


def _identidade_numerica(tokens: list[str]) -> set[str]:
    """
    Numeros que fazem parte do NOME da obra.

    Anos e resolucoes ja sairam em normalize_release_title, entao o que
    sobra de numerico identifica a sequencia: o "3" de "Toy Story 3", o
    "ii" de "Gladiator II".
    """
    return {t for t in tokens if t.isdigit() or t in _NUMERAIS_ROMANOS}


def _numero_da_obra_confere(query_norm: str, candidate_norm: str) -> bool:
    """
    Se a query nomeia uma sequencia, o candidato precisa ter o mesmo numero.

    "Toy Story 3" nao pode aceitar "Toy Story 4"; "Inside Out 2" nao pode
    aceitar "Inside Out". Query sem numero nao restringe nada aqui — esse
    caso e tratado por _e_continuacao_do_titulo.
    """
    numeros_query = _identidade_numerica(query_norm.split())
    if not numeros_query:
        return True
    return numeros_query <= _identidade_numerica(candidate_norm.split())


def _anos_explicitos(texto: str) -> set[str]:
    return set(_ANO.findall(unquote(texto or "")))


def _anos_divergem(query_bruta: str, candidato_bruto: str) -> bool:
    """
    True quando os dois lados declaram ano e nenhum coincide.

    Cobre remake: "The Thing 1982" nao e "The Thing 2011". Se so um dos
    lados tem ano, nao rejeita — "Titanic" deve continuar aceitando
    "Titanic 1997", que e o caso normal de release.
    """
    anos_query = _anos_explicitos(query_bruta)
    if not anos_query:
        return False
    anos_candidato = _anos_explicitos(candidato_bruto)
    if not anos_candidato:
        return False
    return anos_query.isdisjoint(anos_candidato)


def _e_continuacao_do_titulo(frase: str, candidate_norm: str) -> bool:
    """
    True quando o candidato e o titulo buscado MAIS um marcador de
    continuacao imediatamente depois.

        "Taken"     x "Taken 2"          -> digito
        "Gladiator" x "Gladiator II"     -> numeral romano
        "Dune"      x "Dune Part Two"    -> marcador

    Sem isso, buscar por um filme devolve a sequencia dele — que e um
    release diferente, com hash e audio diferentes.
    """
    tokens_frase = frase.split()
    tokens = candidate_norm.split()
    n = len(tokens_frase)
    for i in range(len(tokens) - n + 1):
        if tokens[i:i + n] != tokens_frase:
            continue
        seguinte = tokens[i + n] if i + n < len(tokens) else None
        if seguinte and (
            seguinte.isdigit()
            or seguinte in _NUMERAIS_ROMANOS
            or seguinte in _MARCADORES_DE_SEQUENCIA
        ):
            return True
    return False


def _contem_frase(agulha: str, palheiro: str) -> bool:
    """
    Contenção com fronteira de palavra, não de caractere.

    A versão anterior usava `agulha in palheiro` direto, o que compara
    caracteres: "up" está contido em "superman returns", "her" em
    "sherlock holmes", "cars" em "oscars", "room" em "mushroom". Como essa
    era a PRIMEIRA checagem de is_relevant_release, qualquer título curto
    aceitava candidatos sem nenhuma relação — e o filtro de relevância,
    que existe justamente para barrar falso positivo grosseiro, deixava
    passar o caso mais grosseiro de todos.

    Envolver os dois lados em espaços transforma a checagem em "esta
    sequência de palavras aparece inteira no candidato".
    """
    return f" {agulha} " in f" {palheiro} "


def is_relevant_release(query: str, candidate_title: str, candidate_url: str = "") -> bool:
    """
    Rejeita falsos positivos grosseiros sem exigir igualdade literal.

    A comparação fuzzy permite pequenas diferenças de tradução/grafia, como
    ``Interstellar`` x ``Interestelar``, mas rejeita resultados sem relação,
    como ``Troy`` x ``Zoey 102``.
    """
    candidato_bruto = f"{candidate_title} {_caminho_da_url(candidate_url)}"
    query_norm = normalize_release_title(query)
    candidate_norm = normalize_release_title(candidato_bruto)

    if not query_norm or not candidate_norm:
        return False

    # Portões de identidade da obra. Rodam ANTES de qualquer heurística de
    # similaridade porque nenhuma medida de distância textual salva um
    # candidato que é comprovadamente outro filme.
    if not _numero_da_obra_confere(query_norm, candidate_norm):
        return False
    if _anos_divergem(query, candidato_bruto):
        return False

    if _contem_frase(query_norm, candidate_norm):
        # O candidato contém o título buscado — mas pode ser a sequência dele.
        return not _e_continuacao_do_titulo(query_norm, candidate_norm)

    if _contem_frase(candidate_norm, query_norm):
        return True

    query_tokens = query_norm.split()
    candidate_tokens = candidate_norm.split()
    query_set = set(query_tokens)
    candidate_set = set(candidate_tokens)

    exact_coverage = len(query_set & candidate_set) / max(1, len(query_set))
    if exact_coverage >= 0.60:
        return True

    fuzzy_matches = 0
    for query_token in query_tokens:
        best = max(
            (
                SequenceMatcher(None, query_token, candidate_token).ratio()
                for candidate_token in candidate_tokens
                if len(candidate_token) >= 3
            ),
            default=0.0,
        )
        if best >= 0.88:
            fuzzy_matches += 1

    if fuzzy_matches / max(1, len(query_tokens)) >= 0.75:
        return True

    return SequenceMatcher(None, query_norm, candidate_norm).ratio() >= 0.72


# Padrões de season/episode em releases PT-BR e internacionais.
_EP_PATTERNS = [
    re.compile(r"s(\d{1,2})e(\d{1,3})", re.IGNORECASE),
    re.compile(r"(\d{1,2})x(\d{1,3})", re.IGNORECASE),
    re.compile(r"temporada\s*(\d{1,2}).{0,15}?epis[oó]dio\s*(\d{1,3})", re.IGNORECASE),
]

# Marca pacote de temporada completa (sem episódio específico).
_SEASON_PACK_PATTERNS = [
    re.compile(r"s(\d{1,2})(?!e\d)", re.IGNORECASE),
    re.compile(r"(\d{1,2})[ªa]?\s*temporada", re.IGNORECASE),
    re.compile(r"temporada\s*(\d{1,2})", re.IGNORECASE),
    re.compile(r"complet[ao]", re.IGNORECASE),
]


def matches_episode(candidate_title: str, season: int | None, episode: int | None) -> bool:
    """
    Verifica se um release de série bate com a temporada/episódio pedidos.

    Sem season/episode informado (filme, ou série sem essa info) -> sempre aceita.
    Com season/episode:
      - Se o título tem S{season}E{episode} (ou 1x05) explícito, exige o
        season E episode exatos.
      - Se o título só menciona a temporada (pacote completo, sem episódio),
        aceita — o pacote contém o episódio pedido.
      - Se o título menciona uma temporada diferente da pedida, rejeita.
      - Sem nenhuma marca de temporada/episódio identificável, aceita
        (não penaliza títulos que não seguem o padrão de nomenclatura).
    """
    if season is None or episode is None:
        return True

    texto = candidate_title or ""

    for pattern in _EP_PATTERNS:
        match = pattern.search(texto)
        if match:
            found_season, found_episode = int(match.group(1)), int(match.group(2))
            return found_season == season and found_episode == episode

    for pattern in _SEASON_PACK_PATTERNS:
        match = pattern.search(texto)
        if match and match.groups():
            found_season = int(match.group(1))
            return found_season == season
        if match:
            # "completo/completa" sem número — aceita, não dá pra confirmar a temporada
            return True

    # Nenhuma marca de temporada/episódio no título — não rejeita por isso.
    return True


def build_series_queries(query: str, season: int | None, episode: int | None) -> list[str]:
    """
    Monta variantes de busca para séries, em ordem de prioridade:
      1. "{título} S01E05" — pega releases do episódio específico.
      2. "{título}" (query original) — pega pacotes de temporada completa.

    Para filmes (season/episode None), retorna só a query original.
    """
    if season is None or episode is None:
        return [query]
    tag = f"S{season:02d}E{episode:02d}"
    return [f"{query} {tag}", query]
