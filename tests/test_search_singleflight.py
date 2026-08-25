"""Single-flight por chave e limite global de buscas concorrentes."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.config import settings
from app.models.torrent import TorrentResult
from app.services.stream_aggregator import ScrapeOutcome, SearchBusyError, StreamAggregator


def _torrent() -> TorrentResult:
    return TorrentResult(
        title="Filme 1080p",
        info_hash="a" * 40,
        magnet="magnet:?xt=urn:btih:" + "a" * 40,
        quality="1080p",
        dubbed=False,
        source="Teste",
    )


class _MemoryCache:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.data[key] = value


class TestSingleFlight:
    @pytest.mark.asyncio
    async def test_requests_simultaneos_da_mesma_chave_buscam_uma_vez(self):
        with patch("app.services.stream_aggregator._build_scraper_list", return_value=[]):
            aggregator = StreamAggregator()
        cache = _MemoryCache()
        iniciou = asyncio.Event()
        liberar = asyncio.Event()
        chamadas = 0

        async def scrapers(*args, **kwargs):
            nonlocal chamadas
            chamadas += 1
            iniciou.set()
            await liberar.wait()
            return ScrapeOutcome(torrents=[_torrent()], ok_sources=1)

        with patch("app.services.stream_aggregator.cache", cache):
            with patch.object(
                aggregator, "_fetch_title", AsyncMock(return_value=("Filme", "Filme"))
            ):
                with patch.object(aggregator, "_run_scrapers", side_effect=scrapers):
                    tasks = [
                        asyncio.create_task(
                            aggregator.get_streams(
                                imdb_id="tt1234567", type="movie", req_id=f"r{i}"
                            )
                        )
                        for i in range(20)
                    ]
                    await asyncio.wait_for(iniciou.wait(), timeout=1)
                    await asyncio.sleep(0)
                    liberar.set()
                    resultados = await asyncio.gather(*tasks)

        assert chamadas == 1
        assert all(len(streams) == 1 for streams in resultados)
        assert aggregator._inflight == {}

    @pytest.mark.asyncio
    async def test_resultado_nao_cacheavel_tambem_e_compartilhado(self):
        """É por isso que usamos Task compartilhada, não só lock+cache."""
        with patch("app.services.stream_aggregator._build_scraper_list", return_value=[]):
            aggregator = StreamAggregator()
        cache = _MemoryCache()
        chamadas = 0

        async def falha_total(*args, **kwargs):
            nonlocal chamadas
            chamadas += 1
            await asyncio.sleep(0.05)
            return ScrapeOutcome(torrents=[], failed_sources=3)

        with patch("app.services.stream_aggregator.cache", cache):
            with patch.object(
                aggregator, "_fetch_title", AsyncMock(return_value=("Filme", "Filme"))
            ):
                with patch.object(aggregator, "_run_scrapers", side_effect=falha_total):
                    resultados = await asyncio.gather(*[
                        aggregator.get_streams(
                            imdb_id="tt1234567", type="movie", req_id=f"f{i}"
                        )
                        for i in range(10)
                    ])

        assert chamadas == 1
        assert resultados == [[]] * 10
        assert cache.data == {}, "falha total nao deve virar cache negativo"

    @pytest.mark.asyncio
    async def test_cancelar_um_waiter_nao_cancela_a_busca_compartilhada(self):
        with patch("app.services.stream_aggregator._build_scraper_list", return_value=[]):
            aggregator = StreamAggregator()
        cache = _MemoryCache()
        iniciou = asyncio.Event()
        liberar = asyncio.Event()

        async def scrapers(*args, **kwargs):
            iniciou.set()
            await liberar.wait()
            return ScrapeOutcome(torrents=[_torrent()], ok_sources=1)

        with patch("app.services.stream_aggregator.cache", cache):
            with patch.object(
                aggregator, "_fetch_title", AsyncMock(return_value=("Filme", "Filme"))
            ):
                with patch.object(aggregator, "_run_scrapers", side_effect=scrapers):
                    cancelado = asyncio.create_task(
                        aggregator.get_streams(
                            imdb_id="tt1234567", type="movie", req_id="cancelado"
                        )
                    )
                    sobrevivente = asyncio.create_task(
                        aggregator.get_streams(
                            imdb_id="tt1234567", type="movie", req_id="sobrevivente"
                        )
                    )
                    await iniciou.wait()
                    cancelado.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await cancelado
                    liberar.set()
                    assert len(await sobrevivente) == 1


class TestCapacidadeGlobal:
    @pytest.mark.asyncio
    async def test_conteudos_diferentes_respeitam_o_semaforo(self):
        with patch.object(settings, "MAX_CONCURRENT_SEARCHES", 2):
            with patch.object(settings, "SEARCH_QUEUE_TIMEOUT_SECONDS", 0.05):
                with patch("app.services.stream_aggregator._build_scraper_list", return_value=[]):
                    aggregator = StreamAggregator()

        cache = _MemoryCache()
        entraram = 0
        duas_entraram = asyncio.Event()
        liberar = asyncio.Event()

        async def scrapers(*args, **kwargs):
            nonlocal entraram
            entraram += 1
            if entraram == 2:
                duas_entraram.set()
            await liberar.wait()
            return ScrapeOutcome(torrents=[_torrent()], ok_sources=1)

        with patch("app.services.stream_aggregator.cache", cache):
            with patch.object(
                aggregator, "_fetch_title", AsyncMock(return_value=("Filme", "Filme"))
            ):
                with patch.object(aggregator, "_run_scrapers", side_effect=scrapers):
                    a = asyncio.create_task(
                        aggregator.get_streams(imdb_id="tt1234567", type="movie", req_id="a")
                    )
                    b = asyncio.create_task(
                        aggregator.get_streams(imdb_id="tt1234568", type="movie", req_id="b")
                    )
                    await asyncio.wait_for(duas_entraram.wait(), timeout=1)

                    with pytest.raises(SearchBusyError):
                        await aggregator.get_streams(
                            imdb_id="tt1234569", type="movie", req_id="c"
                        )

                    liberar.set()
                    await asyncio.gather(a, b)

        assert entraram == 2

    @pytest.mark.asyncio
    async def test_slot_e_liberado_apos_excecao(self):
        with patch.object(settings, "MAX_CONCURRENT_SEARCHES", 1):
            with patch("app.services.stream_aggregator._build_scraper_list", return_value=[]):
                aggregator = StreamAggregator()
        cache = _MemoryCache()

        with patch("app.services.stream_aggregator.cache", cache):
            with patch.object(
                aggregator, "_fetch_title", AsyncMock(side_effect=RuntimeError("boom"))
            ):
                with pytest.raises(RuntimeError):
                    await aggregator.get_streams(
                        imdb_id="tt1234567", type="movie", req_id="falha"
                    )

            assert aggregator._search_slots._value == 1


class TestRotaSaturada:
    @pytest.mark.asyncio
    async def test_retorna_503_com_retry_after(self):
        transport = ASGITransport(app=app)
        with patch(
            "app.routes.stream.aggregator.get_streams",
            AsyncMock(side_effect=SearchBusyError("ocupado")),
        ):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/stream/movie/tt1234567.json")

        assert response.status_code == 503
        assert response.headers["retry-after"] == str(settings.SEARCH_RETRY_AFTER_SECONDS)
