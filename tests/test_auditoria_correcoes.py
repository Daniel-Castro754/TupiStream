"""
Cobertura das correções levantadas na auditoria de código.

Cada teste aqui existe porque o comportamento correspondente estava errado
antes — e nenhum teste pegava. O nome de cada um descreve o bug original.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.scrapers.brazuca_addon import BrazucaAddonScraper
from app.services.real_debrid import RealDebridService
from app.services.stream_aggregator import StreamAggregator


def _aggregator_vazio() -> StreamAggregator:
    with patch("app.services.stream_aggregator._build_scraper_list", return_value=[]):
        return StreamAggregator()


def _resp(json_data: dict) -> MagicMock:
    resposta = MagicMock()
    resposta.raise_for_status = MagicMock()
    resposta.json.return_value = json_data
    return resposta


class TestSelecaoDeArquivoIgnoraNaoVideo:
    """
    A seleção de arquivo do Real-Debrid usava blacklist de extensões.
    Um release empacotado em .rar passava pelo filtro e, como para filmes a
    escolha é pelo MAIOR arquivo, era exatamente ele o escolhido.
    """

    @pytest.mark.asyncio
    async def test_filme_ignora_rar_mesmo_sendo_o_maior_arquivo(self):
        service = RealDebridService(api_token="token-teste")
        arquivos = [
            {"id": 1, "path": "/Filme/Filme.2024.1080p.part1.rar", "bytes": 8_000_000_000},
            {"id": 2, "path": "/Filme/Filme.2024.1080p.mkv", "bytes": 2_000_000_000},
        ]

        service.client.get = AsyncMock(
            side_effect=[_resp({"files": arquivos}), _resp({"links": ["https://rd/link"]})]
        )
        service.client.post = AsyncMock(
            side_effect=[
                _resp({"id": "torrent123"}),
                _resp({}),
                _resp({"download": "https://real-debrid.com/final"}),
            ]
        )

        await service.get_stream_url(magnet="magnet:?xt=urn:btih:" + "a" * 40, type="movie")

        chamada_select = service.client.post.await_args_list[1]
        assert chamada_select.kwargs["data"] == {"files": "2"}
        await service.close()

    @pytest.mark.asyncio
    async def test_torrent_so_com_arquivos_nao_video_nao_seleciona_nada(self):
        from app.services.real_debrid import RealDebridPlaybackNotReadyError

        service = RealDebridService(api_token="token-teste")
        arquivos = [
            {"id": 1, "path": "/Pacote/dados.r00", "bytes": 5_000_000_000},
            {"id": 2, "path": "/Pacote/dados.zip", "bytes": 4_000_000_000},
        ]
        service.client.get = AsyncMock(side_effect=[_resp({"files": arquivos})])
        service.client.post = AsyncMock(side_effect=[_resp({"id": "torrent123"})])

        with pytest.raises(RealDebridPlaybackNotReadyError):
            await service.get_stream_url(magnet="magnet:?xt=urn:btih:" + "a" * 40, type="movie")

        await service.close()


class _FonteFake:
    """Scraper mínimo com o contrato que _run_scrapers consome."""

    USES_TEXT_QUERY = True

    def __init__(self, name: str, resultado: list, erro: str | None = None) -> None:
        self.name = name
        self.last_error = None
        self._resultado = resultado
        self._erro = erro

    async def search(self, query, imdb_id, type, season=None, episode=None):
        # _timed_search zera last_error antes de chamar search, então o erro
        # precisa ser registrado aqui dentro pra simular o caso real.
        self.last_error = self._erro
        if isinstance(self._resultado, Exception):
            raise self._resultado
        return self._resultado


class TestStatusUnavailableEhAlcancavel:
    """
    `_registrar_falha` derivava o status do próprio health com
    `"error" if health.get("status") != "unavailable" else "unavailable"`.
    Como nada jamais atribuía "unavailable", a condição era sempre verdadeira
    e o status era sempre "error" — o /health nunca mostrava esse estado.
    """

    def test_default_continua_error(self):
        aggregator = _aggregator_vazio()
        health: dict = {}
        aggregator._registrar_falha(health, "boom")
        assert health["status"] == "error"
        assert health["consecutive_failures"] == 1

    def test_status_unavailable_e_respeitado(self):
        aggregator = _aggregator_vazio()
        health: dict = {}
        aggregator._registrar_falha(health, "HTTP 403", status="unavailable")
        assert health["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_fonte_vazia_com_erro_vira_unavailable(self):
        aggregator = _aggregator_vazio()
        fonte = _FonteFake("Fonte X", [], erro="HTTP 403: bloqueio anti-bot")
        aggregator.scrapers = [fonte]
        aggregator.source_health = {}

        with patch("app.services.stream_aggregator.cache", AsyncMock()):
            await aggregator._run_scrapers("q", "tt1", "movie", "req1", "ptbr", 10.0)

        assert aggregator.source_health["Fonte X"]["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_fonte_que_levanta_excecao_vira_error(self):
        aggregator = _aggregator_vazio()
        fonte = _FonteFake("Fonte Y", RuntimeError("caiu"))
        aggregator.scrapers = [fonte]
        aggregator.source_health = {}

        with patch("app.services.stream_aggregator.cache", AsyncMock()):
            await aggregator._run_scrapers("q", "tt1", "movie", "req1", "ptbr", 10.0)

        assert aggregator.source_health["Fonte Y"]["status"] == "error"


class TestFetchTitleNaoRepeteRequisicao:
    """
    `for content_type in [type, "movie", "series"]` — com type="movie" a lista
    virava ["movie", "movie", "series"] e a mesma URL do Cinemeta era pedida
    duas vezes se a primeira falhasse, dentro de um budget de 4 segundos.
    """

    @pytest.mark.asyncio
    async def test_content_type_duplicado_nao_gera_requisicao_repetida(self):
        aggregator = _aggregator_vazio()
        urls: list[str] = []

        class _FakeResp:
            status_code = 404

            def json(self):
                return {}

        class _FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url):
                urls.append(url)
                return _FakeResp()

        with patch("httpx.AsyncClient", _FakeClient):
            await aggregator._fetch_title("tt1234567", "movie", "req1", budget=10.0)

        cinemeta = [u for u in urls if "cinemeta" in u]
        assert len(cinemeta) == len(set(cinemeta)), f"URL repetida: {cinemeta}"
        assert len(cinemeta) == 2  # movie e series — sem repetir movie


class TestBrazucaRecusaStreamSemHash:
    """
    O ramo antigo dizia gerar "hash fictício baseado na URL", mas atribuía
    info_hash = "" — e _deduplicate descarta hash vazio. O objeto era montado
    para ser jogado fora poucas linhas depois.
    """

    @pytest.mark.asyncio
    async def test_stream_apenas_com_url_e_recusado(self):
        scraper = BrazucaAddonScraper()
        try:
            resultado = scraper._parsear_stream(
                {"url": "https://exemplo.invalid/video.mp4", "title": "Filme 1080p"}
            )
            assert resultado is None
        finally:
            await scraper.close()

    @pytest.mark.asyncio
    async def test_stream_com_infohash_continua_funcionando(self):
        scraper = BrazucaAddonScraper()
        try:
            resultado = scraper._parsear_stream(
                {"infoHash": "  " + "A" * 40 + "  ", "title": "Filme 1080p Dublado 2.1 GB"}
            )
            assert resultado is not None
            assert resultado.info_hash == "a" * 40
            assert resultado.magnet == "magnet:?xt=urn:btih:" + "a" * 40
            assert resultado.quality == "1080p"
            assert resultado.dubbed is True
            assert resultado.size == "2.1 GB"
        finally:
            await scraper.close()

    @pytest.mark.asyncio
    async def test_resultado_recusado_nao_chega_na_deduplicacao(self):
        aggregator = _aggregator_vazio()
        scraper = BrazucaAddonScraper()
        try:
            sem_hash = scraper._parsear_stream({"url": "https://exemplo.invalid/a.mp4"})
            com_hash = scraper._parsear_stream({"infoHash": "b" * 40, "title": "Filme"})
        finally:
            await scraper.close()

        assert sem_hash is None
        assert len(aggregator._deduplicate([com_hash])) == 1
