"""
Relevância: identidade da obra, não só semelhança textual.

A correção anterior (PR #15) matou a classe de substring de caractere —
"Up" aceitando "Superman". Sobreviveram três classes que nenhuma medida de
distância textual resolve, porque o candidato É textualmente parecido:

  sequências     "Taken" aceitava "Taken 2"
  remakes        "The Thing 1982" aceitava "The Thing 2011"
  URL            host e query string injetavam tokens — com a URL
                 https://up.example/superman-returns/, "Up" aceitava
                 Superman porque "up" vinha do DOMÍNIO
"""

import pytest

from app.scrapers.relevance import is_relevant_release

CORPUS = [
    ('Up', 'Superman Returns 1080p Dublado', '', False, 'substring (ja corrigido)'),
    ('Her', 'Sherlock Holmes 2009 1080p', '', False, 'substring (ja corrigido)'),
    ('Cars', 'Oscars 2020 Ceremony', '', False, 'substring (ja corrigido)'),
    ('Coco', 'Coconut Island Documentary', '', False, 'substring (ja corrigido)'),
    ('Room', 'The Mushroom Documentary', '', False, 'substring (ja corrigido)'),
    ('It', 'Little Women 2019 1080p', '', False, 'substring (ja corrigido)'),
    ('Us', 'House of Cards S01E01', '', False, 'substring (ja corrigido)'),
    ('Toy Story 3', 'Toy Story 4 2019 1080p Dublado', '', False, 'numero da obra'),
    ('Toy Story 3', 'Toy Story 3 2010 1080p Dublado', '', True, 'numero da obra'),
    ('Inside Out 2', 'Inside Out 2015 1080p', '', False, 'numero da obra'),
    ('Inside Out 2', 'Inside Out 2 2024 1080p Dublado', '', True, 'numero da obra'),
    ('John Wick Chapter 4', 'John Wick Chapter 3 2019', '', False, 'numero da obra'),
    ('Gladiator II', 'Gladiator 2000 1080p', '', False, 'romano na query'),
    ('Taken', 'Taken 2 2012 1080p', '', False, 'continuacao'),
    ('Taken', 'Taken 2008 1080p Dublado', '', True, 'continuacao'),
    ('Gladiator', 'Gladiator II 2024 1080p', '', False, 'continuacao romana'),
    ('Gladiator', 'Gladiator 2000 1080p Dublado', '', True, 'continuacao romana'),
    ('Dune', 'Dune Part Two 2024 1080p', '', False, "marcador 'part'"),
    ('Dune', 'Dune 2021 1080p Dublado', '', True, "marcador 'part'"),
    ('The Thing 1982', 'The Thing 2011 1080p', '', False, 'ano divergente'),
    ('The Thing 1982', 'The Thing 1982 1080p Dublado', '', True, 'ano divergente'),
    ('Suspiria 2018', 'Suspiria 1977 1080p', '', False, 'ano divergente'),
    ('Titanic', 'Titanic 1997 1080p Dublado', '', True, 'ano so no candidato'),
    ('Blade Runner 2049', 'Blade Runner 1982', '', False, '2049 e nome, nao ano'),
    ('Titanic', 'Avatar 2009 1080p', 'https://site.invalid/avatar?busca=titanic', False, 'query string'),
    ('Up', 'Superman Returns 1080p', 'https://up.example.invalid/superman-returns/', False, 'host'),
    ('Blade Runner', 'Download Torrent 1080p', 'https://s.invalid/blade-runner-1982-1080p/', True, 'slug do path'),
    ('Interestelar', 'Baixar Torrent', 'https://s.invalid/interestelar-2014-dublado/', True, 'slug do path'),
    ('Interstellar', 'Interestelar 2014 1080p Dublado', '', True, 'traducao fuzzy'),
    ('Alien', 'Aliens 1986 1080p', '', True, 'morfologia'),
    ('The Batman', 'Batman 2022 1080p', '', True, 'artigo inicial'),
    ('Batman', 'The Batman 2022 1080p', '', True, 'artigo inicial'),
    ('Up', 'Up - Altas Aventuras 1080p Dublado', '', True, 'titulo curto legitimo'),
    ('It', 'It - A Coisa 2017 1080p Dublado', '', True, 'titulo curto legitimo'),
    ('Us', 'Us - Nos 2019 1080p Dublado', '', True, 'titulo curto legitimo'),
    ('Coco', 'Coco - Viva A Vida e uma Festa 1080p', '', True, 'titulo curto legitimo'),
    ('Rogue One', 'Rogue One Uma Historia Star Wars 1080p', '', True, 'subtitulo expandido'),
    ('Moana', 'Moana Um Mar de Aventuras 1080p Dublado', '', True, 'subtitulo expandido'),
    ('O Auto da Compadecida', 'O Auto da Compadecida 2000 Nacional', '', True, 'PT-BR'),
    ('Tropa de Elite', 'Tropa de Elite 2 O Inimigo Agora e Outro', '', False, 'sequencia PT-BR'),
    ('Tropa de Elite', 'Tropa de Elite 2007 1080p Nacional', '', True, 'sequencia PT-BR'),
    ('Cidade de Deus', 'Cidade de Deus 2002 1080p Nacional', '', True, 'PT-BR'),
    ('Troy', 'Zoey 102 1080p', '', False, 'sem relacao'),
    ('Interstellar', 'Zoey 102 1080p', '', False, 'sem relacao'),
    ('Old', 'Oldboy 2003 1080p', '', False, 'token colado'),
]


@pytest.mark.parametrize("query,titulo,url,esperado,grupo", CORPUS)
def test_corpus_de_relevancia(query, titulo, url, esperado, grupo):
    assert is_relevant_release(query, titulo, url) is esperado, grupo


@pytest.mark.parametrize(
    "query,titulo,url",
    [
        ("Titanic", "Avatar 2009 1080p", "https://site.invalid/avatar?busca=titanic"),
        ("Up", "Superman Returns 1080p", "https://up.example.invalid/superman-returns/"),
    ],
)
def test_apenas_o_path_da_url_conta(query, titulo, url):
    """
    Host e query string são controlados por quem monta a URL, não pelo
    conteúdo. Só o slug do path é evidência do título.
    """
    from app.scrapers.relevance import _caminho_da_url

    assert query.lower() in url.lower(), "o caso só faz sentido se a query estiver na URL"
    assert query.lower() not in _caminho_da_url(url).lower()
    assert is_relevant_release(query, titulo, url) is False


def test_slug_do_path_continua_valendo_como_evidencia():
    """Sem o path, um post cujo <h1> é genérico ficaria sem identificação."""
    assert is_relevant_release(
        "Blade Runner", "Download Torrent 1080p",
        "https://s.invalid/blade-runner-1982-1080p/",
    ) is True


@pytest.mark.parametrize(
    "query,candidato",
    [("Taken", "Taken 2 2012"), ("Gladiator", "Gladiator II 2024"), ("Dune", "Dune Part Two 2024")],
)
def test_sequencia_nao_atende_busca_pelo_original(query, candidato):
    assert is_relevant_release(query, candidato) is False


def test_ano_apenas_no_candidato_nao_rejeita():
    """O caso normal de release: a busca não traz ano, o título do post traz."""
    assert is_relevant_release("Titanic", "Titanic 1997 1080p Dublado") is True
