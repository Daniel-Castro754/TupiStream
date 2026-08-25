from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.real_debrid import (
    RealDebridPlaybackNotReadyError,
    RealDebridResolveError,
    RealDebridService,
)


def _resp(json_data: dict) -> MagicMock:
    resposta = MagicMock()
    resposta.raise_for_status = MagicMock()
    resposta.json.return_value = json_data
    return resposta


def _arquivo(id_: int, path: str, bytes_: int) -> dict:
    return {"id": id_, "path": path, "bytes": bytes_}


class TestSelecaoDeArquivo:
    """Cobre a lógica de negócio central: qual arquivo do torrent vira o
    stream — filtro de lixo (sample/trailer/legendas) e escolha por
    episódio (séries) ou maior arquivo (filmes)."""

    @pytest.mark.asyncio
    async def test_filme_escolhe_o_maior_arquivo_valido(self):
        service = RealDebridService(api_token="token-teste")
        arquivos = [
            _arquivo(1, "/Filme/sample.mp4", 1_000),
            _arquivo(2, "/Filme/filme.mp4", 2_000_000_000),
            _arquivo(3, "/Filme/trailer.mkv", 50_000_000),
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

        url = await service.get_stream_url(
            magnet="magnet:?xt=urn:btih:" + "a" * 40,
            type="movie",
        )

        assert url == "https://real-debrid.com/final"
        chamada_select = service.client.post.await_args_list[1]
        assert chamada_select.kwargs["data"] == {"files": "2"}
        await service.close()

    @pytest.mark.asyncio
    async def test_filtra_amostras_trailers_e_legendas(self):
        service = RealDebridService(api_token="token-teste")
        arquivos = [
            _arquivo(1, "/Filme/sample.mp4", 999_999_999),
            _arquivo(2, "/Filme/legenda.srt", 1_000),
            _arquivo(3, "/Filme/capa.jpg", 500),
            _arquivo(4, "/Filme/filme-real.mp4", 2_000_000_000),
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

        await service.get_stream_url(
            magnet="magnet:?xt=urn:btih:" + "a" * 40,
            type="movie",
        )

        chamada_select = service.client.post.await_args_list[1]
        assert chamada_select.kwargs["data"] == {"files": "4"}
        await service.close()

    @pytest.mark.asyncio
    async def test_serie_escolhe_s01e05_mesmo_nao_sendo_o_maior(self):
        service = RealDebridService(api_token="token-teste")
        arquivos = [
            _arquivo(1, "/Show/Show.S01E01.mp4", 1_500_000_000),
            _arquivo(2, "/Show/Show.S01E05.mp4", 900_000_000),
            _arquivo(3, "/Show/Show.S01E02.mp4", 1_600_000_000),
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

        await service.get_stream_url(
            magnet="magnet:?xt=urn:btih:" + "a" * 40,
            type="series",
            stremio_id="tt1234567:1:5",
        )

        chamada_select = service.client.post.await_args_list[1]
        assert chamada_select.kwargs["data"] == {"files": "2"}
        await service.close()

    @pytest.mark.asyncio
    async def test_serie_reconhece_formato_1x05(self):
        service = RealDebridService(api_token="token-teste")
        arquivos = [
            _arquivo(1, "/Show/Show.1x04.mkv", 1_500_000_000),
            _arquivo(2, "/Show/Show.1x05.mkv", 900_000_000),
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

        await service.get_stream_url(
            magnet="magnet:?xt=urn:btih:" + "a" * 40,
            type="series",
            stremio_id="tt1234567:1:5",
        )

        chamada_select = service.client.post.await_args_list[1]
        assert chamada_select.kwargs["data"] == {"files": "2"}
        await service.close()

    @pytest.mark.asyncio
    async def test_serie_sem_episodio_pedido_nao_abre_o_maior_arquivo(self):
        service = RealDebridService(api_token="token-teste")
        arquivos = [
            _arquivo(1, "/Show/Show.S01E01.mp4", 1_000_000_000),
            _arquivo(2, "/Show/Show.S01E02.mp4", 1_800_000_000),
        ]

        service.client.get = AsyncMock(side_effect=[_resp({"files": arquivos})])
        service.client.post = AsyncMock(side_effect=[_resp({"id": "torrent123"})])

        with pytest.raises(RealDebridPlaybackNotReadyError, match="S01E05"):
            await service.get_stream_url(
                magnet="magnet:?xt=urn:btih:" + "a" * 40,
                type="series",
                stremio_id="tt1234567:1:5",
            )

        assert service.client.post.await_count == 1
        await service.close()

    @pytest.mark.asyncio
    async def test_serie_com_um_unico_video_generico_e_aceita(self):
        service = RealDebridService(api_token="token-teste")
        arquivos = [_arquivo(7, "/Show/video-principal.mkv", 800_000_000)]

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

        await service.get_stream_url(
            magnet="magnet:?xt=urn:btih:" + "a" * 40,
            type="series",
            stremio_id="tt1234567:1:5",
        )

        chamada_select = service.client.post.await_args_list[1]
        assert chamada_select.kwargs["data"] == {"files": "7"}
        await service.close()

    @pytest.mark.asyncio
    async def test_serie_com_id_sem_temporada_e_episodio_falha_com_seguranca(self):
        service = RealDebridService(api_token="token-teste")
        arquivos = [_arquivo(1, "/Show/Show.S01E01.mp4", 1_000_000_000)]
        service.client.get = AsyncMock(side_effect=[_resp({"files": arquivos})])
        service.client.post = AsyncMock(side_effect=[_resp({"id": "torrent123"})])

        with pytest.raises(RealDebridPlaybackNotReadyError, match="identificar"):
            await service.get_stream_url(
                magnet="magnet:?xt=urn:btih:" + "a" * 40,
                type="series",
                stremio_id="tt1234567",
            )

        await service.close()

    @pytest.mark.asyncio
    async def test_todos_arquivos_invalidos_levanta_not_ready(self):
        service = RealDebridService(api_token="token-teste")
        arquivos = [
            _arquivo(1, "/Filme/sample.mp4", 1_000),
            _arquivo(2, "/Filme/legenda.srt", 100),
        ]

        service.client.get = AsyncMock(side_effect=[_resp({"files": arquivos})])
        service.client.post = AsyncMock(side_effect=[_resp({"id": "torrent123"})])

        with pytest.raises(RealDebridPlaybackNotReadyError):
            await service.get_stream_url(
                magnet="magnet:?xt=urn:btih:" + "a" * 40,
                type="movie",
            )

        await service.close()


class TestFluxoCompleto:
    @pytest.mark.asyncio
    async def test_falha_http_vira_resolve_error(self):
        service = RealDebridService(api_token="token-teste")

        request = httpx.Request(
            "POST",
            "https://api.real-debrid.com/rest/1.0/torrents/addMagnet",
        )
        response = httpx.Response(status_code=503, request=request)

        async def post_com_erro(*args, **kwargs):
            raise httpx.HTTPStatusError("erro", request=request, response=response)

        service.client.post = AsyncMock(side_effect=post_com_erro)

        with pytest.raises(RealDebridResolveError):
            await service.get_stream_url(
                magnet="magnet:?xt=urn:btih:" + "a" * 40,
                type="movie",
            )

        await service.close()

    @pytest.mark.asyncio
    async def test_wait_for_links_tenta_de_novo_ate_links_aparecerem(self):
        from unittest.mock import patch

        service = RealDebridService(api_token="token-teste")
        arquivos = [_arquivo(1, "/Filme/filme.mp4", 2_000_000_000)]

        service.client.get = AsyncMock(
            side_effect=[
                _resp({"files": arquivos}),
                _resp({"links": [], "status": "downloading"}),
                _resp({"links": ["https://rd/link"]}),
            ]
        )
        service.client.post = AsyncMock(
            side_effect=[
                _resp({"id": "torrent123"}),
                _resp({}),
                _resp({"download": "https://real-debrid.com/final"}),
            ]
        )

        with patch("app.services.real_debrid.asyncio.sleep", AsyncMock()):
            url = await service.get_stream_url(
                magnet="magnet:?xt=urn:btih:" + "a" * 40,
                type="movie",
            )

        assert url == "https://real-debrid.com/final"
        await service.close()


class TestCheckpointSelectFiles:
    """
    O estado retomável só pode pular uma etapa que terminou com sucesso.

    Antes, `selected_file_id` era preenchido antes do POST selectFiles. Um
    timeout/5xx era persistido no finally da rota como se a seleção estivesse
    concluída. Todos os retries pulavam selectFiles e ficavam em 503/504 até
    a sessão expirar.
    """

    @pytest.mark.asyncio
    async def test_falha_selectfiles_nao_marca_o_checkpoint(self):
        from app.services.real_debrid import EstadoPlayback

        service = RealDebridService(api_token="token-teste")
        estado = EstadoPlayback()
        arquivos = [_arquivo(7, "/Filme/filme.mkv", 2_000_000_000)]

        request = httpx.Request(
            "POST",
            "https://api.real-debrid.com/rest/1.0/torrents/selectFiles/torrent123",
        )
        response = httpx.Response(status_code=503, request=request)
        select_com_erro = _resp({})
        select_com_erro.raise_for_status.side_effect = httpx.HTTPStatusError(
            "indisponivel", request=request, response=response
        )

        service.client.get = AsyncMock(return_value=_resp({"files": arquivos}))
        service.client.post = AsyncMock(
            side_effect=[_resp({"id": "torrent123"}), select_com_erro]
        )

        with pytest.raises(RealDebridResolveError):
            await service.get_stream_url(
                magnet="magnet:?xt=urn:btih:" + "a" * 40,
                type="movie",
                estado=estado,
            )

        assert estado.rd_torrent_id == "torrent123", "addMagnet confirmado e reutilizavel"
        assert estado.selected_file_id is None, (
            "selectFiles falhou — o retry precisa repetir esta etapa"
        )
        await service.close()

    @pytest.mark.asyncio
    async def test_retry_reutiliza_torrent_mas_repete_selectfiles(self):
        from app.services.real_debrid import EstadoPlayback

        estado = EstadoPlayback(rd_torrent_id="torrent123", selected_file_id=None)
        service = RealDebridService(api_token="token-teste")
        arquivos = [_arquivo(7, "/Filme/filme.mkv", 2_000_000_000)]

        service.client.get = AsyncMock(
            side_effect=[
                _resp({"files": arquivos}),
                _resp({"links": ["https://rd/link"]}),
            ]
        )
        service.client.post = AsyncMock(
            side_effect=[
                _resp({}),
                _resp({"download": "https://real-debrid.com/final"}),
            ]
        )

        url = await service.get_stream_url(
            magnet="magnet:?xt=urn:btih:" + "a" * 40,
            type="movie",
            estado=estado,
        )

        assert url == "https://real-debrid.com/final"
        assert estado.rd_torrent_id == "torrent123"
        assert estado.selected_file_id == "7"

        post_urls = [call.args[0] for call in service.client.post.await_args_list]
        assert not any("addMagnet" in url for url in post_urls)
        assert sum("selectFiles" in url for url in post_urls) == 1
        assert sum("unrestrict" in url for url in post_urls) == 1
        await service.close()

    @pytest.mark.asyncio
    async def test_selectfiles_confirmado_continua_pulando_no_retry(self):
        from app.services.real_debrid import EstadoPlayback

        estado = EstadoPlayback(rd_torrent_id="torrent123", selected_file_id="7")
        service = RealDebridService(api_token="token-teste")
        service.client.get = AsyncMock(return_value=_resp({"links": ["https://rd/link"]}))
        service.client.post = AsyncMock(
            return_value=_resp({"download": "https://real-debrid.com/final"})
        )

        await service.get_stream_url(
            magnet="magnet:?xt=urn:btih:" + "a" * 40,
            type="movie",
            estado=estado,
        )

        post_urls = [call.args[0] for call in service.client.post.await_args_list]
        assert not any("addMagnet" in url for url in post_urls)
        assert not any("selectFiles" in url for url in post_urls)
        assert sum("unrestrict" in url for url in post_urls) == 1
        await service.close()
