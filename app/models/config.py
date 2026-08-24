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

    # ── Scrapers — timeout por scraper ──
    SCRAPER_TIMEOUT_SECONDS: float = 8.0

    # ── Scrapers — feature flags ──
    # Fontes verificadas / úteis (ativas por padrão)
    ENABLE_APACHE_TORRENT: bool = True
    ENABLE_COMANDO_FILMES: bool = True
    ENABLE_BRAZUCA: bool = True
    ENABLE_YTS: bool = True
    ENABLE_ARCHIVE_ORG: bool = True

    # Domínios sem resolução confiável em julho/2026. Permanecem disponíveis
    # por feature flag, mas não atrasam todas as buscas por padrão.
    ENABLE_HDR_TORRENT: bool = False
    ENABLE_MICOLEAO: bool = False

    # ── API Keys opcionais ──
    TMDB_API_KEY: str = ""  # opcional — se vazio, usa alternativas gratuitas

    # Fontes instáveis / bloqueadas por anti-bot (desativadas por padrão)
    ENABLE_TORRENT_GALAXY: bool = False
    ENABLE_1337X: bool = False
    ENABLE_RUTRACKER: bool = False

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


# Instância global de configurações
settings = Settings()
