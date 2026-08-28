"""
Correções pequenas, cada uma um bug de lógica que a suíte não cobria.

1. `_deduplicate` perdia o `seeders=0` CONFIRMADO por uma fonte.
2. `_deduplicate` normalizava o hash só para a chave, não para o objeto.
3. `bingeGroup` vinha do hash do torrent — único por torrent, então nunca
   casava com o episódio seguinte e o campo não servia para nada.
4. `get_streams` tinha um parâmetro `title` que o corpo nunca usava.
5. O manifest declarava `configurable: False` apesar de `/configure` existir,
   e não declarava `p2p`.
6. O ramo `notWebReady` era inalcançável: `tem_rd=True` só chegava junto com
   `stream_url`, e nesse caminho a função retorna antes.
"""

import inspect
from unittest.mock import patch

import pytest

from app.manifest import get_manifest
from app.models.torrent import TorrentResult
from app.services.stream_aggregator import StreamAggregator, _binge_group


def _aggregator() -> StreamAggregator:
    with patch("app.services.stream_aggregator._build_scraper_list", return_value=[]):
        return StreamAggregator()


def _torrent(**kw) -> TorrentResult:
    base = dict(
        title="Filme 1080p Dublado",
        info_hash="a" * 40,
        magnet="magnet:?xt=urn:btih:" + "a" * 40,
        quality="1080p",
        dubbed=True,
        source="Fonte X",
    )
    base.update(kw)
    return TorrentResult(**base)


class TestSeedersZeroConfirmado:
    @pytest.mark.parametrize(
        "a,b,esperado",
        [
            (None, 0, 0),      # <- o caso que se perdia
            (0, None, 0),
            (None, 10, 10),
            (0, 10, 10),
            (5, 0, 5),
            (None, None, None),
        ],
    )
    def test_tabela_verdade(self, a, b, esperado):
        agg = _aggregator()
        r = agg._deduplicate([
            _torrent(seeders=a, source="Fonte A"),
            _torrent(seeders=b, source="Fonte B"),
        ])
        assert len(r) == 1
        assert r[0].seeders == esperado

    def test_zero_confirmado_e_penalizado_no_ranking(self):
        """
        _is_confirmed_dead depende de seeders == 0. Se o zero era perdido, um
        torrent morto subia como se a contagem fosse desconhecida.
        """
        from app.services.stream_aggregator import _is_confirmed_dead

        agg = _aggregator()
        r = agg._deduplicate([
            _torrent(seeders=None, source="Fonte A"),
            _torrent(seeders=0, source="Fonte B"),
        ])
        assert _is_confirmed_dead(r[0]) is True


class TestInfoHashNormalizado:
    def test_hash_sai_normalizado_mesmo_sem_duplicata(self):
        agg = _aggregator()
        r = agg._deduplicate([_torrent(info_hash="  " + "A" * 40 + "  ")])
        assert r[0].info_hash == "a" * 40

    def test_hash_normalizado_ao_mesclar(self):
        agg = _aggregator()
        r = agg._deduplicate([
            _torrent(info_hash=" " + "A" * 40 + " ", source="Fonte A"),
            _torrent(info_hash="a" * 40, source="Fonte B"),
        ])
        assert len(r) == 1
        assert r[0].info_hash == "a" * 40
        assert r[0].source == "Fonte A + Fonte B"


class TestBingeGroupEstavel:
    def test_nao_depende_do_hash_do_torrent(self):
        """
        O ponto todo: dois episódios são torrents diferentes, com hashes
        diferentes. Se o bingeGroup vier do hash, nunca casa.
        """
        ep1 = _torrent(info_hash="a" * 40)
        ep2 = _torrent(info_hash="b" * 40)
        assert _binge_group(ep1, "rd") == _binge_group(ep2, "rd")

    def test_separa_por_qualidade(self):
        assert _binge_group(_torrent(quality="1080p"), "rd") != _binge_group(
            _torrent(quality="4K"), "rd"
        )

    def test_separa_por_idioma(self):
        assert _binge_group(_torrent(dubbed=True), "rd") != _binge_group(
            _torrent(dubbed=False), "rd"
        )

    def test_separa_rd_de_p2p(self):
        t = _torrent()
        assert _binge_group(t, "rd") != _binge_group(t, "p2p")

    def test_qualidade_vazia_nao_gera_grupo_degenerado(self):
        assert _binge_group(_torrent(quality=""), "rd").endswith("desconhecida-ptbr")


class TestFormatarStream:
    def test_tem_rd_nao_existe_mais(self):
        """Era um parâmetro que só chegava com stream_url, num caminho que
        retorna antes de consultá-lo."""
        assinatura = inspect.signature(StreamAggregator._formatar_stream)
        assert "tem_rd" not in assinatura.parameters

    def test_stream_rd_marca_not_web_ready(self):
        agg = _aggregator()
        s = agg._formatar_stream(
            torrent=_torrent(), has_play_url=True, stream_url="http://x/play/1"
        )
        assert s.behaviorHints["notWebReady"] is True
        assert s.behaviorHints["bingeGroup"] == _binge_group(_torrent(), "rd")
        assert s.url == "http://x/play/1"
        assert s.infoHash is None

    def test_stream_p2p_ganha_binge_group(self):
        agg = _aggregator()
        s = agg._formatar_stream(torrent=_torrent(), has_play_url=False)
        assert s.behaviorHints["bingeGroup"] == _binge_group(_torrent(), "p2p")
        assert s.infoHash == "a" * 40
        assert s.url is None


class TestParametroTitleMorto:
    def test_get_streams_nao_tem_mais_title(self):
        assinatura = inspect.signature(StreamAggregator.get_streams)
        assert "title" not in assinatura.parameters


class TestManifest:
    def test_identidade_e_versao_atualizadas(self):
        manifest = get_manifest()
        assert manifest["name"] == "Tupi Stream 🇧🇷"
        assert manifest["version"] == "1.2.0"

    def test_declara_configurable(self):
        """A pagina /configure existe; sem isto o Stremio nao mostra o botao."""
        assert get_manifest()["behaviorHints"]["configurable"] is True

    def test_configuracao_nao_e_obrigatoria(self):
        """P2P funciona sem token."""
        assert get_manifest()["behaviorHints"]["configurationRequired"] is False

    def test_declara_p2p(self):
        """O addon entrega infoHash — o Stremio precisa avisar sobre o IP."""
        assert get_manifest()["behaviorHints"]["p2p"] is True

    def test_resto_do_manifest_intacto(self):
        m = get_manifest()
        assert m["id"] == "community.br-streams"
        assert m["resources"] == ["stream"]
        assert m["types"] == ["movie", "series"]
        assert m["idPrefixes"] == ["tt"]
