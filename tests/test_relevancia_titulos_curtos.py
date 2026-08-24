"""
Corpus de relevância: o que `is_relevant_release` deve aceitar e rejeitar.

Histórico das duas correções que este arquivo trava:

1. Substring de caractere (corrigido antes)
   `query_norm in candidate_norm` aceitava "Up" para "Superman Returns",
   porque "up" está literalmente dentro de "superman returns". Sete falsos
   positivos medidos em dezesseis casos.

2. Sequências, remakes e URL (esta rodada)
   A contenção por palavra resolveu o item 1 mas deixou passar outra
   classe inteira: "Taken" aceitava "Taken 2", "Toy Story 3" aceitava
   "Toy Story 4", "The Thing 1982" aceitava "The Thing 2011". E, pior, a
   URL era concatenada inteira ao título antes de normalizar — host e
   query string injetavam tokens, então `https://up.example/...` fazia
   "Up" aceitar qualquer coisa e `?busca=titanic` fazia "Titanic" aceitar
   "Avatar".

Cada caso abaixo é um par real: título de obra existente contra release
plausível. Ao encontrar um novo falso positivo ou falso negativo em
produção, o lugar de registrar é aqui.
"""

import pytest

from app.scrapers.relevance import is_relevant_release, normalize_release_title

CASOS = [
    ('Up', 'Superman Returns 1080p Dublado', '', False),
    ('Up', 'Up - Altas Aventuras 1080p Dublado', '', True),
    ('It', 'Little Women 2019 1080p', '', False),
    ('It', 'It - A Coisa 2017 1080p Dublado', '', True),
    ('Us', 'House of Cards S01E01', '', False),
    ('Us', 'Us - Nos 2019 1080p Dublado', '', True),
    ('Her', 'Sherlock Holmes 2009 1080p', '', False),
    ('Her', 'Her 2013 1080p Legendado', '', True),
    ('Coco', 'Coconut Island Documentary', '', False),
    ('Coco', 'Coco - Viva A Vida e uma Festa 1080p Dublado', '', True),
    ('Cars', 'Oscars 2020 Ceremony', '', False),
    ('Cars', 'Cars - Carros 2006 1080p Dublado', '', True),
    ('Room', 'The Mushroom Documentary', '', False),
    ('Room', 'Room - O Quarto de Jack 2015 1080p', '', True),
    ('Ray', 'Rayman Legends', '', False),
    ('Old', 'Oldboy 2003 1080p', '', False),
    ('Alien', 'Aliens 1986 1080p', '', True),
    ('Troy', 'Zoey 102 1080p', '', False),
    ('Interstellar', 'Interestelar 2014 1080p Dublado', '', True),
    ('Interstellar', 'Zoey 102 1080p', '', False),
    ('Titanic', 'Titanic 1997 1080p Dublado', '', True),
    ('The Batman', 'Batman 2022 1080p', '', True),
    ('O Auto da Compadecida', 'O Auto da Compadecida 2000 Nacional', '', True),
    ('Cidade de Deus', 'Cidade de Deus 2002 Nacional 1080p', '', True),
    ('Toy Story 3', 'Toy Story 4 2019 1080p Dublado', '', False),
    ('Toy Story 3', 'Toy Story 3 2010 1080p Dublado', '', True),
    ('Inside Out 2', 'Inside Out 2015 1080p', '', False),
    ('Inside Out 2', 'Inside Out 2 - Divertida Mente 2 2024', '', True),
    ('Taken', 'Taken 2 2012 1080p', '', False),
    ('Taken', 'Taken 2008 1080p Dublado', '', True),
    ('Gladiator', 'Gladiator II 2024 1080p', '', False),
    ('Gladiator', 'Gladiator 2000 1080p Dublado', '', True),
    ('Dune', 'Dune Part Two 2024 1080p', '', False),
    ('Dune', 'Dune 2021 1080p Dublado', '', True),
    ('Tropa de Elite', 'Tropa de Elite 2 - O Inimigo Agora e Outro', '', False),
    ('Tropa de Elite', 'Tropa de Elite 2007 Nacional 1080p', '', True),
    ('Tropa de Elite 2', 'Tropa de Elite 2 - O Inimigo Agora e Outro', '', True),
    ('The Thing 1982', 'The Thing 2011 1080p', '', False),
    ('The Thing 1982', 'The Thing 1982 Remastered 1080p', '', True),
    ('Suspiria 2018', 'Suspiria 1977 1080p', '', False),
    ('Titanic', 'Avatar 2009 1080p', 'https://site.invalid/avatar?busca=titanic', False),
    ('Up', 'Superman Returns 1080p', 'https://up.example.invalid/superman-returns/', False),
    ('Blade Runner', 'Download Torrent 1080p', 'https://s.invalid/blade-runner-1982-1080p/', True),
    ('Coringa', 'Baixar Torrent HD', 'https://s.invalid/coringa-2019-dublado/', True)
]


@pytest.mark.parametrize("query,candidato,url,esperado", CASOS)
def test_corpus_de_relevancia(query, candidato, url, esperado):
    assert is_relevant_release(query, candidato, url) is esperado


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
    O candidato contém a query como substring de CARACTERES, mas não como
    palavra. Cada um destes era aceito pelo atalho antigo.
    """
    assert normalize_release_title(query) in normalize_release_title(candidato)
    assert is_relevant_release(query, candidato) is False


@pytest.mark.parametrize(
    "query,candidato",
    [
        ("Taken", "Taken 2 2012 1080p"),
        ("Gladiator", "Gladiator II 2024 1080p"),
        ("Dune", "Dune Part Two 2024 1080p"),
        ("Tropa de Elite", "Tropa de Elite 2 - O Inimigo Agora e Outro"),
    ],
)
def test_titulo_contido_mas_seguido_de_marcador_de_sequencia(query, candidato):
    """
    Aqui a query aparece inteira, como palavra, no início do candidato — a
    contenção por palavra aceita sozinha. O que decide é o token logo
    depois: número, numeral romano ou "Part" marcam outra obra.
    """
    assert normalize_release_title(candidato).startswith(
        normalize_release_title(query)
    )
    assert is_relevant_release(query, candidato) is False


@pytest.mark.parametrize(
    "query,candidato",
    [
        ("Up", "Up - Altas Aventuras 1080p Dublado"),
        ("It", "It - A Coisa 2017 1080p Dublado"),
        ("Coco", "Coco - Viva A Vida e uma Festa 1080p Dublado"),
        ("Inside Out 2", "Inside Out 2 - Divertida Mente 2 2024"),
    ],
)
def test_subtitulo_localizado_nao_e_sequencia(query, candidato):
    """
    Mesma forma do teste anterior — query contida no início — mas o que
    vem depois é texto, não numeração. Título PT-BR expandido é o caso
    mais comum no acervo brasileiro e precisa continuar passando.
    """
    assert is_relevant_release(query, candidato) is True


def test_url_nao_pode_injetar_pelo_host():
    """Domínio contendo a palavra buscada fazia o site casar com tudo."""
    assert is_relevant_release(
        "Up", "Superman Returns 1080p", "https://up.example.invalid/superman-returns/"
    ) is False


def test_url_nao_pode_injetar_pela_query_string():
    assert is_relevant_release(
        "Titanic", "Avatar 2009 1080p", "https://site.invalid/avatar?busca=titanic"
    ) is False


def test_slug_do_path_continua_servindo_de_fallback():
    """
    O motivo de a URL entrar na comparação: quando o título da página é
    genérico, o slug do path descreve a obra.
    """
    assert is_relevant_release(
        "Blade Runner", "Download Torrent 1080p",
        "https://s.invalid/blade-runner-1982-1080p/",
    ) is True


def test_ano_divergente_dos_dois_lados_rejeita():
    assert is_relevant_release("The Thing 1982", "The Thing 2011 1080p") is False


def test_ano_so_no_candidato_nao_rejeita():
    """
    Query sem ano não impõe restrição — o Cinemeta costuma devolver só o
    título, e o release quase sempre traz o ano.
    """
    assert is_relevant_release("Titanic", "Titanic 1997 1080p Dublado") is True
