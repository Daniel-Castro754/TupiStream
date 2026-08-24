"""
Regressão: a checagem de relevância aceitava qualquer candidato para
títulos curtos.

`is_relevant_release` começava com `query_norm in candidate_norm`, que é
contenção de CARACTERES. Consequência medida antes da correção: 7 falsos
positivos em 16 casos, incluindo "Up" aceitando "Superman", "Her"
aceitando "Sherlock Holmes" e "Cars" aceitando "Oscars".
"""

import pytest

from app.scrapers.relevance import is_relevant_release

CASOS = [
    ('Up', 'Superman Returns 1080p Dublado', False),
    ('Up', 'Up - Altas Aventuras 1080p Dublado', True),
    ('It', 'Little Women 2019 1080p', False),
    ('It', 'It - A Coisa 2017 1080p Dublado', True),
    ('Us', 'House of Cards S01E01', False),
    ('Us', 'Us - Nos 2019 1080p Dublado', True),
    ('Her', 'Sherlock Holmes 2009 1080p', False),
    ('Her', 'Her 2013 1080p Legendado', True),
    ('Coco', 'Coconut Island Documentary', False),
    ('Coco', 'Coco - Viva A Vida e uma Festa 1080p Dublado', True),
    ('Cars', 'Oscars 2020 Ceremony', False),
    ('Cars', 'Cars - Carros 2006 1080p Dublado', True),
    ('Room', 'The Mushroom Documentary', False),
    ('Room', 'Room - O Quarto de Jack 2015 1080p', True),
    ('Ray', 'Rayman Legends', False),
    ('Alien', 'Aliens 1986 1080p', True),
    ('Troy', 'Zoey 102 1080p', False),
    ('Interstellar', 'Interestelar 2014 1080p Dublado', True),
    ('Interstellar', 'Zoey 102 1080p', False),
    ('Titanic', 'Titanic 1997 1080p Dublado', True),
    ('O Auto da Compadecida', 'O Auto da Compadecida 2000 Nacional', True),
    ('Tropa de Elite', 'Tropa de Elite 2 - O Inimigo Agora e Outro', True),
]


@pytest.mark.parametrize("query,candidato,esperado", CASOS)
def test_relevancia(query, candidato, esperado):
    assert is_relevant_release(query, candidato) is esperado


@pytest.mark.parametrize(
    "query,candidato",
    [
        ("Up", "Superman Returns 1080p"),
        ("Her", "Sherlock Holmes 2009"),
        ("Cars", "Oscars 2020 Ceremony"),
        ("Coco", "Coconut Island Documentary"),
        ("Room", "The Mushroom Documentary"),
        ("It", "Little Women 2019"),
        ("Us", "House of Cards S01E01"),
    ],
)
def test_substring_de_caractere_nao_basta(query, candidato):
    """
    O nome de cada um destes contém a query como substring de caracteres,
    mas não como palavra. Antes da correção, todos eram aceitos pelo
    atalho `query_norm in candidate_norm`.
    """
    from app.scrapers.relevance import normalize_release_title

    assert normalize_release_title(query) in normalize_release_title(candidato)
    assert is_relevant_release(query, candidato) is False
