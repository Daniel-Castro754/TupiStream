"""Regressões de mirrors reais, CDN do Archive.org e telemetria de origem."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from app.scrapers.archive_org import ArchiveOrgScraper
from app.scrapers.comando_filmes import ComandoFilmesScraper
from app.scrapers.yts import YTSScraper
from app.services.stream_aggregator import StreamAggregator


class _StreamResponse:
    def __init__(self, status, url, *, location=None, chunks=(), content_length=None):
        self.status_code = status
        self.url = url
        self.headers = {}
        if location is not None:
            self.headers["location"] = location
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self._chunks = list(chunks)

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class TestComandoFilmesAtual:
    def test_dominio_atual_e_prioridade(self):
        scraper = ComandoFilmesScraper()
        assert scraper.base_url == "https://www.baixetorrents.net"
        assert scraper._fallback_urls[:2] == [
            "https://www.baixetorrents.net",
            "https://baixetorrents.net",
        ]

    def test_hosts_antigos_e_destino_atual_estao_na_allowlist(self):
        scraper = ComandoFilmesScraper()
        hosts = scraper._hosts_permitidos()
        assert "www.baixetorrents.net" in hosts
        assert "baixetorrents.net" in hosts
        assert "www.baixafilmestorrent.net" in hosts
        assert "baixafilmestorrenthd.com" in hosts
        assert "baixafilmestorrenthd.org" in hosts

    def test_parser_reconhece_html_atual_movies_list(self):
        scraper = ComandoFilmesScraper()
        html = """
        <div id="main">
          <div class="movies-list">
            <div class="col"><div class="item">
              <div class="image"><a href="https://www.baixetorrents.net/interestelar-2025/">capa</a></div>
              <div class="title">
                <a href="https://www.baixetorrents.net/interestelar-2025/">
                  Interestelar (2014)
                </a>
              </div>
            </div></div>
          </div>
        </div>
        """
        links = scraper._extrair_links_posts(
            BeautifulSoup(html, "html.parser"),
            "https://www.baixetorrents.net/?s=Interestelar",
        )
        assert links == ["https://www.baixetorrents.net/interestelar-2025/"]

    @pytest.mark.asyncio
    async def test_layout_atual_ignora_h1_vazio_do_logo(self):
        scraper = ComandoFilmesScraper()
        info_hash = "a" * 40
        html = f"""
        <html><body>
          <h1 class="logo"><a><img alt="logo"></a></h1>
          <h1 class="title">Interestelar (2014)</h1>
          <a href="magnet:?xt=urn:btih:{info_hash}">Magnet</a>
        </body></html>
        """
        response = MagicMock()
        response.text = html
        with patch.object(scraper, "_get", AsyncMock(return_value=response)):
            torrent = await scraper._extrair_torrent(
                "https://www.baixetorrents.net/interestelar-2025/"
            )
        assert torrent is not None
        assert torrent.title == "Interestelar (2014)"
        await scraper.close()

    def test_parser_reconhece_layout_kn_card_sem_abrir_host_externo(self):
        scraper = ComandoFilmesScraper()
        html = """
        <div class="kn-cards-container">
          <article class="kn-card">
            <a class="kn-card-link" href="https://www.baixetorrents.net/filme/">
              <h2 class="kn-card-title">Filme</h2>
            </a>
            <a href="https://evil.example/?next=baixetorrents.net">anuncio</a>
          </article>
        </div>
        """
        links = scraper._extrair_links_posts(
            BeautifulSoup(html, "html.parser"),
            "https://www.baixetorrents.net/?s=Filme",
        )
        assert links == ["https://www.baixetorrents.net/filme/"]


class TestYtsOrigins:
    def test_origem_direta_que_funciona_e_priorizada(self):
        scraper = YTSScraper()
        assert scraper.base_url == "https://yts.gg"
        assert scraper._fallback_urls[0] == "https://yts.gg"
        assert "https://yts.bz" in scraper._fallback_urls


class TestArchiveOrgCdn:
    @pytest.mark.parametrize(
        "url",
        [
            "https://archive.org/x",
            "https://www.archive.org/x",
            "https://dn721807.ca.archive.org/x",
            "https://ia800000.us.archive.org/x",
        ],
    )
    def test_aceita_archive_e_subdominios_reais(self, url):
        assert ArchiveOrgScraper()._url_permitida(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://archive.org.evil.com/x",
            "https://evilarchive.org/x",
            "http://127.0.0.1/x",
            "http://10.0.0.1/x",
            "http://169.254.169.254/x",
            "https://localhost/x",
            "file:///etc/passwd",
            "ftp://archive.org/x",
            "https://user:pass@archive.org/x",
            "https://archive.org:8443/x",
        ],
    )
    def test_nao_enfraquece_ssrf(self, url):
        assert ArchiveOrgScraper()._url_permitida(url) is False

    @pytest.mark.asyncio
    async def test_download_limitado_segue_cdn_archive_org(self):
        scraper = ArchiveOrgScraper()
        redirect = _StreamResponse(
            302,
            "https://archive.org/download/item/item_archive.torrent",
            location="https://dn721807.ca.archive.org/0/items/item/item_archive.torrent",
        )
        final = _StreamResponse(
            200,
            "https://dn721807.ca.archive.org/0/items/item/item_archive.torrent",
            chunks=[b"abc", b"def"],
            content_length=6,
        )
        scraper.client.stream = MagicMock(side_effect=[redirect, final])
        try:
            result = await scraper._get_bytes_limited(
                "https://archive.org/download/item/item_archive.torrent", 100
            )
        finally:
            await scraper.close()

        assert result == b"abcdef"
        assert scraper.client.stream.call_count == 2
        assert scraper.client.stream.call_args_list[1].args[1].startswith(
            "https://dn721807.ca.archive.org/"
        )

    @pytest.mark.asyncio
    async def test_redirect_parecido_malicioso_nao_recebe_segunda_requisicao(self):
        scraper = ArchiveOrgScraper()
        redirect = _StreamResponse(
            302,
            "https://archive.org/download/item/item_archive.torrent",
            location="https://archive.org.evil.com/item.torrent",
        )
        scraper.client.stream = MagicMock(return_value=redirect)
        try:
            result = await scraper._get_bytes_limited(
                "https://archive.org/download/item/item_archive.torrent", 100
            )
        finally:
            await scraper.close()

        assert result is None
        assert scraper.client.stream.call_count == 1
        assert "host nao permitido" in (scraper.last_error or "")


class TestHealthDeOrigens:
    def test_expoe_id_origem_ativa_e_quantidade_de_mirrors(self):
        with patch(
            "app.services.stream_aggregator._build_scraper_list",
            return_value=[ComandoFilmesScraper()],
        ):
            aggregator = StreamAggregator()

        health = {item["id"]: item for item in aggregator.get_source_health()}
        comando = health["comando"]
        assert comando["enabled"] is True
        assert comando["active_origin"] == "https://www.baixetorrents.net"
        assert comando["configured_mirrors"] >= 2
        assert health["hdr"]["status"] == "disabled"
        assert health["hdr"]["configured_mirrors"] == 2

    def test_cooldown_e_um_estado_distinto(self, monkeypatch):
        with patch(
            "app.services.stream_aggregator._build_scraper_list",
            return_value=[ComandoFilmesScraper()],
        ):
            aggregator = StreamAggregator()
        aggregator.source_health["Comando Filmes"].update(
            status="unavailable", skip_until=10_000.0
        )
        monkeypatch.setattr("app.services.stream_aggregator.time.monotonic", lambda: 9_900.0)

        comando = next(
            item for item in aggregator.get_source_health() if item["id"] == "comando"
        )
        assert comando["status"] == "cooldown"
        assert comando["status_before_cooldown"] == "unavailable"
        assert comando["cooldown_remaining_seconds"] == 100
