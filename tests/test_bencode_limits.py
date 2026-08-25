"""Limites do download de .torrent e do parser bencode."""

from unittest.mock import MagicMock

import pytest

import app.scrapers.bencode as bencode
from app.scrapers.base import BaseScraper
from app.scrapers.bencode import BencodeDecodeError, parse_torrent


class _Scraper(BaseScraper):
    name = "Teste"
    base_url = "https://example.com"

    async def search(self, query, imdb_id, type, season=None, episode=None):
        return []


class _StreamResponse:
    def __init__(self, chunks, content_length=None):
        self.status_code = 200
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self._chunks = chunks

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class TestDownloadLimitado:
    @pytest.mark.asyncio
    async def test_content_length_acima_do_limite_e_recusado_sem_ler(self):
        scraper = _Scraper()
        response = _StreamResponse([b"nao deve ser lido"], content_length=101)
        scraper.client.stream = MagicMock(return_value=response)
        try:
            assert await scraper._get_bytes_limited("https://example.com/x", 100) is None
            assert "Content-Length" in scraper.last_error
        finally:
            await scraper.close()

    @pytest.mark.asyncio
    async def test_chunked_que_cruza_o_limite_e_recusado(self):
        scraper = _Scraper()
        response = _StreamResponse([b"a" * 60, b"b" * 41])
        scraper.client.stream = MagicMock(return_value=response)
        try:
            assert await scraper._get_bytes_limited("https://example.com/x", 100) is None
            assert "excede limite" in scraper.last_error
        finally:
            await scraper.close()

    @pytest.mark.asyncio
    async def test_bytes_dentro_do_limite_sao_retornados(self):
        scraper = _Scraper()
        response = _StreamResponse([b"abc", b"def"], content_length=6)
        scraper.client.stream = MagicMock(return_value=response)
        try:
            assert await scraper._get_bytes_limited("https://example.com/x", 100) == b"abcdef"
        finally:
            await scraper.close()


class TestBencodeLimits:
    def test_profundidade_acima_do_limite_e_controlada(self):
        payload = (
            b"d4:info"
            + b"l" * (bencode.MAX_BENCODE_DEPTH + 1)
            + b"e" * (bencode.MAX_BENCODE_DEPTH + 1)
            + b"e"
        )
        with pytest.raises(BencodeDecodeError, match="profundidade"):
            parse_torrent(payload)

    def test_trailing_bytes_sao_rejeitados(self):
        with pytest.raises(BencodeDecodeError, match="bytes extras"):
            parse_torrent(b"d4:infod1:a1:beeLIXO")

    @pytest.mark.parametrize(
        "payload",
        [
            b"d4:info9999999:xee",
            b"d4:infoi123e",
            b"d4:infol1:ae",
            b"d4:infoi-0ee",
            b"d4:infoi01ee",
        ],
    )
    def test_payload_malformado_levanta_excecao_controlada(self, payload):
        with pytest.raises(BencodeDecodeError):
            parse_torrent(payload)

    def test_tamanho_total_e_limitado(self, monkeypatch):
        payload = b"d4:infod1:a1:bee"
        monkeypatch.setattr(bencode, "MAX_BENCODE_BYTES", len(payload) - 1)
        with pytest.raises(BencodeDecodeError, match="excede"):
            parse_torrent(payload)

    def test_quantidade_de_nos_e_limitada(self, monkeypatch):
        monkeypatch.setattr(bencode, "MAX_BENCODE_NODES", 5)
        with pytest.raises(BencodeDecodeError, match="quantidade"):
            parse_torrent(b"d4:infol1:a1:b1:c1:dee")

    def test_torrent_valido_preserva_hash_dos_bytes_brutos(self):
        import hashlib

        info = b"d4:name4:filee"
        payload = b"d4:info" + info + b"e"
        top, info_hash = parse_torrent(payload)
        assert top[b"info"] == {b"name": b"file"}
        assert info_hash == hashlib.sha1(info).hexdigest()
