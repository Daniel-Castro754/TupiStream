import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.manifest import get_manifest
from app.models.config import settings
from app.routes.configure import router as configure_router
from app.routes.stream import (
    SourceIdsPath,
    aggregator,
    parse_selected_sources,
    proteger_resposta_com_token,
)
from app.routes.stream import router as stream_router
from app.services.cache import cache

# Configura logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def _limpeza_periodica_do_cache() -> None:
    """
    Remove entradas expiradas em background.

    O TTL do cache é lógico: uma entrada expirada só era apagada quando
    alguém tentava lê-la, ou no shutdown. Play session criada e nunca
    clicada — o caso comum, já que cada busca cria uma por torrent e o
    usuário clica em uma — ficava no arquivo até o próximo desligamento
    gracioso. Se o processo morresse antes, ficava para sempre.
    """
    while True:
        await asyncio.sleep(settings.CACHE_CLEANUP_INTERVAL_SECONDS)
        try:
            await cache.delete_expired()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[JANITOR] Falha na limpeza periódica: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await cache.init()
    await aggregator.restore_health_from_cache()
    janitor = asyncio.create_task(_limpeza_periodica_do_cache())
    logger.info("=" * 50)
    logger.info("🇧🇷 Tupi Stream iniciado!")
    logger.info(f"📺 Configuração: {settings.BASE_URL}/configure")
    logger.info(f"📋 Manifest: {settings.BASE_URL}/manifest.json")
    logger.info(f"💾 Storage backend: {settings.STORAGE_BACKEND}")
    logger.info(f"⏱  Scraper timeout: {settings.SCRAPER_TIMEOUT_SECONDS}s")
    logger.info("=" * 50)
    try:
        yield
    finally:
        # Shutdown em `finally`, e cada recurso isolado: antes, uma falha ao
        # fechar o agregador impediria o fechamento do cache.
        #
        # aggregator.close() fecha o httpx.AsyncClient de cada scraper ativo.
        # Vem antes do cache porque os scrapers nao dependem dele aqui.
        janitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await janitor

        for rotulo, fechar in (
            ("aggregator.close", aggregator.close),
            ("cache.delete_expired", cache.delete_expired),
            ("cache.close", cache.close),
        ):
            try:
                await fechar()
            except Exception as e:
                logger.error(f"Falha em {rotulo} durante o shutdown: {e}")

        logger.info("Agregador e cache fechados.")


# Cria a aplicação FastAPI
app = FastAPI(title="Tupi Stream 🇧🇷", lifespan=lifespan)

# CORS liberado para todas as origens (necessário para Stremio web)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Inclui as rotas
app.include_router(configure_router)
app.include_router(stream_router)


@app.get("/")
async def root():
    """Redireciona para página de configuração"""
    return RedirectResponse(url="/configure")


@app.get("/health")
async def health():
    """Diagnóstico sem fazer novas requisições às fontes."""
    manifest = get_manifest()
    return {
        "status": "ok",
        "version": manifest["version"],
        "storage_backend": settings.STORAGE_BACKEND,
        "request_budget_seconds": settings.REQUEST_BUDGET_SECONDS,
        "scraper_timeout_seconds": settings.SCRAPER_TIMEOUT_SECONDS,
        "sources": aggregator.get_source_health(),
    }


@app.get("/sources/{source_ids}/manifest.json")
async def manifest_with_sources(source_ids: SourceIdsPath):
    """Manifest cuja URL transporta a seleção de fontes do usuário."""
    parse_selected_sources(source_ids)
    return get_manifest()


@app.get("/sources/{source_ids}/hybrid/{rd_token}/manifest.json")
async def manifest_hybrid_with_sources(
    source_ids: SourceIdsPath, rd_token: str, response: Response
):
    parse_selected_sources(source_ids)
    proteger_resposta_com_token(response)
    return get_manifest()


@app.get("/sources/{source_ids}/{rd_token}/manifest.json")
async def manifest_rd_with_sources(
    source_ids: SourceIdsPath, rd_token: str, response: Response
):
    parse_selected_sources(source_ids)
    proteger_resposta_com_token(response)
    return get_manifest()


@app.get("/manifest.json")
async def manifest():
    """Retorna o manifest do addon"""
    return get_manifest()


@app.get("/{rd_token}/manifest.json")
async def manifest_with_token(rd_token: str, response: Response):
    """Retorna manifest no modo Real-Debrid."""
    proteger_resposta_com_token(response)
    return get_manifest()


@app.get("/hybrid/{rd_token}/manifest.json")
async def manifest_hybrid(rd_token: str, response: Response):
    """Retorna manifest no modo híbrido: Real-Debrid + P2P."""
    proteger_resposta_com_token(response)
    return get_manifest()


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),  # uvicorn so aceita minusculo
        reload=True,
    )
