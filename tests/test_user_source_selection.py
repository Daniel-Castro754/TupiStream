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
    def test_valida_remove_duplicata_e_preserva_ids_lowercase(self):
        selected = parse_source_ids("yts,archive,yts")
        assert selected == frozenset({"yts", "archive"})
        assert ordered_source_ids(selected) == ["yts", "archive"]

    @pytest.mark.parametrize("raw", ["", ",", "desconhecida", "yts,evil", "YTS"])
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
    async def test_chave_usa_conjunto_efetivo_e_canonico(self):
        yts = YTSScraper()
        brazuca = BrazucaAddonScraper()
        with patch(
            "app.services.stream_aggregator._build_scraper_list",
            return_value=[yts, brazuca],
        ):
            aggregator = StreamAggregator()
        try:
            effective_yts = aggregator._effective_source_ids(frozenset({"yts", "hdr"}))
            effective_all = aggregator._effective_source_ids(None)
            assert effective_yts == frozenset({"yts"})
            assert effective_all == frozenset({"brazuca", "yts"})

            key_yts = aggregator._stream_cache_key(
                "tt0017925", "movie", None, None, effective_yts
            )
            key_equivalent = aggregator._stream_cache_key(
                "tt0017925", "movie", None, None,
                aggregator._effective_source_ids(frozenset({"yts"})),
            )
            assert key_yts == key_equivalent
            assert key_yts.endswith("sources=yts")
            assert key_yts.startswith("streams:v3:")

            legacy_key = aggregator._stream_cache_key(
                "tt0017925", "movie", None, None, effective_all
            )
            selected_all_key = aggregator._stream_cache_key(
                "tt0017925", "movie", None, None,
                aggregator._effective_source_ids(frozenset({"yts", "brazuca"})),
            )
            assert legacy_key == selected_all_key
        finally:
            await aggregator.close()

    @pytest.mark.asyncio
    async def test_chave_de_serie_preserva_temporada_episodio_e_fontes(self):
        yts = YTSScraper()
        with patch(
            "app.services.stream_aggregator._build_scraper_list", return_value=[yts]
        ):
            aggregator = StreamAggregator()
        try:
            key = aggregator._stream_cache_key(
                "tt1234567", "series", 2, 5, frozenset({"yts"})
            )
            assert key == "streams:v3:tt1234567:series:2:5:sources=yts"
        finally:
            await aggregator.close()

    @pytest.mark.asyncio
    async def test_selecao_apenas_desabilitada_retorna_vazio_sem_trabalho(self):
        yts = YTSScraper()
        with patch(
            "app.services.stream_aggregator._build_scraper_list", return_value=[yts]
        ):
            aggregator = StreamAggregator()
        aggregator._get_cached_torrents = AsyncMock()
        aggregator._fetch_title = AsyncMock()
        aggregator._singleflight = AsyncMock()
        try:
            streams = await aggregator.get_streams(
                "tt0017925", "movie", "disabled-only",
                selected_sources=frozenset({"archive"}),
            )
        finally:
            await aggregator.close()
        assert streams == []
        aggregator._get_cached_torrents.assert_not_awaited()
        aggregator._fetch_title.assert_not_awaited()
        aggregator._singleflight.assert_not_awaited()


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

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/sources/evil/token/manifest.json",
            "/sources/evil/hybrid/token/manifest.json",
            "/sources/evil/token/stream/movie/tt0017925.json",
            "/sources/evil/hybrid/token/stream/movie/tt0017925.json",
        ],
    )
    async def test_erro_em_url_selecionada_com_token_recebe_headers(self, path):
        response = await _get(path)
        assert response.status_code == 422
        assert response.headers["cache-control"] == "no-store, private"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-robots-tag"] == "noindex, nofollow"

    @pytest.mark.asyncio
    async def test_token_selecionado_grande_demais_e_rejeitado_com_headers(self):
        token = "x" * 257
        response = await _get(f"/sources/yts/{token}/manifest.json")
        assert response.status_code == 422
        assert response.headers["cache-control"] == "no-store, private"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source_ids", ["YTS", "yts%20", "yts.", "yts%3C"])
    async def test_boundary_rejeita_ids_fora_do_alfabeto_publico(self, source_ids):
        response = await _get(f"/sources/{source_ids}/manifest.json")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_token_legacy_literal_sources_continua_funcionando(self):
        manifest = await _get("/sources/manifest.json")
        assert manifest.status_code == 200
        assert manifest.headers["cache-control"] == "no-store, private"

        with patch(
            "app.routes.stream.aggregator.get_streams", AsyncMock(return_value=[])
        ) as mocked:
            stream = await _get("/sources/stream/movie/tt0017925.json")
        assert stream.status_code == 200
        assert mocked.await_args.kwargs["rd_token"] == "sources"


class TestRegistrySourceIds:
    def test_mapping_e_total_unico_lowercase_e_path_safe(self):
        from app.services.stream_aggregator import SCRAPER_REGISTRY, SOURCE_ID_BY_FLAG

        registry_flags = {flag for flag, _ in SCRAPER_REGISTRY}
        assert set(SOURCE_ID_BY_FLAG) == registry_flags
        ids = list(SOURCE_ID_BY_FLAG.values())
        assert len(ids) == len(set(ids))
        assert all(source_id == source_id.lower() for source_id in ids)
        assert all(source_id.replace("-", "").isalnum() for source_id in ids)


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

    def test_fontes_globais_desligadas_ficam_indisponiveis(self):
        html = _build_config_html()
        hdr_start = html.index('data-source-card="hdr"')
        hdr_card = html[hdr_start : hdr_start + 900]
        assert "Indisponivel nesta instancia" in hdr_card
        assert " disabled" in hdr_card

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
        assert "Desabilitada pelo administrador" in html
        assert "healthLabels" in html
        assert "cooldown" in html

    def test_alterar_entrada_invalida_manifest_gerado(self):
        html = _build_config_html()
        assert "function invalidateManifest()" in html
        assert "manifestUrl = '';" in html
        assert "resultArea.classList.remove('visible')" in html
        assert "sourceInputs.forEach(function(input)" in html
        assert "input.addEventListener('change', invalidateManifest)" in html
