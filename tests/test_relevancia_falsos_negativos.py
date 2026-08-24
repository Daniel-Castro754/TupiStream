"""
Falsos negativos: o filtro rejeitando resultado legítimo.

Três causas distintas, todas verificadas contra a implementação real:

1. NOTAÇÃO DE CANAL DE ÁUDIO — regressão que eu introduzi na #18.
   "5.1" tokenizava em dois números soltos, e o primeiro ficava encostado no
   título: "Taken 2008 BluRay 5.1 Dublado" -> "taken 5 1". A regra de
   sequência lia o "5" como número de continuação e rejeitava o release do
   próprio filme buscado. Notação de canal é onipresente em release PT-BR.

2. MARCADORES QUE APARECEM EM TÍTULO CANÔNICO — também da #18.
   `volume`, `vol`, `episode`, `episodio` e `capitulo` estavam na lista de
   marcadores de sequência, mas fazem parte de títulos reais e de releases
   de episódio: "Kill Bill Volume 1", "Star Wars Episódio IV",
   "Dark Episódio 5".

3. O FILME "RAY" — pré-existente, mesma classe do apagão de título-ano.
   "ray" estava em `_NOISE` para limpar "Blu-Ray", então o filme Ray (2004)
   normalizava para string vazia e era rejeitado sempre.
"""

import pytest

from app.scrapers.relevance import is_relevant_release, normalize_release_title

CORPUS = [
    ('Taken', 'Taken 2008 BluRay 5.1 Dublado', '', True, 'canal 5.1'),
    ('Up', 'Up 2009 BluRay 7.1', '', True, 'canal 7.1'),
    ('Room', 'Room 2015 AAC 2.0', '', True, 'canal 2.0'),
    ('Coco', 'Coco 2017 DTS 5.1 Dublado', '', True, 'canal 5.1'),
    ('Interstellar', 'Interstellar 2014 1080p 5.1', '', True, 'canal 5.1'),
    ('Dune', 'Dune 2021 Atmos 7.1 Dublado', '', True, 'canal 7.1'),
    ('Titanic', 'Titanic 1997 BluRay DDP5.1 Dublado', '', True, 'canal colado em DDP'),
    ('Alien', 'Alien 1979 1080p AC3 6.1', '', True, 'canal 6.1'),
    ('Kill Bill', 'Kill Bill Volume 1 2003 1080p', '', True, 'volume canonico'),
    ('Kill Bill Vol 1', 'Kill Bill Volume 1 2003 1080p', '', True, 'volume na query'),
    ('Star Wars', 'Star Wars Episodio IV Uma Nova Esperanca', '', True, 'episodio canonico'),
    ('Dark', 'Dark Episodio 5 1080p Dublado', '', True, 'episodio avulso PT-BR'),
    ('Dark', 'Dark Capitulo 5 Dublado', '', True, 'capitulo PT-BR'),
    ('Ray', 'Ray 2004 1080p Dublado', '', True, 'titulo em _NOISE'),
    ('Ray', 'Ray 2004 BluRay 5.1', '', True, 'Ray + blu-ray + canal'),
    ('Dune', 'Dune Part Two 2024', '', False, 'part continua marcador'),
    ('John Wick', 'John Wick Chapter 4 2023', '', False, 'chapter continua marcador'),
    ('Harry Potter e as Reliquias da Morte', 'Harry Potter e as Reliquias da Morte Parte 1', '', False, 'parte continua marcador'),
    ('Taken', 'Taken 2 2012 1080p', '', False, 'sequencia'),
    ('Gladiator', 'Gladiator II 2024', '', False, 'romano'),
    ('Batman', 'Batman v Superman A Origem da Justica 2016', '', False, "'v' (da #21)"),
    ('Rocky', 'Rocky V 1990', '', False, "'v' (da #21)"),
    ('Alien', 'Alien vs Predator 2004', '', False, "'vs' (da #21)"),
    ('Toy Story 3', 'Toy Story 4 2019', '', False, 'numero da obra'),
    ('The Thing 1982', 'The Thing 2011', '', False, 'ano divergente'),
    ('Up', 'Superman Returns', 'https://up.example.invalid/superman-returns/', False, 'host'),
    ('Titanic', 'Avatar 2009', 'https://s.invalid/avatar?busca=titanic', False, 'query string'),
    ('Breaking Bad', 'Breaking Bad 2 Temporada Completa 1080p', '', True, 'temporada'),
    ('The Boys', 'The Boys 4 Temporada 1080p', '', True, 'temporada'),
    ('1917', '1917 2019 1080p Dublado', '', True, 'titulo-ano'),
    ('2012', '2012 2009 1080p Dublado', '', True, 'titulo-ano'),
    ('The Batman', 'Batman 2022 4K Dublado', '', True, 'ruido 4k'),
    ('Interstellar', 'Interestelar 2014 Dublado', '', True, 'traducao'),
    ('Alien', 'Aliens 1986', '', True, 'morfologia'),
    ('Up', 'Up - Altas Aventuras 1080p', '', True, 'subtitulo'),
    ('It', 'It - A Coisa 2017 Dublado', '', True, 'subtitulo'),
    ('O Auto da Compadecida', 'O Auto da Compadecida 2000 Nacional', '', True, 'PT-BR'),
    ('Tropa de Elite', 'Tropa de Elite 2 - O Inimigo Agora e Outro', '', False, 'sequencia PT-BR'),
    ('Blade Runner', 'Download Torrent', 'https://s.invalid/blade-runner-1982-1080p/', True, 'slug'),
    ('Troy', 'Zoey 102 1080p', '', False, 'sem relacao'),
    ('Baixar Torrent Dublado', 'Qualquer Filme 1080p', '', False, 'query so ruido'),
]


@pytest.mark.parametrize("query,titulo,url,esperado,grupo", CORPUS)
def test_corpus(query, titulo, url, esperado, grupo):
    assert is_relevant_release(query, titulo, url) is esperado, grupo


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("Taken 2008 BluRay 5.1 Dublado", "taken"),
        ("Up 2009 BluRay 7.1", "up"),
        ("Room 2015 AAC 2.0", "room"),
        ("Titanic 1997 BluRay DDP5.1 Dublado", "titanic"),
        ("Alien 1979 1080p AC3 6.1", "alien"),
    ],
)
def test_canal_de_audio_sai_na_normalizacao(texto, esperado):
    """O canal precisa sair ANTES da tokenizacao, senao vira numero solto."""
    assert normalize_release_title(texto) == esperado


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("Ray", "ray"),
        ("Ray 2004 1080p Dublado", "ray"),
        ("Ray 2004 BluRay 5.1", "ray"),
        # "ray" continua sendo ruido quando vem de Blu-Ray
        ("Filme Blu-Ray 1080p Dublado", ""),
        ("Baixar Torrent Blu Ray Dublado", ""),
    ],
)
def test_ray_e_ruido_apenas_dentro_de_blu_ray(texto, esperado):
    assert normalize_release_title(texto) == esperado


@pytest.mark.parametrize(
    "query,candidato",
    [
        ("Dune", "Dune Part Two 2024"),
        ("John Wick", "John Wick Chapter 4 2023"),
        ("Harry Potter e as Reliquias da Morte", "Harry Potter e as Reliquias da Morte Parte 1"),
    ],
)
def test_part_parte_chapter_continuam_marcando_outra_obra(query, candidato):
    """
    Encurtar a lista de marcadores nao pode reabrir a classe de falso
    positivo que a #18 fechou.
    """
    assert is_relevant_release(query, candidato) is False
