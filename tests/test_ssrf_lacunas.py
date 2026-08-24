"""
As duas lacunas que a #26 deixou abertas.

1. `hdr_torrent` e `micoleao` validavam link com `href.startswith("http")`.
   Isso não é validação: aceita `localhost`, IP privado e o endpoint de
   metadados de nuvem. Estão desligados por padrão, mas isso é uma variável
   de ambiente de distância — e o `.env.example` os documenta como ativáveis.

2. `_get_with_fallback` adotava a origem final da resposta como novo
   `base_url`. O scraper é um **singleton** criado no startup, então um
   redirect para host arbitrário não contaminava uma requisição: contaminava
   o processo inteiro, e `_prioritize_fallback_urls` passava a preferir essa
   origem em todas as buscas seguintes.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.scrapers.hdr_torrent import HDRTorrentScraper
from app.scrapers.micoleao import MicoLeaoScraper

PAYLOADS = [
    "https://evil.example/?next=hdrtorrent.net",
    "https://www.hdrtorrent.net@169.254.169.254/latest/meta-data/",
    "https://www.hdrtorrent.net.evil.example/x",
    "http://127.0.0.1:8000/pagina",
    "http://169.254.169.254/",
    "http://10.0.0.5/pagina",
    "file:///etc/passwd",
]


class TestScrapersDesativadosAgoraValidam:
    @pytest.mark.parametrize("payload", PAYLOADS)
    @pytest.mark.asyncio
    async def test_hdr_recusa_destino_de_fora(self, payload):
        s = HDRTorrentScraper()
        try:
            assert s._resolver_link(payload, "https://www.hdrtorrent.net/?s=x") is None
        finally:
            await s.close()

    @pytest.mark.parametrize("payload", PAYLOADS)
    @pytest.mark.asyncio
    async def test_micoleao_recusa_destino_de_fora(self, payload):
        s = MicoLeaoScraper()
        try:
            assert s._resolver_link(payload, "https://www.micoleaodublado.net/?s=x") is None
        finally:
            await s.close()

    @pytest.mark.asyncio
    async def test_hdr_aceita_o_proprio_host(self):
        s = HDRTorrentScraper()
        try:
            assert s._resolver_link(
                "/filme-x/", "https://www.hdrtorrent.net/?s=x"
            ) == "https://www.hdrtorrent.net/filme-x/"
        finally:
            await s.close()

    @pytest.mark.asyncio
    async def test_startswith_http_nao_e_mais_o_criterio(self):
        """Todos os payloads acima comecam com http e mesmo assim sao recusados."""
        s = HDRTorrentScraper()
        try:
            comecam_com_http = [p for p in PAYLOADS if p.startswith("http")]
            assert len(comecam_com_http) >= 6
            for p in comecam_com_http:
                assert s._resolver_link(p, "https://www.hdrtorrent.net/") is None
        finally:
            await s.close()


def _resposta(url_final: str) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.url = url_final
    r.raise_for_status = MagicMock()
    return r


class TestRedirectNaoContaminaOSingleton:
    @pytest.mark.asyncio
    async def test_sem_troca_de_host_adota_normalmente(self):
        """
        A adaptacao a mirror continua funcionando: a origem veio da propria
        lista de mirrors, que e codigo, nao HTML de terceiro.
        """
        s = HDRTorrentScraper()
        try:
            s.client.get = AsyncMock(return_value=_resposta("https://www.hdrtorrent.net/p"))
            await s._get_with_fallback(["https://www.hdrtorrent.net/p"])
            assert s.base_url == "https://www.hdrtorrent.net"
        finally:
            await s.close()

    @pytest.mark.asyncio
    async def test_redirect_para_host_nao_declarado_nao_vira_estado(self):
        s = HDRTorrentScraper()
        original = s.base_url
        try:
            # pedimos um mirror declarado; a resposta veio de outro lugar
            s.client.get = AsyncMock(return_value=_resposta("http://169.254.169.254/x"))
            resposta = await s._get_with_fallback(["https://www.hdrtorrent.net/p"])

            assert resposta is not None, "a resposta desta requisicao continua sendo usada"
            assert s.base_url == original, (
                "o scraper e singleton — adotar contamina o processo inteiro"
            )
        finally:
            await s.close()

    @pytest.mark.asyncio
    async def test_redirect_entre_mirrors_declarados_e_adotado(self):
        s = MicoLeaoScraper()
        declarados = s._hosts_permitidos()
        assert len(declarados) >= 2, "o teste precisa de mais de um mirror declarado"
        outro = sorted(declarados)[0]
        try:
            s.client.get = AsyncMock(return_value=_resposta(f"https://{outro}/p"))
            await s._get_with_fallback(["https://www.micoleaodublado.net/p"])
            assert s.base_url == f"https://{outro}"
        finally:
            await s.close()
