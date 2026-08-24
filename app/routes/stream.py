import asyncio
import logging
import time
import uuid
import weakref

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.models.config import settings
from app.scrapers.base import set_req_id
from app.services.cache import CacheWriteError, cache
from app.services.real_debrid import (
    EstadoPlayback,
    RealDebridPlaybackNotReadyError,
    RealDebridResolveError,
    RealDebridService,
    RealDebridTimeoutError,
)
from app.services.stream_aggregator import PLAY_SESSION_TTL_SECONDS, StreamAggregator

logger = logging.getLogger(__name__)
router = APIRouter()
PLAY_NOT_READY_RETRY_AFTER_SECONDS = 2
# Retry mais conservador no 504: enquanto o fluxo RD nao for idempotente
# (o retry recomeca no addMagnet), convidar a repetir rapido multiplica
# torrent na conta do usuario.
PLAY_TIMEOUT_RETRY_AFTER_SECONDS = 5
# resolved_url e guardada na mesma play session.
# Por isso o TTL precisa ficar alinhado ao TTL da sessao para nao encurta-la.
PLAY_RESOLVED_URL_TTL_SECONDS = PLAY_SESSION_TTL_SECONDS

# Instancia global do agregador.
aggregator = StreamAggregator()


class _PlayLocks:
    """
    Um lock por play_id, sem crescimento ilimitado.

    Sem lock, HEAD e GET concorrentes — padrao comum de player, e a propria
    docstring de /play descreve — leem `resolved_url` como ausente e executam
    o fluxo Real-Debrid os DOIS. Medido: addMagnet=2 sem lock, 1 com lock.

    WeakValueDictionary porque cada busca com token RD cria uma play session
    POR TORRENT: com dict comum, 5000 play_ids deixariam 5000 locks mortos na
    memoria. Medido: 0 entradas vivas apos 5000 ids.

    Contrato de uso: quem chama `obter()` PRECISA segurar a referencia numa
    variavel local durante todo o `async with`. E ela que mantem a entrada
    viva e garante que um concorrente pegue o MESMO objeto — soltar entre o
    lookup e o uso criaria um lock novo e quebraria a exclusao mutua.
    """

    def __init__(self) -> None:
        self._locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
            weakref.WeakValueDictionary()
        )
        self._guard = asyncio.Lock()

    async def obter(self, play_id: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(play_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[play_id] = lock
            return lock


_play_locks = _PlayLocks()


async def _persistir_progresso(
    play_key: str, session_data: dict, estado: EstadoPlayback, req_id: str
) -> None:
    """
    Grava na sessao o que o fluxo RD ja conseguiu, mesmo quando ele falha.

    E isto que torna o retry barato: sem o rd_torrent_id gravado, a proxima
    tentativa recomeca no addMagnet e o RD cria mais um torrent na conta.
    """
    if estado.rd_torrent_id is None:
        return
    if (
        session_data.get("rd_torrent_id") == estado.rd_torrent_id
        and session_data.get("selected_file_id") == estado.selected_file_id
    ):
        return

    session_data["rd_torrent_id"] = estado.rd_torrent_id
    session_data["selected_file_id"] = estado.selected_file_id
    try:
        await cache.set(play_key, session_data, ttl=PLAY_RESOLVED_URL_TTL_SECONDS)
        logger.info(
            f"[{req_id}] [PLAY] progresso salvo "
            f"(torrent={estado.rd_torrent_id}, arquivo={estado.selected_file_id})"
        )
    except CacheWriteError as exc:
        # Best-effort: o proximo retry so vai pagar o addMagnet de novo.
        logger.warning(f"[{req_id}] [PLAY] progresso nao persistido: {exc}")


def _play_ref(play_id: str) -> str:
    """Reduz o identificador nos logs para evitar ruido desnecessario."""
    return play_id[:8]


def _request_base_url(request: Request) -> str:
    """Deriva a base URL da request atual (scheme + host), sem trailing slash."""
    return str(request.base_url).rstrip("/")


def _parse_stremio_id(id: str) -> tuple[str, int | None, int | None]:
    """
    Extrai imdb_id, season e episode do id do Stremio.

    Formato de série: imdb_id:season:episode (ex: tt1234567:1:5).
    Formato de filme: só o imdb_id.

    Antes, o código só fazia id.split(":")[0], descartando season/episode
    por completo — todo pedido de série virava uma busca genérica pelo
    título do show, sem filtrar pelo episódio certo.
    """
    raw = id.replace(".json", "")
    partes = raw.split(":")
    imdb_id = partes[0]
    season: int | None = None
    episode: int | None = None
    if len(partes) >= 3:
        try:
            season = int(partes[1])
            episode = int(partes[2])
        except ValueError:
            season = None
            episode = None
    return imdb_id, season, episode


@router.get("/{rd_token}/stream/{type}/{id}.json")
async def get_streams_with_rd(rd_token: str, type: str, id: str, request: Request) -> dict:
    """
    Endpoint de streams com token RD no path.

    Compatibilidade atual:
      O token continua no path do manifest/stream. Este modulo nunca registra
      o token em logs manuais; o risco residual fica nos access logs da infra.
    """
    req_id = uuid.uuid4().hex[:8]
    set_req_id(req_id)
    t0 = time.monotonic()
    imdb_id, season, episode = _parse_stremio_id(id)
    token = rd_token if rd_token.lower() != "none" else None

    streams = await aggregator.get_streams(
        imdb_id=imdb_id,
        stremio_id=id.replace(".json", ""),
        type=type,
        req_id=req_id,
        rd_token=token,
        include_p2p=False,
        request_base_url=_request_base_url(request),
        season=season,
        episode=episode,
    )

    elapsed = (time.monotonic() - t0) * 1000
    logger.info(
        f"[{req_id}] [STREAM] {type}/{imdb_id} -> {len(streams)} resultados ({elapsed:.0f}ms)"
    )
    return {"streams": [stream.model_dump(exclude_none=True) for stream in streams]}


@router.get("/hybrid/{rd_token}/stream/{type}/{id}.json")
async def get_streams_hybrid(
    rd_token: str,
    type: str,
    id: str,
    request: Request,
) -> dict:
    """Endpoint híbrido: resultados Real-Debrid e P2P na mesma busca."""
    req_id = uuid.uuid4().hex[:8]
    set_req_id(req_id)
    t0 = time.monotonic()
    imdb_id, season, episode = _parse_stremio_id(id)
    token = rd_token if rd_token.lower() != "none" else None

    streams = await aggregator.get_streams(
        imdb_id=imdb_id,
        stremio_id=id.replace(".json", ""),
        type=type,
        req_id=req_id,
        rd_token=token,
        include_p2p=True,
        request_base_url=_request_base_url(request),
        season=season,
        episode=episode,
    )

    elapsed = (time.monotonic() - t0) * 1000
    logger.info(
        f"[{req_id}] [STREAM HYBRID] {type}/{imdb_id} -> "
        f"{len(streams)} resultados ({elapsed:.0f}ms)"
    )
    return {"streams": [stream.model_dump(exclude_none=True) for stream in streams]}


@router.get("/stream/{type}/{id}.json")
async def get_streams(type: str, id: str, request: Request) -> dict:
    """Endpoint de streams sem token RD."""
    req_id = uuid.uuid4().hex[:8]
    set_req_id(req_id)
    t0 = time.monotonic()
    imdb_id, season, episode = _parse_stremio_id(id)

    streams = await aggregator.get_streams(
        imdb_id=imdb_id,
        stremio_id=id.replace(".json", ""),
        type=type,
        req_id=req_id,
        rd_token=None,
        request_base_url=_request_base_url(request),
        season=season,
        episode=episode,
    )

    elapsed = (time.monotonic() - t0) * 1000
    logger.info(
        f"[{req_id}] [STREAM] {type}/{imdb_id} -> {len(streams)} resultados ({elapsed:.0f}ms)"
    )
    return {"streams": [stream.model_dump(exclude_none=True) for stream in streams]}


@router.api_route("/play/{play_id}", methods=["GET", "HEAD"])
async def play_stream(play_id: str, request: Request):
    """
    Serializa por play_id e delega.

    O lock precisa envolver ler-a-sessao / checar-resolved_url / resolver /
    gravar. Sem ele, HEAD e GET concorrentes leem `resolved_url` como ausente
    e executam o fluxo RD os dois — criando dois torrents para um clique.
    """
    # Referencia forte durante todo o `async with`: e ela que garante que o
    # concorrente pegue o MESMO lock (ver _PlayLocks).
    lock = await _play_locks.obter(play_id)
    async with lock:
        return await _resolver_play(play_id, request)


async def _resolver_play(play_id: str, request: Request):
    """
    Resolve a sessao de playback e redireciona para o link HTTP do RD.

    Aceita GET e HEAD:
      Alguns clientes (Stremio, players de video) fazem HEAD antes do GET
      para validar que a URL e alcancavel. Sem suporte a HEAD, o servidor
      retornava 405 e o cliente abortava com ERR_OPENING_MEDIA.

    Cache de URL resolvida:
      Apos resolver via RD, a URL e guardada na mesma play session.
      Como o cache reutiliza a mesma chave `play:{id}`, o TTL da resolved_url
      fica alinhado ao TTL da propria sessao para nao encurta-la.
      Se o cliente fizer HEAD seguido de GET (padrao comum), o segundo
      request reutiliza a URL ja resolvida sem repetir o fluxo RD.

    Fluxo atual:
      - usa a play session criada em /stream sem pre-checagem do RD
      - tenta o fluxo lazy addMagnet -> selectFiles -> info -> unrestrict/link
      - se o torrent ainda nao estiver pronto, faz retries curtos e retorna 503 temporario
      - continua multi-use com TTL curto

    Escolha de status:
      - sucesso continua em 302
      - "nao pronto" usa 503 + Retry-After, e nao 409, porque representa
        indisponibilidade temporaria e nao depende de suporte especifico do
        cliente a um codigo de conflito
      - falha operacional continua em 502
    """
    t0 = time.monotonic()
    method = request.method
    play_key = f"play:{play_id}"
    play_ref = _play_ref(play_id)
    session_data, session_status = await cache.get_with_status(play_key)
    if not session_data:
        if session_status == "expired":
            logger.warning(f"[PLAY] {method} 404 sessao expirada {play_ref} (TTL excedido)")
            raise HTTPException(
                status_code=404,
                detail="Sessao de playback expirada. Gere um novo stream.",
            )
        logger.warning(
            f"[PLAY] {method} 404 sessao inexistente {play_ref} "
            "(play_id invalido ou nunca criado)"
        )
        raise HTTPException(
            status_code=404,
            detail="Sessao de playback inexistente. Gere um novo stream.",
        )

    if not isinstance(session_data, dict):
        logger.error(
            f"[PLAY] {method} 500 sessao corrompida {play_ref} "
            f"(tipo={type(session_data).__name__})"
        )
        raise HTTPException(status_code=500, detail="Sessao de playback corrompida")

    req_id = session_data.get("req_id", play_ref)

    # Reutiliza URL ja resolvida se disponivel (evita re-resolve no HEAD+GET)
    cached_url = session_data.get("resolved_url")
    if cached_url:
        elapsed = (time.monotonic() - t0) * 1000
        logger.info(f"[{req_id}] [PLAY] {method} 302 (cached) {play_ref} ({elapsed:.0f}ms)")
        return RedirectResponse(url=cached_url, status_code=302)

    rd_token = session_data.get("rd_token")
    magnet = session_data.get("magnet")
    type_ = session_data.get("type", "movie")
    stremio_id = session_data.get("stremio_id", "")

    missing_fields = [
        field_name
        for field_name, value in {
            "rd_token": rd_token,
            "magnet": magnet,
        }.items()
        if not value
    ]
    if missing_fields:
        logger.error(
            f"[{req_id}] [PLAY] {method} 500 sessao corrompida {play_ref} "
            f"(faltando: {', '.join(missing_fields)})"
        )
        raise HTTPException(status_code=500, detail="Sessao de playback corrompida")

    logger.info(f"[{req_id}] [PLAY] {method} Inicio {play_ref}")

    rd = RealDebridService(rd_token, req_id=req_id, play_ref=play_ref)
    prazo = asyncio.get_running_loop().time() + settings.PLAYBACK_BUDGET_SECONDS
    # Retoma o que uma tentativa anterior ja tiver conseguido.
    estado = EstadoPlayback(
        rd_torrent_id=session_data.get("rd_torrent_id"),
        selected_file_id=session_data.get("selected_file_id"),
    )
    if estado.rd_torrent_id:
        logger.info(
            f"[{req_id}] [PLAY] {method} retomando {play_ref} "
            f"(torrent={estado.rd_torrent_id})"
        )
    try:
        try:
            stream_url = await rd.get_stream_url(
                magnet=magnet,
                type=type_,
                stremio_id=stremio_id,
                deadline=prazo,
                estado=estado,
            )
        except RealDebridTimeoutError as exc:
            # 504 e nao 502: o upstream nao respondeu a tempo, nao falhou.
            # 503 continua reservado para "torrent ainda nao pronto".
            elapsed = (time.monotonic() - t0) * 1000
            logger.error(
                f"[{req_id}] [PLAY] {method} 504 deadline de playback "
                f"({settings.PLAYBACK_BUDGET_SECONDS}s) {play_ref} ({elapsed:.0f}ms)"
            )
            raise HTTPException(
                status_code=504,
                detail="Tempo esgotado ao resolver o playback. Tente novamente.",
                headers={
                    "Retry-After": str(PLAY_TIMEOUT_RETRY_AFTER_SECONDS),
                    "Cache-Control": "no-store",
                },
            ) from exc
        except RealDebridPlaybackNotReadyError as exc:
            elapsed = (time.monotonic() - t0) * 1000
            logger.warning(
                f"[{req_id}] [PLAY] {method} 503 nao pronto {play_ref} ({elapsed:.0f}ms)"
            )
            raise HTTPException(
                status_code=503,
                detail=str(exc),
                headers={
                    "Retry-After": str(PLAY_NOT_READY_RETRY_AFTER_SECONDS),
                    "Cache-Control": "no-store",
                },
            ) from exc
        except RealDebridResolveError as exc:
            elapsed = (time.monotonic() - t0) * 1000
            logger.error(
                f"[{req_id}] [PLAY] {method} 502 falha operacional "
                f"{play_ref} ({elapsed:.0f}ms)"
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        # Guarda URL resolvida na session para reuso (HEAD+GET, retries).
        # Best-effort: se falhar, o redirect desta requisicao continua valido;
        # o proximo acesso apenas paga o fluxo RD de novo.
        session_data["resolved_url"] = stream_url
        session_data["rd_torrent_id"] = estado.rd_torrent_id
        session_data["selected_file_id"] = estado.selected_file_id
        try:
            await cache.set(play_key, session_data, ttl=PLAY_RESOLVED_URL_TTL_SECONDS)
        except CacheWriteError as exc:
            logger.warning(
                f"[{req_id}] [PLAY] {method} nao foi possivel cachear a URL "
                f"resolvida de {play_ref}: {exc}"
            )

        elapsed = (time.monotonic() - t0) * 1000
        logger.info(f"[{req_id}] [PLAY] {method} 302 redirect {play_ref} ({elapsed:.0f}ms)")
        return RedirectResponse(url=stream_url, status_code=302)
    finally:
        # Persiste o progresso ANTES de fechar, e em finally: o caminho que
        # mais importa e justamente o de falha (503/504), onde o retry vem
        # logo depois.
        await _persistir_progresso(play_key, session_data, estado, req_id)
        await rd.close()
