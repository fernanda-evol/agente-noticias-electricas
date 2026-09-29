"""
Pipeline de contratos de suministro y maestro de empresas (EVOL).

Descarga desde la API pública SIPUB del Coordinador Eléctrico Nacional,
grupo "v2/recursos" (distinto del v4 que usa pipeline.py para capacidad
instalada, pero mismo dominio y misma API key).

Endpoints usados:
  - /api/v2/recursos/infotecnica/empresas/  -> maestro de empresas
    (mnemotécnico tipo "G0004", nombre, y las centrales/barras que le
    pertenecen). No trae RUT (viene siempre null en esta API).
  - /api/v2/recursos/contratos_de_suministro_vigentes/slices/ -> lista
    de "numero" de empresas que aparecen como suministradoras en algún
    contrato. Sirve solo como índice: hay que iterar esta lista y
    consultar el endpoint principal filtrando por cada una, porque sin
    el filtro `suministrador_mnemotecnico` la API responde 400.
  - /api/v2/recursos/contratos_de_suministro_vigentes/?suministrador_mnemotecnico=G000N
    -> contratos de esa empresa como suministradora. Un contrato real
    aparece como varias filas (una por año de vigencia), con la energía
    y potencia contratada de ESE año — así se arma la evolución en el
    tiempo sin tener que derivarla.

`suministrador` y `cliente` en cada contrato son el campo `numero` de
infotecnica/empresas (no el mnemotécnico), así que el cruce a nombre de
empresa se hace por ese número.

Requiere la variable de entorno SIPUB_API_KEY (misma credencial que
pipeline.py).

Se ejecuta vía GitHub Actions con cadencia mensual, junto con
pipeline.py, o a mano con: python pipeline_contratos.py
"""

import os
import sys
import time
import traceback
from datetime import datetime, timezone

import duckdb
import requests

BASE_URL = "https://sipub.api.coordinador.cl"
DB_PATH = os.path.join(os.path.dirname(__file__), "mercado.duckdb")
PAGE_LIMIT = 100
MAX_PAGES_SAFETY = 500
REQUEST_TIMEOUT = 30
RETRY_ATTEMPTS = 3
RETRY_SLEEP_SECONDS = 5


def _get_api_key() -> str:
    key = os.environ.get("SIPUB_API_KEY")
    if not key:
        print("ERROR: falta la variable de entorno SIPUB_API_KEY", file=sys.stderr)
        sys.exit(1)
    return key


def _request_with_retry(url: str, params: dict) -> dict:
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


def _paginate_drf(url: str, api_key: str) -> list[dict]:
    """Pagina un endpoint con paginación estilo Django REST Framework:
    {"count": N, "next": "...offset=X...", "previous": ..., "results": [...]}"""
    registros: list[dict] = []
    offset = 0
    page = 0
    while page <= MAX_PAGES_SAFETY:
        body = _request_with_retry(url, {"user_key": api_key, "limit": PAGE_LIMIT, "offset": offset})
        results = body.get("results", [])
        registros.extend(results)
        if not body.get("next") or not results:
            break
        offset += PAGE_LIMIT
        page += 1
    return registros


def fetch_empresas(api_key: str) -> list[dict]:
    url = f"{BASE_URL}/api/v2/recursos/infotecnica/empresas/"
    registros = _paginate_drf(url, api_key)
    print(f"  infotecnica/empresas: {len(registros)} registros")
    return registros


def fetch_suministradores_index(api_key: str) -> list[int]:
    """/contratos_de_suministro_vigentes/slices/ trae el listado de
    `numero` de empresas que aparecen como suministradoras en algún
    contrato. Es solo un índice, sin el detalle de cada contrato."""
    url = f"{BASE_URL}/api/v2/recursos/contratos_de_suministro_vigentes/slices/"
    registros = _paginate_drf(url, api_key)
    numeros = [r if isinstance(r, int) else r.get("suministrador") for r in registros]
    numeros = [n for n in numeros if n is not None]
    print(f"  índice de suministradores: {len(numeros)} empresas")
    return numeros


def fetch_contratos(api_key: str, numero_a_mnemotecnico: dict[int, str]) -> list[dict]:
    """Contratos de suministro vigentes, iterando por cada suministrador
    del índice (el endpoint exige el filtro `suministrador_mnemotecnico`;
    sin él responde 400 aunque el Swagger lo marque como opcional)."""
    url = f"{BASE_URL}/api/v2/recursos/contratos_de_suministro_vigentes/"
    numeros = fetch_suministradores_index(api_key)
    todos: list[dict] = []
    for i, numero in enumerate(numeros, start=1):
        mnemotecnico = numero_a_mnemotecnico.get(numero)
        if not mnemotecnico:
            print(f"  [{i}/{len(numeros)}] numero={numero}: sin mnemotécnico en infotecnica/empresas, se omite", file=sys.stderr)
            continue
        registros: list[dict] = []
        offset = 0
        while offset <= MAX_PAGES_SAFETY * PAGE_LIMIT:
            body = _request_with_retry(url, {
                "user_key": api_key,
                "suministrador_mnemotecnico": mnemotecnico,
                "limit": PAGE_LIMIT,
                "offset": offset,
            })
            results = body.get("results", [])
            registros.extend(results)
            if not body.get("next") or not results:
                break
            offset += PAGE_LIMIT
        todos.extend(registros)
        print(f"  [{i}/{len(numeros)}] {mnemotecnico}: {len(registros)} filas de contrato")
    print(f"  contratos_de_suministro_vigentes: {len(todos)} filas en total")
    return todos


def build_database(empresas: list[dict], contratos: list[dict]) -> None:
    con = duckdb.connect(DB_PATH)

    con.execute("DROP TABLE IF EXISTS empresas_infotecnica_raw")
    con.execute("DROP TABLE IF EXISTS contratos_suministro_raw")
    con.execute("DROP VIEW IF EXISTS contratos_con_nombres")

    import pandas as pd

    df_emp = pd.DataFrame(empresas)
    # barra_set / central_set / linea_set / subestacion_set / paño_set son listas -> a texto,
    # para que DuckDB no tenga que modelar tipos anidados que no usamos en los cruces.
    for col in ["barra_set", "central_set", "linea_set", "subestacion_set", "paño_set"]:
        if col in df_emp.columns:
            df_emp[col] = df_emp[col].apply(lambda v: ", ".join(v) if isinstance(v, list) else v)

    df_con = pd.DataFrame(contratos)

    con.register("df_emp", df_emp)
    con.register("df_con", df_con)
    con.execute("CREATE TABLE empresas_infotecnica_raw AS SELECT * FROM df_emp")
    con.execute("CREATE TABLE contratos_suministro_raw AS SELECT * FROM df_con")

    # Vista de análisis: cada fila de contrato con el nombre de la empresa
    # suministradora y del cliente resueltos (join por `numero`).
    con.execute("""
        CREATE VIEW contratos_con_nombres AS
        SELECT
            c.*,
            sup.nombre        AS suministrador_nombre,
            sup.mnemotecnico  AS suministrador_mnemotecnico_resuelto,
            cli.nombre        AS cliente_nombre,
            cli.mnemotecnico  AS cliente_mnemotecnico_resuelto
        FROM contratos_suministro_raw c
        LEFT JOIN empresas_infotecnica_raw sup ON sup.numero = c.suministrador
        LEFT JOIN empresas_infotecnica_raw cli ON cli.numero = c.cliente
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS meta_actualizaciones_contratos (
            corrida_utc TIMESTAMP,
            n_empresas INTEGER,
            n_contratos INTEGER
        )
    """)
    con.execute(
        "INSERT INTO meta_actualizaciones_contratos VALUES (?, ?, ?)",
        [datetime.now(timezone.utc), len(empresas), len(contratos)],
    )

    n_total = con.execute("SELECT COUNT(*) FROM contratos_con_nombres").fetchone()[0]
    print(f"  vista contratos_con_nombres: {n_total} filas")
    con.close()


def main():
    api_key = _get_api_key()
    print("Descargando infotecnica/empresas...")
    empresas = fetch_empresas(api_key)
    numero_a_mnemotecnico = {e["numero"]: e["mnemotecnico"] for e in empresas if e.get("numero") is not None}
    print("Descargando contratos_de_suministro_vigentes...")
    contratos = fetch_contratos(api_key, numero_a_mnemotecnico)
    print("Actualizando base DuckDB...")
    build_database(empresas, contratos)
    print(f"Listo: {DB_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Se deja constancia del traceback en un archivo (además de stderr)
        # porque los logs de este workflow no son fáciles de inspeccionar
        # fuera de GitHub; el workflow lo comitea al repo si el paso falla.
        error_path = os.path.join(os.path.dirname(__file__), "pipeline_contratos_error.log")
        with open(error_path, "w") as f:
            f.write(f"Corrida fallida: {datetime.now(timezone.utc).isoformat()}\n\n")
            f.write(traceback.format_exc())
        traceback.print_exc()
        sys.exit(1)
