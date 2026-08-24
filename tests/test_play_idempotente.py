"""
/play idempotente: um clique não pode virar N torrents na conta do usuário.

Dois caminhos levavam ao mesmo estrago, e os dois estão cobertos aqui:

1. RETRY — `torrent_id` era variável **local** de `get_stream_url`. Cada
   tentativa após 503/504 recomeçava no `addMagnet`, e o Real-Debrid cria um
   torrent novo a cada chamada. Pior: o próprio 503 devolve `Retry-After`,
   convidando o cliente a repetir.

2. CORRIDA — HEAD e GET concorrentes (padrão comum de player, descrito na
   própria docstring de `/play`) liam `resolved_url` como ausente e
   executavam o fluxo inteiro os dois.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routes.stream import (
    PLAY_SESSION_TTL_SECONDS,
    _PlayLocks,
    _persistir_progresso,
    _ttl_restante_da_sessao,
)
from app.services.real_debrid import EstadoPlayback, RealDebridService


def _resp(json_data: dict) -> MagicMock:
    r = MagicMock()
    r.raise_for_status = MagicMock()
    r.json.return_value = json_data
    return r


def _servico_pronto() -> RealDebridService:
    """Serviço cujo fluxo RD conclui na primeira passada."""
    s = RealDebridService(api_token="t")
    arquivos = [{"id": 7, "path": "/f/filme.mkv", "bytes": 2_000_000_000}]
    s.client.get = AsyncMock(
        side_effect=[_resp({"files": arquivos}), _resp({"links": ["https://rd/l"]})]
    )
    s.client.post = AsyncMock(
        side_effect=[_resp({"id": "torrent-1"}), _resp({}), _resp({"download": "https://rd/final"})]
    )
    return s


class TestEstadoRetomavel:
    @pytest.mark.asyncio
    async def test_sem_estado_o_fluxo_e_o_de_sempre(self):
        """Retrocompatibilidade dos 16 pontos de chamada existentes."""
        s = _servico_pronto()
        url = await s.get_stream_url(magnet="magnet:?xt=urn:btih:" + "a" * 40, type="movie")
        assert url == "https://rd/final"
        assert s.client.post.await_count == 3   # addMagnet, selectFiles, unrestrict
        await s.close()

    @pytest.mark.asyncio
    async def test_estado_registra_o_torrent_e_o_arquivo(self):
        s = _servico_pronto()
        estado = EstadoPlayback()
        await s.get_stream_url(
            magnet="magnet:?xt=urn:btih:" + "a" * 40, type="movie", estado=estado
        )
        assert estado.rd_torrent_id == "torrent-1"
        assert estado.selected_file_id == "7"
        await s.close()

    @pytest.mark.asyncio
    async def test_retry_com_torrent_conhecido_nao_refaz_addmagnet(self):
        """O caso que multiplicava torrent na conta."""
        s = RealDebridService(api_token="t")
        s.client.get = AsyncMock(side_effect=[_resp({"links": ["https://rd/l"]})])
        s.client.post = AsyncMock(side_effect=[_resp({"download": "https://rd/final"})])

        estado = EstadoPlayback(rd_torrent_id="torrent-1", selected_file_id="7")
        url = await s.get_stream_url(
            magnet="magnet:?xt=urn:btih:" + "a" * 40, type="movie", estado=estado
        )

        assert url == "https://rd/final"
        # So o unrestrict. Nem addMagnet nem selectFiles.
        assert s.client.post.await_count == 1
        chamada = s.client.post.await_args_list[0]
        assert "unrestrict" in chamada.args[0]
        await s.close()

    @pytest.mark.asyncio
    async def test_tres_retries_geram_um_unico_addmagnet(self):
        """
        Simula o ciclo real: 503, 503, sucesso. Hoje seriam 3 torrents na
        conta do usuário.
        """
        estado = EstadoPlayback()
        add_magnets = 0

        for tentativa in range(3):
            s = RealDebridService(api_token="t")
            arquivos = [{"id": 7, "path": "/f/filme.mkv", "bytes": 2_000_000_000}]
            pronto = tentativa == 2
            gets = [_resp({"files": arquivos})] if not estado.selected_file_id else []
            gets += (
                [_resp({"links": ["https://rd/l"]})]
                if pronto
                else [_resp({"links": [], "status": "downloading"})] * 3
            )
            posts = []
            if not estado.rd_torrent_id:
                posts.append(_resp({"id": "torrent-1"}))
            if not estado.selected_file_id:
                posts.append(_resp({}))
            if pronto:
                posts.append(_resp({"download": "https://rd/final"}))

            s.client.get = AsyncMock(side_effect=gets)
            s.client.post = AsyncMock(side_effect=posts)

            with patch("app.services.real_debrid.asyncio.sleep", AsyncMock()):
                try:
                    await s.get_stream_url(
                        magnet="magnet:?xt=urn:btih:" + "a" * 40,
                        type="movie", estado=estado,
                    )
                except Exception:
                    pass

            add_magnets += sum(
                1 for c in s.client.post.await_args_list if "addMagnet" in c.args[0]
            )
            await s.close()

        assert add_magnets == 1, f"{add_magnets} torrents criados — deveria ser 1"
        assert estado.rd_torrent_id == "torrent-1"


class TestLockPorPlayId:
    @pytest.mark.asyncio
    async def test_concorrentes_no_mesmo_id_pegam_o_mesmo_lock(self):
        locks = _PlayLocks()
        a = await locks.obter("p1")
        b = await locks.obter("p1")
        assert a is b

    @pytest.mark.asyncio
    async def test_ids_diferentes_pegam_locks_diferentes(self):
        locks = _PlayLocks()
        a = await locks.obter("p1")
        b = await locks.obter("p2")
        assert a is not b

    @pytest.mark.asyncio
    async def test_serializa_de_verdade(self):
        locks = _PlayLocks()
        ordem = []

        async def tarefa(nome):
            lock = await locks.obter("p1")
            async with lock:
                ordem.append(f"{nome}-entra")
                await asyncio.sleep(0.01)
                ordem.append(f"{nome}-sai")

        await asyncio.gather(tarefa("HEAD"), tarefa("GET"))
        # Nao pode haver interleaving: cada par entra/sai fica junto.
        assert ordem[0].endswith("-entra") and ordem[1].endswith("-sai")
        assert ordem[2].endswith("-entra") and ordem[3].endswith("-sai")

    @pytest.mark.asyncio
    async def test_nao_cresce_sem_limite(self):
        """
        Cada busca com token RD cria uma play session POR TORRENT. Com dict
        comum, isso vazaria um lock por id para sempre.
        """
        import gc

        locks = _PlayLocks()
        for i in range(300):
            lock = await locks.obter(f"p{i}")
            async with lock:
                pass
            del lock
        gc.collect()
        assert len(locks._locks) == 0


class TestPersistenciaDoProgresso:
    @pytest.mark.asyncio
    async def test_grava_o_que_o_fluxo_conseguiu(self):
        sessao = {"rd_token": "t", "magnet": "m"}
        mock_cache = AsyncMock()
        estado = EstadoPlayback(rd_torrent_id="torrent-1", selected_file_id="7")

        with patch("app.routes.stream.cache", mock_cache):
            await _persistir_progresso("play:x", sessao, estado, "r1")

        mock_cache.set.assert_awaited_once()
        assert sessao["rd_torrent_id"] == "torrent-1"
        assert sessao["selected_file_id"] == "7"

    @pytest.mark.asyncio
    async def test_nao_grava_se_nada_avancou(self):
        mock_cache = AsyncMock()
        with patch("app.routes.stream.cache", mock_cache):
            await _persistir_progresso("play:x", {}, EstadoPlayback(), "r1")
        mock_cache.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nao_regrava_o_que_ja_estava_na_sessao(self):
        sessao = {"rd_torrent_id": "torrent-1", "selected_file_id": "7"}
        mock_cache = AsyncMock()
        estado = EstadoPlayback(rd_torrent_id="torrent-1", selected_file_id="7")

        with patch("app.routes.stream.cache", mock_cache):
            await _persistir_progresso("play:x", sessao, estado, "r1")

        mock_cache.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falha_ao_persistir_nao_propaga(self):
        from app.services.cache import CacheWriteError

        mock_cache = AsyncMock()
        mock_cache.set.side_effect = CacheWriteError("disco cheio")
        estado = EstadoPlayback(rd_torrent_id="torrent-1")

        with patch("app.routes.stream.cache", mock_cache):
            await _persistir_progresso("play:x", {}, estado, "r1")  # nao levanta


class TestTtlNaoEhRenovado:
    """
    `cache.set` faz INSERT OR REPLACE e regrava `created_at`. Gravar o
    checkpoint com o TTL cheio reiniciaria os 30 minutos — e um cliente
    insistindo em retry manteria a sessao, com o token RD em texto puro
    dentro dela, viva indefinidamente.
    """

    def test_sessao_recem_criada_mantem_o_ttl_cheio(self):
        import time

        ttl = _ttl_restante_da_sessao({"created_at": time.time()})
        assert ttl == PLAY_SESSION_TTL_SECONDS

    def test_sessao_antiga_recebe_apenas_o_que_resta(self):
        import time

        ttl = _ttl_restante_da_sessao({"created_at": time.time() - 1500})
        assert 290 <= ttl <= 310, ttl
        assert ttl < PLAY_SESSION_TTL_SECONDS

    def test_sessao_ja_expirada_nao_devolve_ttl_negativo(self):
        import time

        assert _ttl_restante_da_sessao({"created_at": time.time() - 9999}) == 1

    def test_sessao_sem_created_at_cai_no_ttl_cheio(self):
        assert _ttl_restante_da_sessao({}) == PLAY_SESSION_TTL_SECONDS
        assert _ttl_restante_da_sessao({"created_at": "invalido"}) == PLAY_SESSION_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_checkpoint_nao_estende_a_sessao(self):
        import time

        from app.services.real_debrid import EstadoPlayback

        sessao = {"created_at": time.time() - 1500, "rd_token": "t", "magnet": "m"}
        mock_cache = AsyncMock()
        with patch("app.routes.stream.cache", mock_cache):
            await _persistir_progresso(
                "play:x", sessao, EstadoPlayback(rd_torrent_id="t1"), "r1"
            )

        ttl = mock_cache.set.await_args.kwargs["ttl"]
        assert ttl < PLAY_SESSION_TTL_SECONDS, f"ttl={ttl} renovaria a sessao"
