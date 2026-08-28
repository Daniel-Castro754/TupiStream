"""Seleção de fontes por usuário, transportada na URL do manifest."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routes.configure import _build_config_html
from app.scrapers.brazuca_addon import BrazucaAddonScraper
from app.scrapers.yts import YTSScraper
from app.services.stream_aggregator import (
    StreamAggregator,
    ordered_source_ids,
    parse_source_ids,
)


class TestSourceIdParsing:
    def test_valida_normaliza_e_remove_duplicata(self):
        selected = parse_source_ids("yts,archive,YTS")
        assert selected == frozenset({"yts", "archive"})
        assert ordered_source_ids(selected) == ["yts", "archive"]

    @pytest.mark.parametrize("raw", ["", ",", "desconhecida", "yts,evil"])
    def test_rejeita_selecao_invalida(self, raw):
        with pytest.raises(ValueError):
            parse_source_ids(raw)


class TestAggregatorSelection:
    @pytest.mark.asyncio
    async def test_executa_apenas_fonte_selecionada(self):
        yts = YTSScraper()
        brazuca = BrazucaAddonScraper()
        yts.search = AsyncMock(return_value=[])
        brazuca.search = AsyncMock(return_value=[])
        with patch(
            "app.services.stream_aggregator._build_scraper_list",
            return_value=[yts, brazuca],
        ):
            aggregator = StreamAggregator()
        try:
            await aggregator._run_scrapers(
                "The General",
                "tt0017925",
                "movie",
                "req1",
                "ptbr",
                budget=5,
                selected_sources=frozenset({"yts"}),
            )
        finally:
            await aggregator.close()

        yts.search.assert_awaited_once()
        brazuca.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fonte_bloqueada_pelo_admin_nao_executa(self):
        yts = YTSScraper()
        yts.search = AsyncMock(return_value=[])
        with patch(
            "app.services.stream_aggregator._build_scraper_list",
            return_value=[yts],
        ):
            aggregator = StreamAggregator()
        try:
            outcome = await aggregator._run_scrapers(
                "The General",
                "tt0017925",
                "movie",
                "req1",
                "ptbr",
                budget=5,
                selected_sources=frozenset({"archive"}),
            )
        finally:
            await aggregator.close()

        assert outcome.ran is False
        yts.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_selecao_faz_parte_da_chave_de_cache_e_singleflight(self):
        with patch(
            "app.services.stream_aggregator._build_scraper_list", return_value=[]
        ):
            aggregator = StreamAggregator()
        aggregator._get_cached_torrents = AsyncMock(return_value=None)
        aggregator._singleflight = AsyncMock(return_value=[])

        await aggregator.get_streams(
            "tt0017925",
            "movie",
            "a",
            selected_sources=frozenset({"yts"}),
        )
        key_yts = aggregator._singleflight.await_args.args[0]
        aggregator._singleflight.reset_mock()
        await aggregator.get_streams(
            "tt0017925",
            "movie",
            "b",
            selected_sources=frozenset({"archive"}),
        )
        key_archive = aggregator._singleflight.await_args.args[0]

        assert key_yts != key_archive
        assert key_yts.endswith("sources=yts")
        assert key_archive.endswith("sources=archive")

    @pytest.mark.asyncio
    async def test_url_antiga_preserva_padrao_todas_as_fontes(self):
        with patch(
            "app.services.stream_aggregator._build_scraper_list", return_value=[]
        ):
            aggregator = StreamAggregator()
        aggregator._get_cached_torrents = AsyncMock(return_value=[])
        await aggregator.get_streams("tt0017925", "movie", "legacy")
        key = aggregator._get_cached_torrents.await_args.args[0]
        assert "sources=" not in key


async def _get(path: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


class TestSelectedRoutes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/sources/yts,archive/manifest.json",
            "/sources/yts,archive/token/manifest.json",
            "/sources/yts,archive/hybrid/token/manifest.json",
        ],
    )
    async def test_manifests_selecionados(self, path):
        response = await _get(path)
        assert response.status_code == 200
        assert response.json()["id"] == "community.br-streams"

    @pytest.mark.asyncio
    async def test_manifest_rejeita_fonte_desconhecida(self):
        response = await _get("/sources/yts,evil/manifest.json")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_rota_p2p_repassa_selecao(self):
        with patch(
            "app.routes.stream.aggregator.get_streams",
            AsyncMock(return_value=[]),
        ) as mocked:
            response = await _get(
                "/sources/yts,archive/stream/movie/tt0017925.json"
            )
        assert response.status_code == 200
        assert mocked.await_args.kwargs["selected_sources"] == frozenset(
            {"yts", "archive"}
        )
        assert mocked.await_args.kwargs["rd_token"] is None

    @pytest.mark.asyncio
    async def test_rota_hibrida_repassa_token_modo_e_selecao(self):
        with patch(
            "app.routes.stream.aggregator.get_streams",
            AsyncMock(return_value=[]),
        ) as mocked:
            response = await _get(
                "/sources/yts/hybrid/token/stream/movie/tt0017925.json"
            )
        assert response.status_code == 200
        assert mocked.await_args.kwargs["selected_sources"] == frozenset({"yts"})
        assert mocked.await_args.kwargs["include_p2p"] is True
        assert mocked.await_args.kwargs["rd_token"] == "token"

    @pytest.mark.asyncio
    async def test_rota_rd_repassa_token_e_selecao(self):
        with patch(
            "app.routes.stream.aggregator.get_streams",
            AsyncMock(return_value=[]),
        ) as mocked:
            response = await _get(
                "/sources/yts/token/stream/movie/tt0017925.json"
            )
        assert response.status_code == 200
        assert mocked.await_args.kwargs["selected_sources"] == frozenset({"yts"})
        assert mocked.await_args.kwargs["include_p2p"] is False
        assert mocked.await_args.kwargs["rd_token"] == "token"


class TestConfigureSourcePicker:
    def test_renderiza_toggle_para_todas_as_fontes(self):
        html = _build_config_html()
        assert html.count('class="source-toggle"') == 10
        for source_id in (
            "apache",
            "comando",
            "hdr",
            "micoleao",
            "brazuca",
            "yts",
            "archive",
            "torrentgalaxy",
            "1337x",
            "rutracker",
        ):
            assert f'data-source-id="{source_id}"' in html

    def test_todas_as_fontes_ficam_disponiveis_por_padrao(self):
        html = _build_config_html()
        hdr_start = html.index('data-source-card="hdr"')
        hdr_card = html[hdr_start : hdr_start + 900]
        assert "Disponivel" in hdr_card
        assert " checked" in hdr_card
        assert " disabled" not in hdr_card

    def test_fontes_disponiveis_vem_marcadas_por_padrao(self):
        html = _build_config_html()
        yts_start = html.index('data-source-card="yts"')
        yts_card = html[yts_start : yts_start + 900]
        assert "Disponivel" in yts_card
        assert " checked" in yts_card
        assert " disabled" not in yts_card

    def test_url_gerada_transporta_fontes_sem_estado_global(self):
        html = _build_config_html()
        assert "'/sources/' + selectedSources.join(',')" in html
        assert "selected_sources" not in html
        assert "Sua selecao fica na URL instalada" in html

    def test_env_example_documenta_todas_as_flags_administrativas(self):
        import pathlib

        env_example = pathlib.Path(__file__).resolve().parents[1] / ".env.example"
        text = env_example.read_text(encoding="utf-8")
        expected = {
            "ENABLE_APACHE_TORRENT",
            "ENABLE_COMANDO_FILMES",
            "ENABLE_HDR_TORRENT",
            "ENABLE_MICOLEAO",
            "ENABLE_BRAZUCA",
            "ENABLE_YTS",
            "ENABLE_ARCHIVE_ORG",
            "ENABLE_TORRENT_GALAXY",
            "ENABLE_1337X",
            "ENABLE_RUTRACKER",
        }
        assert all(f"{flag}=" in text for flag in expected)

    def test_interface_distingue_selecao_saude_e_disponibilidade(self):
        html = _build_config_html()
        assert "Fontes que desejo utilizar" in html
        assert "Ainda nao verificada" in html
        assert "healthLabels" in html
        assert "cooldown" in html
