import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.scrapers.base import BaseScraper, set_req_id


class _DummyScraper(BaseScraper):
    name = "Dummy"
    base_url = "http://dummy-original.com"

    async def search(self, query, imdb_id, type, season=None, episode=None):
        return []


def _ok_response(url: str) -> MagicMock:
    resposta = MagicMock(status_code=200)
    resposta.raise_for_status = MagicMock()
    resposta.url = url
    return resposta


class TestGetComRetryEsgotado:
    @pytest.mark.asyncio
    async def test_5xx_persistente_esgota_tentativas_e_retorna_none(self):
        scraper = _DummyScraper()
        scraper.client.get = AsyncMock(return_value=MagicMock(status_code=503))

        with patch("app.scrapers.base.asyncio.sleep", AsyncMock()):
            resultado = await scraper._get("http://dummy/x")

        assert resultado is None
        assert scraper.client.get.await_count == 2  # DEFAULT_RETRIES=1 -> 2 tentativas
        assert "503" in scraper.last_error
        await scraper.close()


class TestGetWithFallback:
    @pytest.mark.asyncio
    async def test_primeira_url_funciona_direto(self):
        scraper = _DummyScraper()
        ok = _ok_response("https://mirror1.com/pagina")
        scraper.client.get = AsyncMock(return_value=ok)

        resultado = await scraper._get_with_fallback(
            ["https://mirror1.com/pagina", "https://mirror2.com/pagina"]
        )

        assert resultado is ok
        assert scraper.client.get.await_count == 1
        await scraper.close()

    @pytest.mark.asyncio
    async def test_primeiro_mirror_esgota_retry_e_cai_para_segundo(self):
        scraper = _DummyScraper()
        ok = _ok_response("https://mirror2.com/pagina")
        scraper.client.get = AsyncMock(
            side_effect=[
                httpx.ConnectError("recusado 1"),
                httpx.ConnectError("recusado 2"),
                ok,
            ]
        )

        with patch("app.scrapers.base.asyncio.sleep", AsyncMock()):
            resultado = await scraper._get_with_fallback(
                ["https://mirror1.com/pagina", "https://mirror2.com/pagina"]
            )

        assert resultado is ok
        assert scraper.client.get.await_count == 3
        await scraper.close()

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_so_adota_base_url_de_host_na_allowlist(self):
        """
        REVISTO. Este teste afirmava que qualquer origem final de redirect
        virava o novo base_url — que era exatamente o vetor de persistencia
        do SSRF: o scraper e um singleton criado no startup, e
        _prioritize_fallback_urls passa a PREFERIR esse destino, entao um
        redirect malicioso contaminava todas as buscas seguintes.

        Agora a adocao so acontece se a origem ja estiver na allowlist
        derivada de base_url + _fallback_urls.
        """
        scraper = _DummyScraper()
        original = scraper.base_url

        # host fora da allowlist: nao adota
        resp = MagicMock()
        resp.url = "https://mirror-novo.com/pagina"
        scraper._get = AsyncMock(return_value=resp)
        await scraper._get_with_fallback(["http://dummy-original.com/x"])
        assert scraper.base_url == original, "host desconhecido nao pode reapontar o scraper"

        # host que ESTA na allowlist: adota
        permitido = sorted(scraper._hosts_permitidos())[0]
        resp2 = MagicMock()
        resp2.url = f"https://{permitido}/pagina"
        scraper._get = AsyncMock(return_value=resp2)
        await scraper._get_with_fallback(["http://dummy-original.com/x"])
        assert permitido in scraper.base_url

class TestLastErrorPorRequest:
    @pytest.mark.asyncio
    async def test_buscas_simultaneas_nao_sobrescrevem_last_error(self):
        scraper = _DummyScraper()
        ready = asyncio.Event()
        started = 0
        lock = asyncio.Lock()

        async def worker(req_id: str, error: str) -> str | None:
            nonlocal started
            set_req_id(req_id)
            scraper.last_error = error
            async with lock:
                started += 1
                if started == 2:
                    ready.set()
            await ready.wait()
            await asyncio.sleep(0)
            return scraper.last_error

        error_a, error_b = await asyncio.gather(
            worker("req-a", "erro-a"),
            worker("req-b", "erro-b"),
        )

        assert error_a == "erro-a"
        assert error_b == "erro-b"
        await scraper.close()
