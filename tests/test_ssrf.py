"""
SSRF: `dominio in href` comparava substring, não hostname.

A checagem original aceitava qualquer destino que contivesse o domínio em
qualquer posição da URL:

    https://evil.example/?next=apachetorrent.com      -> aceito
    https://apachetorrent.com@169.254.169.254/        -> aceito
    https://apachetorrent.com.evil.example/x          -> aceito

Os três apontam para fora do site. O terceiro é o clássico subdomínio
controlado pelo atacante que *termina* com o domínio esperado; o segundo usa
userinfo para disfarçar o host real, e `169.254.169.254` é o endpoint de
metadados de nuvem.

Dois dos quatro scrapers que passavam por aí estão ativos por padrão.
"""

import pytest

from app.scrapers.apache_torrent import ApacheTorrentScraper
from app.scrapers.comando_filmes import ComandoFilmesScraper


@pytest.fixture
def apache():
    s = ApacheTorrentScraper()
    yield s


class TestUrlPermitida:
    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example/?next=apachetorrent.com",
            "https://apachetorrent.com@169.254.169.254/latest/meta-data/",
            "https://apachetorrent.com.evil.example/x",
            "http://169.254.169.254/#apachetorrent.com",
            "https://localhost:8000/apachetorrent.com",
            "http://127.0.0.1/apachetorrent.com",
            "https://10.0.0.1/apachetorrent.com",
            "file:///etc/passwd",
            "https://apachetorrent.com:9000/x",
        ],
    )
    def test_recusa_destino_de_fora(self, apache, url):
        assert apache._url_permitida(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "https://apachetorrent.com/filme-x/",
            "https://apachetorrent.com/",
            "https://www.apachetorrent.net/filme-y/",
            "https://APACHETORRENT.COM/filme-z/",
            "https://apachetorrent.com./trailing-dot/",
        ],
    )
    def test_aceita_os_hosts_declarados(self, apache, url):
        assert apache._url_permitida(url) is True

    def test_hosts_vem_da_configuracao_existente(self, apache):
        hosts = apache._hosts_permitidos()
        assert "apachetorrent.com" in hosts
        assert "www.apachetorrent.net" in hosts

    def test_scraper_sem_hosts_nao_restringe(self):
        """
        Duble de teste sem base_url nao faz rede — fechar aqui quebraria
        testes sem ganho real.
        """
        class _Duble(ApacheTorrentScraper):
            base_url = ""
            _fallback_urls: list[str] = []

        d = _Duble()
        assert d._hosts_permitidos() == frozenset()
        assert d._url_permitida("https://qualquer.coisa/x") is True


class TestResolverLink:
    def test_link_relativo_e_resolvido_contra_a_pagina(self, apache):
        assert apache._resolver_link(
            "/filme-x/", "https://apachetorrent.com/?s=filme"
        ) == "https://apachetorrent.com/filme-x/"

    def test_link_absoluto_de_fora_e_recusado(self, apache):
        assert apache._resolver_link(
            "https://evil.example/?next=apachetorrent.com",
            "https://apachetorrent.com/?s=filme",
        ) is None

    def test_href_vazio(self, apache):
        assert apache._resolver_link("", "https://apachetorrent.com/") is None

    def test_link_para_mirror_declarado_passa(self, apache):
        assert apache._resolver_link(
            "https://www.apachetorrent.net/filme/", "https://apachetorrent.com/"
        ) == "https://www.apachetorrent.net/filme/"


class TestComandoFilmesTemAMesmaProtecao:
    """Os dois scraper WordPress ativos por padrao compartilham o desenho."""

    def test_recusa_destino_de_fora(self):
        s = ComandoFilmesScraper()
        assert s._url_permitida("https://evil.example/?x=baixetorrents.net") is False
        assert s._url_permitida("https://www.baixetorrents.net/filme/") is True
        assert s._url_permitida("https://baixetorrents.net/filme/") is True
        assert s._url_permitida("https://baixafilmestorrent.org/filme/") is False
