from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.models.config import settings
from app.services.stream_aggregator import SCRAPER_REGISTRY, SOURCE_ID_BY_FLAG

router = APIRouter()

SCRAPER_UI_INFO: dict[str, dict[str, str]] = {
    "ENABLE_APACHE_TORRENT": {
        "emoji": "&#x1F525;",
        "description": "WordPress BR com foco em dublado e dual audio.",
    },
    "ENABLE_COMANDO_FILMES": {
        "emoji": "&#x1F3AC;",
        "description": "Acervo BR em WordPress com parsing semelhante ao Apache Torrent.",
    },
    "ENABLE_HDR_TORRENT": {
        "emoji": "&#x1F4FA;",
        "description": "Implementação pronta, mas os domínios configurados não resolvem DNS.",
    },
    "ENABLE_MICOLEAO": {
        "emoji": "&#x1F981;",
        "description": "Implementação pronta, mas os domínios configurados não resolvem DNS.",
    },
    "ENABLE_BRAZUCA": {
        "emoji": "&#x1F310;",
        "description": "Consome JSON de um addon Stremio existente, com baixo risco de anti-bot.",
    },
    "ENABLE_YTS": {
        "emoji": "&#x1F39E;",
        "description": "API JSON oficial do YTS, muito confiavel, mas majoritariamente legendado.",
    },
    "ENABLE_ARCHIVE_ORG": {
        "emoji": "&#x1F4DA;",
        "description": "API publica do Internet Archive. Dominio publico e licenca aberta, sem scraping.",
    },
    "ENABLE_TORRENT_GALAXY": {
        "emoji": "&#x1F6E1;",
        "description": "Domínio principal sem DNS; mirrors testados responderam 403 anti-bot.",
    },
    "ENABLE_1337X": {
        "emoji": "&#x1F512;",
        "description": "Domínio e mirrors testados responderam 403 anti-bot.",
    },
    "ENABLE_RUTRACKER": {
        "emoji": "&#x1F510;",
        "description": "Busca pública bloqueada por 403; magnets de tópicos podem exigir login.",
    },
}

STABILITY_LABELS: dict[str, str] = {
    "estável": "Estavel",
    "bloqueado_antibot": "Bloqueado por anti-bot",
    "não_confiável_cloud": "Nao confiavel em cloud",
    "dominio_indisponivel": "Dominio indisponivel",
}

SOURCE_TYPE_BY_FLAG = {
    "ENABLE_BRAZUCA": "Addon Stremio / JSON",
    "ENABLE_YTS": "API JSON",
    "ENABLE_ARCHIVE_ORG": "API pública / .torrent",
    "ENABLE_APACHE_TORRENT": "WordPress / HTML",
    "ENABLE_COMANDO_FILMES": "WordPress / HTML",
    "ENABLE_HDR_TORRENT": "WordPress / HTML",
    "ENABLE_MICOLEAO": "WordPress / HTML",
    "ENABLE_TORRENT_GALAXY": "HTML",
    "ENABLE_1337X": "HTML",
    "ENABLE_RUTRACKER": "HTML / fórum",
}


def _get_scraper_entries() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Separa scrapers ativos e desativados com base no registry real."""
    enabled_entries: list[dict[str, str]] = []
    disabled_entries: list[dict[str, str]] = []

    for flag_name, scraper_cls in SCRAPER_REGISTRY:
        meta = SCRAPER_UI_INFO.get(flag_name, {})
        stability = getattr(scraper_cls, "stability", "estável")
        entry = {
            "flag": flag_name,
            "id": SOURCE_ID_BY_FLAG[flag_name],
            "name": getattr(scraper_cls, "name", scraper_cls.__name__),
            "emoji": meta.get("emoji", "&#x1F4E6;"),
            "description": meta.get("description", "Sem nota operacional cadastrada."),
            "stability": STABILITY_LABELS.get(stability, stability.replace("_", " ").title()),
            "type": SOURCE_TYPE_BY_FLAG.get(flag_name, "Fonte externa"),
            "enabled": "true" if getattr(settings, flag_name, False) else "false",
        }
        if entry["enabled"] == "true":
            enabled_entries.append(entry)
        else:
            disabled_entries.append(entry)

    return enabled_entries, disabled_entries


def _render_source_picker(entries: list[dict[str, str]]) -> str:
    """Renderiza seleção do usuário sem confundir seleção com saúde."""
    items = []
    for entry in entries:
        available = entry["enabled"] == "true"
        disabled = "" if available else " disabled"
        checked = " checked" if available else ""
        card_class = "" if available else " is-unavailable"
        badge_class = "badge-on" if available else "badge-off"
        badge = "Disponivel" if available else "Indisponivel nesta instancia"
        initial_status = "Ainda nao verificada" if available else "Desabilitada pelo administrador"
        items.append(
            f"""
    <label class="source-item{card_class}" data-source-card="{entry['id']}">
      <input class="source-toggle" type="checkbox" data-source-id="{entry['id']}"{checked}{disabled}>
      <span class="source-emoji">{entry['emoji']}</span>
      <span class="source-info">
        <span class="source-head">
          <span class="source-name">{entry['name']}</span>
          <span class="source-badge {badge_class}">{badge}</span>
        </span>
        <span class="source-desc">{entry['description']}</span>
        <span class="source-meta">{entry['type']} • {entry['stability']} • {entry['flag']}</span>
        <span class="source-state" data-health-id="{entry['id']}">{initial_status}</span>
      </span>
    </label>"""
        )

    return f"""
  <div class="card">
    <p class="sources-title">Fontes que desejo utilizar</p>
    <p class="sources-help">
      A fonte so executa quando esta disponivel no servidor e marcada aqui.
      Sua selecao fica na URL instalada; nao e salva como estado global.
    </p>
    {''.join(items)}
  </div>"""


def _build_config_html() -> str:
    """Monta a página de configuração com fontes disponíveis e bloqueadas."""
    enabled_entries, disabled_entries = _get_scraper_entries()
    sections_html = _render_source_picker(enabled_entries + disabled_entries)
    return CONFIG_HTML_TEMPLATE.replace("__SCRAPER_SECTIONS__", sections_html)


@router.get("/configure", response_class=HTMLResponse)
async def configure_page() -> HTMLResponse:
    """Pagina de configuracao do addon."""
    return HTMLResponse(content=_build_config_html())


CONFIG_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tupi Stream - Configuração</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
    background: #0f0f0f;
    color: #e0e0e0;
    min-height: 100vh;
    display: flex;
    justify-content: center;
    padding: 1.25rem 0.75rem;
  }

  .container {
    width: 100%;
    max-width: 560px;
  }

  .header {
    text-align: center;
    margin-bottom: 1.25rem;
  }

  .header h1 {
    font-size: 1.75rem;
    font-weight: 800;
    color: #fff;
    margin-bottom: 0.4rem;
  }

  .header p {
    color: #888;
    font-size: 0.95rem;
  }

  .card {
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 12px;
    padding: 1rem;
    margin-bottom: 0.85rem;
  }

  .form-group { margin-bottom: 0.9rem; }

  .form-group label {
    display: block;
    font-weight: 600;
    font-size: 0.9rem;
    margin-bottom: 0.4rem;
    color: #ccc;
  }

  .form-group input[type="text"],
  .form-group input[type="password"] {
    width: 100%;
    padding: 0.75rem 1rem;
    background: #111;
    border: 1px solid #333;
    border-radius: 8px;
    color: #fff;
    font-size: 0.95rem;
    outline: none;
    transition: border-color 0.2s;
  }

  .form-group input[type="text"]:focus,
  .form-group input[type="password"]:focus {
    border-color: #00b4d8;
  }

  .form-group .hint {
    display: inline-block;
    margin-top: 0.4rem;
    font-size: 0.8rem;
    color: #00b4d8;
    text-decoration: none;
    transition: color 0.2s;
  }

  .form-group .hint:hover { color: #48cae4; }

  .security-note {
    margin-top: 0.65rem;
    font-size: 0.8rem;
    color: #a9a9a9;
    line-height: 1.45;
  }

  .mode-option {
    margin-bottom: 0.9rem;
    padding: 0.8rem;
    background: #111;
    border: 1px solid #303030;
    border-radius: 8px;
  }

  .checkbox-row {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    cursor: pointer;
    color: #e0e0e0;
  }

  .checkbox-row input {
    width: 1.1rem;
    height: 1.1rem;
    margin-top: 0.15rem;
    accent-color: #00b4d8;
  }

  .checkbox-row span {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .checkbox-row small,
  .mode-summary {
    color: #929292;
    font-size: 0.8rem;
    line-height: 1.4;
  }

  .mode-summary {
    margin-top: 0.75rem;
    padding-top: 0.75rem;
    border-top: 1px solid #282828;
  }

  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0.7rem 1.25rem;
    border: none;
    border-radius: 8px;
    font-size: 0.9rem;
    font-weight: 600;
    cursor: pointer;
    transition: transform 0.15s, opacity 0.2s;
    text-decoration: none;
    color: #fff;
  }

  .btn:hover { transform: translateY(-1px); opacity: 0.9; }
  .btn:active { transform: translateY(0); }

  .btn-primary {
    background: #00b4d8;
    width: 100%;
  }

  .btn-secondary {
    background: #2a2a2a;
    border: 1px solid #3a3a3a;
  }

  .btn-stremio {
    background: #7b5bf5;
  }

  .btn-group {
    display: flex;
    gap: 0.6rem;
    margin-top: 0.75rem;
    flex-wrap: wrap;
  }

  .btn-group .btn { flex: 1; min-width: 140px; }

  .result {
    max-height: 0;
    overflow: hidden;
    opacity: 0;
    transition: max-height 0.4s ease, opacity 0.3s ease, margin 0.3s ease;
    margin-top: 0;
  }

  .result.visible {
    max-height: 300px;
    opacity: 1;
    margin-top: 1.25rem;
  }

  .result-url {
    width: 100%;
    padding: 0.75rem 1rem;
    background: #111;
    border: 1px solid #333;
    border-radius: 8px;
    color: #00b4d8;
    font-size: 0.85rem;
    font-family: monospace;
    outline: none;
  }

  .result-label {
    font-size: 0.8rem;
    color: #888;
    margin-bottom: 0.4rem;
  }

  .toast {
    position: fixed;
    bottom: 2rem;
    left: 50%;
    transform: translateX(-50%) translateY(20px);
    background: #00b4d8;
    color: #000;
    padding: 0.6rem 1.5rem;
    border-radius: 8px;
    font-size: 0.85rem;
    font-weight: 600;
    opacity: 0;
    transition: opacity 0.3s, transform 0.3s;
    pointer-events: none;
    z-index: 100;
  }

  .toast.show {
    opacity: 1;
    transform: translateX(-50%) translateY(0);
  }

  .sources-title {
    font-weight: 700;
    font-size: 1rem;
    margin-bottom: 0.35rem;
    color: #fff;
  }

  .sources-help {
    color: #929292;
    font-size: 0.82rem;
    line-height: 1.45;
    margin-bottom: 0.75rem;
  }

  .source-item {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 0.6rem 0;
    border-bottom: 1px solid #222;
    cursor: pointer;
  }

  .source-item.is-unavailable { opacity: 0.55; cursor: not-allowed; }

  .source-toggle {
    width: 1.1rem;
    height: 1.1rem;
    margin-top: 0.2rem;
    accent-color: #00b4d8;
    flex-shrink: 0;
  }

  .source-item:last-child { border-bottom: none; }

  .source-emoji {
    font-size: 1.15rem;
    width: 2rem;
    text-align: center;
    flex-shrink: 0;
  }

  .source-info { flex: 1; display: block; min-width: 0; }

  .source-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    margin-bottom: 0.2rem;
  }

  .source-name {
    font-weight: 600;
    font-size: 0.9rem;
    color: #e0e0e0;
  }

  .source-desc {
    font-size: 0.78rem;
    color: #b0b0b0;
    line-height: 1.45;
  }

  .source-meta {
    font-size: 0.76rem;
    color: #7e7e7e;
    margin-top: 0.25rem;
  }

  .source-state {
    display: block;
    font-size: 0.76rem;
    color: #63d8ef;
    margin-top: 0.3rem;
  }

  .source-item.is-unavailable .source-state { color: #ffcf7e; }

  .source-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0.18rem 0.5rem;
    border-radius: 999px;
    font-size: 0.72rem;
    font-weight: 700;
    white-space: nowrap;
  }

  .badge-on {
    background: rgba(0, 180, 216, 0.14);
    color: #63d8ef;
    border: 1px solid rgba(0, 180, 216, 0.28);
  }

  .badge-off {
    background: rgba(255, 181, 71, 0.12);
    color: #ffcf7e;
    border: 1px solid rgba(255, 181, 71, 0.24);
  }

  .footer {
    text-align: center;
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 1px solid #1a1a1a;
  }

  .footer a {
    color: #555;
    font-size: 0.8rem;
    text-decoration: none;
    transition: color 0.2s;
  }

  .footer a:hover { color: #00b4d8; }

  @media (max-width: 480px) {
    .header h1 { font-size: 1.6rem; }
    .btn-group { flex-direction: column; }
    .btn-group .btn { min-width: 100%; }
    .source-head { align-items: flex-start; flex-direction: column; }
  }
</style>
</head>
<body>

<div class="container">
  <div class="header">
    <h1>Tupi Stream &#x1F1E7;&#x1F1F7;</h1>
    <p>P2P gratuito com Real-Debrid opcional</p>
  </div>

  <div class="card">
    <div class="form-group">
      <label for="rd-token">Token Real-Debrid</label>
      <input type="password" id="rd-token" placeholder="Insira seu token da API do Real-Debrid" autocomplete="off" spellcheck="false">
      <a href="https://real-debrid.com/apitoken" target="_blank" rel="noopener" class="hint">Onde encontro meu token? &rarr;</a>
      <p class="security-note">
        O token e opcional. Sem token, o addon usa P2P. Com token, o modo padrao
        usa Real-Debrid. O token e enviado uma vez e armazenado criptografado;
        a URL instalada contem apenas um identificador aleatorio.
      </p>
    </div>

    <div class="mode-option">
      <label class="checkbox-row" for="include-p2p">
        <input type="checkbox" id="include-p2p">
        <span>
          <strong>Tambem mostrar opcoes P2P</strong>
          <small>Com token preenchido, exibe RD e P2P juntos.</small>
        </span>
      </label>
      <p class="mode-summary" id="mode-summary">
        Sem token: o link sera gerado no modo P2P.
      </p>
    </div>

    <button class="btn btn-primary" id="btn-generate" type="button">Gerar link de instalacao</button>

    <div class="result" id="result-area">
      <p class="result-label">URL do Manifest:</p>
      <input type="text" class="result-url" id="manifest-url" readonly>
      <div class="btn-group">
        <button class="btn btn-secondary" id="btn-copy" type="button">Copiar URL</button>
        <button class="btn btn-stremio" id="btn-stremio" type="button">Instalar no Stremio</button>
        <button class="btn btn-secondary" id="btn-web" type="button">Instalar no Stremio Web</button>
      </div>
    </div>
  </div>

__SCRAPER_SECTIONS__

  <div class="card">
    <p class="sources-title">Como compartilhar com outras pessoas</p>
    <div class="share-info">
      <p style="color:#b0b0b0;font-size:0.9rem;line-height:1.6;margin-bottom:0.75rem;">
        Cada pessoa pode instalar sem token no modo P2P ou usar o proprio
        token Real-Debrid. Compartilhe apenas esta pagina de configuracao.
      </p>
      <div class="form-group" style="margin-bottom:0.75rem;">
        <label for="share-url">URL desta pagina</label>
        <div style="display:flex;gap:0.5rem;">
          <input type="text" id="share-url" readonly style="flex:1;color:#00b4d8;font-family:monospace;font-size:0.85rem;">
          <button class="btn btn-secondary" id="btn-share" type="button" style="flex:none;min-width:auto;padding:0.7rem 1rem;">Copiar</button>
        </div>
      </div>
      <p style="color:#ff6b6b;font-size:0.82rem;line-height:1.5;">
        &#x26A0; Nao compartilhe sua URL de manifest: embora ela nao contenha o
        token, o identificador permite usar a configuracao da sua conta.
      </p>
    </div>
  </div>

  <div class="footer">
    <a href="https://github.com/Daniel-Castro754/TupiStream" target="_blank" rel="noopener">GitHub &middot; Tupi Stream</a>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
(function() {
  var tokenInput = document.getElementById('rd-token');
  var includeP2PInput = document.getElementById('include-p2p');
  var sourceInputs = Array.prototype.slice.call(document.querySelectorAll('.source-toggle'));
  var modeSummary = document.getElementById('mode-summary');
  var resultArea = document.getElementById('result-area');
  var manifestInput = document.getElementById('manifest-url');
  var toast = document.getElementById('toast');
  var manifestUrl = '';

  var generateButton = document.getElementById('btn-generate');
  generateButton.addEventListener('click', async function() {
    var token = tokenInput.value.trim();
    var baseUrl = window.location.origin;
    var selectedSources = sourceInputs.filter(function(input) {
      return input.checked && !input.disabled;
    }).map(function(input) { return input.dataset.sourceId; });

    if (!selectedSources.length) {
      showToast('Selecione pelo menos uma fonte');
      return;
    }

    if (!token) {
      var sourcePrefix = '/sources/' + selectedSources.join(',');
      manifestUrl = baseUrl + sourcePrefix + '/manifest.json';
    } else {
      generateButton.disabled = true;
      generateButton.textContent = 'Protegendo configuracao...';
      try {
        var response = await fetch('/api/configurations', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            rd_token: token,
            include_p2p: includeP2PInput.checked,
            source_ids: selectedSources
          })
        });
        var data = await response.json();
        if (!response.ok) {
          throw new Error(data.detail || 'Nao foi possivel salvar a configuracao');
        }
        manifestUrl = data.manifest_url;
        tokenInput.value = '';
        updateModeSummary();
      } catch (error) {
        showToast(error.message || 'Erro ao gerar configuracao');
        return;
      } finally {
        generateButton.disabled = false;
        generateButton.textContent = 'Gerar link de instalacao';
      }
    }

    manifestInput.value = manifestUrl;
    resultArea.classList.add('visible');
  });

  function updateModeSummary() {
    var token = tokenInput.value.trim();

    if (!token) {
      modeSummary.textContent = 'Modo P2P: nao exige token e depende de seeders.';
    } else if (includeP2PInput.checked) {
      modeSummary.textContent = 'Modo hibrido: resultados RD e P2P aparecerao juntos.';
    } else {
      modeSummary.textContent = 'Modo Real-Debrid: somente opcoes HTTP via RD.';
    }
  }

  tokenInput.addEventListener('input', updateModeSummary);
  includeP2PInput.addEventListener('change', updateModeSummary);
  updateModeSummary();

  var healthLabels = {
    ok: 'Saudavel',
    empty: 'Saudavel, sem resultado na ultima busca',
    unavailable: 'Indisponivel',
    error: 'Erro na ultima consulta',
    cooldown: 'Em cooldown',
    disabled: 'Desabilitada pelo administrador',
    not_checked: 'Ainda nao verificada'
  };
  fetch('/health').then(function(response) {
    return response.json();
  }).then(function(data) {
    (data.sources || []).forEach(function(source) {
      var node = document.querySelector('[data-health-id="' + source.id + '"]');
      if (!node) return;
      var label = healthLabels[source.status] || source.status;
      if (source.active_origin) label += ' • ' + source.active_origin;
      if (source.configured_mirrors) {
        label += ' • ' + source.configured_mirrors + ' origem(ns) configurada(s)';
      }
      node.textContent = label;
    });
  }).catch(function() {
    // A configuração continua utilizável mesmo se a telemetria estiver fora.
  });

  document.getElementById('btn-copy').addEventListener('click', function() {
    navigator.clipboard.writeText(manifestUrl).then(function() {
      showToast('URL copiada');
    });
  });

  document.getElementById('btn-stremio').addEventListener('click', function() {
    window.open('stremio://install?manifest=' + encodeURIComponent(manifestUrl), '_blank');
  });

  document.getElementById('btn-web').addEventListener('click', function() {
    var webUrl = 'https://web.stremio.com/#/addons?addon=' + encodeURIComponent(manifestUrl);
    window.open(webUrl, '_blank');
  });

  // URL de compartilhamento (pagina /configure)
  var shareInput = document.getElementById('share-url');
  var configureUrl = window.location.origin + '/configure';
  shareInput.value = configureUrl;

  document.getElementById('btn-share').addEventListener('click', function() {
    navigator.clipboard.writeText(configureUrl).then(function() {
      showToast('URL de configuracao copiada');
    });
  });

  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.add('show');
    setTimeout(function() { toast.classList.remove('show'); }, 2000);
  }
})();
</script>

</body>
</html>
"""
