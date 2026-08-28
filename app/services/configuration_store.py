"""Configurações privadas referenciadas por um ID opaco no manifest."""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.models.config import settings
from app.services.cache import cache

CONFIG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
CONFIG_CACHE_PREFIX = "configuration:v1:"


class ConfigurationNotFoundError(LookupError):
    """O ID não existe ou expirou."""


class ConfigurationCorruptError(RuntimeError):
    """O registro existe, mas não pode ser autenticado/decifrado."""


@dataclass(frozen=True)
class PrivateConfiguration:
    rd_token: str
    include_p2p: bool
    source_ids: tuple[str, ...]


def _read_or_create_file_key(path_value: str) -> bytes:
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return path.read_bytes().strip()
    except FileNotFoundError:
        key = Fernet.generate_key()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_bytes().strip()
        with os.fdopen(fd, "wb") as handle:
            handle.write(key + b"\n")
        return key


def _fernet() -> Fernet:
    key = (
        settings.CONFIG_ENCRYPTION_KEY.encode("ascii")
        if settings.CONFIG_ENCRYPTION_KEY
        else _read_or_create_file_key(settings.CONFIG_ENCRYPTION_KEY_FILE)
    )
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "CONFIG_ENCRYPTION_KEY inválida; use uma chave Fernet url-safe de 32 bytes"
        ) from exc


class ConfigurationStore:
    async def create(
        self,
        *,
        rd_token: str,
        include_p2p: bool,
        source_ids: tuple[str, ...],
    ) -> str:
        config_id = secrets.token_urlsafe(24)
        plaintext = json.dumps(
            {
                "rd_token": rd_token,
                "include_p2p": include_p2p,
                "source_ids": list(source_ids),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        ciphertext = _fernet().encrypt(plaintext).decode("ascii")
        await cache.set(
            CONFIG_CACHE_PREFIX + config_id,
            {"version": 1, "ciphertext": ciphertext},
            ttl=settings.CONFIG_TTL_SECONDS,
        )
        return config_id

    async def get(self, config_id: str) -> PrivateConfiguration:
        if not CONFIG_ID_RE.fullmatch(config_id):
            raise ConfigurationNotFoundError
        record = await cache.get(CONFIG_CACHE_PREFIX + config_id)
        if not isinstance(record, dict):
            raise ConfigurationNotFoundError
        ciphertext = record.get("ciphertext")
        if not isinstance(ciphertext, str):
            raise ConfigurationCorruptError
        try:
            plaintext = _fernet().decrypt(ciphertext.encode("ascii"))
            payload = json.loads(plaintext)
            rd_token = payload["rd_token"]
            include_p2p = payload["include_p2p"]
            source_ids = payload["source_ids"]
        except (InvalidToken, UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise ConfigurationCorruptError from exc
        if (
            not isinstance(rd_token, str)
            or not rd_token
            or not isinstance(include_p2p, bool)
            or not isinstance(source_ids, list)
            or not source_ids
            or not all(isinstance(item, str) for item in source_ids)
        ):
            raise ConfigurationCorruptError
        return PrivateConfiguration(rd_token, include_p2p, tuple(source_ids))


configuration_store = ConfigurationStore()
