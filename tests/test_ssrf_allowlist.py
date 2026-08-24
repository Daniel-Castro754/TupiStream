"""
SSRF: o href do HTML da fonte nao pode virar destino arbitrario.

Os quatro scrapers WordPress extraem `<a href>` da pagina de busca e passam
o resultado direto para `self._get()` — ou seja, o servidor do addon faz a
requisicao. O href e conteudo EXTERNO, controlado por quem publica no site.

Como a validacao era feita:

    apache_torrent / comando_filmes   `dominio in href`   (substring)
    hdr_torrent / micoleao            `href.startswith("http")`  (nada)

Substring aceita qualquer destino que apenas CONTENHA o dominio em qualquer
posicao — query string, credencial embutida, sufixo de dominio. Os dois
primeiros estao ativos por padrao.

Medido antes da correcao: 10 de 10 payloads passavam, incluindo
169.254.169.254 (metadata de nuvem), 127.0.0.1 e [::1]:6379.
"""

import pytest

from app.scrapers.apache_torrent import ApacheTorrentScraper
from app.scrapers.comando_filmes import ComandoFilmesScraper
from app.scrapers.hdr_torrent import HDRTorrentScraper
from app.scrapers.micoleao import MicoLeaoScraper

PAGINA = "https://apachetorrent.com/?s=filme"

CASOS = [
    ('https://evil.example/?next=apachetorrent.com', False, 'dominio na query string'),
    ('https://apachetorrent.com@169.254.169.254/latest/meta-data/', False, 'credencial embutida -> metadata'),
    ('https://apachetorrent.com.evil.example/', False, 'sufixo de dominio'),
    ('http://127.0.0.1:8000/apachetorrent.com', False, 'loopback'),
    ('http://169.254.169.254/?x=apachetorrent.com', False, 'metadata da nuvem'),
    ('http://[::1]:6379/apachetorrent.com', False, 'Redis local via IPv6'),
    ('https://apachetorrent.com.s3.evil.example/p', False, 'sufixo com subdominio'),
    ('file:///etc/passwd', False, 'esquema file'),
    ('javascript:alert(1)', False, 'esquema javascript'),
    ('http://10.0.0.5/apachetorrent.com', False, 'rede privada'),
    ('https://apachetorrent.com/filme-x-1080p/', True, 'post no dominio principal'),
    ('https://www.apachetorrent.com/filme-x/', True, 'variante com www'),
    ('https://www.apachetorrent.net/filme-y/', True, 'mirror declarado'),
    ('https://apachetorrent.net/filme-y/', True, 'mirror sem www'),
    ('/filme-relativo-1080p/', True, 'href relativo'),
    ('filme-relativo-2/', True, 'href relativo sem barra'),
    ('//apachetorrent.com/filme/', True, 'protocol-relative no dominio'),
    ('//evil.example/filme/', False, 'protocol-relative fora do dominio'),
    ('https://APACHETORRENT.COM/Filme/', True, 'host em maiuscula'),
    ('https://apachetorrent.com./filme/', True, 'ponto final no host')
]


@pytest.fixture
def scraper():
    s = ApacheTorrentScraper()
    yield s


@pytest.mark.parametrize("href,aceita,rotulo", CASOS)
def test_allowlist(scraper, href, aceita, rotulo):
    resultado = scraper._url_do_mesmo_site(href, PAGINA)
    assert (resultado is not None) is aceita, rotulo


class TestAllowlistDerivada:
    def test_inclui_base_url_e_mirrors(self):
        s = ApacheTorrentScraper()
        hosts = s._hosts_permitidos()
        assert "apachetorrent.com" in hosts
        assert "www.apachetorrent.net" in hosts
        assert "apachetorrent.net" in hosts, "variante sem www do mirror"

    def test_nao_inclui_host_arbitrario(self):
        s = ApacheTorrentScraper()
        assert "evil.example" not in s._hosts_permitidos()
        assert "169.254.169.254" not in s._hosts_permitidos()

    @pytest.mark.parametrize(
        "classe",
        [ApacheTorrentScraper, ComandoFilmesScraper, HDRTorrentScraper, MicoLeaoScraper],
    )
    def test_todo_scraper_wordpress_tem_allowlist_nao_vazia(self, classe):
        """Allowlist vazia recusaria tudo — falha fechada, mas quebra o scraper."""
        s = classe()
        assert s._hosts_permitidos(), classe.__name__


class TestPayloadsCriticos:
    """Os destinos que importam de verdade num container em nuvem."""

    @pytest.mark.parametrize(
        "href",
        [
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            "http://169.254.169.254/?x=apachetorrent.com",
            "https://apachetorrent.com@169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8000/apachetorrent.com",
            "http://localhost:8000/apachetorrent.com",
            "http://[::1]:6379/apachetorrent.com",
            "http://10.0.0.5/apachetorrent.com",
            "http://192.168.1.1/apachetorrent.com",
        ],
    )
    def test_destino_interno_e_recusado(self, href):
        assert ApacheTorrentScraper()._url_do_mesmo_site(href, PAGINA) is None


class TestRedirectNaoReaponta:
    """
    `_get_with_fallback` adotava `self.base_url = new_base` a partir da URL
    final do redirect. O scraper e um singleton criado no startup, e
    `_prioritize_fallback_urls` passa a PREFERIR esse destino — um unico
    redirect malicioso contaminava todas as buscas seguintes.
    """

    def test_host_fora_da_allowlist_nao_e_adotado(self):
        s = ApacheTorrentScraper()
        assert "evil.example" not in s._hosts_permitidos()

    def test_mirror_conhecido_continua_sendo_adotavel(self):
        s = ApacheTorrentScraper()
        assert "www.apachetorrent.net" in s._hosts_permitidos()
