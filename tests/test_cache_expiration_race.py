"""Regressão: um leitor de entrada expirada não pode apagar um valor novo."""

import json
import time

import pytest

from app.services.cache import SQLiteCacheBackend


async def _backend_com_entrada_expirada(tmp_path):
    backend = SQLiteCacheBackend(db_path=str(tmp_path / "race.db"))
    await backend.init()
    await backend.set("chave", {"versao": "antiga"}, ttl=1)
    await backend._db.execute(
        "UPDATE stream_cache SET created_at = ? WHERE key = ?",
        (time.time() - 3600, "chave"),
    )
    await backend._db.commit()
    return backend


async def _intercalar_valor_novo_antes_do_delete(backend):
    """
    Intercepta exatamente o DELETE de expiração.

    Nesse instante get/get_with_status já leu a linha antiga e decidiu que
    expirou. Inserimos uma linha nova antes de deixar o DELETE prosseguir,
    reproduzindo deterministicamente a corrida que em produção depende de
    timing entre tasks.
    """
    execute_real = backend._db.execute
    intercalou = False

    async def execute(sql, parameters=None):
        nonlocal intercalou
        params = parameters or ()
        normalized = " ".join(str(sql).split()).upper()
        if not intercalou and normalized.startswith("DELETE FROM STREAM_CACHE"):
            intercalou = True
            agora = time.time()
            await execute_real(
                """
                INSERT OR REPLACE INTO stream_cache (key, value, created_at, ttl)
                VALUES (?, ?, ?, ?)
                """,
                ("chave", json.dumps({"versao": "nova"}), agora, 3600),
            )
            await backend._db.commit()
        return await execute_real(sql, params)

    backend._db.execute = execute
    return execute_real, lambda: intercalou


class TestGetNaoApagaValorNovo:
    @pytest.mark.asyncio
    async def test_get_expirado_preserva_set_concorrente(self, tmp_path):
        backend = await _backend_com_entrada_expirada(tmp_path)
        execute_real, intercalou = await _intercalar_valor_novo_antes_do_delete(backend)
        try:
            # A chamada leu a versao antiga, portanto pode retornar None.
            assert await backend.get("chave") is None
            assert intercalou()

            backend._db.execute = execute_real
            assert await backend.get("chave") == {"versao": "nova"}
        finally:
            backend._db.execute = execute_real
            await backend.close()

    @pytest.mark.asyncio
    async def test_get_with_status_preserva_set_concorrente(self, tmp_path):
        backend = await _backend_com_entrada_expirada(tmp_path)
        execute_real, intercalou = await _intercalar_valor_novo_antes_do_delete(backend)
        try:
            data, status = await backend.get_with_status("chave")
            assert data is None
            assert status == "expired"
            assert intercalou()

            backend._db.execute = execute_real
            data, status = await backend.get_with_status("chave")
            assert data == {"versao": "nova"}
            assert status == "hit"
        finally:
            backend._db.execute = execute_real
            await backend.close()
