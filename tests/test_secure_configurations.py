"""Configuração privada: token fora da URL e das sessões de playback."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.torrent import TorrentResult
from app.services.cache import SQLiteCacheBackend
from app.services.configuration_store import (
    CONFIG_CACHE_PREFIX,
    ConfigurationCorruptError,
    ConfigurationStore,
    PrivateConfiguration,
)
from app.services.stream_aggregator import StreamAggregator

CONFIG_ID = "A" * 32
TOKEN = "token-real-debrid-super-secreto"


class TestConfigurationStore:
    @pytest.mark.asyncio
    async def test_grava_cifrado_e_recupera_payload(self, tmp_path, monkeypatch):
        backend = SQLiteCacheBackend(str(tmp_path / "cache.db"))
        await backend.init()
        monkeypatch.setattr(
            "app.services.configuration_store.settings.CONFIG_ENCRYPTION_KEY",
            Fernet.generate_key().decode(),
        )
        store = ConfigurationStore()
        try:
            with patch("app.services.configuration_store.cache", backend):
                config_id = await store.create(
                    rd_token=TOKEN,
                    include_p2p=True,
                    source_ids=("yts", "archive"),
                )
                raw = await backend.get(CONFIG_CACHE_PREFIX + config_id)
                loaded = await store.get(config_id)
        finally:
            await backend.close()

        assert len(config_id) == 32
        assert TOKEN not in json.dumps(raw)
        assert raw["ciphertext"]
        assert loaded == PrivateConfiguration(TOKEN, True, ("yts", "archive"))

    @pytest.mark.asyncio
    async def test_chave_diferente_nao_decifra(self, tmp_path, monkeypatch):
        backend = SQLiteCacheBackend(str(tmp_path / "cache.db"))
        await backend.init()
        store = ConfigurationStore()
        try:
            with patch("app.services.configuration_store.cache", backend):
                monkeypatch.setattr(
                    "app.services.configuration_store.settings.CONFIG_ENCRYPTION_KEY",
                    Fernet.generate_key().decode(),
                )
                config_id = await store.create(
                    rd_token=TOKEN,
                    include_p2p=False,
                    source_ids=("yts",),
                )
                monkeypatch.setattr(
                    "app.services.configuration_store.settings.CONFIG_ENCRYPTION_KEY",
                    Fernet.generate_key().decode(),
                )
                with pytest.raises(ConfigurationCorruptError):
                    await store.get(config_id)
        finally:
            await backend.close()


class TestConfigurationRoutes:
    @pytest.mark.asyncio
    async def test_post_retorna_id_sem_expor_token(self):
        with patch(
            "app.routes.configurations.configuration_store.create",
            AsyncMock(return_value=CONFIG_ID),
        ) as create:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/configurations",
                    json={
                        "rd_token": TOKEN,
                        "include_p2p": True,
                        "source_ids": ["yts", "archive"],
                    },
                )

        assert response.status_code == 201
        assert response.json()["manifest_url"] == (
            f"http://test/config/{CONFIG_ID}/manifest.json"
        )
        assert TOKEN not in response.text
        assert response.headers["cache-control"] == "no-store, private"
        assert create.await_args.kwargs["rd_token"] == TOKEN

    @pytest.mark.asyncio
    async def test_stream_configurado_repassa_referencia_sem_token_na_sessao(self):
        private = PrivateConfiguration(TOKEN, True, ("yts", "archive"))
        with (
            patch(
                "app.routes.configurations.configuration_store.get",
                AsyncMock(return_value=private),
            ),
            patch(
                "app.routes.stream.aggregator.get_streams",
                AsyncMock(return_value=[]),
            ) as get_streams,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    f"/config/{CONFIG_ID}/stream/movie/tt0133093.json"
                )

        assert response.status_code == 200
        kwargs = get_streams.await_args.kwargs
        assert kwargs["rd_token"] == TOKEN
        assert kwargs["rd_config_id"] == CONFIG_ID
        assert kwargs["include_p2p"] is True
        assert kwargs["selected_sources"] == frozenset({"yts", "archive"})


class TestSecurePlaySessions:
    @pytest.mark.asyncio
    async def test_nova_sessao_guarda_config_id_e_nao_token(self):
        torrent = TorrentResult(
            title="Matrix 1999 1080p",
            info_hash="a" * 40,
            magnet="magnet:?xt=urn:btih:" + "a" * 40,
            quality="1080p",
            dubbed=False,
            source="YTS",
        )
        with patch(
            "app.services.stream_aggregator._build_scraper_list", return_value=[]
        ):
            aggregator = StreamAggregator()
        aggregator._get_cached_torrents = AsyncMock(return_value=[torrent])

        with patch(
            "app.services.stream_aggregator.cache.set", AsyncMock()
        ) as cache_set:
            streams = await aggregator.get_streams(
                "tt0133093",
                "movie",
                "secure1",
                rd_token=TOKEN,
                rd_config_id=CONFIG_ID,
            )

        assert len(streams) == 1
        session = cache_set.await_args.args[1]
        assert session["rd_config_id"] == CONFIG_ID
        assert "rd_token" not in session
        assert TOKEN not in json.dumps(session)

    @pytest.mark.asyncio
    async def test_play_resolve_token_pelo_config_id(self):
        session = {
            "rd_config_id": CONFIG_ID,
            "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
            "type": "movie",
            "stremio_id": "tt0133093",
            "req_id": "secure2",
        }
        private = PrivateConfiguration(TOKEN, False, ("yts",))
        with (
            patch(
                "app.routes.stream.cache.get_with_status",
                AsyncMock(return_value=(session, "hit")),
            ),
            patch("app.routes.stream.cache.set", AsyncMock()),
            patch(
                "app.routes.stream.configuration_store.get",
                AsyncMock(return_value=private),
            ),
            patch("app.routes.stream.RealDebridService") as service_class,
        ):
            service = service_class.return_value
            service.get_stream_url = AsyncMock(return_value="https://cdn.example/video.mkv")
            service.close = AsyncMock()
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                response = await client.get("/play/secure-play")

        assert response.status_code == 302
        service_class.assert_called_once_with(
            TOKEN, req_id="secure2", play_ref="secure-p"
        )
