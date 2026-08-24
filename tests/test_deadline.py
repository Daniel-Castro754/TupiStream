"""
Deadline absoluto: o budget declarado precisa ser um teto de verdade.

`httpx.AsyncClient(timeout=X)` aplica X **por operação**, não ao bloco
`async with`. Como as chamadas de metadados são sequenciais, o pior caso de
`_fetch_title` era 4 × 4,0s = 16s numa etapa documentada como tendo teto de
4s — dentro de um `REQUEST_BUDGET_SECONDS` de 12s.

O fluxo Real-Debrid era pior: 7 chamadas sequenciais de 15s mais 1,5s de
sleep, ~106,5s, sem nenhum teto agregado, enquanto o Stremio corta em ~20s.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.config import settings
from app.services.real_debrid import (
    RealDebridPlaybackNotReadyError,
    RealDebridService,
    RealDebridTimeoutError,
)
from app.services.stream_aggregator import StreamAggregator


def _aggregator() -> StreamAggregator:
    with patch("app.services.stream_aggregator._build_scraper_list", return_value=[]):
        return StreamAggregator()


def _resp(json_data: dict, status: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.raise_for_status = MagicMock()
    r.json.return_value = json_data
    return r


class _ClienteLento:
    """Cliente HTTP cujo .get() dorme — simula upstream travado."""

    def __init__(self, *args, atraso: float = 5.0, respostas=None, **kwargs):
        self._atraso = atraso
        self._respostas = list(respostas or [])
        self.chamadas: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, *args, **kwargs):
        self.chamadas.append(url)
        if self._respostas:
            return self._respostas.pop(0)
        await asyncio.sleep(self._atraso)
        return _resp({})


def _tetos_curtos(minimo: float = 0.05, maximo: float = 0.4):
    """
    Encurta MIN/MAX_BUDGET_TITLE_FETCH para o teste rodar rapido.

    Sem baixar o MINIMO, um budget pequeno faz `_fetch_title` retornar antes
    de abrir o cliente HTTP — o teste passaria sem exercitar o deadline. Foi
    exatamente o que aconteceu na primeira versao destes testes.
    """
    return (
        patch("app.services.stream_aggregator.MIN_BUDGET_TITLE_FETCH", minimo),
        patch("app.services.stream_aggregator.MAX_BUDGET_TITLE_FETCH", maximo),
    )


class TestFetchTitleTemTetoAgregado:
    @pytest.mark.asyncio
    async def test_deadline_corta_o_bloco_inteiro(self):
        """
        Antes, cada chamada tinha seu próprio teto e eles somavam: 4 chamadas
        sequenciais de 4s dentro de uma etapa documentada como tendo teto de
        4s. O tempo total precisa ficar próximo do teto, não de N vezes ele.
        """
        agg = _aggregator()
        criados = []

        def fabrica(*args, **kwargs):
            c = _ClienteLento(atraso=5.0)
            criados.append(c)
            return c

        minimo, maximo = _tetos_curtos()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with minimo, maximo, patch("httpx.AsyncClient", fabrica):
            original, ptbr = await agg._fetch_title("tt0816692", "movie", "req1", budget=0.4)
        decorrido = loop.time() - t0

        assert criados, "o cliente HTTP precisa ter sido aberto — senao o teste passa por vazio"
        assert criados[0].chamadas, "nenhuma requisicao foi feita"
        assert decorrido < 1.5, f"levou {decorrido:.2f}s — o deadline nao cortou o bloco"
        assert original == "tt0816692"
        assert ptbr == "tt0816692"

    @pytest.mark.asyncio
    async def test_resultado_parcial_sobrevive_ao_deadline(self):
        """
        O `return` fica FORA do bloco, então o que o Cinemeta já entregou
        chega ao chamador mesmo com o OMDb travando depois.
        """
        agg = _aggregator()
        cinemeta = _resp({"meta": {"name": "Interstellar"}})
        criados = []

        def fabrica(*args, **kwargs):
            c = _ClienteLento(atraso=5.0, respostas=[cinemeta])
            criados.append(c)
            return c

        minimo, maximo = _tetos_curtos()
        with minimo, maximo, patch("httpx.AsyncClient", fabrica):
            original, ptbr = await agg._fetch_title("tt0816692", "movie", "req1", budget=0.4)

        assert criados and len(criados[0].chamadas) >= 2, (
            "precisa ter feito a chamada do Cinemeta E travado na seguinte"
        )
        assert original == "Interstellar", "o titulo ja obtido nao pode ser perdido"

    @pytest.mark.asyncio
    async def test_budget_insuficiente_nem_abre_cliente(self):
        """Com os tetos REAIS (MIN=2,0s), budget de 0,1s nem chega na rede."""
        chamou = {"v": False}

        def fabrica(*args, **kwargs):
            chamou["v"] = True
            return _ClienteLento()

        agg = _aggregator()
        with patch("httpx.AsyncClient", fabrica):
            await agg._fetch_title("tt1", "movie", "req1", budget=0.1)

        assert chamou["v"] is False


class TestMargemReservada:
    def test_budget_para_scrapers_desconta_a_reserva(self):
        agg = _aggregator()
        with patch.object(agg, "_budget_remaining", return_value=10.0):
            assert agg._budget_para_scrapers(0.0) == pytest.approx(
                10.0 - settings.BUDGET_RESERVE_SECONDS
            )

    def test_nunca_negativo(self):
        agg = _aggregator()
        with patch.object(agg, "_budget_remaining", return_value=0.1):
            assert agg._budget_para_scrapers(0.0) == 0.0


class TestRealDebridDeadline:
    @pytest.mark.asyncio
    async def test_sem_deadline_comportamento_identico(self):
        """
        Retrocompatibilidade: os pontos de chamada que nao passam deadline
        precisam funcionar exatamente como antes.
        """
        service = RealDebridService(api_token="t")
        arquivos = [{"id": 1, "path": "/f/filme.mkv", "bytes": 2_000_000_000}]
        service.client.get = AsyncMock(
            side_effect=[_resp({"files": arquivos}), _resp({"links": ["https://rd/l"]})]
        )
        service.client.post = AsyncMock(
            side_effect=[_resp({"id": "t1"}), _resp({}), _resp({"download": "https://rd/final"})]
        )

        url = await service.get_stream_url(magnet="magnet:?xt=urn:btih:" + "a" * 40, type="movie")

        assert url == "https://rd/final"
        assert service._deadline is None
        await service.close()

    @pytest.mark.asyncio
    async def test_deadline_ja_vencido_levanta_timeout_error(self):
        service = RealDebridService(api_token="t")

        async def lento(*args, **kwargs):
            await asyncio.sleep(5)
            return _resp({})

        service.client.post = AsyncMock(side_effect=lento)
        service.client.get = AsyncMock(side_effect=lento)

        loop = asyncio.get_running_loop()
        with pytest.raises(RealDebridTimeoutError):
            await service.get_stream_url(
                magnet="magnet:?xt=urn:btih:" + "a" * 40,
                type="movie",
                deadline=loop.time() + 0.2,
            )
        await service.close()

    @pytest.mark.asyncio
    async def test_deadline_corta_antes_do_pior_caso(self):
        service = RealDebridService(api_token="t")

        async def lento(*args, **kwargs):
            await asyncio.sleep(15)
            return _resp({})

        service.client.post = AsyncMock(side_effect=lento)
        service.client.get = AsyncMock(side_effect=lento)

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(RealDebridTimeoutError):
            await service.get_stream_url(
                magnet="magnet:?xt=urn:btih:" + "a" * 40, type="movie",
                deadline=loop.time() + 0.3,
            )
        decorrido = loop.time() - t0
        assert decorrido < 2.0, f"levou {decorrido:.2f}s"
        await service.close()


class TestWaitForLinksRespeitaOrcamento:
    @pytest.mark.asyncio
    async def test_nao_inicia_consulta_sem_orcamento(self):
        service = RealDebridService(api_token="t")
        loop = asyncio.get_running_loop()

        service._deadline = loop.time() + 0.2   # menor que delay + margem
        assert service._ha_orcamento_para_nova_consulta(0.75) is False

        service._deadline = loop.time() + 30
        assert service._ha_orcamento_para_nova_consulta(0.75) is True

        service._deadline = None
        assert service._ha_orcamento_para_nova_consulta(0.75) is True
        await service.close()

    @pytest.mark.asyncio
    async def test_sem_orcamento_devolve_not_ready_em_vez_de_insistir(self):
        service = RealDebridService(api_token="t")
        service._deadline = asyncio.get_running_loop().time() + 0.2
        service.client.get = AsyncMock(return_value=_resp({"links": [], "status": "downloading"}))

        with pytest.raises(RealDebridPlaybackNotReadyError):
            await service._wait_for_links("t1")

        # Uma consulta feita; a segunda nao chegou a comecar.
        assert service.client.get.await_count == 1
        await service.close()


class TestFallbackDeTituloNaoEnvenenaOCache:
    """
    Interação entre o deadline desta PR e o negative cache da #16.

    Quando `_fetch_title` falha, ela devolve o próprio `imdb_id` como título.
    Os scrapers textuais então buscam por `"tt0816692"`, não acham nada e
    respondem **bem** — contam como `ok_sources`, o `ScrapeOutcome` fica
    `confiavel` e o vazio ia para o cache negativo como se o conteúdo não
    existisse.

    Não existe: existe uma busca feita com a query errada. E o deadline torna
    esse caminho mais frequente, não menos.
    """

    @staticmethod
    async def _rodar(titulos):
        from app.services.stream_aggregator import ScrapeOutcome

        agg = _aggregator()
        mock_cache = AsyncMock()
        mock_cache.get.return_value = None

        with patch("app.services.stream_aggregator.cache", mock_cache):
            with patch.object(agg, "_fetch_title", return_value=titulos):
                with patch.object(
                    agg, "_run_scrapers",
                    return_value=ScrapeOutcome(torrents=[], ok_sources=3),
                ):
                    await agg.get_streams(imdb_id="tt0816692", type="movie", req_id="r1")

        return [
            c for c in mock_cache.set.await_args_list
            if c.args and str(c.args[0]).startswith("streams:")
        ]

    @pytest.mark.asyncio
    async def test_fallback_de_titulo_nao_cacheia_vazio(self):
        gravacoes = await self._rodar(("tt0816692", "tt0816692"))
        assert gravacoes == [], (
            "vazio com titulo nao resolvido nao pode ser cacheado como "
            "'conteudo nao existe'"
        )

    @pytest.mark.asyncio
    async def test_titulo_resolvido_mantem_o_negative_cache(self):
        """A correção não pode desligar o negative cache do caso normal."""
        gravacoes = await self._rodar(("Interstellar", "Interestelar"))
        assert len(gravacoes) == 1
        assert gravacoes[0].kwargs["ttl"] == settings.NEGATIVE_CACHE_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_titulo_parcial_ja_conta_como_resolvido(self):
        """Basta um dos dois títulos ter resolvido."""
        gravacoes = await self._rodar(("Interstellar", "tt0816692"))
        assert len(gravacoes) == 1

    @pytest.mark.asyncio
    async def test_resultado_POSITIVO_continua_cacheavel_sem_titulo(self):
        """
        YTS e Brazuca têm USES_TEXT_QUERY=False e buscam por imdb_id — podem
        achar conteúdo mesmo com o título não resolvido. O bloqueio vale só
        para o vazio.
        """
        from app.models.torrent import TorrentResult
        from app.services.stream_aggregator import ScrapeOutcome

        agg = _aggregator()
        mock_cache = AsyncMock()
        mock_cache.get.return_value = None
        torrent = TorrentResult(
            title="Filme 1080p", info_hash="a" * 40,
            magnet="magnet:?xt=urn:btih:" + "a" * 40,
            quality="1080p", dubbed=False, source="YTS",
        )

        with patch("app.services.stream_aggregator.cache", mock_cache):
            with patch.object(agg, "_fetch_title", return_value=("tt0816692", "tt0816692")):
                with patch.object(
                    agg, "_run_scrapers",
                    return_value=ScrapeOutcome(torrents=[torrent], ok_sources=1),
                ):
                    streams = await agg.get_streams(
                        imdb_id="tt0816692", type="movie", req_id="r1"
                    )

        assert len(streams) == 1
        gravacoes = [c for c in mock_cache.set.await_args_list
                     if c.args and str(c.args[0]).startswith("streams:")]
        assert len(gravacoes) == 1, "resultado positivo tem de ser cacheado"
