"""P2P de séries: não reproduzir o arquivo errado de um pacote."""

from unittest.mock import AsyncMock, patch

import pytest

from app.episode_matching import extract_explicit_episode
from app.models.torrent import TorrentResult
from app.scrapers.brazuca_addon import BrazucaAddonScraper
from app.scrapers.relevance import matches_episode
from app.services.stream_aggregator import (
    ScrapeOutcome,
    StreamAggregator,
    _p2p_safe_for_request,
    _p2p_sources,
)


def _torrent(**overrides) -> TorrentResult:
    data = dict(
        title="Serie 1 Temporada Completa 1080p Dublado",
        info_hash="a" * 40,
        magnet="magnet:?xt=urn:btih:" + "a" * 40,
        quality="1080p",
        dubbed=True,
        source="Teste",
    )
    data.update(overrides)
    return TorrentResult(**data)


def _aggregator() -> StreamAggregator:
    with patch("app.services.stream_aggregator._build_scraper_list", return_value=[]):
        return StreamAggregator()


class TestEpisodeMatchingCompartilhado:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Serie S01E05 1080p", (1, 5)),
            ("Serie 1x05 Dublado", (1, 5)),
            ("Serie T01E05 Nacional", (1, 5)),
            ("Serie Temporada 1 Episodio 5", (1, 5)),
            ("Serie 1920x1080 1 Temporada Completa", None),
            ("Serie DTS5.1 1 Temporada Completa", None),
        ],
    )
    def test_extracao_com_fronteiras(self, title, expected):
        assert extract_explicit_episode(title) == expected

    def test_resolucao_nao_vira_episodio(self):
        assert matches_episode("Serie 1920x1080 1 Temporada Completa", 1, 5)

    def test_audio_nao_vira_temporada(self):
        assert matches_episode("Serie DTS5.1 1 Temporada Completa", 1, 5)


class TestP2PSeguro:
    def test_filme_continua_seguro(self):
        assert _p2p_safe_for_request(_torrent(), "movie", None, None)

    def test_pacote_de_temporada_sem_indice_e_omitido(self):
        assert not _p2p_safe_for_request(_torrent(), "series", 1, 5)

    def test_release_de_episodio_explicito_e_permitido(self):
        torrent = _torrent(title="Serie S01E05 1080p Dublado")
        assert _p2p_safe_for_request(torrent, "series", 1, 5)

    def test_release_de_outro_episodio_e_omitido(self):
        torrent = _torrent(title="Serie S01E06 1080p Dublado")
        assert not _p2p_safe_for_request(torrent, "series", 1, 5)

    def test_fileidx_da_origem_torna_o_stream_inequivoco(self):
        assert _p2p_safe_for_request(_torrent(file_idx=7), "series", 1, 5)


class TestTrackers:
    def test_extrai_trackers_do_magnet(self):
        torrent = _torrent(
            magnet=(
                "magnet:?xt=urn:btih:" + "a" * 40
                + "&tr=udp%3A%2F%2Ftracker.example%3A80%2Fannounce"
                + "&tr=https%3A%2F%2Ftracker2.example%2Fannounce"
            )
        )
        assert _p2p_sources(torrent) == [
            "tracker:udp://tracker.example:80/announce",
            "tracker:https://tracker2.example/announce",
        ]

    def test_mescla_sources_da_origem_sem_duplicar(self):
        torrent = _torrent(
            sources=["tracker:udp://tracker.example:80/announce"],
            magnet=(
                "magnet:?xt=urn:btih:" + "a" * 40
                + "&tr=udp%3A%2F%2Ftracker.example%3A80%2Fannounce"
            ),
        )
        assert _p2p_sources(torrent) == ["tracker:udp://tracker.example:80/announce"]


class TestBrazucaPreservaContratoStremio:
    def test_fileidx_zero_e_sources_sao_preservados(self):
        scraper = BrazucaAddonScraper()
        torrent = scraper._parsear_stream(
            {
                "infoHash": "a" * 40,
                "fileIdx": 0,
                "sources": ["tracker:udp://tracker.example:80/announce"],
                "title": "Serie S01E05 1080p",
            }
        )
        assert torrent is not None
        assert torrent.file_idx == 0
        assert torrent.sources == ["tracker:udp://tracker.example:80/announce"]

    @pytest.mark.parametrize("invalid", [-1, True, "2"])
    def test_fileidx_invalido_e_ignorado(self, invalid):
        scraper = BrazucaAddonScraper()
        torrent = scraper._parsear_stream({"infoHash": "a" * 40, "fileIdx": invalid})
        assert torrent is not None
        assert torrent.file_idx is None


class TestFluxoDoAgregador:
    @staticmethod
    async def _run(torrent: TorrentResult, *, rd_token=None, include_p2p=False):
        aggregator = _aggregator()
        mock_cache = AsyncMock()
        mock_cache.get.return_value = None
        with patch("app.services.stream_aggregator.cache", mock_cache):
            with patch.object(
                aggregator, "_fetch_title", new=AsyncMock(return_value=("Serie", "Serie"))
            ):
                with patch.object(
                    aggregator,
                    "_run_scrapers",
                    new=AsyncMock(return_value=ScrapeOutcome(torrents=[torrent], ok_sources=1)),
                ):
                    return await aggregator.get_streams(
                        imdb_id="tt1234567",
                        type="series",
                        req_id="p2p-series",
                        rd_token=rd_token,
                        include_p2p=include_p2p,
                        request_base_url="https://addon.example",
                        season=1,
                        episode=5,
                    )

    @pytest.mark.asyncio
    async def test_sem_token_pacote_ambiguo_nao_e_oferecido(self):
        streams = await self._run(_torrent())
        assert streams == []

    @pytest.mark.asyncio
    async def test_hibrido_pacote_oferece_apenas_rd(self):
        streams = await self._run(_torrent(), rd_token="token", include_p2p=True)
        assert len(streams) == 1
        assert streams[0].url is not None
        assert streams[0].infoHash is None

    @pytest.mark.asyncio
    async def test_episodio_explicito_continua_p2p(self):
        streams = await self._run(_torrent(title="Serie S01E05 1080p"))
        assert len(streams) == 1
        assert streams[0].infoHash == "a" * 40

    @pytest.mark.asyncio
    async def test_fileidx_e_sources_chegam_ao_stream(self):
        torrent = _torrent(
            title="Serie sem marcador",
            file_idx=4,
            sources=["tracker:udp://tracker.example:80/announce"],
        )
        streams = await self._run(torrent)
        assert len(streams) == 1
        assert streams[0].fileIdx == 4
        assert streams[0].sources == ["tracker:udp://tracker.example:80/announce"]
