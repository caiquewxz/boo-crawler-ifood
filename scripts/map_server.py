"""
Dashboard de mapa em tempo real para o crawler iFood.

Assiste points.jsonl enquanto o crawl roda e mostra:
  - Marcadores coloridos por densidade de restaurantes
  - Triangulação de Delaunay (Turf.js) conectando os pontos com merchants
  - Painel de stats em tempo real

Uso:
    pip install flask
    python scripts/map_server.py                              # auto-detecta captura mais recente
    python scripts/map_server.py --capture-dir captures/...  # aponta para captura específica
    python scripts/map_server.py --port 8080                  # porta alternativa

Abrir: http://localhost:5000
"""

import argparse
import json
import queue
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string

BASE_DIR = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# State compartilhado entre threads
# ---------------------------------------------------------------------------

_clients: list[queue.Queue] = []
_points: list[dict] = []
_lock = threading.Lock()
_capture_label = ''   # nome do diretório de captura ativo


def broadcast(point: dict):
    with _lock:
        _points.append(point)
        for q in list(_clients):
            q.put(point)


# ---------------------------------------------------------------------------
# Watcher de arquivo
# ---------------------------------------------------------------------------

def watch_capture(capture_dir: Path):
    global _capture_label
    _capture_label = capture_dir.name
    f_path = capture_dir / 'points.jsonl'
    print(f'[map] Monitorando: {f_path}')

    while not f_path.exists():
        time.sleep(0.5)

    with open(f_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    broadcast(json.loads(line))
                except json.JSONDecodeError:
                    pass
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.3)
                continue
            line = line.strip()
            if line:
                try:
                    broadcast(json.loads(line))
                except json.JSONDecodeError:
                    pass


def auto_detect_thread(base: Path):
    """Aguarda o diretório de captura mais recente e começa a monitorar."""
    print('[map] Aguardando novo diretório de captura em captures/...')
    seen: set[str] = set()
    while True:
        dirs = sorted(base.glob('crawl_*'), key=lambda d: d.stat().st_mtime, reverse=True)
        if dirs:
            latest = dirs[0]
            if latest.name not in seen:
                seen.add(latest.name)
                watch_capture(latest)
                return
        time.sleep(2)


# ---------------------------------------------------------------------------
# HTML do mapa (Leaflet + Turf.js, tudo inline)
# ---------------------------------------------------------------------------

HTML_MAP = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iFood Crawler — Mapa em tempo real</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/@turf/turf@6/turf.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; font-family: 'Segoe UI', sans-serif; background: #111; }
  #map { height: 100vh; width: 100%; }

  #stats {
    position: fixed; top: 12px; right: 12px; z-index: 1000;
    background: rgba(15,15,20,0.92); color: #e8e8e8;
    border: 1px solid #333; border-radius: 10px;
    padding: 14px 18px; min-width: 200px;
    backdrop-filter: blur(6px);
    font-size: 13px; line-height: 1.7;
  }
  #stats h3 { color: #ff6b35; margin-bottom: 8px; font-size: 14px; letter-spacing: 0.5px; }
  .stat-val { color: #7ec8e3; font-weight: bold; }
  #status-dot {
    display: inline-block; width: 8px; height: 8px;
    border-radius: 50%; background: #4caf50;
    margin-right: 5px; animation: pulse 1.5s infinite;
  }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  #status-dot.idle { background: #888; animation: none; }

  #legend {
    position: fixed; bottom: 28px; right: 12px; z-index: 1000;
    background: rgba(15,15,20,0.88); color: #ccc;
    border: 1px solid #333; border-radius: 8px;
    padding: 10px 14px; font-size: 12px;
  }
  #legend h4 { margin-bottom: 6px; color: #aaa; }
  .legend-row { display: flex; align-items: center; gap: 7px; margin: 3px 0; }
  .legend-dot { width: 12px; height: 12px; border-radius: 50%; border: 1px solid #555; flex-shrink: 0; }
</style>
</head>
<body>
<div id="map"></div>

<div id="stats">
  <h3>&#9679; iFood Crawler</h3>
  <div><span id="dot" class="idle" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#888;margin-right:5px;"></span>Aguardando dados...</div>
  <div style="margin-top:8px;">
    Pontos visitados: <span class="stat-val" id="s-points">0</span><br>
    Restaurantes únicos: <span class="stat-val" id="s-total">0</span><br>
    Pontos c/ dados: <span class="stat-val" id="s-active">0</span><br>
    Triângulos: <span class="stat-val" id="s-tris">0</span>
  </div>
  <div style="margin-top:8px; font-size:11px; color:#666;" id="s-capture"></div>
</div>

<div id="legend">
  <h4>Restaurantes / ponto</h4>
  <div class="legend-row"><div class="legend-dot" style="background:#888"></div> Erro / sem dados</div>
  <div class="legend-row"><div class="legend-dot" style="background:#4fc3f7"></div> 1–5</div>
  <div class="legend-row"><div class="legend-dot" style="background:#aed581"></div> 6–15</div>
  <div class="legend-row"><div class="legend-dot" style="background:#ffb300"></div> 16–30</div>
  <div class="legend-row"><div class="legend-dot" style="background:#ef5350"></div> 30+</div>
</div>

<script>
// ---------------------------------------------------------------------------
// Mapa base
// ---------------------------------------------------------------------------
const map = L.map('map', { zoomControl: true }).setView([-14.2, -51.9], 5);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://carto.com/">CARTO</a> | <a href="https://www.openstreetmap.org/copyright">OSM</a>',
    maxZoom: 19,
}).addTo(map);

const markersLayer = L.layerGroup().addTo(map);
const tinLayer     = L.layerGroup().addTo(map);

// ---------------------------------------------------------------------------
// Dados
// ---------------------------------------------------------------------------
const allPoints = [];
let autoFit = true;   // centraliza no primeiro ponto

// ---------------------------------------------------------------------------
// Cores
// ---------------------------------------------------------------------------
function countToColor(count) {
    if (count <= 0)  return '#888888';
    if (count <= 5)  return '#4fc3f7';
    if (count <= 15) return '#aed581';
    if (count <= 30) return '#ffb300';
    return '#ef5350';
}

function avgColor(counts) {
    const avg = counts.reduce((a,b)=>a+b,0)/counts.length;
    return countToColor(avg);
}

// ---------------------------------------------------------------------------
// Marcadores
// ---------------------------------------------------------------------------
function addMarker(pt) {
    allPoints.push(pt);
    const r = pt.error ? 4 : Math.max(6, Math.min(20, 5 + pt.count * 0.4));
    const color = countToColor(pt.count);

    const circle = L.circleMarker([pt.lat, pt.lon], {
        radius: r,
        fillColor: color,
        color: pt.error ? '#555' : '#fff',
        weight: pt.error ? 0.5 : 1,
        fillOpacity: pt.error ? 0.35 : 0.85,
    });

    let popupHtml = `<b>Ponto #${pt.index}</b><br>
        ${pt.lat.toFixed(5)}, ${pt.lon.toFixed(5)}<br>`;
    if (pt.error) {
        popupHtml += '<span style="color:#e57373">Erro / sem resposta</span>';
    } else {
        popupHtml += `<b>${pt.count}</b> restaurantes (total acum: ${pt.total})<br>`;
        if (pt.names && pt.names.length) {
            popupHtml += '<hr style="margin:4px 0">' + pt.names.slice(0,8).join('<br>');
            if (pt.count > 8) popupHtml += `<br><i>... +${pt.count-8} mais</i>`;
        }
    }
    circle.bindPopup(popupHtml, {maxWidth: 260});
    circle.addTo(markersLayer);

    if (autoFit) {
        map.setView([pt.lat, pt.lon], 12);
        autoFit = false;
    }
}

// ---------------------------------------------------------------------------
// Triangulação de Delaunay (Turf.js TIN)
// ---------------------------------------------------------------------------
function updateTIN() {
    const active = allPoints.filter(p => !p.error && p.count > 0);
    document.getElementById('s-active').textContent = active.length;
    if (active.length < 3) {
        document.getElementById('s-tris').textContent = '0';
        return;
    }

    const fc = turf.featureCollection(
        active.map(p => turf.point([p.lon, p.lat], { count: p.count }))
    );
    let tin;
    try {
        tin = turf.tin(fc, 'count');
    } catch(e) {
        return;
    }

    tinLayer.clearLayers();
    document.getElementById('s-tris').textContent = tin.features.length;

    L.geoJSON(tin, {
        style: function(feature) {
            const props = feature.properties;
            const counts = [props.a, props.b, props.c].filter(v => v != null);
            return {
                fillColor:   avgColor(counts),
                fillOpacity: 0.22,
                color:       '#aaa',
                weight:      0.6,
            };
        },
        onEachFeature: function(feature, layer) {
            const props = feature.properties;
            const counts = [props.a, props.b, props.c].filter(v => v != null);
            const avg = counts.length ? (counts.reduce((a,b)=>a+b,0)/counts.length).toFixed(1) : '?';
            layer.bindPopup(`Triângulo — densidade média: <b>${avg}</b> restaurantes/ponto`);
        },
    }).addTo(tinLayer);
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------
function updateStats(pt) {
    document.getElementById('s-points').textContent = allPoints.length;
    document.getElementById('s-total').textContent  = pt.total;
    const dot = document.getElementById('dot');
    dot.style.background = '#4caf50';
    dot.style.animation  = 'pulse 1.5s infinite';
}

// ---------------------------------------------------------------------------
// SSE
// ---------------------------------------------------------------------------
const es = new EventSource('/stream');
es.onopen = () => {
    document.querySelector('#stats div').innerHTML =
        '<span id="dot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#4caf50;margin-right:5px;animation:pulse 1.5s infinite;"></span>Conectado';
};
es.onmessage = (e) => {
    const pt = JSON.parse(e.data);
    addMarker(pt);
    updateTIN();
    updateStats(pt);
};
es.onerror = () => {
    const dot = document.getElementById('dot');
    if (dot) { dot.style.background = '#f44336'; dot.style.animation = 'none'; }
};

// Busca label do diretório de captura
fetch('/meta').then(r=>r.json()).then(d=>{
    if (d.capture) document.getElementById('s-capture').textContent = d.capture;
});

// ---------------------------------------------------------------------------
// Controles de camadas
// ---------------------------------------------------------------------------
const overlays = {
    'Marcadores': markersLayer,
    'Triangulação': tinLayer,
};
L.control.layers(null, overlays, {position: 'bottomleft'}).addTo(map);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.route('/')
def index():
    return render_template_string(HTML_MAP)


@app.route('/meta')
def meta():
    return jsonify({'capture': _capture_label})


@app.route('/data')
def data():
    with _lock:
        return jsonify(list(_points))


@app.route('/stream')
def stream():
    def generate():
        q: queue.Queue = queue.Queue()
        with _lock:
            snapshot = list(_points)
            _clients.append(q)
        try:
            for p in snapshot:
                yield f'data: {json.dumps(p)}\n\n'
            while True:
                try:
                    p = q.get(timeout=15)
                    yield f'data: {json.dumps(p)}\n\n'
                except queue.Empty:
                    yield ': keepalive\n\n'
        finally:
            with _lock:
                if q in _clients:
                    _clients.remove(q)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'},
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Servidor de mapa em tempo real — iFood Crawler')
    parser.add_argument('--capture-dir', default=None,
                        help='Diretório de captura específico (padrão: auto-detecta o mais recente)')
    parser.add_argument('--port', type=int, default=5000, help='Porta HTTP (padrão: 5000)')
    args = parser.parse_args()

    captures_base = BASE_DIR / 'captures'
    captures_base.mkdir(exist_ok=True)

    if args.capture_dir:
        capture_path = Path(args.capture_dir)
        if not capture_path.is_absolute():
            capture_path = BASE_DIR / capture_path
        t = threading.Thread(target=watch_capture, args=(capture_path,), daemon=True)
    else:
        t = threading.Thread(target=auto_detect_thread, args=(captures_base,), daemon=True)

    t.start()

    print(f'[map] Servidor iniciado em http://localhost:{args.port}')
    print('[map] Pressione Ctrl+C para parar.')
    app.run(host='0.0.0.0', port=args.port, threaded=True, use_reloader=False)
