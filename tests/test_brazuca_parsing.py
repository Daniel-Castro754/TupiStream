from unittest.mock import AsyncMock, MagicMock

import pytest

from app.scrapers.brazuca_addon import BrazucaAddonScraper


class TestDeteccaoDeQualidadeEDublado:
    def test_qualidade_4k(self):
        s = BrazucaAddonScraper()
        assert s._detectar_qualidade("Filme 2026 4K HDR") == "4K"

    def test_qualidade_1080p(self):
        s = BrazucaAddonScraper()
        assert s._detectar_qualidade("Filme 2026 1080p") == "1080p"

    def test_qualidade_desconhecida(self):
        s = BrazucaAddonScraper()
        assert s._detectar_qualidade("Filme sem info") == "Desconhecida"

    @pytest.mark.parametrize("tag", ["DUBLADO", "DUAL", "NACIONAL", "PORTUGUES", "PT-BR"])
    def test_dublado_reconhece_tags(self, tag):
        s = BrazucaAddonScraper()
        assert s._detectar_dublado(f"Filme 1080p {tag}") is True

    def test_dublado_false_sem_tag(self):
        s = BrazucaAddonScraper()
        assert s._detectar_dublado("Movie 1080p English") is False


class TestExtracaoDeTamanho:
    def test_extrai_tamanho_do_titulo(self):
        s = BrazucaAddonScraper()
        assert s._extrair_tamanho_titulo("Filme 2026 1080p 2.3 GB Dublado") == "2.3 GB"

    def test_sem_tamanho_retorna_none(self):
        s = BrazucaAddonScraper()
        assert s._extrair_tamanho_titulo("Filme 2026 1080p Dublado") is None


class TestParsearStream:
    def test_stream_com_infohash_gera_magnet(self):
        s = BrazucaAddonScraper()
        stream = {
            "infoHash": "ABCDEF1234567890ABCDEF1234567890ABCDEF12",
            "title": "Filme 2026 1080p Dublado 2.1 GB",
        }

        torrent = s._parsear_stream(stream)

        assert torrent is not None
        assert torrent.info_hash == "abcdef1234567890abcdef1234567890abcdef12"
        assert torrent.magnet == "magnet:?xt=urn:btih:abcdef1234567890abcdef1234567890abcdef12"
        assert torrent.quality == "1080p"
        assert torrent.dubbed is True
        assert torrent.size == "2.1 GB"

    def test_stream_sem_infohash_e_sem_url_e_ignorado(self):
        s = BrazucaAddonScraper()
        assert s._parsear_stream({"title": "Sem hash e sem url"}) is None

    def test_stream_so_com_url_direta_e_recusado(self):
        """
        Um stream só com `url` (sem infoHash) agora é recusado de imediato.

        A versão anterior deste teste fixava o comportamento antigo:
        `_parsear_stream` devolvia um TorrentResult com info_hash="" e
        magnet="". O raciocínio registrado ali continua válido e é a razão
        desta mudança — gerar um hash fictício seria pior (o Stremio tentaria
        usá-lo como infoHash real e o stream nunca tocaria), e suporte de
        verdade a URL direta exigiria um campo próprio no pipeline.

        O que faltava era a terceira opção: não construir o objeto. O
        resultado com hash vazio era descartado logo depois pelo
        _deduplicate do agregador (`if not info_hash: continue`), então
        montá-lo era trabalho garantido para o lixo — e o comentário no
        código afirmava fazer algo ("hash fictício baseado na URL") que
        nunca foi implementado.

        Recusar na origem tem o mesmo efeito observável, sem código que
        mente sobre o que faz.
        """
        s = BrazucaAddonScraper()
        stream = {"url": "https://exemplo.com/stream.mp4", "title": "Filme Direto"}

        assert s._parsear_stream(stream) is None

    def test_usa_name_quando_nao_ha_title(self):
        s = BrazucaAddonScraper()
        stream = {"infoHash": "a" * 40, "name": "Nome Alternativo"}

        torrent = s._parsear_stream(stream)

        assert torrent.title == "Nome Alternativo"

    def test_sem_title_nem_name_usa_fallback(self):
        s = BrazucaAddonScraper()
        stream = {"infoHash": "a" * 40}

        torrent = s._parsear_stream(stream)

        assert torrent.title == "Sem título"


class TestSearchIntegracao:
    @pytest.mark.asyncio
    async def test_search_filtra_streams_invalidos_e_mantem_validos(self):
        s = BrazucaAddonScraper()
        response = MagicMock()
        response.json.return_value = {
            "streams": [
                {"infoHash": "a" * 40, "title": "Válido 1080p Dublado"},
                {"title": "Sem hash nem url — deve ser descartado"},
            ]
        }

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(s, "_get", AsyncMock(return_value=response))
            resultados = await s.search("Filme", "tt1234567", "movie")

        assert len(resultados) == 1
        assert resultados[0].title == "Válido 1080p Dublado"
        await s.close()

    @pytest.mark.asyncio
    async def test_search_resposta_json_invalida_retorna_vazio(self):
        s = BrazucaAddonScraper()
        response = MagicMock()
        response.json.side_effect = ValueError("JSON inválido")

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(s, "_get", AsyncMock(return_value=response))
            resultados = await s.search("Filme", "tt1234567", "movie")

        assert resultados == []
        await s.close()
