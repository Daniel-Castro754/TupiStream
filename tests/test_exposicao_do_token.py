"""
Mitigação da exposição do token Real-Debrid.

Isto **não** tira o token da URL — essa correção quebra toda instalação
existente e é decisão de produto. O que dá para fazer sem quebrar ninguém:

1. parar de afirmar no README que o servidor não guarda tokens (guarda, por
   até 30 minutos, em texto puro no disco persistente);
2. não exibir o token na tela ao digitá-lo;
3. impedir que a resposta seja cacheada e que o caminho — com o token — vaze
   como `Referer` numa navegação que parta dali.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routes.configure import CONFIG_HTML_TEMPLATE

TOKEN = "TOKEN-DE-TESTE-123"
HEADERS_ESPERADOS = {
    "cache-control": "no-store, private",
    "referrer-policy": "no-referrer",
    "x-robots-tag": "noindex, nofollow",
}


async def _get(caminho: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(caminho)


class TestHeadersNasRotasComToken:
    @pytest.mark.parametrize(
        "caminho",
        [
            f"/{TOKEN}/manifest.json",
            f"/hybrid/{TOKEN}/manifest.json",
        ],
    )
    @pytest.mark.asyncio
    async def test_manifest_protege_a_resposta(self, caminho):
        r = await _get(caminho)
        assert r.status_code == 200
        for header, valor in HEADERS_ESPERADOS.items():
            assert r.headers.get(header) == valor, header

    @pytest.mark.asyncio
    async def test_manifest_sem_token_nao_precisa_dos_headers(self):
        """A rota sem token nao carrega segredo — nao ha o que proteger."""
        r = await _get("/manifest.json")
        assert r.status_code == 200
        assert r.headers.get("cache-control") != "no-store, private"

    @pytest.mark.asyncio
    async def test_o_token_nao_aparece_no_corpo_do_manifest(self):
        r = await _get(f"/{TOKEN}/manifest.json")
        assert TOKEN not in r.text


class TestPaginaDeConfiguracao:
    def test_campo_do_token_nao_exibe_o_valor(self):
        assert 'type="password" id="rd-token"' in CONFIG_HTML_TEMPLATE
        assert 'type="text" id="rd-token"' not in CONFIG_HTML_TEMPLATE

    def test_continua_sem_autocomplete(self):
        i = CONFIG_HTML_TEMPLATE.index('id="rd-token"')
        campo = CONFIG_HTML_TEMPLATE[i - 60:i + 160]
        assert 'autocomplete="off"' in campo


class TestReadmeNaoMenteMais:
    def test_nao_afirma_que_o_servidor_nao_guarda_token(self):
        import pathlib

        readme = pathlib.Path(__file__).resolve().parents[1] / "README.md"
        texto = readme.read_text(encoding="utf-8")
        assert "não armazena tokens" not in texto, (
            "o servidor guarda o token nas play sessions por ate 30 minutos"
        )

    def test_diz_onde_o_token_fica(self):
        import pathlib

        readme = pathlib.Path(__file__).resolve().parents[1] / "README.md"
        texto = readme.read_text(encoding="utf-8")
        assert "caminho da URL" in texto
        assert "30 minutos" in texto
