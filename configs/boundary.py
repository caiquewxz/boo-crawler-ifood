"""
Busca e cache de polígonos municipais via Nominatim (OSM).
Cache em configs/cache/{city_key}.geojson — evita re-fetch a cada execução.

Uso:
    from configs.boundary import fetch_city_boundary
    polygon = fetch_city_boundary('sao-paulo', 'São Paulo, SP, Brazil')
    from shapely.geometry import Point
    Point(lon, lat).within(polygon)   # lon=x, lat=y em shapely
"""

import json
from pathlib import Path

import requests
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

CACHE_DIR = Path(__file__).parent / 'cache'
NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
_HEADERS = {'User-Agent': 'boo-crawler-ifood/1.0 (github.com/caiquewxz/boo-crawler-ifood)'}


def fetch_city_boundary(city_key: str, osm_query: str) -> BaseGeometry:
    """
    Retorna Shapely Polygon/MultiPolygon do município.
    Na primeira chamada busca no Nominatim e salva cache.
    Chamadas seguintes leem do cache sem rede.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f'{city_key}.geojson'

    if cache_file.exists():
        geometry = json.loads(cache_file.read_text(encoding='utf-8'))
        return shape(geometry)

    print(f'[boundary] Buscando polígono OSM: "{osm_query}"...')
    resp = requests.get(NOMINATIM_URL, params={
        'q': osm_query,
        'format': 'geojson',
        'polygon_geojson': '1',
        'limit': '1',
    }, headers=_HEADERS, timeout=20)
    resp.raise_for_status()

    features = resp.json().get('features', [])
    if not features:
        raise ValueError(f"Nominatim não encontrou resultado para '{osm_query}'")

    geometry = features[0]['geometry']
    if geometry['type'] not in ('Polygon', 'MultiPolygon'):
        raise ValueError(
            f"OSM retornou tipo '{geometry['type']}' em vez de polígono para '{osm_query}'. "
            "Tente uma query mais específica (ex: adicionar o estado)."
        )

    cache_file.write_text(json.dumps(geometry, ensure_ascii=False), encoding='utf-8')
    print(f'[boundary] Cache salvo: {cache_file.name}')
    return shape(geometry)
