import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

LINK_READY_RETRY_DELAYS: tuple[float, ...] = (0.75, 0.75)
# Allowlist de conteineres de video.
#
# Antes isto era uma blacklist de extensoes invalidas:
#     (".txt", ".nfo", ".srt", ".jpg", ".png", ".exe")
# Blacklist de formato e sempre incompleta. Releases de cena empacotados em
# .rar/.r00/.zip passavam pelo filtro e, como para filmes a escolha e pelo
# MAIOR arquivo, o .rar era justamente o escolhido: o Real-Debrid
# desbloqueava um arquivo comprimido e o player quebrava na reproducao.
#
# Inverter para allowlist troca "esqueci de bloquear X" (quebra o playback)
# por "esqueci de permitir Y" (cai no erro tratado de torrent indisponivel).
VIDEO_FILE_EXTENSIONS = (
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".webm", ".wmv", ".flv", ".ogm", ".ogv",
    ".divx", ".vob", ".rmvb", ".3gp", ".asf", ".mts",
)
INVALID_PATH_WORDS = ("sample", "trailer", "extras")

# Folga minima para valer a pena iniciar mais uma consulta ao RD. Comecar uma
# chamada de 15s faltando 2s de orcamento so joga fora o trabalho ja feito.
MARGEM_PARA_NOVA_CONSULTA_SECONDS = 1.0

_EPISODE_MARKER_PATTERNS = (
    re.compile(r"(?<![a-z0-9])s\d{1,2}e\d{1,3}(?!\d)", re.IGNORECASE),
    re.compile(r"(?<!\d)\d{1,2}x\d{1,3}(?!\d)", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])t\d{1,2}e\d{1,3}(?!\d)", re.IGNORECASE),
    re.compile(
        r"temporada\s*\d{1,2}.{0,20}?epis[oó]dio\s*\d{1,3}",
        re.IGNORECASE,
    ),
)


def _summarize_http_error(exc: httpx.HTTPStatusError) -> str:
    """Resume erro HTTP sem expor URLs sensiveis ou payloads."""
    response = exc.response
    request = response.request
    return f"HTTP {response.status_code} em {request.method}"


def _parse_episode_target(stremio_id: str) -> tuple[int, int] | None:
    parts = stremio_id.split(":")
    if len(parts) < 3:
        return None
    try:
        season = int(parts[1])
        episode = int(parts[2])
    except (TypeError, ValueError):
        return None
    if season < 0 or episode < 0:
        return None
    return season, episode


def _path_has_episode_marker(path: str) -> bool:
    return any(pattern.search(path) for pattern in _EPISODE_MARKER_PATTERNS)


def _path_matches_episode(path: str, season: int, episode: int) -> bool:
    normalized = path.lower()
    patterns = (
        re.compile(
            rf"(?<![a-z0-9])s0*{season}e0*{episode}(?!\d)",
            re.IGNORECASE,
        ),
        re.compile(rf"(?<!\d)0*{season}x0*{episode}(?!\d)", re.IGNORECASE),
        re.compile(
            rf"(?<![a-z0-9])t0*{season}e0*{episode}(?!\d)",
            re.IGNORECASE,
        ),
        re.compile(
            rf"temporada\s*0*{season}.{0,20}?epis[oó]dio\s*0*{episode}(?!\d)",
            re.IGNORECASE,
        ),
    )
    return any(pattern.search(normalized) for pattern in patterns)


@dataclass
class EstadoPlayback:
    """
    Progresso do fluxo Real-Debrid, para que um retry retome em vez de
    recomecar.

    `torrent_id` era variavel LOCAL de get_stream_url. Cada retry apos 503
    ou 504 refazia o addMagnet — e o RD cria um torrent novo a cada chamada,
    entao a conta do usuario acumulava um torrent por tentativa. E o proprio
    503 devolve Retry-After, convidando o cliente a repetir.

    Medido com 3 tentativas ate o torrent ficar pronto:
        sem persistir  ->  3 addMagnet
        persistindo    ->  1 addMagnet + 2 reaproveitamentos
    """

    rd_torrent_id: str | None = None
    selected_file_id: str | None = None


class RealDebridError(Exception):
    """Erro base do fluxo de resolucao via Real-Debrid."""


class RealDebridPlaybackNotReadyError(RealDebridError):
    """Torrent ainda nao esta pronto para playback imediato."""


class RealDebridResolveError(RealDebridError):
    """Falha operacional ao resolver um link via Real-Debrid."""


class RealDebridTimeoutError(RealDebridError):
    """
    O deadline do playback estourou antes de o fluxo terminar.

    Separada de RealDebridResolveError de proposito: "o upstream demorou
    demais" e "o upstream falhou" pedem codigos HTTP diferentes (504 x 502)
    e acoes diferentes de quem consome.
    """


class RealDebridService:
    """Cliente para a API do Real-Debrid."""

    def __init__(
        self,
        api_token: str,
        req_id: str | None = None,
        play_ref: str | None = None,
    ) -> None:
        self.base_url = "https://api.real-debrid.com/rest/1.0"
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=15.0,
        )
        self._deadline: float | None = None
        prefix = []
        if req_id:
            prefix.append(f"[{req_id}]")
        prefix.append("[PLAY]")
        prefix.append(f"[RD {play_ref}]" if play_ref else "[RD]")
        self.log_prefix = " ".join(prefix)

    def _log(self, stage: str, message: str, level: int = logging.INFO) -> None:
        """Centraliza logs curtos do fluxo RD sem expor token ou URL final."""
        logger.log(level, f"{self.log_prefix} {stage} -> {message}")

    async def _get_torrent_info(self, torrent_id: str, purpose: str) -> dict:
        """Lê o estado atual do torrent no RD."""
        self._log("info", purpose)
        resp_info = await self.client.get(f"{self.base_url}/torrents/info/{torrent_id}")
        resp_info.raise_for_status()
        return resp_info.json()

    def _ha_orcamento_para_nova_consulta(self, espera: float) -> bool:
        """
        Sobra tempo para dormir `espera` e ainda fazer outra consulta?

        Sem esta checagem, `_wait_for_links` decidia continuar olhando so o
        contador de tentativas. A terceira consulta de 15s podia comecar
        faltando 2s de orcamento — o deadline cortava no meio e o trabalho
        ja feito (addMagnet, selectFiles) ia junto, sem nem uma resposta
        util para o cliente.
        """
        if self._deadline is None:
            return True
        restante = self._deadline - asyncio.get_running_loop().time()
        return restante > espera + MARGEM_PARA_NOVA_CONSULTA_SECONDS

    async def _wait_for_links(self, torrent_id: str) -> list[str]:
        """
        Faz retries curtos e controlados apos selectFiles.

        Motivo operacional:
          O RD pode levar alguns instantes para popular `links` logo apos o
          `selectFiles`. Um retry curto reduz falso negativo imediato sem
          transformar /play em polling longo.
        """
        total_attempts = 1 + len(LINK_READY_RETRY_DELAYS)
        for attempt in range(1, total_attempts + 1):
            torrent_info = await self._get_torrent_info(
                torrent_id,
                f"checando links ({attempt}/{total_attempts})",
            )
            links = torrent_info.get("links", [])
            if links:
                self._log("info", f"links prontos ({attempt}/{total_attempts})")
                return links

            status = torrent_info.get("status", "desconhecido")
            if attempt < total_attempts:
                delay = LINK_READY_RETRY_DELAYS[attempt - 1]
                if not self._ha_orcamento_para_nova_consulta(delay):
                    self._log(
                        "info",
                        f"sem orcamento para nova consulta (status={status})",
                        level=logging.WARNING,
                    )
                    raise RealDebridPlaybackNotReadyError(
                        "Torrent temporariamente indisponivel no Real-Debrid. "
                        "Tente novamente em instantes."
                    )
                self._log(
                    "info",
                    f"sem links ainda (status={status}), retry em {delay:.2f}s",
                )
                await asyncio.sleep(delay)
                continue

            self._log(
                "info",
                f"sem links apos {total_attempts} consultas curtas (status={status})",
                level=logging.WARNING,
            )
            raise RealDebridPlaybackNotReadyError(
                "Torrent temporariamente indisponivel no Real-Debrid. Tente novamente em instantes."
            )

    def _select_file_id(self, files: list[dict], type: str, stremio_id: str) -> str:
        valid_files = []
        for file_info in files:
            path = str(file_info.get("path") or "").lower()
            if not path:
                continue
            if not path.endswith(VIDEO_FILE_EXTENSIONS):
                continue
            if any(word in path for word in INVALID_PATH_WORDS):
                continue
            if "id" not in file_info or "bytes" not in file_info:
                continue
            valid_files.append(file_info)

        if not valid_files:
            self._log("info", "nenhum arquivo de video valido", level=logging.WARNING)
            raise RealDebridPlaybackNotReadyError(
                "Torrent temporariamente indisponivel no Real-Debrid. Tente novamente em instantes."
            )

        if type != "series":
            largest_file = max(valid_files, key=lambda item: item["bytes"])
            return str(largest_file["id"])

        target = _parse_episode_target(stremio_id)
        if target is None:
            self._log("selectFiles", "identificacao de episodio invalida", level=logging.WARNING)
            raise RealDebridPlaybackNotReadyError(
                "Nao foi possivel identificar a temporada e o episodio solicitados."
            )

        season, episode = target
        matching_files = [
            file_info
            for file_info in valid_files
            if _path_matches_episode(str(file_info["path"]), season, episode)
        ]
        if matching_files:
            # Pode haver duas versões do mesmo episódio; escolhe a maior delas.
            selected = max(matching_files, key=lambda item: item["bytes"])
            return str(selected["id"])

        # Release de episódio avulso pode ter um único vídeo com nome genérico.
        # Só aceita esse caso quando não existe marcador explícito de outro
        # episódio; pacotes com vários arquivos nunca caem no "maior arquivo".
        if len(valid_files) == 1:
            only_file = valid_files[0]
            if not _path_has_episode_marker(str(only_file["path"])):
                return str(only_file["id"])

        self._log(
            "selectFiles",
            f"episodio S{season:02d}E{episode:02d} nao encontrado no torrent",
            level=logging.WARNING,
        )
        raise RealDebridPlaybackNotReadyError(
            f"O episodio S{season:02d}E{episode:02d} nao foi encontrado neste torrent."
        )

    async def get_stream_url(
        self,
        magnet: str,
        type: str = "movie",
        stremio_id: str = "",
        deadline: float | None = None,
        estado: "EstadoPlayback | None" = None,
    ) -> str:
        """
        Resolve um magnet aplicando um teto agregado ao fluxo inteiro.

        `deadline` e um instante absoluto no relogio do event loop. Com None
        o comportamento e identico ao anterior — sem teto — para nao mudar
        nada em quem ja chamava assim.

        Por que existe: o fluxo sao ate 7 chamadas sequenciais de 15s cada,
        mais 1,5s de sleep entre retries. Pior caso 106,5s, sem nenhum limite
        agregado, enquanto o Stremio corta em ~20s. O servidor seguia
        trabalhando muito depois de o cliente ter desistido.
        """
        self._deadline = deadline
        # Sem `estado`, cada chamada comeca do zero — comportamento identico
        # ao anterior para quem ja chamava assim.
        estado = estado if estado is not None else EstadoPlayback()

        if deadline is None:
            return await self._resolver(magnet, type, stremio_id, estado)

        try:
            async with asyncio.timeout_at(deadline):
                return await self._resolver(magnet, type, stremio_id, estado)
        except TimeoutError as exc:
            self._log("deadline", "orcamento de playback esgotado", level=logging.ERROR)
            raise RealDebridTimeoutError(
                "Tempo esgotado ao resolver o playback via Real-Debrid"
            ) from exc

    async def _resolver(
        self,
        magnet: str,
        type: str = "movie",
        stremio_id: str = "",
        estado: "EstadoPlayback | None" = None,
    ) -> str:
        """
        Resolve um magnet no clique usando apenas endpoints suportados.

        Fluxo lazy:
          1. addMagnet
          2. info para inspecionar arquivos
          3. selectFiles
          4. info para obter links
          5. unrestrict/link
        """
        estado = estado if estado is not None else EstadoPlayback()
        stage = "addMagnet"
        try:
            # Etapa 1 — addMagnet. Retomavel: se uma tentativa anterior ja
            # criou o torrent, refazer isso criaria OUTRO na conta do usuario.
            if estado.rd_torrent_id:
                torrent_id = estado.rd_torrent_id
                self._log("addMagnet", "reaproveitando torrent de tentativa anterior")
            else:
                self._log("addMagnet", "enviando magnet")
                resp_add = await self.client.post(
                    f"{self.base_url}/torrents/addMagnet",
                    data={"magnet": magnet},
                )
                resp_add.raise_for_status()
                torrent_id = resp_add.json()["id"]
                estado.rd_torrent_id = torrent_id
                self._log("addMagnet", "torrent criado")

            # Etapa 2 — escolher e selecionar o arquivo. Tambem retomavel:
            # a selecao ja feita continua valendo no torrent do RD.
            if estado.selected_file_id:
                self._log("selectFiles", "arquivo ja selecionado em tentativa anterior")
            else:
                stage = "torrents/info"
                torrent_info = await self._get_torrent_info(
                    torrent_id,
                    "lendo arquivos do torrent",
                )
                selected_file_id = self._select_file_id(
                    torrent_info.get("files", []),
                    type,
                    stremio_id,
                )
                estado.selected_file_id = selected_file_id

                stage = "selectFiles"
                self._log("selectFiles", "selecionando arquivo principal")
                resp_select = await self.client.post(
                    f"{self.base_url}/torrents/selectFiles/{torrent_id}",
                    data={"files": selected_file_id},
                )
                resp_select.raise_for_status()
                self._log("selectFiles", "arquivo selecionado")

            stage = "torrents/info.links"
            links = await self._wait_for_links(torrent_id)

            stage = "unrestrict/link"
            self._log("unrestrict/link", "gerando link HTTP")
            resp_unrestrict = await self.client.post(
                f"{self.base_url}/unrestrict/link",
                data={"link": links[0]},
            )
            resp_unrestrict.raise_for_status()
            download_url: str = resp_unrestrict.json()["download"]

            self._log("unrestrict/link", "link HTTP resolvido")
            return download_url

        except RealDebridPlaybackNotReadyError:
            raise
        except httpx.HTTPStatusError as exc:
            self._log(
                stage,
                f"falha HTTP: {_summarize_http_error(exc)}",
                level=logging.ERROR,
            )
            raise RealDebridResolveError(
                "Falha ao resolver playback via Real-Debrid"
            ) from exc
        except Exception as exc:
            self._log(
                stage,
                f"falha inesperada: {exc.__class__.__name__}",
                level=logging.ERROR,
            )
            raise RealDebridResolveError(
                "Falha inesperada ao resolver playback via Real-Debrid"
            ) from exc

    async def close(self) -> None:
        """Fecha o cliente HTTP."""
        await self.client.aclose()
