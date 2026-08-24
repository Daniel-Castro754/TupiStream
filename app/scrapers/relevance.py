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


# Numerais romanos usados em sequencias de filme.
_NUMERAIS_ROMANOS = frozenset({"ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"})

# Palavras que introduzem uma parte especifica de uma obra.
_MARCADORES_DE_SEQUENCIA = frozenset({
    "part", "parte", "chapter", "capitulo", "episode", "episodio", "vol", "volume",
})

_ANO = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def _caminho_da_url(url: str) -> str:
    """
    Só o path da URL entra na comparação.

    A versão anterior concatenava a URL inteira ao título antes de
    normalizar, então host e query string injetavam tokens no candidato:

      - `https://up.example/superman-returns/` fazia a busca por "Up"
        aceitar "Superman Returns", porque o host virava o token "up";
      - `.../avatar?busca=titanic` fazia "Titanic" aceitar "Avatar".

    Um site cujo domínio contenha a palavra buscada casaria com tudo. O
    path é a única parte da URL que descreve a obra — é onde vive o slug,
    que é justamente o fallback que essa comparação quer aproveitar.
    """
    if not url:
        return ""
    return urlsplit(url).path


def _anos(texto: str) -> set[str]:
    """Anos de 4 dígitos no texto cru (a normalização os remove)."""
    return set(_ANO.findall(unquote(texto or "")))


def _numeros_da_obra(tokens: list[str]) -> set[str]:
    """
    Números que identificam a obra dentro do título já normalizado.

    Anos e resoluções (1080p) foram removidos por normalize_release_title,
    então o que sobra é numeração de sequência: o "3" de "Toy Story 3".
    """
    return {token for token in tokens if token.isdigit()}


def _posicao_apos_frase(agulha: list[str], palheiro: list[str]) -> int | None:
    """Índice logo após a ocorrência de `agulha` em `palheiro`, ou None."""
    tamanho = len(agulha)
    for inicio in range(len(palheiro) - tamanho + 1):
        if palheiro[inicio:inicio + tamanho] == agulha:
            return inicio + tamanho
    return None


def _proximo_token_marca_outra_parte(sufixo: list[str]) -> bool:
    """
    True quando o token LOGO APÓS o título buscado marca outra parte da obra.

    "Taken" está inteiro dentro de "Taken 2", e "Gladiator" dentro de
    "Gladiator II" — contenção por palavra, sozinha, aceita os dois. Um
    número, numeral romano ou "Part/Parte/Chapter" imediatamente depois do
    título indicam outro filme, não uma variação do mesmo.

    Olha só o token seguinte, de propósito. Varrer o sufixo inteiro
    rejeitaria "Inside Out 2 - Divertida Mente 2", onde o número reaparece
    no título localizado. E subtítulo comum nunca dispara: "Up - Altas
    Aventuras" e "It - A Coisa" seguem passando, porque o que vem depois é
    texto, não numeração.
    """
    if not sufixo:
        return False
    seguinte = sufixo[0]
    return (
        seguinte.isdigit()
        or seguinte in _NUMERAIS_ROMANOS
        or seguinte in _MARCADORES_DE_SEQUENCIA
    )


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
    caminho = _caminho_da_url(candidate_url)
    candidate_bruto = f"{candidate_title} {caminho}"

    query_norm = normalize_release_title(query)
    candidate_norm = normalize_release_title(candidate_bruto)

    if not query_norm or not candidate_norm:
        return False

    query_tokens = query_norm.split()
    candidate_tokens = candidate_norm.split()

    # Ano explícito dos dois lados e sem interseção: remake ou obra
    # diferente. "The Thing 1982" não é "The Thing 2011". Ano só de um dos
    # lados não decide nada — "Titanic" deve continuar aceitando
    # "Titanic 1997".
    anos_query = _anos(query)
    anos_candidato = _anos(candidate_bruto)
    if anos_query and anos_candidato and anos_query.isdisjoint(anos_candidato):
        return False

    # Numeração da obra pedida precisa estar presente no candidato:
    # "Toy Story 3" não é "Toy Story 4" nem "Toy Story".
    numeros_query = _numeros_da_obra(query_tokens)
    if numeros_query and not numeros_query.issubset(_numeros_da_obra(candidate_tokens)):
        return False

    fim_da_frase = _posicao_apos_frase(query_tokens, candidate_tokens)
    if fim_da_frase is not None:
        # O título buscado aparece inteiro. Só aceita se o que vem depois
        # não for marcação de outra parte da obra.
        return not _proximo_token_marca_outra_parte(candidate_tokens[fim_da_frase:])

    if _contem_frase(candidate_norm, query_norm):
        return True

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
