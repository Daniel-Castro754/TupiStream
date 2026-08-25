"""BTIH canonicalization and source-security regressions."""

import base64
from unittest.mock import AsyncMock

import httpx
import pytest
from bs4 import BeautifulSoup
from pydantic import ValidationError

from app.info_hash import info_hash_from_magnet, normalize_info_hash
from app.models.torrent import TorrentResult
from app.scrapers.archive_org import ArchiveOrgScraper
from app.scrapers.base import BaseScraper
from app.scrapers.brazuca_addon import BrazucaAddonScraper
from app.scrapers.comando_filmes import ComandoFilmesScraper
from app.scrapers.yts import YTSScraper


class _DummyScraper(BaseScraper):
    name = "dummy"
    base_url = "https://allowed.example"

    async def search(self, query, imdb_id, type, season=None, episode=None):
        return []


def _torrent(hash_value):
    return TorrentResult(
        title="Filme", info_hash=hash_value,
        magnet="magnet:?xt=urn:btih:" + str(hash_value),
        quality="1080p", dubbed=False, source="Teste",
    )


class TestInfoHashCanonical:
    def test_accepts_exact_hex_and_normalizes(self):
        assert normalize_info_hash("  " + "A" * 40 + "  ") == "a" * 40

    @pytest.mark.parametrize("raw", ["", "a", "a" * 39, "a" * 41, "z" * 40, "abc&tr=x"])
    def test_rejects_malformed_values(self, raw):
        assert normalize_info_hash(raw) is None

    def test_accepts_lowercase_base32_and_converts_to_hex(self):
        raw_bytes = bytes(range(20))
        base32_hash = base64.b32encode(raw_bytes).decode().lower()
        assert len(base32_hash) == 32
        assert normalize_info_hash(base32_hash) == raw_bytes.hex()

    def test_extracts_complete_base32_xt_without_hex_prefix_truncation(self):
        raw_bytes = b"01234567890123456789"
        base32_hash = base64.b32encode(raw_bytes).decode().lower()
        magnet = f"magnet:?xt=urn:btih:{base32_hash}&dn=Serie"
        assert info_hash_from_magnet(magnet) == raw_bytes.hex()

    def test_model_is_a_final_backstop(self):
        with pytest.raises(ValidationError):
            _torrent("cafebabe")
        assert _torrent("A" * 40).info_hash == "a" * 40


class TestAffectedScrapers:
    def test_comando_parses_live_style_base32(self):
        raw_bytes = b"01234567890123456789"
        base32_hash = base64.b32encode(raw_bytes).decode().lower()
        soup = BeautifulSoup(
            f'<a href="magnet:?xt=urn:btih:{base32_hash}&dn=Serie">Magnet</a>',
            "html.parser",
        )
        magnet = ComandoFilmesScraper()._extrair_magnet(soup)
        assert info_hash_from_magnet(magnet) == raw_bytes.hex()

    def test_brazuca_skips_malformed_upstream_hash(self):
        assert BrazucaAddonScraper()._parsear_stream(
            {"infoHash": "not-a-hash", "title": "Filme"}
        ) is None

    def test_yts_skips_malformed_upstream_hash(self):
        assert YTSScraper()._parsear_torrent("Filme", {"hash": "abc"}) is None

    @pytest.mark.asyncio
    async def test_archive_identifier_stays_in_one_encoded_path_segment(self):
        scraper = ArchiveOrgScraper()
        scraper._get_bytes_limited = AsyncMock(return_value=None)
        try:
            assert await scraper._extrair_torrent("../item?admin=true#fragment", "Filme") is None
        finally:
            await scraper.close()

        requested_url = scraper._get_bytes_limited.await_args.args[0]
        assert requested_url.startswith("https://archive.org/download/")
        assert "../" not in requested_url
        assert "?admin=" not in requested_url
        assert "#fragment" not in requested_url
        assert "%2F" in requested_url and "%3F" in requested_url and "%23" in requested_url


@pytest.mark.asyncio
async def test_real_httpx_transport_does_not_auto_follow_disallowed_redirect():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example/secret"})

    scraper = _DummyScraper()
    await scraper.client.aclose()
    scraper.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    try:
        assert await scraper._get("https://allowed.example/start") is None
    finally:
        await scraper.close()
    assert calls == ["https://allowed.example/start"]


@pytest.mark.asyncio
async def test_https_redirect_cannot_downgrade_to_http():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            302, headers={"Location": "http://allowed.example/cleartext"}
        )

    scraper = _DummyScraper()
    await scraper.client.aclose()
    scraper.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    try:
        assert await scraper._get("https://allowed.example/start") is None
        assert "HTTPS" in (scraper.last_error or "")
    finally:
        await scraper.close()
    assert calls == ["https://allowed.example/start"]
