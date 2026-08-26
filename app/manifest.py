def get_manifest() -> dict:
    """Retorna o manifest do addon no formato esperado pelo Stremio."""
    return {
        # Mantido por compatibilidade com instalacoes existentes.
        "id": "community.br-streams",
        "version": "1.0.0",
        "name": "Tupi Stream 🇧🇷",
        "description": "Agregador comunitário PT-BR para Stremio com suporte a P2P e Real-Debrid",
        "resources": ["stream"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "behaviorHints": {
            # A pagina /configure existe desde sempre, mas com configurable
            # False o Stremio nao mostra o botao que leva a ela.
            "configurable": True,
            # P2P funciona sem token, entao configurar e opcional.
            "configurationRequired": False,
            # O addon entrega infoHash: o Stremio precisa saber para avisar
            # o usuario sobre exposicao de IP em conexao P2P.
            "p2p": True,
        },
        "catalogs": [],
    }
