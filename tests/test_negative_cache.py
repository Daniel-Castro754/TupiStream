"""
Regressão: falha das fontes era cacheada como "nenhum resultado" por 1 hora.

`_run_scrapers` devolvia `list[TorrentResult]`, e uma lista vazia era
ambígua — podia ser "busquei e não achei" ou "todas as fontes falharam /
estavam em cooldown / o budget acabou". `get_streams` gravava `[]` com o
TTL padrão de 3600s nos quatro casos, congelando uma indisponibilidade
temporária como "esse conteúdo não existe" por uma hora.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.models.config import settings
from app.models.torrent import TorrentResult
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


class TestScrapeOutcome:
    def test_vazio_com_fonte_saudavel_e_confiavel(self):
        assert ScrapeOutcome(ok_sources=2).confiavel is True

    def test_vazio_sem_fonte_saudavel_nao_e_confiavel(self):
        assert ScrapeOutcome(failed_sources=3).confiavel is False

    def test_que_nao_executou_nunca_e_confiavel(self):
        assert ScrapeOutcome(ran=False, ok_sources=5).confiavel is False

    def test_combinar_soma_o_diagnostico(self):
        a = ScrapeOutcome(ok_sources=2, failed_sources=1, skipped_sources=1)
        b = ScrapeOutcome(ok_sources=1, failed_sources=3, ran=False)
        torrents = [_torrent()]

        c = a.combinar(b, torrents)

        assert c.torrents == torrents
        assert (c.ok_sources, c.failed_sources, c.skipped_sources) == (3, 4, 1)
        assert c.ran is True


class TestPoliticaDeCache:
    """Verifica a decisão de _cachear_busca nos três cenários."""

    @pytest.mark.asyncio
    async def test_com_resultado_usa_ttl_padrao(self):
        agg = _aggregator()
        mock_cache = AsyncMock()

        with patch("app.services.stream_aggregator.cache", mock_cache):
            await agg._cachear_busca(
                "streams:v2:tt1:movie",
                ScrapeOutcome(torrents=[_torrent()], ok_sources=1),
                "req1",
            )

        mock_cache.set.assert_awaited_once()
        assert "ttl" not in mock_cache.set.await_args.kwargs

    @pytest.mark.asyncio
    async def test_vazio_confirmado_usa_ttl_curto(self):
        agg = _aggregator()
        mock_cache = AsyncMock()

        with patch("app.services.stream_aggregator.cache", mock_cache):
            await agg._cachear_busca(
                "streams:v2:tt1:movie", ScrapeOutcome(ok_sources=3), "req1"
            )

        mock_cache.set.assert_awaited_once()
        ttl = mock_cache.set.await_args.kwargs["ttl"]
        assert ttl == settings.NEGATIVE_CACHE_TTL_SECONDS
        assert ttl < settings.CACHE_TTL

    @pytest.mark.asyncio
    async def test_todas_as_fontes_falharam_nao_cacheia(self):
        agg = _aggregator()
        mock_cache = AsyncMock()

        with patch("app.services.stream_aggregator.cache", mock_cache):
            await agg._cachear_busca(
                "streams:v2:tt1:movie", ScrapeOutcome(failed_sources=4), "req1"
            )

        mock_cache.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_todas_em_cooldown_nao_cacheia(self):
        agg = _aggregator()
        mock_cache = AsyncMock()

        with patch("app.services.stream_aggregator.cache", mock_cache):
            await agg._cachear_busca(
                "streams:v2:tt1:movie",
                ScrapeOutcome(ran=False, skipped_sources=4),
                "req1",
            )

        mock_cache.set.assert_not_awaited()


class TestBudgetEsgotadoNaoEnvenenaOCache:
    @pytest.mark.asyncio
    async def test_sem_budget_para_scrapers_nao_grava_vazio(self):
        """
        O encadeamento mais nocivo: _fetch_title demora, o budget acaba, os
        scrapers sao pulados inteiramente e o vazio resultante era gravado
        por uma hora. Uma requisicao lenta envenenava o titulo.
        """
        agg = _aggregator()
        mock_cache = AsyncMock()
        mock_cache.get.return_value = None

        with patch("app.services.stream_aggregator.cache", mock_cache):
            with patch.object(agg, "_fetch_title", return_value=("Filme", "Filme")):
                with patch.object(agg, "_budget_remaining", return_value=0.0):
                    streams = await agg.get_streams(
                        imdb_id="tt1234567", type="movie", req_id="req1"
                    )

        assert streams == []
        gravacoes = [
            c for c in mock_cache.set.await_args_list
            if c.args and str(c.args[0]).startswith("streams:")
        ]
        assert gravacoes == [], "vazio por falta de budget nao pode ir para o cache"
