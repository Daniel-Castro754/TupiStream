"""Decoder bencode limitado para metainfo BitTorrent (BEP 3)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

MAX_BENCODE_BYTES = 4 * 1024 * 1024
MAX_BENCODE_DEPTH = 32
MAX_BENCODE_NODES = 100_000
MAX_INTEGER_DIGITS = 20
MAX_LENGTH_DIGITS = 10


class BencodeDecodeError(ValueError):
    """Bencode malformado ou fora dos limites de segurança."""


@dataclass
class _Parser:
    data: bytes
    pos: int = 0
    nodes: int = 0

    def _fail(self, message: str) -> BencodeDecodeError:
        return BencodeDecodeError(f"{message} (offset {self.pos})")

    def _peek(self) -> bytes:
        if self.pos >= len(self.data):
            raise self._fail("fim inesperado")
        return self.data[self.pos : self.pos + 1]

    def _count_node(self, depth: int) -> None:
        if depth > MAX_BENCODE_DEPTH:
            raise self._fail(f"profundidade excede {MAX_BENCODE_DEPTH}")
        self.nodes += 1
        if self.nodes > MAX_BENCODE_NODES:
            raise self._fail(f"quantidade de valores excede {MAX_BENCODE_NODES}")

    def read(self, depth: int = 0) -> object:
        self._count_node(depth)
        marker = self._peek()

        if marker == b"i":
            return self._read_integer()
        if marker == b"l":
            return self._read_list(depth)
        if marker == b"d":
            return self._read_dict(depth)
        if b"0" <= marker <= b"9":
            return self._read_bytes()
        raise self._fail(f"marcador invalido {marker!r}")

    def _read_integer(self) -> int:
        self.pos += 1
        end = self.data.find(b"e", self.pos)
        if end < 0:
            raise self._fail("inteiro sem terminador")
        raw = self.data[self.pos:end]
        digits = raw[1:] if raw.startswith(b"-") else raw
        if not digits or not digits.isdigit():
            raise self._fail("inteiro invalido")
        if len(digits) > MAX_INTEGER_DIGITS:
            raise self._fail("inteiro grande demais")
        if (digits.startswith(b"0") and len(digits) > 1) or raw == b"-0":
            raise self._fail("inteiro com zero inicial")
        self.pos = end + 1
        return int(raw)

    def _read_bytes(self) -> bytes:
        colon = self.data.find(b":", self.pos)
        if colon < 0:
            raise self._fail("string sem separador")
        raw_length = self.data[self.pos:colon]
        if not raw_length.isdigit():
            raise self._fail("comprimento invalido")
        if len(raw_length) > MAX_LENGTH_DIGITS:
            raise self._fail("comprimento grande demais")
        if raw_length.startswith(b"0") and len(raw_length) > 1:
            raise self._fail("comprimento com zero inicial")
        length = int(raw_length)
        start = colon + 1
        end = start + length
        if end > len(self.data):
            raise self._fail("string truncada")
        self.pos = end
        return self.data[start:end]

    def _read_list(self, depth: int) -> list:
        self.pos += 1
        result = []
        while True:
            marker = self._peek()
            if marker == b"e":
                self.pos += 1
                return result
            result.append(self.read(depth + 1))

    def _read_dict(self, depth: int) -> dict:
        self.pos += 1
        result = {}
        while True:
            marker = self._peek()
            if marker == b"e":
                self.pos += 1
                return result
            key = self.read(depth + 1)
            if not isinstance(key, bytes):
                raise self._fail("chave de dicionario nao e bytes")
            if key in result:
                raise self._fail("chave duplicada")
            result[key] = self.read(depth + 1)


def parse_torrent(data: bytes) -> tuple[dict, str | None]:
    """
    Retorna o dicionário e SHA-1 dos bytes BRUTOS do valor `info`.

    Dados que nem começam como dicionário continuam retornando ({}, None)
    por compatibilidade. Bencode iniciado mas malformado levanta a exceção
    controlada `BencodeDecodeError`.
    """
    if data[:1] != b"d":
        return {}, None
    if len(data) > MAX_BENCODE_BYTES:
        raise BencodeDecodeError(f"torrent excede {MAX_BENCODE_BYTES} bytes")

    parser = _Parser(data=data, pos=1, nodes=1)  # dicionário top-level já contado
    top: dict = {}
    info_hash: str | None = None

    while True:
        marker = parser._peek()
        if marker == b"e":
            parser.pos += 1
            break

        key = parser.read(depth=1)
        if not isinstance(key, bytes):
            raise BencodeDecodeError("chave top-level nao e bytes")
        if key in top:
            raise BencodeDecodeError("chave top-level duplicada")

        value_start = parser.pos
        value = parser.read(depth=1)
        top[key] = value
        if key == b"info":
            info_hash = hashlib.sha1(data[value_start:parser.pos]).hexdigest()

    if parser.pos != len(data):
        raise BencodeDecodeError(f"bytes extras apos objeto top-level: {len(data) - parser.pos}")

    return top, info_hash
