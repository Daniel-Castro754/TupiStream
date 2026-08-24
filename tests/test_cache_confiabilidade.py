"""
Confiabilidade da camada de cache.

Regressão principal: `SQLiteCacheBackend.set()` capturava qualquer exceção,
logava um warning e retornava como se tivesse funcionado. O agregador
seguia entregando ao Stremio URLs /play/{id} cujas sessões nunca foram
gravadas — cada clique dava 404, e nada no fluxo indicava o problema.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.main import _limpeza_periodica_do_cache, lifespan
from app.main import app as fastapi_app
from app.models.torrent import TorrentResult
from app.services.cache import CacheError, CacheWriteError, SQLiteCacheBackend
from app.services.stream_aggregator import ScrapeOutcome, StreamAggregator


def _aggregator() -> StreamAggregator:
    with patch("app.services.stream_aggregator._build_scraper_list", return_value=[]):
        return StreamAggregator()


def _torrent() -> TorrentResult:
    return TorrentResult(
        title="Filme 1080p Dublado",
        info_hash="a" * 40,
        magnet="magnet:?xt=urn:btih:" + "a" * 40,
        quality="1080p",
        dubbed=True,
        source="Fonte X",
    )


class TestPragmasDeAbertura:
    """WAL, synchronous e busy_timeout precisam valer na conexao real."""

    @pytest.mark.asyncio
    async def test_journal_mode_e_wal(self, tmp_path):
        backend = SQLiteCacheBackend(db_path=str(tmp_path / "c.db"))
        await backend.init()
        try:
            cursor = await backend._db.execute("PRAGMA journal_mode")
            assert (await cursor.fetchone())[0].lower() == "wal"
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_busy_timeout_configurado(self, tmp_path):
        backend = SQLiteCacheBackend(db_path=str(tmp_path / "c.db"))
        await backend.init()
        try:
            cursor = await backend._db.execute("PRAGMA busy_timeout")
            assert (await cursor.fetchone())[0] == 5000
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_roundtrip_continua_funcionando_com_wal(self, tmp_path):
        backend = SQLiteCacheBackend(db_path=str(tmp_path / "c.db"))
        await backend.init()
        try:
            await backend.set("k", {"a": 1}, ttl=60)
            assert await backend.get("k") == {"a": 1}
        finally:
            await backend.close()


class TestEscritaFalhaAlto:
    @pytest.mark.asyncio
    async def test_set_levanta_cache_write_error(self, tmp_path):
        backend = SQLiteCacheBackend(db_path=str(tmp_path / "c.db"))
        await backend.init()
        try:
            backend._db.execute = AsyncMock(side_effect=RuntimeError("disco cheio"))
            with pytest.raises(CacheWriteError):
                await backend.set("k", {"a": 1}, ttl=60)
        finally:
            backend._db = None

    @pytest.mark.asyncio
    async def test_set_sem_backend_inicializado_levanta(self):
        backend = SQLiteCacheBackend(db_path="ignorado.db")
        with pytest.raises(CacheWriteError):
            await backend.set("k", {"a": 1})

    def test_cache_write_error_e_um_cache_error(self):
        assert issubclass(CacheWriteError, CacheError)


class TestPlaySessionEhEscritaObrigatoria:
    """
    Sem a sessao gravada, /play devolveria 404 no clique. O stream RD nao
    deve ser oferecido — mas o P2P daquele torrent continua.
    """

    @pytest.mark.asyncio
    async def test_falha_na_sessao_nao_oferece_stream_rd(self):
        agg = _aggregator()
        mock_cache = AsyncMock()
        mock_cache.get.return_value = None
        mock_cache.set.side_effect = CacheWriteError("banco travado")

        with patch("app.services.stream_aggregator.cache", mock_cache):
            with patch.object(agg, "_fetch_title", return_value=("Filme", "Filme")):
                with patch.object(
                    agg, "_run_scrapers",
                    return_value=ScrapeOutcome(torrents=[_torrent()], ok_sources=1),
                ):
                    streams = await agg.get_streams(
                        imdb_id="tt1234567", type="movie", req_id="r1",
                        rd_token="token-fake",
                    )

        assert len(streams) == 1
        assert streams[0].url is None, "nao pode oferecer /play sem sessao gravada"
        assert streams[0].infoHash == _torrent().info_hash

    @pytest.mark.asyncio
    async def test_modo_hibrido_degrada_para_p2p(self):
        agg = _aggregator()
        mock_cache = AsyncMock()
        mock_cache.get.return_value = None
        mock_cache.set.side_effect = CacheWriteError("banco travado")

        with patch("app.services.stream_aggregator.cache", mock_cache):
            with patch.object(agg, "_fetch_title", return_value=("Filme", "Filme")):
                with patch.object(
                    agg, "_run_scrapers",
                    return_value=ScrapeOutcome(torrents=[_torrent()], ok_sources=1),
                ):
                    streams = await agg.get_streams(
                        imdb_id="tt1234567", type="movie", req_id="r1",
                        rd_token="token-fake", include_p2p=True,
                    )

        assert all(s.url is None for s in streams)
        assert any(s.infoHash for s in streams)

    @pytest.mark.asyncio
    async def test_sessao_gravada_com_sucesso_oferece_rd(self):
        agg = _aggregator()
        mock_cache = AsyncMock()
        mock_cache.get.return_value = None

        with patch("app.services.stream_aggregator.cache", mock_cache):
            with patch.object(agg, "_fetch_title", return_value=("Filme", "Filme")):
                with patch.object(
                    agg, "_run_scrapers",
                    return_value=ScrapeOutcome(torrents=[_torrent()], ok_sources=1),
                ):
                    streams = await agg.get_streams(
                        imdb_id="tt1234567", type="movie", req_id="r1",
                        rd_token="token-fake",
                    )

        assert streams[0].url is not None
        assert "/play/" in streams[0].url


class TestCacheDeBuscaEhBestEffort:
    """Falha ao cachear a busca nao pode derrubar a resposta."""

    @pytest.mark.asyncio
    async def test_falha_ao_cachear_busca_nao_propaga(self):
        agg = _aggregator()
        mock_cache = AsyncMock()
        mock_cache.set.side_effect = CacheWriteError("disco cheio")

        with patch("app.services.stream_aggregator.cache", mock_cache):
            await agg._cachear_busca(
                "streams:v2:tt1:movie",
                ScrapeOutcome(torrents=[_torrent()], ok_sources=1),
                "r1",
            )  # nao deve levantar

    @pytest.mark.asyncio
    async def test_busca_retorna_streams_mesmo_sem_cachear(self):
        agg = _aggregator()
        mock_cache = AsyncMock()
        mock_cache.get.return_value = None
        mock_cache.set.side_effect = CacheWriteError("disco cheio")

        with patch("app.services.stream_aggregator.cache", mock_cache):
            with patch.object(agg, "_fetch_title", return_value=("Filme", "Filme")):
                with patch.object(
                    agg, "_run_scrapers",
                    return_value=ScrapeOutcome(torrents=[_torrent()], ok_sources=1),
                ):
                    streams = await agg.get_streams(
                        imdb_id="tt1234567", type="movie", req_id="r1"
                    )

        assert len(streams) == 1


class TestJanitorPeriodico:
    @pytest.mark.asyncio
    async def test_chama_delete_expired_a_cada_intervalo(self):
        mock_cache = AsyncMock()
        with patch("app.main.cache", mock_cache):
            with patch("app.main.settings") as s:
                s.CACHE_CLEANUP_INTERVAL_SECONDS = 0
                tarefa = asyncio.create_task(_limpeza_periodica_do_cache())
                await asyncio.sleep(0.05)
                tarefa.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tarefa

        assert mock_cache.delete_expired.await_count >= 1

    @pytest.mark.asyncio
    async def test_falha_na_limpeza_nao_encerra_o_loop(self):
        mock_cache = AsyncMock()
        mock_cache.delete_expired.side_effect = RuntimeError("banco travado")
        with patch("app.main.cache", mock_cache):
            with patch("app.main.settings") as s:
                s.CACHE_CLEANUP_INTERVAL_SECONDS = 0
                tarefa = asyncio.create_task(_limpeza_periodica_do_cache())
                await asyncio.sleep(0.05)
                assert not tarefa.done(), "o loop deve sobreviver a falha"
                tarefa.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tarefa

        assert mock_cache.delete_expired.await_count >= 2


class TestShutdownEmFinally:
    @pytest.mark.asyncio
    async def test_cache_fecha_mesmo_se_agregador_falhar(self):
        mock_cache = AsyncMock()
        mock_agg = AsyncMock()
        mock_agg.close.side_effect = RuntimeError("scraper travado")

        with patch("app.main.cache", mock_cache), patch("app.main.aggregator", mock_agg):
            async with lifespan(fastapi_app):
                pass

        mock_agg.close.assert_awaited_once()
        mock_cache.close.assert_awaited_once(), "cache deve fechar mesmo com falha antes"

    @pytest.mark.asyncio
    async def test_cache_fecha_mesmo_com_excecao_no_corpo(self):
        mock_cache = AsyncMock()
        mock_agg = AsyncMock()

        with patch("app.main.cache", mock_cache), patch("app.main.aggregator", mock_agg):
            with pytest.raises(RuntimeError):
                async with lifespan(fastapi_app):
                    raise RuntimeError("boom no runtime da app")

        mock_cache.close.assert_awaited_once()
