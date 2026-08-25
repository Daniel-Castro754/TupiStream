import logging
import re

from app.info_hash import normalize_info_hash
from app.models.config import settings
from app.models.torrent import TorrentResult
from app.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class BrazucaAddonScraper(BaseScraper):
    """Scraper que consome o addon Brazuca Torrents via API Stremio"""

    name = "Brazuca Torrents"
    base_url = "https://94c8cb9f702d-brazuca-torrents.baby-beamup.club"
    # Busca só por imdb_id (+ season/episode) — o texto de `query` nunca é usado.
    USES_TEXT_QUERY = False

    async def search(
        self,
        query: str,
        imdb_id: str,
        type: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[TorrentResult]:
        """Busca streams no addon Brazuca Torrents via API JSON.

        Esse scraper é um proxy para outro addon Stremio — para séries,
        o addon de origem espera o id no formato imdb:season:episode para
        retornar os streams do episódio certo. Usando só o imdb_id puro,
        a origem não sabe qual episódio foi pedido.
        """
        resultados: list[TorrentResult] = []

        stremio_id = imdb_id
        if type == "series" and season is not None and episode is not None:
            stremio_id = f"{imdb_id}:{season}:{episode}"

        # Consome a API do addon Stremio diretamente (não faz web scraping)
        url = f"{self.base_url}/stream/{type}/{stremio_id}.json"
        response = await self._get(url)
        if not response:
            return resultados

        try:
            data = response.json()
        except Exception as e:
            logger.error(f"[{self.name}] Erro ao parsear JSON de {url}: {e}")
            return resultados

        streams = data.get("streams", [])
        if not isinstance(streams, list):
            self.last_error = "campo streams nao e uma lista"
            return resultados

        # Limita ANTES de criar TorrentResult. Uma origem comprometida ou com
        # bug não pode nos fazer instanciar/deduplicar milhares de objetos.
        streams = streams[: settings.MAX_UPSTREAM_STREAMS]

        for stream in streams:
            try:
                torrent = self._parsear_stream(stream)
                if torrent:
                    resultados.append(torrent)
            except Exception as e:
                logger.error(f"[{self.name}] Erro ao processar stream: {e}")
                continue

        logger.info(f"[{self.name}] Encontrados {len(resultados)} torrents para '{stremio_id}'")
        return resultados

    def _parsear_stream(self, stream: dict) -> TorrentResult | None:
        """Converte um objeto stream do Stremio em TorrentResult.

        Streams sem `infoHash` (só com `url` direta) são recusados aqui.

        O ramo anterior alegava, em comentário, "gera um hash fictício baseado
        na URL para deduplicação" — mas o que o código fazia era atribuir
        info_hash = "". E _deduplicate descarta todo resultado com hash vazio
        (`if not info_hash: continue`). Ou seja: o TorrentResult era montado
        para ser jogado fora poucas linhas depois, com magnet="" e sem
        nenhuma chance de ser elegível a Real-Debrid.

        Recusar de imediato tem o mesmo efeito observável e deixa o custo e a
        intenção explícitos. Suportar url direta de verdade exigiria um campo
        próprio no modelo e validação de destino — mudança de escopo maior.
        """
        info_hash = normalize_info_hash(stream.get("infoHash"))
        if info_hash is None:
            return None

        magnet = f"magnet:?xt=urn:btih:{info_hash}"

        # Extrai título do stream
        titulo = stream.get("title", "") or stream.get("name", "") or "Sem título"

        # Detecta qualidade e dublado pelo título
        quality = self._detectar_qualidade(titulo)
        dubbed = self._detectar_dublado(titulo)

        # Tenta extrair tamanho do título (ex: "1.4 GB" no texto)
        size = self._extrair_tamanho_titulo(titulo)

        file_idx_raw = stream.get("fileIdx")
        file_idx = (
            file_idx_raw
            if isinstance(file_idx_raw, int)
            and not isinstance(file_idx_raw, bool)
            and file_idx_raw >= 0
            else None
        )
        sources = [
            source
            for source in stream.get("sources", [])[:30]
            if isinstance(source, str)
            and source.startswith(("tracker:", "dht:"))
        ]

        return TorrentResult(
            title=titulo,
            info_hash=info_hash,
            magnet=magnet,
            quality=quality,
            dubbed=dubbed,
            source=self.name,
            size=size,
            seeders=None,
            file_idx=file_idx,
            sources=sources,
        )

    def _detectar_qualidade(self, titulo: str) -> str:
        """Detecta a qualidade pelo título"""
        titulo_upper = titulo.upper()
        if "4K" in titulo_upper or "2160P" in titulo_upper:
            return "4K"
        if "1080P" in titulo_upper:
            return "1080p"
        if "720P" in titulo_upper:
            return "720p"
        if "480P" in titulo_upper:
            return "480p"
        return "Desconhecida"

    def _detectar_dublado(self, titulo: str) -> bool:
        """Detecta se o torrent é dublado PT-BR"""
        titulo_upper = titulo.upper()
        return any(
            tag in titulo_upper
            for tag in ["DUBLADO", "DUAL ÁUDIO", "DUAL AUDIO", "DUAL", "NACIONAL", "PORTUGUES", "PORTUGUESE", "PT-BR"]
        )

    def _extrair_tamanho_titulo(self, titulo: str) -> str | None:
        """Tenta extrair o tamanho do arquivo a partir do texto do título"""
        match = re.search(r"(\d+[.,]?\d*\s*(?:GB|MB|TB))", titulo, re.IGNORECASE)
        return match.group(1).strip() if match else None
