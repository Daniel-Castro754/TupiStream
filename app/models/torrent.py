from pydantic import BaseModel, Field, field_validator

from app.info_hash import normalize_info_hash


class TorrentResult(BaseModel):
    """Resultado de um torrent encontrado por um scraper"""

    title: str  # ex: "Interestelar 1080p Dublado"
    info_hash: str  # hash do torrent, lowercase, sem espaço
    magnet: str  # link magnet completo
    quality: str  # ex: "1080p", "720p", "4K"
    dubbed: bool  # True se dublado PT-BR
    source: str  # nome da fonte, ex: "Apache Torrent"
    size: str | None = None
    seeders: int | None = None
    file_idx: int | None = None
    sources: list[str] = Field(default_factory=list)

    @field_validator("info_hash", mode="before")
    @classmethod
    def canonical_info_hash(cls, value: object) -> str:
        normalized = normalize_info_hash(value)
        if normalized is None:
            raise ValueError("info_hash must be a 40-hex or 32-char Base32 BTIH")
        return normalized


class StreamResult(BaseModel):
    """Resultado formatado para a API do Stremio"""

    name: str  # label exibido no Stremio, ex: "🇧🇷 1080p | Dublado | RD ✅"
    title: str  # subtítulo com detalhes
    url: str | None = None  # link HTTP direto do RD
    infoHash: str | None = None  # fallback magnet (campo camelCase para Stremio)
    fileIdx: int | None = None  # arquivo correto dentro do torrent
    sources: list[str] | None = None  # trackers/DHT adicionais do Stremio
    behaviorHints: dict = Field(default_factory=dict)
