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
    """/contratos_de_suministro_vigentes/slices/ trae el listado de IDs de
    "suministrador" que aparecen en algún contrato. Es solo un índice, sin
    el detalle de cada contrato.

    OJO: este ID **no es** el campo `numero` de infotecnica/empresas, sino
    su campo `grupo` (grupo económico) -- confirmado probando en vivo:
    filtrar por suministrador_mnemotecnico=G0021 (ENEL GENERACIÓN CHILE,
    numero=21, grupo=20) devuelve filas con "suministrador": 20, no 21.
    Además `grupo` tampoco es único (varias empresas del mismo grupo
    económico lo comparten), así que resolver el nombre "hacia atrás" desde
    este ID es ambiguo -- por eso fetch_contratos anota el nombre real
    directamente al bajar los datos, en vez de cruzarlo después por ID."""
    url = f"{BASE_URL}/api/v2/recursos/contratos_de_suministro_vigentes/slices/"
    registros = _paginate_drf(url, api_key)
    # Cada elemento viene como {"suministrador": "10"} -- el número como
    # STRING, no int, a diferencia de infotecnica/empresas donde `numero`
    # y `grupo` son int. Sin este cast, el cruce no encuentra nada.
    grupos: list[int] = []
    for r in registros:
        crudo = r if isinstance(r, (int, str)) else r.get("suministrador")
        if crudo is None:
            continue
        try:
            grupos.append(int(crudo))
        except (TypeError, ValueError):
            print(f"  valor de suministrador no numérico, se omite: {crudo!r}", file=sys.stderr)
    print(f"  índice de suministradores: {len(grupos)} grupos económicos")
    return grupos


def _elegir_representante(candidatos: list[dict]) -> dict:
    """Cuando varias empresas comparten el mismo `grupo`, se prioriza la que
    tiene numero == grupo (empíricamente, suele ser la entidad "raíz" del
    grupo económico -- ej. Colbún numero=4/grupo=4, mientras sus filiales
    comparten grupo=4 con numero propio distinto) y que no esté marcada
    "[No_Mostrar]"; si ninguna calza con eso, la primera disponible."""
    for c in candidatos:
        if c.get("numero") == c.get("grupo") and "[No_Mostrar]" not in (c.get("nombre") or ""):
            return c
    for c in candidatos:
        if "[No_Mostrar]" not in (c.get("nombre") or ""):
            return c
    return candidatos[0]


def fetch_contratos(api_key: str, empresas: list[dict]) -> list[dict]:
    """Contratos de suministro vigentes, iterando por cada grupo económico
    del índice (el endpoint exige el filtro `suministrador_mnemotecnico`;
    sin él responde 400 aunque el Swagger lo marque como opcional).

    El nombre del suministrador se anota directamente sobre cada fila
    usando la empresa representante que se eligió para consultar ese
    grupo -- no se resuelve después por ID, porque `grupo` no es único
    (ver fetch_suministradores_index)."""
    url = f"{BASE_URL}/api/v2/recursos/contratos_de_suministro_vigentes/"

    candidatos_por_grupo: dict[int, list[dict]] = {}
    for e in empresas:
        mnem = str(e.get("mnemotecnico", ""))
        if mnem.startswith("G") and e.get("grupo") is not None:
            candidatos_por_grupo.setdefault(e["grupo"], []).append(e)
    grupo_a_representante = {g: _elegir_representante(cs) for g, cs in candidatos_por_grupo.items()}
    print(f"  ({len(grupo_a_representante)} grupos económicos con al menos una empresa 'G...' disponible como suministradora)")

    grupos = fetch_suministradores_index(api_key)
    todos: list[dict] = []
    debug_lines = [f"índice de suministradores: {len(grupos)} grupos -> {grupos}"]
    for i, grupo in enumerate(grupos, start=1):
        rep = grupo_a_representante.get(grupo)
        if not rep:
            msg = f"  [{i}/{len(grupos)}] grupo={grupo}: sin empresa 'G...' con ese grupo en infotecnica/empresas, se omite"
            print(msg, file=sys.stderr)
            debug_lines.append(msg)
            continue
        mnemotecnico, nombre = rep["mnemotecnico"], rep["nombre"]
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
            debug_lines.append(
                f"  [{i}/{len(grupos)}] grupo={grupo} mnemotecnico={mnemotecnico} ({nombre}) "
                f"offset={offset}: count={body.get('count')} len(results)={len(results)} next={bool(body.get('next'))}"
            )
            if not body.get("next") or not results:
                break
            offset += PAGE_LIMIT
        for r in registros:
            r["suministrador_nombre"] = nombre
            r["suministrador_mnemotecnico_resuelto"] = mnemotecnico
        todos.extend(registros)
        print(f"  [{i}/{len(grupos)}] {mnemotecnico} ({nombre}): {len(registros)} filas de contrato")
    print(f"  contratos_de_suministro_vigentes: {len(todos)} filas en total")

    debug_path = os.path.join(os.path.dirname(__file__), "pipeline_contratos_debug.log")
    with open(debug_path, "w") as f:
        f.write(f"Corrida: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"grupos económicos resolubles: {len(grupo_a_representante)}\n")
        f.write("\n".join(debug_lines))
        f.write("\n")

    return todos


def build_database(empresas: list[dict], contratos: list[dict]) -> None:
    if not empresas:
        raise RuntimeError("fetch_empresas() no devolvió ningún registro; no se actualiza la base.")
    if not contratos:
        raise RuntimeError("fetch_contratos() no devolvió ningún registro; no se actualiza la base.")

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
    df_con.insert(0, "contrato_row_id", range(len(df_con)))

    con.register("df_emp", df_emp)
    con.register("df_con", df_con)
    con.execute("CREATE TABLE empresas_infotecnica_raw AS SELECT * FROM df_emp")
    con.execute("CREATE TABLE contratos_suministro_raw AS SELECT * FROM df_con")

    # Vista de análisis: cada fila de contrato con el nombre del cliente
    # resuelto. El nombre del suministrador YA viene anotado en cada fila
    # desde fetch_contratos (columnas suministrador_nombre /
    # suministrador_mnemotecnico_resuelto) -- no se cruza por ID acá.
    #
    # ¿Por qué no cruzar también al cliente por `numero` o `grupo` sin más?
    # `suministrador`/`cliente` en el contrato son un ID de GRUPO económico
    # (confirmado en vivo: filtrar por G0021 -- numero=21, grupo=20 --
    # devuelve filas con "suministrador": 20), y `grupo` tampoco es único
    # entre empresas del mismo grupo económico. Sin poder repetir el mismo
    # truco que con el suministrador (acá no elegimos con qué mnemotécnico
    # consultar), se prioriza por heurística: `tipo` del contrato (R/L/C)
    # sugiere la categoría, luego se evita nombres "[No_Mostrar]" y se
    # prefiere numero==grupo (suele ser la entidad "raíz"); QUALIFY se
    # queda con una sola fila por contrato aunque la pista no alcance a
    # desambiguar del todo (mejor una coincidencia posiblemente imperfecta
    # que filas duplicadas).
    con.execute("""
        CREATE VIEW contratos_con_nombres AS
        WITH cliente_candidato AS (
            SELECT
                c.contrato_row_id,
                cli.nombre       AS cliente_nombre,
                cli.mnemotecnico AS cliente_mnemotecnico_resuelto,
                ROW_NUMBER() OVER (
                    PARTITION BY c.contrato_row_id
                    ORDER BY
                        CASE
                            WHEN c.tipo = 'C' AND cli.mnemotecnico LIKE 'G%' THEN 0
                            WHEN c.tipo = 'L' AND cli.mnemotecnico LIKE 'L%' THEN 0
                            WHEN c.tipo = 'R' AND cli.mnemotecnico NOT LIKE 'G%' THEN 0
                            ELSE 1
                        END,
                        CASE WHEN cli.nombre LIKE '%[No_Mostrar]%' THEN 1 ELSE 0 END,
                        CASE WHEN cli.numero = cli.grupo THEN 0 ELSE 1 END,
                        cli.id_infotecnica
                ) AS rn
            FROM contratos_suministro_raw c
            LEFT JOIN empresas_infotecnica_raw cli ON cli.grupo = c.cliente
        )
        SELECT
            c.*,
            cc.cliente_nombre,
            cc.cliente_mnemotecnico_resuelto
        FROM contratos_suministro_raw c
        LEFT JOIN cliente_candidato cc
            ON cc.contrato_row_id = c.contrato_row_id AND cc.rn = 1
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
    # Limpia el log de una corrida fallida anterior; si esta corrida
    # también falla, el bloque de abajo lo vuelve a escribir.
    error_path = os.path.join(os.path.dirname(__file__), "pipeline_contratos_error.log")
    if os.path.exists(error_path):
        os.remove(error_path)

    api_key = _get_api_key()
    print("Descargando infotecnica/empresas...")
    empresas = fetch_empresas(api_key)
    print("Descargando contratos_de_suministro_vigentes...")
    contratos = fetch_contratos(api_key, empresas)
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
