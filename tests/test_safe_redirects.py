"""Redirects manuais: todo destino é validado antes da rede."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.scrapers.base import BaseScraper, MAX_REDIRECTS


class _Scraper(BaseScraper):
    name = "Redirect Test"
    base_url = "https://origin.example"
    _fallback_urls = ["https://origin.example", "https://mirror.example"]

    async def search(self, query, imdb_id, type, season=None, episode=None):
        return []


def _response(status, url, location=None):
    response = MagicMock(status_code=status)
    response.url = url
    response.headers = {} if location is None else {"location": location}
    response.raise_for_status = MagicMock()
    return response


class TestRedirectsSeguros:
    @pytest.mark.asyncio
    async def test_redirect_relativo_no_mesmo_host_e_seguido(self):
        scraper = _Scraper()
        redirect = _response(302, "https://origin.example/a", "/b")
        final = _response(200, "https://origin.example/b")
        scraper.client.get = AsyncMock(side_effect=[redirect, final])
        try:
            result = await scraper._get("https://origin.example/a")
            assert result is final
            assert [call.args[0] for call in scraper.client.get.await_args_list] == [
                "https://origin.example/a",
                "https://origin.example/b",
            ]
        finally:
            await scraper.close()

    @pytest.mark.asyncio
    async def test_redirect_entre_mirrors_declarados_e_seguido(self):
        scraper = _Scraper()
        redirect = _response(301, "https://origin.example/a", "https://mirror.example/b")
        final = _response(200, "https://mirror.example/b")
        scraper.client.get = AsyncMock(side_effect=[redirect, final])
        try:
            assert await scraper._get("https://origin.example/a") is final
            assert scraper.client.get.await_count == 2
        finally:
            await scraper.close()

    @pytest.mark.parametrize(
        "location",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8000/admin",
            "http://10.0.0.1/private",
            "https://evil.example/x",
            "file:///etc/passwd",
        ],
    )
    @pytest.mark.asyncio
    async def test_redirect_inseguro_nao_recebe_segunda_requisicao(self, location):
        scraper = _Scraper()
        redirect = _response(302, "https://origin.example/a", location)
        scraper.client.get = AsyncMock(return_value=redirect)
        try:
            assert await scraper._get("https://origin.example/a") is None
            assert scraper.client.get.await_count == 1
        finally:
            await scraper.close()

    @pytest.mark.asyncio
    async def test_ip_privado_inicial_e_recusado_antes_da_rede(self):
        class _PrivateScraper(_Scraper):
            base_url = "http://127.0.0.1"
            _fallback_urls = ["http://127.0.0.1"]

        scraper = _PrivateScraper()
        scraper.client.get = AsyncMock()
        try:
            assert await scraper._get("http://127.0.0.1/x") is None
            scraper.client.get.assert_not_awaited()
        finally:
            await scraper.close()

    @pytest.mark.asyncio
    async def test_cadeia_acima_do_limite_e_interrompida(self):
        scraper = _Scraper()
        scraper.client.get = AsyncMock(
            side_effect=[
                _response(
                    302,
                    f"https://origin.example/{i}",
                    f"https://origin.example/{i + 1}",
                )
                for i in range(MAX_REDIRECTS + 1)
            ]
        )
        try:
            assert await scraper._get("https://origin.example/0") is None
            assert scraper.client.get.await_count == MAX_REDIRECTS + 1
            assert "redirects" in (scraper.last_error or "")
        finally:
            await scraper.close()

    @pytest.mark.asyncio
    async def test_redirect_sem_location_e_rejeitado(self):
        scraper = _Scraper()
        scraper.client.get = AsyncMock(return_value=_response(302, "https://origin.example/a"))
        try:
            assert await scraper._get("https://origin.example/a") is None
            assert "Location" in (scraper.last_error or "")
        finally:
            await scraper.close()
