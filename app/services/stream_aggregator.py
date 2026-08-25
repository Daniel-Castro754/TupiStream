import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from app.episode_matching import matches_explicit_episode
from app.models.config import settings
from app.models.torrent import StreamResult, TorrentResult
from app.scrapers.apache_torrent import ApacheTorrentScraper
from app.scrapers.archive_org import ArchiveOrgScraper
from app.scrapers.base import BaseScraper, set_req_id
from app.scrapers.brazuca_addon import BrazucaAddonScraper
from app.scrapers.comando_filmes import ComandoFilmesScraper
from app.scrapers.hdr_torrent import HDRTorrentScraper
from app.scrapers.micoleao import MicoLeaoScraper
from app.scrapers.rutracker import RuTrackerScraper
from app.scrapers.torrent_1337x import Torrent1337xScraper
from app.scrapers.torrent_galaxy import TorrentGalaxyScraper
from app.scrapers.yts import YTSScraper
from app.services.cache import CacheWriteError, cache
from app.services.labeler import build_stream_name, build_stream_title

logger = logging.getLogger(__name__)

# Ordem de prioridade das qualidades (menor = melhor)
QUALITY_ORDER: dict[str, int] = {
    "4K": 0,
    "4K HDR": 0,
    "4K DolbyVision": 0,
    "2160p": 0,
    "1080p": 1,
    "720p": 2,
    "480p": 3,
    "Desconhecida": 4,
}

MAX_P2P_SOURCES = 30


def _p2p_sources(torrent: TorrentResult) -> list[str]:
    """Preserva sources da origem e trackers `tr=` do magnet, sem duplicatas."""
    candidates = list(torrent.sources)
    try:
        trackers = parse_qs(urlsplit(torrent.magnet).query).get("tr", [])
    except ValueError:
        trackers = []
    candidates.extend(f"tracker:{tracker}" for tracker in trackers)

    result: list[str] = []
    for source in candidates:
        if not isinstance(source, str):
            continue
        if not source.startswith(("tracker:http://", "tracker:https://", "tracker:udp://", "dht:")):
            continue
        if source not in result:
            result.append(source)
        if len(result) >= MAX_P2P_SOURCES:
            break
    return result


def _p2p_safe_for_request(
    torrent: TorrentResult,
    content_type: str,
    season: int | None,
    episode: int | None,
) -> bool:
    """
    Evita servir o episódio errado no modo P2P.

    Sem `fileIdx`, a especificação do Stremio escolhe o MAIOR arquivo do
    torrent. Isso é correto para release de episódio único e incorreto para
    pacote de temporada. RD pode selecionar o arquivo; P2P não pode.
    """
    if content_type != "series":
        return True
    if torrent.file_idx is not None:
        return True
    if season is None or episode is None:
        return False
    return matches_explicit_episode(torrent.title, season, episode)


# ── Scraper Registry ──
SCRAPER_REGISTRY: list[tuple[str, type[BaseScraper]]] = [
    ("ENABLE_APACHE_TORRENT", ApacheTorrentScraper),
    ("ENABLE_COMANDO_FILMES", ComandoFilmesScraper),
    ("ENABLE_HDR_TORRENT",    HDRTorrentScraper),
    ("ENABLE_MICOLEAO",       MicoLeaoScraper),
    ("ENABLE_BRAZUCA",        BrazucaAddonScraper),
    ("ENABLE_YTS",            YTSScraper),
    ("ENABLE_ARCHIVE_ORG",    ArchiveOrgScraper),
    ("ENABLE_TORRENT_GALAXY", TorrentGalaxyScraper),
    ("ENABLE_1337X",          Torrent1337xScraper),
    ("ENABLE_RUTRACKER",      RuTrackerScraper),
]

# ── Circuit breaker por fonte ──
# Depois de N falhas seguidas (erro/indisponível — não conta "vazio", que é
# uma busca normal sem resultado), a fonte fica em cooldown: os próximos
# requests pulam ela direto em vez de pagar o timeout inteiro de novo.
# Sem isso, uma fonte fora do ar consome ~SCRAPER_TIMEOUT_SECONDS do budget
# de TODO request, atrasando as fontes saudáveis por nada.
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 3
CIRCUIT_BREAKER_COOLDOWN_SECONDS = 300  # 5 min — reavalia periodicamente

# ── Persistência da telemetria de saúde ──
# consecutive_failures/status/skip_until viviam só em memória — zeravam a
# cada restart do container (comum em Railway/Render), perdendo o estado
# "essa fonte tá quebrada" bem na hora que mais serviria. Persistimos um
# snapshot no cache já existente (SQLite/Redis) e recarregamos no startup.
HEALTH_CACHE_KEY = "source_health:v1"
HEALTH_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 dias — é só telemetria/diagnóstico


@dataclass(frozen=True)
class ScrapeOutcome:
    """
    Resultado de uma rodada de scrapers, com o diagnostico que o chamador
    precisa para decidir se aquele resultado pode ir para o cache.

    Antes `_run_scrapers` devolvia so `list[TorrentResult]`, e uma lista
    vazia era ambigua — podia significar qualquer um destes:

      1. as fontes responderam bem e o conteudo nao existe nelas;
      2. todas as fontes falharam (erro, timeout, bloqueio anti-bot);
      3. todas estavam em cooldown do circuit breaker;
      4. o budget acabou antes de chegar a roda-las.

    `get_streams` gravava `[]` no cache com o TTL padrao nos quatro casos.
    Ou seja: uma indisponibilidade temporaria — ou uma unica requisicao
    lenta que estourou o budget — congelava o conteudo como "nao existe"
    por uma hora inteira.

    So o caso 1 e informacao. Os outros tres sao ausencia de informacao.
    """

    torrents: list[TorrentResult] = field(default_factory=list)
    ok_sources: int = 0        # responderam sem erro (com ou sem resultado)
    failed_sources: int = 0    # erro, timeout ou indisponivel
    skipped_sources: int = 0   # circuit breaker aberto, ou nao usa texto
    ran: bool = True           # False = nem chegou a executar

    @property
    def confiavel(self) -> bool:
        """Vazio so e informacao se alguma fonte respondeu bem."""
        return self.ran and self.ok_sources > 0

    def combinar(self, outra: "ScrapeOutcome", torrents: list[TorrentResult]) -> "ScrapeOutcome":
        """Soma o diagnostico de duas rodadas sobre uma lista ja deduplicada."""
        return ScrapeOutcome(
            torrents=torrents,
            ok_sources=self.ok_sources + outra.ok_sources,
            failed_sources=self.failed_sources + outra.failed_sources,
            skipped_sources=self.skipped_sources + outra.skipped_sources,
            ran=self.ran or outra.ran,
        )


def _binge_group(torrent: TorrentResult, canal: str) -> str:
    """
    Grupo de binge estavel entre episodios.

    Era `f"rd-{info_hash[:8]}"` — unico por torrent, entao NUNCA casava com
    o episodio seguinte e o proposito do campo simplesmente nao acontecia.
    O bingeGroup existe para o Stremio reusar o MESMO tipo de stream ao
    avancar na serie; se o identificador muda a cada episodio, nao ha o que
    reusar.

    Qualidade e idioma sao o que o usuario quer manter constante ao
    maratonar, e sao estaveis entre episodios.
    """
    idioma = "ptbr" if torrent.dubbed else "orig"
    qualidade = torrent.quality.lower().replace(" ", "") or "desconhecida"
    return f"brstreams-{canal}-{qualidade}-{idioma}"


def _build_scraper_list() -> list[BaseScraper]:
    """Instancia APENAS os scrapers habilitados por feature flag."""
    active: list[BaseScraper] = []
    for flag_name, scraper_cls in SCRAPER_REGISTRY:
        enabled = getattr(settings, flag_name, False)
        if enabled:
            scraper = scraper_cls()
            active.append(scraper)
            logger.info(f"[REGISTRY] ✅ {scraper.name} ({scraper.stability})")
        else:
            logger.info(f"[REGISTRY] ❌ {scraper_cls.__name__} desativado ({flag_name}=false)")
    return active


def _quality_score(quality: str) -> int:
    return QUALITY_ORDER.get(quality, 4)


def _is_confirmed_dead(torrent: TorrentResult) -> bool:
    """
    True apenas quando a fonte CONFIRMOU 0 seeders (ex: API da YTS).
    seeders=None significa "fonte não informa contagem" (a maioria dos
    scrapers via scraping de página) — isso não é sinal de torrent morto,
    então não pode ser penalizado como se fosse.
    """
    return torrent.seeders == 0


def _is_cross_verified(torrent: TorrentResult) -> bool:
    """True quando o mesmo hash foi encontrado em 2+ fontes na deduplicação."""
    return " + " in torrent.source


def _sort_streams(results: list[tuple[TorrentResult, bool]]) -> list[tuple[TorrentResult, bool]]:
    """
    Ordena por: elegibilidade RD > vivo antes de morto > qualidade > dublado
    > confirmado em múltiplas fontes > seeders.

    O critério "vivo antes de morto" vem antes da qualidade de propósito:
    um torrent 4K com 0 seeders confirmados não vai baixar nada — não faz
    sentido ele aparecer acima de um 1080p saudável só pela qualidade.
    """
    return sorted(results, key=lambda item: (
        not item[1],
        _is_confirmed_dead(item[0]),
        _quality_score(item[0].quality),
        not item[0].dubbed,
        not _is_cross_verified(item[0]),
        -(item[0].seeders or 0),
    ))


# Budget mínimo para cada etapa, em segundos.
# Se o budget restante for menor que estes valores, a etapa é ignorada.
MIN_BUDGET_TITLE_FETCH = 2.0   # fetch de título Cinemeta/TMDB
MAX_BUDGET_TITLE_FETCH = 4.0   # teto do fetch de título (não consome mais que isso)
MIN_BUDGET_SCRAPERS = 1.0      # rodada de scrapers
PLAY_SESSION_TTL_SECONDS = 1800  # 30 min — cobre delay, reload e navegacao entre conteudos
STREAM_CACHE_VERSION = "v2"  # invalida caches anteriores ao filtro de relevância


class SearchBusyError(RuntimeError):
    """Todos os slots globais de busca estão ocupados."""


class StreamAggregator:
    """Agrega resultados de múltiplos scrapers e integra com Real-Debrid"""

    def __init__(self) -> None:
        self.scrapers: list[BaseScraper] = _build_scraper_list()
        self._inflight: dict[str, asyncio.Task[list[TorrentResult]]] = {}
        self._inflight_guard = asyncio.Lock()
        self._search_slots = asyncio.Semaphore(max(1, settings.MAX_CONCURRENT_SEARCHES))
        self.source_health: dict[str, dict] = {
            scraper.name: {
                "status": "not_checked",
                "last_count": None,
                "last_error": None,
                "last_elapsed_ms": None,
                "last_checked_at": None,
                "consecutive_failures": 0,
                "skip_until": None,
            }
            for scraper in self.scrapers
        }

    async def restore_health_from_cache(self) -> None:
        """
        Recarrega o snapshot de saúde salvo antes do último restart.

        skip_until é armazenado como timestamp monotonic (sem época fixa,
        só vale dentro do processo que o criou) — por isso persistimos e
        recarregamos como skip_until_wall (time.time(), com época real) e
        convertemos de volta pra um novo valor monotonic ancorado no
        processo atual, com o cooldown restante recalculado.
        """
        try:
            salvo = await cache.get(HEALTH_CACHE_KEY)
        except Exception as e:
            logger.warning(f"[HEALTH] Falha ao carregar telemetria persistida: {e}")
            return

        if not isinstance(salvo, dict):
            return

        agora_wall = time.time()
        restaurados = 0
        for name, entry in salvo.items():
            if name not in self.source_health or not isinstance(entry, dict):
                continue

            health = self.source_health[name]
            health.update(
                status=entry.get("status", health.get("status")),
                last_count=entry.get("last_count"),
                last_error=entry.get("last_error"),
                last_elapsed_ms=entry.get("last_elapsed_ms"),
                last_checked_at=entry.get("last_checked_at"),
                consecutive_failures=entry.get("consecutive_failures", 0),
            )

            skip_until_wall = entry.get("skip_until_wall")
            if skip_until_wall:
                restante = skip_until_wall - agora_wall
                if restante > 0:
                    health["skip_until"] = time.monotonic() + restante
                    logger.info(
                        f"[HEALTH] {name} ainda em cooldown após restart "
                        f"({restante:.0f}s restantes)"
                    )
            restaurados += 1

        if restaurados:
            logger.info(f"[HEALTH] Telemetria restaurada para {restaurados} fonte(s)")

    async def _persist_health(self) -> None:
        """Salva um snapshot de source_health no cache pra sobreviver a restart."""
        agora_monotonic = time.monotonic()
        agora_wall = time.time()
        snapshot: dict[str, dict] = {}

        for name, health in self.source_health.items():
            entry = dict(health)
            skip_until = entry.pop("skip_until", None)
            entry["skip_until_wall"] = (
                agora_wall + (skip_until - agora_monotonic) if skip_until else None
            )
            snapshot[name] = entry

        try:
            await cache.set(HEALTH_CACHE_KEY, snapshot, ttl=HEALTH_CACHE_TTL_SECONDS)
        except Exception as e:
            logger.warning(f"[HEALTH] Falha ao persistir telemetria: {e}")

    def _budget_remaining(self, t_start: float) -> float:
        elapsed = time.monotonic() - t_start
        return max(0.0, settings.REQUEST_BUDGET_SECONDS - elapsed)

    def _budget_para_scrapers(self, t_start: float) -> float:
        """
        Budget restante menos a margem reservada para fechar a resposta.

        Depois dos scrapers ainda e preciso ordenar, serializar os
        StreamResult e gravar as play sessions. Sem reservar nada, os
        scrapers podiam consumir ate o ultimo milissegundo do budget.
        """
        return max(0.0, self._budget_remaining(t_start) - settings.BUDGET_RESERVE_SECONDS)

    async def _fetch_title(
        self, imdb_id: str, type: str, req_id: str, budget: float
    ) -> tuple[str, str]:
        """
        Busca título original e PT-BR usando fontes gratuitas.
        Prioridade:
          1. Se TMDB_API_KEY configurada → TMDB (título PT-BR confiável)
          2. Cinemeta (meta.name — pode vir em PT-BR para conteúdo brasileiro)
          3. OMDB (fallback — título original na maioria dos casos)
        Budget-aware: usa min(budget, MAX_BUDGET_TITLE_FETCH) como timeout.
        Se budget < MIN_BUDGET_TITLE_FETCH, retorna fallback imediatamente.
        """
        import httpx

        titulo_original = imdb_id
        titulo_ptbr = imdb_id

        if budget < MIN_BUDGET_TITLE_FETCH:
            logger.warning(
                f"[{req_id}] [_fetch_title] Budget insuficiente ({budget:.1f}s) — usando fallback"
            )
            return titulo_original, titulo_ptbr

        timeout = min(budget, MAX_BUDGET_TITLE_FETCH)

        # `httpx.AsyncClient(timeout=X)` aplica X POR OPERACAO, nao ao bloco.
        # Como as chamadas aqui sao sequenciais — 2x Cinemeta + TMDB + OMDb —
        # o pior caso era 4 x 4,0s = 16s numa etapa documentada como tendo
        # teto de 4s, dentro de um REQUEST_BUDGET_SECONDS de 12s. Medido:
        # 4 chamadas travadas levavam 1,63s com teto de 0,4s cada, e 0,40s
        # sob timeout_at. O timeout do cliente continua valendo como teto por
        # operacao; o timeout_at e o teto agregado.
        prazo = asyncio.get_running_loop().time() + timeout

        try:
            async with (
                asyncio.timeout_at(prazo),
                httpx.AsyncClient(timeout=timeout) as client,
            ):
                # Cinemeta — título principal (pode vir em PT-BR)
                # dict.fromkeys remove duplicata preservando a ordem: com
                # type="movie" a lista era ["movie", "movie", "series"] e a
                # MESMA URL era pedida duas vezes se a primeira falhasse —
                # uma ida de rede jogada fora dentro de um budget de 4s.
                for content_type in dict.fromkeys([type, "movie", "series"]):
                    r = await client.get(
                        f"https://v3-cinemeta.strem.io/meta/{content_type}/{imdb_id}.json"
                    )
                    if r.status_code == 200:
                        data = r.json()
                        nome = data.get("meta", {}).get("name", "")
                        if nome:
                            titulo_original = nome
                            titulo_ptbr = nome
                            break

                # TMDB — título PT-BR (só se API key configurada)
                if settings.TMDB_API_KEY:
                    try:
                        r_tmdb = await client.get(
                            f"https://api.themoviedb.org/3/find/{imdb_id}"
                            f"?external_source=imdb_id&language=pt-BR"
                            f"&api_key={settings.TMDB_API_KEY}"
                        )
                        if r_tmdb.status_code == 200:
                            data_tmdb = r_tmdb.json()
                            resultados = (
                                data_tmdb.get("movie_results")
                                or data_tmdb.get("tv_results")
                                or []
                            )
                            if resultados:
                                nome_ptbr = (
                                    resultados[0].get("title")
                                    or resultados[0].get("name")
                                    or ""
                                )
                                if nome_ptbr:
                                    titulo_ptbr = nome_ptbr
                        elif r_tmdb.status_code == 401:
                            logger.warning(
                                f"[{req_id}] [_fetch_title] TMDB 401 — API key inválida, ignorando"
                            )
                    except Exception as e:
                        logger.warning(f"[{req_id}] [_fetch_title] TMDB falhou: {e}")

                # OMDB — fallback para título (API key pública)
                if titulo_ptbr == imdb_id or titulo_ptbr == titulo_original:
                    try:
                        r_omdb = await client.get(
                            f"https://omdbapi.com/?i={imdb_id}&apikey=trilogy"
                        )
                        if r_omdb.status_code == 200:
                            data_omdb = r_omdb.json()
                            nome_omdb = data_omdb.get("Title", "")
                            if nome_omdb:
                                # OMDB retorna título original na maioria dos casos
                                if titulo_original == imdb_id:
                                    titulo_original = nome_omdb
                                # Se ainda não temos PT-BR diferente, usa o do OMDB
                                if titulo_ptbr == imdb_id:
                                    titulo_ptbr = nome_omdb
                    except Exception as e:
                        logger.warning(f"[{req_id}] [_fetch_title] OMDB falhou: {e}")

        except TimeoutError:
            # Parcial sobrevive: o `return` fica FORA do bloco e as variaveis
            # ja atribuidas permanecem. Se o Cinemeta respondeu antes de o
            # OMDb travar, o titulo dele chega ao chamador.
            logger.warning(
                f"[{req_id}] [_fetch_title] deadline de {timeout:.1f}s esgotado "
                f"— seguindo com original={titulo_original!r}"
            )
        except Exception as e:
            logger.warning(f"[{req_id}] [_fetch_title] Erro ({timeout:.1f}s timeout): {e}")
            titulo_ptbr = titulo_original

        # Garante que nunca retorna imdb_id como título se temos algo melhor
        if titulo_ptbr == imdb_id and titulo_original != imdb_id:
            titulo_ptbr = titulo_original

        logger.info(
            f"[{req_id}] Títulos: original='{titulo_original}' ptbr='{titulo_ptbr}'"
        )
        return titulo_original, titulo_ptbr

    async def _singleflight(
        self,
        cache_key: str,
        factory: Callable[[], Awaitable[list[TorrentResult]]],
    ) -> list[TorrentResult]:
        """
        Compartilha uma única Task entre requests simultâneos da mesma chave.

        Um lock + rechecagem de cache não basta: quando o resultado não pode
        ser cacheado (falha total das fontes), cada waiter faria a mesma busca
        em sequência. A Task compartilha inclusive resultado vazio/erro.

        `shield` impede que o cancelamento de um cliente cancele a busca que
        outros clientes aguardam. A Task continua e pode preencher o cache.
        """
        async with self._inflight_guard:
            task = self._inflight.get(cache_key)
            if task is None:
                task = asyncio.create_task(factory())
                self._inflight[cache_key] = task
                logger.info(f"[SINGLEFLIGHT] nova busca compartilhada: {cache_key}")
            else:
                logger.info(f"[SINGLEFLIGHT] aguardando busca existente: {cache_key}")

        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._inflight_guard:
                    if self._inflight.get(cache_key) is task:
                        self._inflight.pop(cache_key, None)

    async def _acquire_search_slot(self, req_id: str) -> None:
        try:
            await asyncio.wait_for(
                self._search_slots.acquire(),
                timeout=settings.SEARCH_QUEUE_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            logger.warning(
                f"[{req_id}] [CAPACITY] todos os "
                f"{settings.MAX_CONCURRENT_SEARCHES} slots de busca ocupados"
            )
            raise SearchBusyError("Capacidade de busca temporariamente esgotada") from exc

    async def _search_and_cache(
        self,
        *,
        cache_key: str,
        imdb_id: str,
        type: str,
        req_id: str,
        t_start: float,
        season: int | None,
        episode: int | None,
    ) -> list[TorrentResult]:
        await self._acquire_search_slot(req_id)
        try:
            # Uma Task pode ter aguardado o semáforo enquanto outra instância
            # preenchia o cache. Rechecagem barata antes do fan-out externo.
            cached = await self._get_cached_torrents(cache_key, req_id)
            if cached is not None:
                return cached

            remaining = self._budget_remaining(t_start)
            titulo_original, titulo_ptbr = await self._fetch_title(
                imdb_id, type, req_id, remaining
            )

            titulo_resolvido = titulo_original != imdb_id or titulo_ptbr != imdb_id
            if not titulo_resolvido:
                logger.warning(
                    f"[{req_id}] Titulo nao resolvido — buscando pelo imdb_id. "
                    "Vazio resultante nao sera cacheado."
                )

            remaining = self._budget_para_scrapers(t_start)
            if remaining > MIN_BUDGET_SCRAPERS:
                resultado = await self._run_scrapers(
                    titulo_ptbr,
                    imdb_id,
                    type,
                    req_id,
                    "ptbr",
                    remaining,
                    season=season,
                    episode=episode,
                )
            else:
                logger.warning(
                    f"[{req_id}] Budget esgotado antes dos scrapers ({remaining:.1f}s)"
                )
                resultado = ScrapeOutcome(ran=False)

            remaining = self._budget_para_scrapers(t_start)
            if (
                len(resultado.torrents) < 3
                and titulo_original != titulo_ptbr
                and remaining > MIN_BUDGET_SCRAPERS
            ):
                logger.info(
                    f"[{req_id}] Poucos resultados ({len(resultado.torrents)}), "
                    f"segunda rodada (budget={remaining:.1f}s)..."
                )
                extras = await self._run_scrapers(
                    titulo_original,
                    imdb_id,
                    type,
                    req_id,
                    "original",
                    remaining,
                    season=season,
                    episode=episode,
                    skip_query_independent=True,
                )
                resultado = resultado.combinar(
                    extras,
                    self._deduplicate(resultado.torrents + extras.torrents),
                )
            elif len(resultado.torrents) < 3 and titulo_original != titulo_ptbr:
                logger.warning(
                    f"[{req_id}] Budget insuficiente para segunda rodada ({remaining:.1f}s)"
                )

            await self._cachear_busca(
                cache_key,
                resultado,
                req_id,
                permitir_cache_negativo=titulo_resolvido,
            )
            return resultado.torrents
        finally:
            self._search_slots.release()

    async def get_streams(
        self,
        imdb_id: str,
        type: str,
        req_id: str,
        stremio_id: str | None = None,
        rd_token: str | None = None,
        include_p2p: bool = False,
        request_base_url: str | None = None,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[StreamResult]:
        """
        Busca streams em todos os scrapers e formata para o Stremio.

        Budget total real — cada etapa consulta o budget restante:
          1. _fetch_title: limitado a MAX_BUDGET_TITLE_FETCH (4s)
          2. scrapers ptbr: budget restante
          3. scrapers original: só se budget > MIN_BUDGET_SCRAPERS

        season/episode:
          Para séries, identificam o episódio pedido (extraídos do id do
          Stremio, formato imdb:season:episode). Usados para: (1) compor a
          chave de cache por episódio — sem isso, todo episódio de uma
          série reaproveitava o cache de qualquer outro episódio já
          buscado; e (2) repassar aos scrapers para busca/filtragem
          específica de episódio.

        Real-Debrid:
          - não há mais pré-checagem via instantAvailability
          - se houver token RD e magnet válido, o addon cria play session
            e delega a resolução real para /play no clique
          - include_p2p=True mantém também uma opção P2P para cada torrent
          - sem token, o retorno é sempre P2P
        """
        t_start = time.monotonic()
        logger.info(
            f"[{req_id}] Início: {type}/{imdb_id} "
            f"(season={season}, episode={episode}, budget={settings.REQUEST_BUDGET_SECONDS}s)"
        )

        cache_key = f"streams:{STREAM_CACHE_VERSION}:{imdb_id}:{type}"
        if season is not None and episode is not None:
            cache_key = f"{cache_key}:{season}:{episode}"
        torrent_results = await self._get_cached_torrents(cache_key, req_id)

        if torrent_results is None:
            torrent_results = await self._singleflight(
                cache_key,
                lambda: self._search_and_cache(
                    cache_key=cache_key,
                    imdb_id=imdb_id,
                    type=type,
                    req_id=req_id,
                    t_start=t_start,
                    season=season,
                    episode=episode,
                ),
            )

        # Ordena os torrents uma vez. No modo híbrido, cada torrent elegível
        # pode gerar duas opções: uma via RD e outra via P2P.
        pares = [(torrent, bool(rd_token and torrent.magnet)) for torrent in torrent_results]
        torrents_ordenados = [torrent for torrent, _ in _sort_streams(pares)]

        max_streams = max(1, settings.MAX_STREAMS_PER_REQUEST)
        # No modo híbrido cada torrent pode virar duas entradas (RD + P2P).
        # Limitar torrents ANTES do loop também limita play sessions/SQLite.
        max_torrents = max_streams // 2 if rd_token and include_p2p else max_streams
        max_torrents = max(1, max_torrents)
        if len(torrents_ordenados) > max_torrents:
            logger.info(
                f"[{req_id}] [LIMIT] {len(torrents_ordenados)} torrents ranqueados; "
                f"processando os {max_torrents} primeiros"
            )
            torrents_ordenados = torrents_ordenados[:max_torrents]

        rd_streams: list[StreamResult] = []
        p2p_streams: list[StreamResult] = []
        play_sessions_count = 0
        p2p_suppressed_count = 0

        for torrent in torrents_ordenados:
            p2p_safe = _p2p_safe_for_request(torrent, type, season, episode)
            sessao_gravada = False
            play_id = ""

            if rd_token and torrent.magnet:
                play_id = str(uuid.uuid4())
                session_data = {
                    "rd_token": rd_token,
                    "magnet": torrent.magnet,
                    "info_hash": torrent.info_hash,
                    "imdb_id": imdb_id,
                    "stremio_id": stremio_id or imdb_id,
                    "type": type,
                    "req_id": req_id,
                    "created_at": time.time(),
                    "play_session_ttl": PLAY_SESSION_TTL_SECONDS,
                }
                try:
                    await cache.set(
                        f"play:{play_id}",
                        session_data,
                        ttl=PLAY_SESSION_TTL_SECONDS,
                    )
                    sessao_gravada = True
                except CacheWriteError as exc:
                    # Escrita obrigatória: sem a sessão no cache, /play
                    # devolveria 404 no clique. Oferecer o link seria pior
                    # que não oferecer — cai no fallback P2P abaixo.
                    logger.error(
                        f"[{req_id}] [PLAY SESSION] falha ao gravar "
                        f"{play_id[:8]}: {exc} — stream RD omitido para "
                        f"'{torrent.title[:40]}'"
                    )

            if sessao_gravada:
                base = request_base_url or settings.BASE_URL
                rd_streams.append(
                    self._formatar_stream(
                        torrent=torrent,
                        has_play_url=True,
                        stream_url=f"{base}/play/{play_id}",
                    )
                )
                play_sessions_count += 1

                if include_p2p and p2p_safe:
                    p2p_streams.append(
                        self._formatar_stream(
                            torrent=torrent,
                            has_play_url=False,
                            p2p_label=True,
                        )
                    )
                elif include_p2p:
                    p2p_suppressed_count += 1
            elif p2p_safe:
                # Sem token, ou quando uma fonte não trouxe magnet, mantém o
                # fallback P2P existente somente quando o arquivo é inequívoco.
                p2p_streams.append(
                    self._formatar_stream(
                        torrent=torrent,
                        has_play_url=False,
                    )
                )
            else:
                p2p_suppressed_count += 1

        # RD primeiro para preservar a experiência premium; P2P vem abaixo.
        streams = (rd_streams + p2p_streams)[:max_streams]

        elapsed_total = (time.monotonic() - t_start) * 1000
        logger.info(
            f"[{req_id}] Concluído: {len(streams)} streams "
            f"({len(rd_streams)} RD, {len(p2p_streams)} P2P), "
            f"{play_sessions_count} play sessions, "
            f"{p2p_suppressed_count} P2P ambiguos omitidos, {elapsed_total:.0f}ms"
        )

        return streams

    async def _run_scrapers(
        self, query: str, imdb_id: str, type: str,
        req_id: str, label: str, budget: float,
        season: int | None = None, episode: int | None = None,
        skip_query_independent: bool = False,
    ) -> ScrapeOutcome:
        """
        Executa scrapers em paralelo e registra a saúde da última consulta.

        skip_query_independent=True pula fontes com USES_TEXT_QUERY=False
        (ex: YTS, Brazuca — buscam só por imdb_id/season/episode). Usado na
        segunda rodada (título original): rodar essas fontes de novo com um
        texto de busca diferente não muda o resultado, então re-executá-las
        só desperdiça tempo de rede sem chance de achar algo novo.

        Circuit breaker: fontes com CIRCUIT_BREAKER_FAILURE_THRESHOLD falhas
        seguidas (erro ou indisponível — "vazio" não conta, é busca normal
        sem resultado) entram em cooldown e são puladas nas próximas
        chamadas até CIRCUIT_BREAKER_COOLDOWN_SECONDS se passarem. Isso evita
        pagar o timeout inteiro de uma fonte fora do ar em todo request.
        """
        if not self.scrapers:
            logger.warning(f"[{req_id}] Nenhum scraper ativo!")
            return ScrapeOutcome(ran=False)

        agora = time.monotonic()
        scrapers_a_rodar: list[BaseScraper] = []
        pulados = 0
        for scraper in self.scrapers:
            if skip_query_independent and not scraper.USES_TEXT_QUERY:
                pulados += 1
                logger.debug(
                    f"[{req_id}] [{label}] [{scraper.name}] pulado — não depende "
                    f"do texto de busca, já rodou na primeira rodada"
                )
                continue

            health = self.source_health.setdefault(scraper.name, {})
            skip_until = health.get("skip_until")
            if skip_until and agora < skip_until:
                pulados += 1
                logger.info(
                    f"[{req_id}] [{label}] [{scraper.name}] pulado — circuit breaker "
                    f"aberto (volta em {skip_until - agora:.0f}s)"
                )
                continue
            scrapers_a_rodar.append(scraper)

        if not scrapers_a_rodar:
            logger.warning(f"[{req_id}] Todas as fontes estão em cooldown (circuit breaker)")
            return ScrapeOutcome(ran=False, skipped_sources=pulados)

        effective_timeout = min(settings.SCRAPER_TIMEOUT_SECONDS + 2.0, budget)

        async def _timed_search(
            scraper: BaseScraper,
        ) -> tuple[BaseScraper, list[TorrentResult] | Exception, float]:
            set_req_id(req_id)
            scraper.last_error = None
            t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    scraper.search(query, imdb_id, type, season=season, episode=episode),
                    timeout=effective_timeout,
                )
                elapsed = (time.monotonic() - t0) * 1000
                return scraper, result, elapsed
            except TimeoutError:
                elapsed = (time.monotonic() - t0) * 1000
                error = TimeoutError(f"Timeout ({elapsed:.0f}ms, budget={budget:.1f}s)")
                scraper.last_error = str(error)
                return scraper, error, elapsed
            except Exception as exc:
                elapsed = (time.monotonic() - t0) * 1000
                scraper.last_error = str(exc)
                return scraper, exc, elapsed

        resultados = await asyncio.gather(*[_timed_search(scraper) for scraper in scrapers_a_rodar])

        todos: list[TorrentResult] = []
        fontes_ok = 0
        fontes_com_falha = 0
        for scraper, resultado, elapsed_ms in resultados:
            health = self.source_health.setdefault(scraper.name, {})
            health.update(
                last_elapsed_ms=round(elapsed_ms),
                last_checked_at=time.time(),
            )

            if isinstance(resultado, Exception):
                fontes_com_falha += 1
                self._registrar_falha(health, str(resultado))
                logger.warning(
                    f"[{req_id}] [{label}] [{scraper.name}] FALHOU: "
                    f"{resultado} ({elapsed_ms:.0f}ms) "
                    f"[falhas seguidas: {health['consecutive_failures']}]"
                )
                if health.get("skip_until"):
                    logger.warning(
                        f"[{req_id}] [{label}] [{scraper.name}] circuit breaker ABERTO "
                        f"— pausado por {CIRCUIT_BREAKER_COOLDOWN_SECONDS}s"
                    )
                continue

            count = len(resultado)
            if count == 0 and scraper.last_error:
                fontes_com_falha += 1
                self._registrar_falha(health, scraper.last_error, status="unavailable")
                logger.warning(
                    f"[{req_id}] [{label}] [{scraper.name}] INDISPONÍVEL: "
                    f"{scraper.last_error} ({elapsed_ms:.0f}ms) "
                    f"[falhas seguidas: {health['consecutive_failures']}]"
                )
                if health.get("skip_until"):
                    logger.warning(
                        f"[{req_id}] [{label}] [{scraper.name}] circuit breaker ABERTO "
                        f"— pausado por {CIRCUIT_BREAKER_COOLDOWN_SECONDS}s"
                    )
            else:
                # Sucesso real (com ou sem resultado) — zera o contador de
                # falhas e fecha o circuit breaker se estava aberto.
                fontes_ok += 1
                health.update(
                    status="ok" if count else "empty",
                    last_count=count,
                    last_error=None,
                    consecutive_failures=0,
                    skip_until=None,
                )
                logger.info(
                    f"[{req_id}] [{label}] [{scraper.name}] "
                    f"{count} resultados ({elapsed_ms:.0f}ms)"
                )
            todos.extend(resultado)

        await self._persist_health()
        return ScrapeOutcome(
            torrents=self._deduplicate(todos),
            ok_sources=fontes_ok,
            failed_sources=fontes_com_falha,
            skipped_sources=pulados,
        )

    async def _gravar_best_effort(
        self, chave: str, payload: list, ttl: int | None, req_id: str
    ) -> bool:
        """
        Grava no cache tolerando falha.

        O cache de busca é best-effort: se a escrita falhar, a resposta desta
        requisição continua correta — só não vai ser reaproveitada. Bem
        diferente da play session, onde a escrita é obrigatória.
        """
        try:
            if ttl is None:
                await cache.set(chave, payload)
            else:
                await cache.set(chave, payload, ttl=ttl)
            return True
        except CacheWriteError as exc:
            logger.warning(
                f"[{req_id}] [CACHE] resultado de {chave} não foi gravado ({exc}) "
                "— a resposta desta requisição não é afetada"
            )
            return False

    async def _cachear_busca(
        self,
        cache_key: str,
        resultado: ScrapeOutcome,
        req_id: str,
        *,
        permitir_cache_negativo: bool = True,
    ) -> None:
        """
        Decide se — e por quanto tempo — o resultado da busca vai para o cache.

        Regra: vazio só é cacheável quando alguma fonte respondeu bem. Se
        todas falharam, estavam em cooldown ou nem chegaram a rodar, gravar
        `[]` transformaria uma indisponibilidade de segundos num "esse
        conteúdo não existe" de uma hora — e o próximo request nem tentaria
        de novo, porque acharia cache válido.
        """
        payload = [t.model_dump() for t in resultado.torrents]

        if resultado.torrents:
            if await self._gravar_best_effort(cache_key, payload, None, req_id):
                logger.info(f"[{req_id}] [CACHE SET] {cache_key} → {len(payload)} torrents")
            return

        if resultado.confiavel and not permitir_cache_negativo:
            logger.warning(
                f"[{req_id}] [CACHE SKIP] {cache_key} -> vazio, mas a busca foi "
                "feita pelo imdb_id porque o titulo nao resolveu. Isso nao prova "
                "ausencia de conteudo — nao cacheado."
            )
            return

        if resultado.confiavel:
            ttl = settings.NEGATIVE_CACHE_TTL_SECONDS
            if await self._gravar_best_effort(cache_key, payload, ttl, req_id):
                logger.info(
                    f"[{req_id}] [CACHE SET] {cache_key} → vazio confirmado por "
                    f"{resultado.ok_sources} fonte(s), TTL curto de {ttl}s"
                )
            return

        logger.warning(
            f"[{req_id}] [CACHE SKIP] {cache_key} → vazio sem nenhuma fonte saudável "
            f"({resultado.failed_sources} falharam, {resultado.skipped_sources} puladas, "
            f"executou={resultado.ran}) — não cacheado para não congelar indisponibilidade"
        )

    def _registrar_falha(self, health: dict, erro: str, status: str = "error") -> None:
        """Incrementa o contador de falhas seguidas e abre o circuit breaker
        (define skip_until) quando o limite é atingido.

        `status` distingue os dois modos de falha que _run_scrapers já separa
        nos logs mas nunca conseguia refletir no /health:
          - "error":       o scraper levantou exceção ou estourou o timeout
          - "unavailable": retornou vazio tendo registrado erro de transporte

        Antes o valor era derivado do próprio health:
            "error" if health.get("status") != "unavailable" else "unavailable"
        Como nada em lugar nenhum jamais atribuía "unavailable", a condição
        era sempre verdadeira e o status era sempre "error" — o ramo
        "unavailable" era inalcançável e o /health nunca mostrava esse estado.
        """
        falhas = health.get("consecutive_failures", 0) + 1
        health.update(
            status=status,
            last_count=0,
            last_error=erro,
            consecutive_failures=falhas,
        )
        if falhas >= CIRCUIT_BREAKER_FAILURE_THRESHOLD:
            health["skip_until"] = time.monotonic() + CIRCUIT_BREAKER_COOLDOWN_SECONDS

    def _deduplicate(self, results: list[TorrentResult]) -> list[TorrentResult]:
        """
        Remove hashes repetidos, preserva os nomes de todas as fontes e
        reconcilia os metadados entre os duplicados.

        Antes, ao mesclar um hash repetido, os campos quality/dubbed/title
        ficavam travados no que o PRIMEIRO scraper a achar aquele hash
        tinha detectado — mesmo que outra fonte tivesse identificado a
        qualidade corretamente (a primeira só tinha "Desconhecida"),
        confirmado áudio dublado, ou trazido um release name mais
        descritivo. Isso jogava fora sinal que já tínhamos em mãos.
        """
        by_hash: dict[str, TorrentResult] = {}
        for torrent in results:
            info_hash = torrent.info_hash.lower().strip()
            if not info_hash:
                continue

            existing = by_hash.get(info_hash)
            if existing is None:
                # Guarda com o hash JA normalizado. Antes a normalizacao valia
                # so para a CHAVE do dicionario, e o objeto seguia com o hash
                # original — espacos e maiusculas vazavam para o campo infoHash
                # entregue ao Stremio e para os logs.
                by_hash[info_hash] = (
                    torrent
                    if torrent.info_hash == info_hash
                    else torrent.model_copy(update={"info_hash": info_hash})
                )
                continue

            sources = [part.strip() for part in existing.source.split(" + ") if part.strip()]
            for source in [part.strip() for part in torrent.source.split(" + ") if part.strip()]:
                if source not in sources:
                    sources.append(source)

            updates: dict = {"source": " + ".join(sources)}
            if existing.file_idx is None and torrent.file_idx is not None:
                updates["file_idx"] = torrent.file_idx
            merged_sources = list(existing.sources)
            for peer_source in torrent.sources:
                if peer_source not in merged_sources:
                    merged_sources.append(peer_source)
            if merged_sources != existing.sources:
                updates["sources"] = merged_sources[:MAX_P2P_SOURCES]
            if existing.size is None and torrent.size is not None:
                updates["size"] = torrent.size
            # `(a or 0) > (b or 0)` perdia o zero CONFIRMADO: com
            # existing=None e novo=0, `0 > 0` e falso e o resultado fica None.
            # _is_confirmed_dead entao deixava de penalizar um torrent que a
            # fonte afirmou ter 0 seeders — ele subia no ranking como se a
            # contagem fosse desconhecida.
            seeders_conhecidos = [
                s for s in (existing.seeders, torrent.seeders) if s is not None
            ]
            if seeders_conhecidos:
                updates["seeders"] = max(seeders_conhecidos)

            # "Desconhecida" perde para qualquer qualidade identificada por
            # outra fonte.
            if existing.quality == "Desconhecida" and torrent.quality != "Desconhecida":
                updates["quality"] = torrent.quality

            # Dublado é aditivo: se QUALQUER fonte confirmou áudio PT-BR no
            # mesmo arquivo, essa faixa existe — não faz sentido "esquecer"
            # só porque a primeira fonte não detectou a tag no título dela.
            if torrent.dubbed and not existing.dubbed:
                updates["dubbed"] = True

            # Título nitidamente mais longo costuma carregar mais info de
            # release (grupo, tags de áudio/vídeo) — só troca com folga
            # (>20%) pra não ficar trocando por diferenças triviais.
            if len(torrent.title.strip()) > len(existing.title.strip()) * 1.2:
                updates["title"] = torrent.title

            by_hash[info_hash] = existing.model_copy(update=updates)

        return list(by_hash.values())

    def get_source_health(self) -> list[dict]:
        """Retorna fontes ativas, desativadas e o resultado da última consulta."""
        agora = time.monotonic()
        items: list[dict] = []
        for flag_name, scraper_cls in SCRAPER_REGISTRY:
            enabled = bool(getattr(settings, flag_name, False))
            name = scraper_cls.name
            if enabled:
                data = dict(self.source_health.get(name, {"status": "not_checked"}))
                data.update(source=name, flag=flag_name, enabled=True)
                # skip_until é um timestamp monotonic (relativo ao processo,
                # sem época fixa) — não serve pra quem consome o /health de
                # fora. Troca por segundos restantes, que é o que importa.
                skip_until = data.pop("skip_until", None)
                data["cooldown_remaining_seconds"] = (
                    max(0, round(skip_until - agora)) if skip_until else 0
                )
            else:
                data = {
                    "source": name,
                    "flag": flag_name,
                    "enabled": False,
                    "status": "disabled",
                    "last_count": None,
                    "last_error": None,
                    "last_elapsed_ms": None,
                    "last_checked_at": None,
                    "consecutive_failures": 0,
                    "cooldown_remaining_seconds": 0,
                }
            items.append(data)
        return items

    async def _get_cached_torrents(self, cache_key: str, req_id: str) -> list[TorrentResult] | None:
        dados = await cache.get(cache_key)
        if dados is None:
            logger.info(f"[{req_id}] [CACHE MISS] {cache_key}")
            return None

        logger.info(f"[{req_id}] [CACHE HIT] {cache_key}")
        try:
            return [TorrentResult(**item) for item in dados]
        except Exception as e:
            logger.error(f"[{req_id}] [CACHE] Erro ao deserializar: {e}")
            return None

    def _formatar_stream(
        self,
        torrent: TorrentResult,
        has_play_url: bool,
        stream_url: str | None = None,
        p2p_label: bool = False,
    ) -> StreamResult:
        """
        Formata TorrentResult para StreamResult.

        Exclusão mútua explícita:
          stream_url presente -> url preenchida, infoHash OMITIDO
          stream_url ausente  -> infoHash preenchido, url OMITIDO
          Nunca os dois ao mesmo tempo.

        O parâmetro `tem_rd` saiu: ele só era passado como True junto com
        `stream_url`, e nesse caminho a função retorna antes de consultá-lo.
        O ramo `notWebReady` que ele controlava era inalcançável em produção
        — só um teste o mantinha vivo. A flag foi para onde ela sempre quis
        estar: no stream do Real-Debrid, que resolve para um link HTTP que
        frequentemente é MKV e não toca em navegador.
        """
        if stream_url:
            return StreamResult(
                name=build_stream_name(torrent, has_play_url=True),
                title=build_stream_title(torrent, has_play_url=True),
                url=stream_url,
                behaviorHints={
                    "bingeGroup": _binge_group(torrent, "rd"),
                    "notWebReady": True,
                },
            )

        behavior: dict = {"bingeGroup": _binge_group(torrent, "p2p")}

        return StreamResult(
            name=build_stream_name(torrent, has_play_url, p2p=p2p_label),
            title=build_stream_title(torrent, has_play_url, p2p=p2p_label),
            infoHash=torrent.info_hash,
            fileIdx=torrent.file_idx,
            sources=_p2p_sources(torrent) or None,
            behaviorHints=behavior,
        )

    async def close(self) -> None:
        for scraper in self.scrapers:
            await scraper.close()
