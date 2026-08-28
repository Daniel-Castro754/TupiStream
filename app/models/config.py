from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configurações do addon carregadas do .env"""

    # ── Servidor ──
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    BASE_URL: str = "http://localhost:8000"
    LOG_LEVEL: str = "info"

    # ── Cache / Storage ──
    CACHE_TTL: int = 3600
    # TTL curto para "busquei e realmente nao achei nada". Separado do
    # CACHE_TTL porque ausencia de resultado envelhece muito mais rapido
    # que uma lista de torrents encontrada.
    NEGATIVE_CACHE_TTL_SECONDS: int = 300
    CACHE_DB_PATH: str = "data/cache.db"
    # Intervalo da limpeza periodica de entradas expiradas. O TTL e logico:
    # sem isto, uma entrada so era removida quando alguem tentava le-la, ou
    # no shutdown.
    CACHE_CLEANUP_INTERVAL_SECONDS: int = 300
    STORAGE_BACKEND: str = "sqlite"          # "sqlite" | "redis"
    REDIS_URL: str = "redis://localhost:6379"

    # ── Request — budget total ──
    # Tempo máximo que get_streams() pode gastar antes de retornar resultados parciais.
    # O Stremio corta em ~20s — este budget garante resposta antes disso.
    REQUEST_BUDGET_SECONDS: float = 12.0

    # Margem do budget reservada para fechar a resposta: serializar os
    # StreamResult e gravar as play sessions. Antes os scrapers podiam
    # consumir ate o ultimo milissegundo e sobrava zero para isso.
    BUDGET_RESERVE_SECONDS: float = 0.5

    # ── Playback — budget total do /play ──
    # O fluxo Real-Debrid sao ate 7 chamadas sequenciais de 15s cada, mais
    # 1,5s de sleep entre retries: 106,5s no pior caso, sem teto algum. O
    # Stremio corta em ~20s, entao o servidor ficava trabalhando muito tempo
    # depois de o cliente ja ter desistido.
    PLAYBACK_BUDGET_SECONDS: float = 20.0

    # ── Limites de resposta/upstream ──
    # Um request com token cria uma play session por torrent. Sem teto, uma
    # fonte que devolva milhares de itens vira milhares de linhas SQLite e
    # cópias do token. O limite também mantém a UI do Stremio utilizável.
    MAX_STREAMS_PER_REQUEST: int = 30
    # Corta listas externas antes de criar modelos/deduplicar. O agregador
    # aplica o teto final menor depois do ranking.
    MAX_UPSTREAM_STREAMS: int = 100
    # Limite sobre bytes já descomprimidos do arquivo .torrent. O parser aplica
    # o mesmo teto como defesa em profundidade.
    MAX_TORRENT_BYTES: int = 4 * 1024 * 1024

    # ── Concorrência de buscas ──
    # Cada busca abre vários scrapers e páginas de detalhe. O semáforo limita
    # quantos conteúdos DIFERENTES podem fazer esse fan-out ao mesmo tempo.
    MAX_CONCURRENT_SEARCHES: int = 5
    # Se todos os slots estiverem ocupados, falha rápido com 503 em vez de
    # esperar até o timeout do Stremio mantendo mais requests na memória.
    SEARCH_QUEUE_TIMEOUT_SECONDS: float = 0.25
    SEARCH_RETRY_AFTER_SECONDS: int = 2

    # ── Scrapers — timeout por scraper ──
    SCRAPER_TIMEOUT_SECONDS: float = 8.0

    # ── Scrapers — feature flags ──
    # Fontes verificadas / úteis (ativas por padrão)
    ENABLE_APACHE_TORRENT: bool = True
    ENABLE_COMANDO_FILMES: bool = True
    ENABLE_BRAZUCA: bool = True
    ENABLE_YTS: bool = True
    ENABLE_ARCHIVE_ORG: bool = True

    # Todas as fontes ficam disponíveis na instalação padrão. As fontes
    # instáveis continuam protegidas pelo circuit breaker do agregador.
    ENABLE_HDR_TORRENT: bool = True
    ENABLE_MICOLEAO: bool = True

    # ── API Keys opcionais ──
    TMDB_API_KEY: str = ""  # opcional — se vazio, usa alternativas gratuitas

    # Fontes instáveis / sujeitas a anti-bot. Ativas para poderem se recuperar
    # quando um domínio/mirror voltar, sem exigir um novo deploy.
    ENABLE_TORRENT_GALAXY: bool = True
    ENABLE_1337X: bool = True
    ENABLE_RUTRACKER: bool = True

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


# Instância global de configurações
settings = Settings()
