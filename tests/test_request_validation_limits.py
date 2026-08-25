"""Validação de fronteira e limites de amplificação."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.config import settings
from app.models.torrent import TorrentResult
from app.scrapers.brazuca_addon import BrazucaAddonScraper
from app.services.stream_aggregator import ScrapeOutcome, StreamAggregator


async def _get(path: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


class TestValidacaoDasRotas:
    @pytest.mark.parametrize(
        "path",
        [
            "/stream/anime/tt1234567.json",
            "/stream/serie/tt1234567:1:5.json",
            "/stream/movie/qualquer-coisa.json",
            "/stream/movie/tt123.json",
            "/stream/movie/tt1234567:1:5.json",
            "/stream/series/tt1234567.json",
            "/stream/series/tt1234567:-1:5.json",
            "/stream/series/tt1234567:1:5:extra.json",
        ],
    )
    @pytest.mark.asyncio
    async def test_rejeita_antes_de_tocar_o_agregador(self, path):
        with patch("app.routes.stream.aggregator.get_streams", AsyncMock()) as buscar:
            response = await _get(path)
        assert response.status_code == 422, (path, response.text)
        buscar.assert_not_awaited()

    @pytest.mark.parametrize(
        "path",
        [
            "/stream/movie/tt1234567.json",
            "/stream/series/tt1234567:0:1.json",
            "/stream/series/tt1234567890:12:345.json",
        ],
    )
    @pytest.mark.asyncio
    async def test_formatos_validos_chegam_ao_agregador(self, path):
        with patch(
            "app.routes.stream.aggregator.get_streams", AsyncMock(return_value=[])
        ) as buscar:
            response = await _get(path)
        assert response.status_code == 200, (path, response.text)
        buscar.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_token_grande_demais_e_rejeitado(self):
        token = "x" * 257
        with patch("app.routes.stream.aggregator.get_streams", AsyncMock()) as buscar:
            response = await _get(f"/{token}/stream/movie/tt1234567.json")
        assert response.status_code == 422
        buscar.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_play_id_com_caractere_invalido_e_rejeitado(self):
        response = await _get("/play/id%20com%20espaco")
        assert response.status_code == 422


class TestLimiteDoBrazuca:
    @pytest.mark.asyncio
    async def test_lista_externa_e_cortada_antes_do_parse(self):
        scraper = BrazucaAddonScraper()
        response = AsyncMock()
        response.json.return_value = {
            "streams": [
                {"infoHash": f"{i:040x}", "title": f"Filme {i}"}
                for i in range(settings.MAX_UPSTREAM_STREAMS + 50)
            ]
        }
        try:
            with patch.object(scraper, "_get", AsyncMock(return_value=response)):
                with patch.object(
                    scraper, "_parsear_stream", wraps=scraper._parsear_stream
                ) as parsear:
                    resultados = await scraper.search("Filme", "tt1234567", "movie")
            assert parsear.call_count == settings.MAX_UPSTREAM_STREAMS
            assert len(resultados) == settings.MAX_UPSTREAM_STREAMS
        finally:
            await scraper.close()

    @pytest.mark.asyncio
    async def test_streams_nao_lista_e_rejeitado(self):
        scraper = BrazucaAddonScraper()
        response = AsyncMock()
        response.json.return_value = {"streams": {"nao": "lista"}}
        try:
            with patch.object(scraper, "_get", AsyncMock(return_value=response)):
                assert await scraper.search("Filme", "tt1234567", "movie") == []
            assert scraper.last_error == "campo streams nao e uma lista"
        finally:
            await scraper.close()


def _torrent(index: int) -> TorrentResult:
    return TorrentResult(
        title=f"Filme {index} 1080p",
        info_hash=f"{index:040x}",
        magnet=f"magnet:?xt=urn:btih:{index:040x}",
        quality="1080p",
        dubbed=False,
        source="Teste",
    )


class TestLimiteDoAgregador:
    @staticmethod
    def _aggregator():
        with patch("app.services.stream_aggregator._build_scraper_list", return_value=[]):
            return StreamAggregator()

    @pytest.mark.asyncio
    async def test_p2p_limita_resposta(self):
        aggregator = self._aggregator()
        torrents = [_torrent(i + 1) for i in range(settings.MAX_STREAMS_PER_REQUEST + 20)]
        mock_cache = AsyncMock()
        mock_cache.get.return_value = None
        with patch("app.services.stream_aggregator.cache", mock_cache):
            with patch.object(
                aggregator, "_fetch_title", AsyncMock(return_value=("Filme", "Filme"))
            ):
                with patch.object(
                    aggregator,
                    "_run_scrapers",
                    AsyncMock(return_value=ScrapeOutcome(torrents=torrents, ok_sources=1)),
                ):
                    streams = await aggregator.get_streams(
                        imdb_id="tt1234567", type="movie", req_id="limit-p2p"
                    )
        assert len(streams) == settings.MAX_STREAMS_PER_REQUEST
        assert not any(
            call.args and str(call.args[0]).startswith("play:")
            for call in mock_cache.set.await_args_list
        )

    @pytest.mark.asyncio
    async def test_hibrido_limita_streams_e_play_sessions(self):
        aggregator = self._aggregator()
        torrents = [_torrent(i + 1) for i in range(settings.MAX_STREAMS_PER_REQUEST + 20)]
        mock_cache = AsyncMock()
        mock_cache.get.return_value = None
        with patch("app.services.stream_aggregator.cache", mock_cache):
            with patch.object(
                aggregator, "_fetch_title", AsyncMock(return_value=("Filme", "Filme"))
            ):
                with patch.object(
                    aggregator,
                    "_run_scrapers",
                    AsyncMock(return_value=ScrapeOutcome(torrents=torrents, ok_sources=1)),
                ):
                    streams = await aggregator.get_streams(
                        imdb_id="tt1234567",
                        type="movie",
                        req_id="limit-hybrid",
                        rd_token="token-valido",
                        include_p2p=True,
                        request_base_url="https://addon.example",
                    )

        play_sets = [
            call for call in mock_cache.set.await_args_list
            if call.args and str(call.args[0]).startswith("play:")
        ]
        assert len(streams) <= settings.MAX_STREAMS_PER_REQUEST
        assert len(play_sets) <= settings.MAX_STREAMS_PER_REQUEST // 2
