"""
`v` como marcador de sequência — correção de um raciocínio invertido.

A PR #18 deixou `"v"` de fora dos numerais romanos com esta justificativa:
"aparece como versus em Batman v Superman, incluir criaria falso negativo".

O raciocínio estava invertido. A regra só dispara quando o título BUSCADO é
seguido do marcador — e buscar `Batman` para receber `Batman v Superman` é
exatamente o falso positivo que a regra existe para matar, igual a `Rocky`
não dever devolver `Rocky V`. Nos dois casos são obras diferentes.

Buscar pela própria obra (`Batman v Superman`, `Rocky V`) continua
funcionando, porque aí o marcador faz parte da query e não do sufixo.

Corpus completo de regressão junto, cobrindo tudo que as PRs #15, #18 e #20
corrigiram — nenhum deles pode quebrar por causa de uma letra.
"""

import pytest

from app.scrapers.relevance import is_relevant_release

CORPUS = [
    ('Batman', 'Batman v Superman A Origem da Justica 2016', '', False, "'v' como versus"),
    ('Rocky', 'Rocky V 1990 1080p', '', False, "'v' como numeral"),
    ('Alien', 'Alien vs Predator 2004', '', False, "'vs' por extenso"),
    ('Alien vs Predator', 'Alien vs Predator 2004 1080p', '', True, 'busca pelo crossover'),
    ('Kramer vs Kramer', 'Kramer vs Kramer 1979 Legendado', '', True, 'vs no titulo canonico'),
    ('Freddy vs Jason', 'Freddy vs Jason 2003 Dublado', '', True, 'busca pelo crossover'),
    ('Batman v Superman', 'Batman v Superman 2016 1080p Dublado', '', True, 'busca pela propria obra'),
    ('Rocky V', 'Rocky V 1990 1080p', '', True, 'busca pela propria sequencia'),
    ('Rocky', 'Rocky 1976 1080p Dublado', '', True, 'obra original'),
    ('Batman', 'Batman 1989 1080p Dublado', '', True, 'obra original'),
    ('The Batman', 'Batman 2022 1080p', '', True, 'artigo inicial'),
    ('The Batman', 'Batman 2022 4K Dublado', '', True, 'ruido 4k'),
    ('Breaking Bad', 'Breaking Bad 2 Temporada Completa 1080p', '', True, 'temporada'),
    ('Dark', 'Dark 2 Temporada Completa Dublado', '', True, 'temporada'),
    ('The Boys', 'The Boys 4 Temporada 1080p', '', True, 'temporada'),
    ('La Casa de Papel', 'La Casa de Papel 3 Temporada Completa', '', True, 'temporada'),
    ('Stranger Things', 'Stranger Things 1 Temporada Completa', '', True, 'temporada'),
    ('The Office', 'The Office 9 Temporada Completa Legendado', '', True, 'temporada'),
    ('1917', '1917 2019 1080p Dublado', '', True, 'titulo-ano'),
    ('2012', '2012 2009 1080p Dublado', '', True, 'titulo-ano'),
    ('1984', '1984 1984 1080p Legendado', '', True, 'titulo-ano'),
    ('2046', '2046 2004 1080p', '', True, 'titulo-ano'),
    ('2012', '2019 1080p Dublado', '', False, 'titulo-ano vizinho'),
    ('Taken', 'Taken 2 2012 1080p', '', False, 'sequencia'),
    ('Taken', 'Taken 2008 1080p Dublado', '', True, 'obra original'),
    ('Gladiator', 'Gladiator II 2024', '', False, 'romano'),
    ('Gladiator II', 'Gladiator II 2024 1080p', '', True, 'busca pela sequencia'),
    ('Dune', 'Dune Part Two 2024', '', False, 'marcador part'),
    ('Dune', 'Dune 2021 1080p Dublado', '', True, 'obra original'),
    ('Toy Story 3', 'Toy Story 4 2019', '', False, 'numero da obra'),
    ('Toy Story 3', 'Toy Story 3 2010 Dublado', '', True, 'numero certo'),
    ('The Thing 1982', 'The Thing 2011', '', False, 'ano divergente'),
    ('Titanic', 'Titanic 1997 1080p Dublado', '', True, 'ano so no candidato'),
    ('Up', 'Superman Returns', 'https://up.example.invalid/superman-returns/', False, 'host'),
    ('Titanic', 'Avatar 2009', 'https://s.invalid/avatar?busca=titanic', False, 'query string'),
    ('Blade Runner', 'Download Torrent', 'https://s.invalid/blade-runner-1982-1080p/', True, 'slug'),
    ('Interstellar', 'Interestelar 2014 Dublado', '', True, 'traducao'),
    ('Alien', 'Aliens 1986', '', True, 'morfologia'),
    ('Up', 'Up - Altas Aventuras 1080p', '', True, 'subtitulo'),
    ('It', 'It - A Coisa 2017 Dublado', '', True, 'subtitulo'),
    ('Coco', 'Coco - Viva A Vida e uma Festa', '', True, 'subtitulo'),
    ('O Auto da Compadecida', 'O Auto da Compadecida 2000 Nacional', '', True, 'PT-BR'),
    ('Tropa de Elite', 'Tropa de Elite 2 - O Inimigo Agora e Outro', '', False, 'sequencia PT-BR'),
    ('Cidade de Deus', 'Cidade de Deus 2002 BDRip AC3 Nacional', '', True, 'PT-BR'),
    ('Troy', 'Zoey 102 1080p', '', False, 'sem relacao'),
]


@pytest.mark.parametrize("query,titulo,url,esperado,grupo", CORPUS)
def test_corpus(query, titulo, url, esperado, grupo):
    assert is_relevant_release(query, titulo, url) is esperado, grupo


@pytest.mark.parametrize(
    "query,candidato",
    [
        ("Batman", "Batman v Superman A Origem da Justica 2016"),
        ("Rocky", "Rocky V 1990 1080p"),
        ("Alien", "Alien vs Predator 2004"),
    ],
)
def test_v_marca_outra_obra(query, candidato):
    assert is_relevant_release(query, candidato) is False


@pytest.mark.parametrize(
    "query,candidato",
    [
        ("Batman v Superman", "Batman v Superman 2016 1080p Dublado"),
        ("Rocky V", "Rocky V 1990 1080p"),
    ],
)
def test_buscar_a_propria_obra_com_v_continua_funcionando(query, candidato):
    """O marcador na query não é sufixo — a regra não deve disparar."""
    assert is_relevant_release(query, candidato) is True
