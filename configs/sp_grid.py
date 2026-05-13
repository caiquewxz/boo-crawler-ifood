"""
Gera grade de coordenadas cobrindo São Paulo - SP.
Retorna lista de (lat, lon) com espaçamento configurável.

Uso:
    from configs.sp_grid import generate_grid
    points = generate_grid(step_km=2.0)
"""

import math

# Bounding box de São Paulo (município)
SP_LAT_MIN = -24.008
SP_LAT_MAX = -23.357
SP_LON_MIN = -46.826
SP_LON_MAX = -46.365

# 1 grau de latitude ≈ 111.32 km
# 1 grau de longitude ≈ 111.32 * cos(lat) km
LAT_KM = 111.32


def generate_grid(step_km: float = 2.0) -> list[tuple[float, float]]:
    """
    Gera grade retangular cobrindo o bounding box de SP.
    step_km: espaçamento entre pontos em quilômetros.
    Retorna lista de (lat, lon) com 6 casas decimais.
    """
    lat_step = step_km / LAT_KM
    mid_lat  = (SP_LAT_MIN + SP_LAT_MAX) / 2
    lon_step = step_km / (LAT_KM * math.cos(math.radians(mid_lat)))

    points = []
    lat = SP_LAT_MAX
    while lat >= SP_LAT_MIN:
        lon = SP_LON_MIN
        while lon <= SP_LON_MAX:
            points.append((round(lat, 6), round(lon, 6)))
            lon += lon_step
        lat -= lat_step

    return points


if __name__ == '__main__':
    pts = generate_grid(step_km=2.0)
    print(f"Grade 2 km: {len(pts)} pontos")
    pts_dense = generate_grid(step_km=1.0)
    print(f"Grade 1 km: {len(pts_dense)} pontos")
    print(f"Exemplo primeiros 5: {pts[:5]}")
