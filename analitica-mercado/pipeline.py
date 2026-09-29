"""
Pipeline de datos de Analítica de Mercado (EVOL).

Descarga desde la API pública SIPUB del Coordinador Eléctrico Nacional
(https://sipub.api.coordinador.cl) y arma una base DuckDB local que el
Streamlit de este mismo proyecto lee directo, sin llamar nunca a la API
en vivo desde la app.

Endpoints usados hoy:
  - /capacidad-instalada/v4/findByDate  -> snapshot mensual de potencia
    máxima declarada por central (concepto "capacidad instalada del SEN").
  - /centrales/v4/findByDate            -> registro técnico de centrales:
    estado, tecnología, ubicación, punto de conexión, potencia.

Se cruzan por `id_central`. `centrales` es la tabla base porque trae el
campo `estado` (que `capacidad-instalada` no tiene) y el resto del detalle
técnico; `capacidad-instalada` se guarda aparte y se cruza para dejar un
control cruzado entre ambas fuentes (quedan registradas ambas potencias
por si difieren).

Requiere la variable de entorno SIPUB_API_KEY (credencial "EVOL Analytics
Platform" / servicio "Información Pública (SIP)" en
https://portal.api.coordinador.cl/aplicaciones).

Se ejecuta vía GitHub Actions (ver .github/workflows/) con cadencia
mensual, o a mano con: python pipeline.py
"""

import os
import sys
import time
from datetime import datetime, timezone

import duckdb
import requests

BASE_URL = "https://sipub.api.coordinador.cl"
DB_PATH = os.path.join(os.path.dirname(__file__), "mercado.duckdb")
PAGE_LIMIT = 100          # tope observado empíricamente para /centrales
MAX_PAGES_SAFETY = 500    # corta el loop si algo sale mal, para no golpear la API sin fin
REQUEST_TIMEOUT = 30
RETRY_ATTEMPTS = 3
RETRY_SLEEP_SECONDS = 5


def _get_api_key() -> str:
    key = os.environ.get("SIPUB_API_KEY")
    if not key:
        print("ERROR: falta la variable de entorno SIPUB_API_KEY", file=sys.stderr)
        sys.exit(1)
    return key


def _request_with_retry(url: str, params: dict) -> dict | list:
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 401:
                print("ERROR 401: la API key no fue enviada correctamente", file=sys.stderr)
                sys.exit(1)
            if resp.status_code == 403:
                print("ERROR 403: la API key es incorrecta o fue revocada", file=sys.stderr)
                sys.exit(1)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_error = exc
            print(f"Intento {attempt}/{RETRY_ATTEMPTS} falló: {exc}", file=sys.stderr)
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_SLEEP_SECONDS)
    raise RuntimeError(f"No se pudo consultar {url} tras {RETRY_ATTEMPTS} intentos: {last_error}")


def fetch_capacidad_instalada(api_key: str) -> list[dict]:
    """/capacidad-instalada devuelve {"data": [...], "totalPages": N, "page": N, "limit": N}."""
    url = f"{BASE_URL}/capacidad-instalada/v4/findByDate"
    registros: list[dict] = []
    page = 1
    while page <= MAX_PAGES_SAFETY:
        body = _request_with_retry(url, {"user_key": api_key, "page": page, "limit": PAGE_LIMIT})
        data = body.get("data", [])
        registros.extend(data)
        total_pages = body.get("totalPages", page)
        if page >= total_pages or not data:
            break
        page += 1
    print(f"  capacidad-instalada: {len(registros)} registros ({page} páginas)")
    return registros


def fetch_centrales(api_key: str) -> list[dict]:
    """/centrales devuelve directamente una lista, sin metadata de paginación
    (a diferencia de capacidad-instalada). Se corta cuando una página vuelve
    con menos elementos que el límite pedido, o vacía."""
    url = f"{BASE_URL}/centrales/v4/findByDate"
    registros: list[dict] = []
    page = 1
    while page <= MAX_PAGES_SAFETY:
        body = _request_with_retry(url, {"user_key": api_key, "page": page, "limit": PAGE_LIMIT})
        data = body if isinstance(body, list) else body.get("data", [])
        registros.extend(data)
        if len(data) < PAGE_LIMIT:
            break
        page += 1
    print(f"  centrales: {len(registros)} registros ({page} páginas)")
    return registros


def _to_float(value):
    """Los campos numéricos de /centrales vienen inconsistentes: algunos
    registros usan coma decimal ('2,96'), otros punto decimal ('88.6173'),
    y hay strings vacíos cuando el registro está en revisión. Se detecta
    el formato por la presencia de coma en vez de asumir uno fijo — tratar
    todo como "coma decimal, punto de miles" corrompía los valores que ya
    venían con punto decimal (ej. '88.6173' -> 886173.0, 10000x más grande).
    None si no se puede convertir."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    if "," in s:
        # Coma = separador decimal; cualquier punto presente es de miles.
        s = s.replace(".", "").replace(",", ".")
    # Si no hay coma, el punto (si existe) ya es el separador decimal: no tocar.
    try:
        return float(s)
    except ValueError:
        return None


def build_database(capacidad: list[dict], centrales: list[dict]) -> None:
    con = duckdb.connect(DB_PATH)

    con.execute("DROP TABLE IF EXISTS capacidad_instalada_raw")
    con.execute("DROP TABLE IF EXISTS centrales_raw")
    con.execute("DROP VIEW IF EXISTS capacidad_central")

    import pandas as pd

    df_cap = pd.DataFrame(capacidad)
    df_cen = pd.DataFrame(centrales)

    # Limpieza numérica de /centrales (potencias vienen como texto con coma decimal)
    for col in ["pot_max_bruta", "capac_max", "pot_min_tecnica", "coordenada_este", "coordenada_norte"]:
        if col in df_cen.columns:
            df_cen[col + "_num"] = df_cen[col].apply(_to_float)

    con.register("df_cap", df_cap)
    con.register("df_cen", df_cen)
    con.execute("CREATE TABLE capacidad_instalada_raw AS SELECT * FROM df_cap")
    con.execute("CREATE TABLE centrales_raw AS SELECT * FROM df_cen")

    # Vista de análisis: centrales como base (trae estado + tecnología),
    # cruzada con capacidad_instalada por id_central para dejar registrada
    # la potencia declarada en ambas fuentes lado a lado.
    con.execute("""
        CREATE VIEW capacidad_central AS
        SELECT
            cen.id_central,
            cen.central                                AS nombre_central,
            cen.propietario,
            cen.estado,
            cen.tipo_central,
            cen.tipo_tecnologia,
            cen.conv_ernc,
            cen.combus_termo,
            cen.region,
            cen.provincia,
            cen.comuna,
            cen.punto_conexion,
            cen.fecha_ent_oper,
            cen.capac_max_num                          AS capacidad_mw_centrales,
            cap.potencia_maxima                        AS capacidad_mw_capacidad_instalada,
            cen.coordenada_este_num                    AS coordenada_este,
            cen.coordenada_norte_num                   AS coordenada_norte
        FROM centrales_raw cen
        LEFT JOIN capacidad_instalada_raw cap USING (id_central)
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS meta_actualizaciones (
            corrida_utc TIMESTAMP,
            n_capacidad_instalada INTEGER,
            n_centrales INTEGER
        )
    """)
    con.execute(
        "INSERT INTO meta_actualizaciones VALUES (?, ?, ?)",
        [datetime.now(timezone.utc), len(capacidad), len(centrales)],
    )

    n_total = con.execute("SELECT COUNT(*) FROM capacidad_central").fetchone()[0]
    print(f"  vista capacidad_central: {n_total} filas")
    con.close()


def main():
    api_key = _get_api_key()
    print("Descargando capacidad-instalada...")
    capacidad = fetch_capacidad_instalada(api_key)
    print("Descargando centrales...")
    centrales = fetch_centrales(api_key)
    print("Armando base DuckDB...")
    build_database(capacidad, centrales)
    print(f"Listo: {DB_PATH}")


if __name__ == "__main__":
    main()
