"""
Dois bugs pré-existentes na normalização de títulos.

1. Filmes cujo título É um ano — 1917, 2012, 1984, 2046, 1922 — normalizavam
   para string vazia, porque o token casava o padrão de ano e era removido
   como ruído de release. `is_relevant_release` rejeita de imediato quando a
   query normaliza vazia, então esses filmes retornavam ZERO resultados em
   todas as fontes que usam o filtro. Falha total, silenciosa.

2. `4k` não estava em `_NOISE` e não casa o padrão `\d{3,4}p`, então
   sobrevivia à normalização e derrubava a similaridade: "The Batman"
   rejeitava "Batman 2022 4K Dublado" (ratio 0,632 < limiar 0,72). Perda
   silenciosa de resultado na faixa de qualidade mais procurada.
"""

import pytest

from app.scrapers.relevance import is_relevant_release, normalize_release_title

CORPUS = [
    ('1917', '1917 2019 1080p Dublado', '', True, 'titulo-ano'),
    ('2012', '2012 2009 1080p Dublado', '', True, 'titulo-ano'),
    ('1984', '1984 1984 1080p Legendado', '', True, 'titulo-ano'),
    ('2046', '2046 2004 1080p', '', True, 'titulo-ano'),
    ('1922', '1922 2017 1080p Nacional', '', True, 'titulo-ano'),
    ('2012', '2019 1080p Dublado', '', False, 'titulo-ano vizinho'),
    ('1917', '1918 1080p', '', False, 'titulo-ano vizinho'),
    ('2046', '2045 1080p', '', False, 'titulo-ano vizinho'),
    ('300', '3000 1080p', '', False, 'numero vizinho'),
    ('300', '300 2006 1080p Dublado', '', True, 'numero no titulo'),
    ('1408', '1408 2007 1080p Dublado', '', True, 'numero no titulo'),
    ('The Batman', 'Batman 2022 4K Dublado', '', True, 'ruido 4k'),
    ('The Godfather', 'Godfather 4K UHD', '', True, 'ruido 4k'),
    ('Interstellar', 'Interestelar 2014 4K HDR Dublado', '', True, 'ruido 4k'),
    ('Cidade de Deus', 'Cidade de Deus 2002 BDRip AC3 Nacional', '', True, 'ruido novo'),
    ('Baixar Torrent Dublado', 'Qualquer Filme 1080p', '', False, 'query so ruido'),
    ('Breaking Bad', 'Breaking Bad 2 Temporada Completa 1080p', '', True, 'temporada'),
    ('Taken', 'Taken 2 2012 1080p', '', False, 'sequencia'),
    ('Gladiator', 'Gladiator II 2024', '', False, 'sequencia'),
    ('Toy Story 3', 'Toy Story 4 2019', '', False, 'numero da obra'),
    ('The Thing 1982', 'The Thing 2011', '', False, 'ano divergente'),
    ('Interstellar', 'Interestelar 2014 Dublado', '', True, 'traducao'),
    ('Alien', 'Aliens 1986', '', True, 'morfologia'),
    ('Up', 'Up - Altas Aventuras 1080p', '', True, 'subtitulo'),
    ('Up', 'Superman Returns', 'https://up.example.invalid/superman-returns/', False, 'host'),
    ('Blade Runner', 'Download Torrent', 'https://s.invalid/blade-runner-1982-1080p/', True, 'slug'),
]


@pytest.mark.parametrize("query,titulo,url,esperado,grupo", CORPUS)
def test_corpus(query, titulo, url, esperado, grupo):
    assert is_relevant_release(query, titulo, url) is esperado, grupo


@pytest.mark.parametrize("titulo", ["1917", "2012", "1984", "2046", "1922", "1941"])
def test_titulo_que_e_um_ano_sobrevive_a_normalizacao(titulo):
    assert normalize_release_title(titulo) == titulo


@pytest.mark.parametrize(
    "texto",
    ["Baixar Torrent Dublado 1080p", "Torrent Dublado", "Download Filme Legendado"],
)
def test_texto_que_e_so_ruido_continua_vazio(texto):
    """
    O fallback recupera ANO, não ruído. Se não havia ano, não há o que
    recuperar — e uma string de puro ruído deve continuar vazia, senão a
    relevância passaria a comparar lixo com lixo.
    """
    assert normalize_release_title(texto) == ""


def test_ano_de_release_continua_sendo_removido():
    """O fallback só age quando NADA sobra. Com título presente, o ano sai."""
    assert normalize_release_title("Interstellar 2014 1080p Dublado") == "interstellar"
    assert normalize_release_title("Toy Story 3 2010 BluRay") == "toy story 3"


def test_ano_preservado_nao_e_lido_como_sequencia():
    """
    Com o fallback, "1917 2019 1080p" normaliza para "1917 2019" — e o
    token seguinte ao título é um ano de 4 dígitos, não número de sequência.
    _e_marcador_de_sequencia exige no máximo 2 dígitos por isso.
    """
    assert is_relevant_release("1917", "1917 2019 1080p Dublado") is True
    assert is_relevant_release("Taken", "Taken 2 2012 1080p") is False
