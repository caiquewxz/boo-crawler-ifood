"""
Configurações de bounding box para cidades brasileiras.
Cada cidade define os limites geográficos do município e o step padrão
que produz ~100-200 pontos de grade para cobertura completa.

Uso:
    from configs.cities import CITIES, generate_city_grid
    points = generate_city_grid('sao-paulo')          # step padrão da cidade
    points = generate_city_grid('sao-paulo', step_km=3.0)  # override
"""

import math

LAT_KM = 111.32  # 1° de latitude ≈ 111.32 km

CITIES: dict[str, dict] = {
    "sao-paulo": {
        "name": "São Paulo - SP",
        "lat_min": -24.008,
        "lat_max": -23.357,
        "lon_min": -46.826,
        "lon_max": -46.365,
        "step_km": 5.0,   # ~150 pontos
        "osm_query": "São Paulo, SP, Brazil",
    },
    "rio-de-janeiro": {
        "name": "Rio de Janeiro - RJ",
        "lat_min": -23.082,
        "lat_max": -22.746,
        "lon_min": -43.795,
        "lon_max": -43.101,
        "step_km": 5.0,   # ~130 pontos
        "osm_query": "Rio de Janeiro, RJ, Brazil",
    },
    "belo-horizonte": {
        "name": "Belo Horizonte - MG",
        "lat_min": -20.060,
        "lat_max": -19.787,
        "lon_min": -44.068,
        "lon_max": -43.843,
        "step_km": 4.0,   # ~100 pontos
        "osm_query": "Belo Horizonte, MG, Brazil",
    },
    "curitiba": {
        "name": "Curitiba - PR",
        "lat_min": -25.650,
        "lat_max": -25.332,
        "lon_min": -49.385,
        "lon_max": -49.192,
        "step_km": 4.0,   # ~90 pontos
        "osm_query": "Curitiba, PR, Brazil",
    },
    "porto-alegre": {
        "name": "Porto Alegre - RS",
        "lat_min": -30.252,
        "lat_max": -29.935,
        "lon_min": -51.290,
        "lon_max": -51.054,
        "step_km": 4.0,   # ~100 pontos
        "osm_query": "Porto Alegre, RS, Brazil",
    },
    "fortaleza": {
        "name": "Fortaleza - CE",
        "lat_min": -3.895,
        "lat_max": -3.700,
        "lon_min": -38.635,
        "lon_max": -38.418,
        "step_km": 4.0,   # ~80 pontos
        "osm_query": "Fortaleza, CE, Brazil",
    },
    "salvador": {
        "name": "Salvador - BA",
        "lat_min": -13.012,
        "lat_max": -12.851,
        "lon_min": -38.532,
        "lon_max": -38.282,
        "step_km": 4.0,   # ~80 pontos
        "osm_query": "Salvador, BA, Brazil",
    },
    "brasilia": {
        "name": "Brasília - DF",
        "lat_min": -15.976,
        "lat_max": -15.600,
        "lon_min": -48.241,
        "lon_max": -47.832,
        "step_km": 5.0,   # ~120 pontos
        "osm_query": "Brasília, DF, Brazil",
    },
    "manaus": {
        "name": "Manaus - AM",
        "lat_min": -3.207,
        "lat_max": -2.960,
        "lon_min": -60.110,
        "lon_max": -59.844,
        "step_km": 5.0,   # ~80 pontos
        "osm_query": "Manaus, AM, Brazil",
    },
    "recife": {
        "name": "Recife - PE",
        "lat_min": -8.168,
        "lat_max": -7.955,
        "lon_min": -35.016,
        "lon_max": -34.873,
        "step_km": 3.0,   # ~80 pontos
        "osm_query": "Recife, PE, Brazil",
    },
    "sao-joao-del-rei": {
        "name": "São João del Rei - MG",
        "lat_min": -21.130,
        "lat_max": -21.060,
        "lon_min": -44.340,
        "lon_max": -44.180,
        "step_km": 2.0,   # ~36 pontos — zona urbana real (calibrado pelo log 2026-05-22)
        "osm_query": "São João del Rei, MG, Brazil",
    },
}


def generate_city_grid(
    city_key: str,
    step_km: float | None = None,
    use_boundary: bool = False,
) -> list[tuple[float, float]]:
    """
    Gera grade retangular cobrindo o bounding box da cidade.
    step_km: espaçamento em km (None = usa o padrão da cidade).
    use_boundary: filtra pontos fora do polígono real do município via OSM.
    Retorna lista de (lat, lon) com 6 casas decimais.
    """
    if city_key not in CITIES:
        raise ValueError(f"Cidade '{city_key}' não encontrada. Use --list-cities para ver as opções.")

    cfg = CITIES[city_key]
    step = step_km if step_km is not None else cfg["step_km"]

    lat_step = step / LAT_KM
    mid_lat  = (cfg["lat_min"] + cfg["lat_max"]) / 2
    lon_step = step / (LAT_KM * math.cos(math.radians(mid_lat)))

    points = []
    lat = cfg["lat_max"]
    while lat >= cfg["lat_min"]:
        lon = cfg["lon_min"]
        while lon <= cfg["lon_max"]:
            points.append((round(lat, 6), round(lon, 6)))
            lon += lon_step
        lat -= lat_step

    if use_boundary:
        from configs.boundary import fetch_city_boundary
        from shapely.geometry import Point
        polygon = fetch_city_boundary(city_key, cfg["osm_query"])
        before = len(points)
        points = [p for p in points if Point(p[1], p[0]).within(polygon)]
        print(f'[boundary] {before} → {len(points)} pontos ({before - len(points)} fora do município)')

    return points


if __name__ == '__main__':
    print(f"{'Chave':<20} {'Cidade':<25} {'Step':<8} {'Pontos'}")
    print('-' * 65)
    for key, cfg in CITIES.items():
        pts = generate_city_grid(key)
        print(f"{key:<20} {cfg['name']:<25} {cfg['step_km']:<8.1f} {len(pts)}")
