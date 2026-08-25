import asyncio
import contextvars
import logging
import time
from abc import ABC, abstractmethod
from urllib.parse import urljoin, urlparse

import httpx

from app.models.config import settings
from app.models.torrent import TorrentResult

logger = logging.getLogger(__name__)

# ContextVar para request_id — seguro sob concorrência async.
# Cada asyncio.Task herda uma cópia do contexto do pai. O request_id também
# permite que a instância compartilhada do scraper mantenha o erro separado
# por requisição, sem uma busca sobrescrever a telemetria de outra.
_current_req_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_req_id", default=""
)

# Retry só para falhas TRANSITÓRIAS (timeout, conexão, 5xx) — nunca para
# 403/429/404, que são estados estáveis (bloqueio ou recurso inexistente)
# onde tentar de novo não muda o resultado, só desperdiça budget.
DEFAULT_RETRIES = 1
RETRY_BACKOFF_SECONDS = 0.4
MAX_TRACKED_REQUEST_ERRORS = 256

# Headers realistas para evitar bloqueio
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}


def set_req_id(req_id: str) -> contextvars.Token[str]:
    """Define o request_id no contexto atual. Retorna token para reset."""
    return _current_req_id.set(req_id)


def get_req_id() -> str:
    """Retorna o request_id do contexto atual."""
    return _current_req_id.get()


class ResponseTooLargeError(ValueError):
    """Resposta externa ultrapassou o limite de bytes descomprimidos."""


class BaseScraper(ABC):
    """Classe base para todos os scrapers de torrent"""

    name: str = ""
    base_url: str = ""

    # Classificação operacional — usada para documentação e triagem.
    stability: str = "estável"

    # True se o resultado da busca muda com o texto de `query` (a maioria
    # dos scrapers busca por título). Scrapers que ignoram `query` e usam
    # só imdb_id/season/episode (ex: consomem outra API por ID) devem
    # marcar False — rodar de novo com um título diferente não muda o
    # resultado, então o agregador pode pular esse re-run com segurança.
    USES_TEXT_QUERY: bool = True

    def __init__(self) -> None:
        self._default_last_error: str | None = None
        self._last_errors_by_req_id: dict[str, str | None] = {}
        self.client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=settings.SCRAPER_TIMEOUT_SECONDS,
        )

    @property
    def last_error(self) -> str | None:
        """Erro da requisição atual, isolado pelo req_id quando disponível."""
        req_id = get_req_id()
        if req_id:
            return self._last_errors_by_req_id.get(req_id)
        return self._default_last_error

    @last_error.setter
    def last_error(self, value: str | None) -> None:
        req_id = get_req_id()
        if not req_id:
            self._default_last_error = value
            return

        self._last_errors_by_req_id[req_id] = value
        # Evita crescimento indefinido numa instância global de longa duração.
        while len(self._last_errors_by_req_id) > MAX_TRACKED_REQUEST_ERRORS:
            oldest_req_id = next(iter(self._last_errors_by_req_id))
            self._last_errors_by_req_id.pop(oldest_req_id, None)

    def _log_prefix(self) -> str:
        """Prefixo de log com req_id do contexto atual."""
        req_id = get_req_id()
        if req_id:
            return f"[{req_id}] [{self.name}]"
        return f"[{self.name}]"

    def _hosts_permitidos(self) -> frozenset[str]:
        """
        Hosts que este scraper pode acessar: o base_url e os mirrors.

        Derivado da configuracao que ja existe, entao nenhum scraper precisa
        declarar nada novo. Se o conjunto sair vazio — caso de duble de teste
        sem base_url —, a validacao nao restringe nada: fechar aqui quebraria
        testes sem ganho de seguranca real, porque duble nao faz rede.
        """
        urls = [self.base_url, *getattr(self, "_fallback_urls", [])]
        hosts = {
            (urlparse(u).hostname or "").lower().rstrip(".")
            for u in urls
            if u
        }
        return frozenset(h for h in hosts if h)

    def _url_permitida(self, url: str) -> bool:
        """
        True quando a URL aponta para um host declarado pelo scraper.

        Substitui a checagem `dominio in href`, que comparava SUBSTRING e por
        isso aceitava qualquer destino que contivesse o dominio em qualquer
        posicao:

            https://evil.example/?next=apachetorrent.com     -> aceito
            https://apachetorrent.com@169.254.169.254/       -> aceito
            https://apachetorrent.com.evil.example/x         -> aceito

        Os tres apontam para fora. O terceiro e o classico: um subdominio
        controlado pelo atacante que termina com o dominio esperado. O
        segundo usa userinfo para disfarcar o host real — e 169.254.169.254
        e o endpoint de metadados de nuvem.

        A comparacao passa a ser de HOSTNAME exato, com esquema e porta
        restritos e credenciais embutidas recusadas.
        """
        permitidos = self._hosts_permitidos()
        if not permitidos:
            return True

        try:
            partes = urlparse(url)
        except ValueError:
            return False

        if partes.scheme not in ("http", "https"):
            return False
        if partes.username or partes.password:
            return False
        try:
            porta = partes.port
        except ValueError:
            return False
        if porta not in (None, 80, 443):
            return False

        host = (partes.hostname or "").lower().rstrip(".")
        return host in permitidos

    def _resolver_link(self, href: str, url_da_pagina: str) -> str | None:
        """
        Transforma um href da pagina em URL absoluta validada, ou None.

        `urljoin` resolve link relativo contra a pagina de origem, que e o
        comportamento correto de um navegador. A validacao vem depois: href
        vem de HTML de terceiro e nao e evidencia de destino seguro.
        """
        if not href:
            return None
        try:
            absoluta = urljoin(url_da_pagina, href.strip())
        except ValueError:
            return None
        if not self._url_permitida(absoluta):
            logger.warning(
                f"{self._log_prefix()} link recusado por host nao permitido: {absoluta[:120]}"
            )
            return None
        return absoluta

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""

    def _prioritize_fallback_urls(self, urls: list[str]) -> list[str]:
        """Coloca primeiro o último domínio funcional, preservando a ordem restante."""
        preferred_origin = self._origin(self.base_url)
        if not preferred_origin:
            return list(urls)

        preferred = [url for url in urls if self._origin(url) == preferred_origin]
        others = [url for url in urls if self._origin(url) != preferred_origin]
        return preferred + others

    async def _get(self, url: str, *, retries: int = DEFAULT_RETRIES) -> httpx.Response | None:
        """
        Faz GET com retry em falhas transitórias, métricas de tempo e
        classificação de falha.

        Retry só acontece para timeout, erro de transporte e HTTP 5xx — falhas
        que podem se resolver sozinhas numa tentativa seguinte. 403/429
        continuam retornando na hora: são bloqueio/limite conhecidos, não
        adianta tentar de novo no mesmo request.
        """
        prefix = self._log_prefix()
        self.last_error = None
        tentativas_totais = retries + 1

        for tentativa in range(1, tentativas_totais + 1):
            t0 = time.monotonic()
            try:
                response = await self.client.get(url)
                elapsed = (time.monotonic() - t0) * 1000
                status = response.status_code

                if status == 403:
                    self.last_error = "HTTP 403: provável bloqueio anti-bot/Cloudflare"
                    logger.warning(
                        f"{prefix} HTTP 403 Forbidden ({elapsed:.0f}ms) "
                        f"— provável bloqueio anti-bot/Cloudflare"
                    )
                    return None
                if status == 429:
                    self.last_error = "HTTP 429: limite de requisições"
                    logger.warning(f"{prefix} HTTP 429 Rate Limited ({elapsed:.0f}ms)")
                    return None

                if status >= 500:
                    self.last_error = f"HTTP {status}: erro no servidor"
                    if tentativa < tentativas_totais:
                        logger.warning(
                            f"{prefix} HTTP {status} ({elapsed:.0f}ms) — "
                            f"tentativa {tentativa}/{tentativas_totais}, retry..."
                        )
                        await asyncio.sleep(RETRY_BACKOFF_SECONDS * tentativa)
                        continue
                    logger.warning(
                        f"{prefix} HTTP {status} ({elapsed:.0f}ms) — esgotou tentativas"
                    )
                    return None

                response.raise_for_status()
                logger.debug(f"{prefix} GET {status} ({elapsed:.0f}ms)")
                return response

            except httpx.TimeoutException:
                elapsed = (time.monotonic() - t0) * 1000
                self.last_error = f"timeout após {elapsed:.0f}ms"
                if tentativa < tentativas_totais:
                    logger.warning(
                        f"{prefix} TIMEOUT após {elapsed:.0f}ms — "
                        f"tentativa {tentativa}/{tentativas_totais}, retry..."
                    )
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * tentativa)
                    continue
                logger.warning(
                    f"{prefix} TIMEOUT após {elapsed:.0f}ms "
                    f"(limite: {settings.SCRAPER_TIMEOUT_SECONDS}s) — esgotou tentativas"
                )
                return None

            except httpx.TransportError as e:
                elapsed = (time.monotonic() - t0) * 1000
                self.last_error = str(e)
                if tentativa < tentativas_totais:
                    logger.warning(
                        f"{prefix} Erro de transporte ({elapsed:.0f}ms) — "
                        f"tentativa {tentativa}/{tentativas_totais}, retry..."
                    )
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * tentativa)
                    continue
                logger.error(
                    f"{prefix} Erro de transporte após {elapsed:.0f}ms: "
                    f"{e} — esgotou tentativas"
                )
                return None

            except Exception as e:
                # Falhas não classificadas como transitórias (ex: JSON
                # inválido, HTTPStatusError de um 4xx que não seja
                # 403/429) — não adianta tentar de novo no mesmo request.
                elapsed = (time.monotonic() - t0) * 1000
                self.last_error = str(e)
                logger.error(f"{prefix} ERRO ({elapsed:.0f}ms): {e}")
                return None

        return None

    def _pode_adotar_origem(self, url_pedida: str, url_final: str) -> bool:
        """
        A origem final pode virar o novo `base_url` deste scraper?

        Isto importa mais do que parece: o scraper e um SINGLETON criado no
        startup. Adotar uma origem nao contamina uma requisicao — contamina
        o scraper pelo resto da vida do processo, e `_prioritize_fallback_urls`
        passa a PREFERIR essa origem em todas as buscas seguintes.

        Adotar e seguro em dois casos:
          - nao houve troca de host, ou seja, a origem veio da propria lista
            de mirrors, que e codigo e nao HTML de terceiro;
          - o host final esta declarado nos mirrors.

        Um redirect que CRUZA para host nao declarado continua sendo seguido
        e usado nesta requisicao — a adaptacao a mirror que mudou de dominio
        e deliberada e nao vai embora. O que ele deixa de fazer e virar
        estado permanente do processo.
        """
        host_pedido = (urlparse(url_pedida).hostname or "").lower().rstrip(".")
        host_final = (urlparse(url_final).hostname or "").lower().rstrip(".")
        if host_pedido and host_pedido == host_final:
            return True
        return self._url_permitida(url_final)

    async def _get_bytes_limited(self, url: str, max_bytes: int) -> bytes | None:
        """
        Baixa bytes em streaming e aborta antes de materializar corpo enorme.

        `httpx.Response.content` carrega tudo em memória. `aiter_bytes()` entrega
        bytes já descomprimidos, então o teto também cobre respostas comprimidas
        que expandem muito além do Content-Length transferido.
        """
        prefix = self._log_prefix()
        self.last_error = None
        try:
            async with self.client.stream("GET", url) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = 0
                    if declared > max_bytes:
                        raise ResponseTooLargeError(
                            f"Content-Length {declared} excede limite {max_bytes}"
                        )

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ResponseTooLargeError(
                            f"resposta excede limite de {max_bytes} bytes"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except ResponseTooLargeError as exc:
            self.last_error = str(exc)
            logger.warning(f"{prefix} resposta recusada: {exc}")
            return None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"{prefix} falha no download limitado: {type(exc).__name__}")
            return None

    async def _get_with_fallback(self, urls: list[str]) -> httpx.Response | None:
        """
        Tenta cada URL em ordem, com retry transitório por mirror.

        O último domínio funcional passa a ser priorizado nas buscas seguintes,
        evitando repetir timeouts conhecidos antes de chegar ao mirror saudável.

        Risco residual conhecido, e por que ele fica:
          O cliente segue redirects, e `self.base_url` passa a ser o destino
          final — inclusive um domínio novo, não declarado. Isso é
          DELIBERADO: sites de torrent trocam de domínio com frequência, e é
          essa adaptação que mantém o addon funcionando sem redeploy.

          A consequência é que um mirror configurado, se comprometido, pode
          redirecionar para endereço interno. Mas as URLs iniciais vêm de
          `_fallback_urls`, que é código, não de HTML de terceiro — o vetor
          que esta camada realmente precisava fechar era o href extraído da
          página, e esse está fechado em `_resolver_link`.

          Fechar também o redirect exige egress firewall na infraestrutura,
          não validação de nome na aplicação: por nome não há como distinguir
          "mirror novo legítimo" de "mirror comprometido".
        """
        prefix = self._log_prefix()
        self.last_error = None

        for url in self._prioritize_fallback_urls(urls):
            response = await self._get(url)
            if response is None:
                logger.debug(f"{prefix} Mirror falhou: {url} ({self.last_error})")
                continue

            parsed = urlparse(str(response.url))
            new_base = f"{parsed.scheme}://{parsed.netloc}"
            if new_base and new_base != self.base_url:
                if self._pode_adotar_origem(url, str(response.url)):
                    logger.info(f"{prefix} URL ativa: {new_base}")
                    self.base_url = new_base
                else:
                    logger.warning(
                        f"{prefix} redirect cruzou para host nao declarado "
                        f"({new_base}) — usado nesta requisicao, nao adotado"
                    )
            self.last_error = None
            return response

        logger.error(f"{prefix} Todas as URLs falharam: {urls}")
        return None

    @abstractmethod
    async def search(
        self,
        query: str,
        imdb_id: str,
        type: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> list[TorrentResult]:
        """Busca torrents — implementar em cada scraper"""
        ...

    async def close(self) -> None:
        """Fecha o cliente HTTP"""
        await self.client.aclose()
