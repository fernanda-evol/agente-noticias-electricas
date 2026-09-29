"""Pipeline de ingesta del Balance de Energía de Plabacom (Coordinador Eléctrico).

A diferencia de pipeline.py y pipeline_contratos.py, este pipeline NO se
ejecuta automáticamente vía GitHub Actions: Plabacom no tiene una API
pública, solo descarga manual desde su plataforma web. El flujo es:

    1. Fernanda descarga el zip mensual desde plabacom.coordinador.cl
       (carpeta "01 Resultados_AAMM_BD01" o "..._BP01").
    2. Sube el archivo Balance_AAMM{D|P}.xlsm (o el zip completo) al chat.
    3. Se ejecuta este script apuntando a ese archivo, una vez por mes.
    4. El resultado se commitea a mercado.duckdb igual que los otros datos.

Este script lee dos hojas del Balance:
  - "Balance Físico": el detalle por punto de medida (barra, medidor,
    propietario/empresa, tipo de retiro/inyección, energía). Es la fuente
    de la vista "clientes por empresa".
  - No se usa "Balance Comercial" (son transacciones de trading entre
    generadoras, no relación generador-cliente) ni "Contratos" (agregado
    sin contraparte) — ver conversación del 2026-09-29 para el porqué.

tipo_medidor relevantes para la vista de clientes:
  L    = Retiro Libre (cliente libre identificado individualmente)
  L_D  = Retiro Libre vía Distribución
  L_S  = Retiro Libre, servicios auxiliares/propios
  R    = Retiro Regulado (agregado por distribuidora/zona, sin cliente individual)
  G / G_SAE = Inyección de generación
  C_FIS / C_FIN = Compraventa física/financiera entre generadoras (trading, no clientes)
  T    = Transmisión
  N    = Neteo
"""

import argparse
import re
import sys
import zipfile
from pathlib import Path

import duckdb
import openpyxl
import pandas as pd

DB_PATH = Path(__file__).parent / "mercado.duckdb"

COLUMNAS_BALANCE_FISICO = [
    "barra", "nivel_tension", "nombre_medidor", "clave_medidor",
    "propietario_medidor", "rut", "nombre_corto", "numero_linea",
    "calificacion_linea", "linea_barra_inicial", "linea_nivel_tension_inicial",
    "linea_barra_final", "linea_nivel_tension_final",
    "medida1", "flag1", "medida2", "flag2", "medida2a", "flag2a",
    "medida3", "flag3", "error", "tipo_medidor", "calculo", "zona",
    "origen_medida",
]


def _detectar_periodo(nombre_archivo: str) -> tuple[str, str]:
    """Extrae (periodo AAMM, tipo D/P) desde un nombre tipo Balance_2607D.xlsm."""
    m = re.search(r"Balance_(\d{4})([DP])", nombre_archivo, re.IGNORECASE)
    if not m:
        raise ValueError(
            f"No pude reconocer el período en el nombre de archivo '{nombre_archivo}'. "
            "Se espera algo como Balance_2607D.xlsm o Balance_2607P.xlsm."
        )
    return m.group(1), m.group(2).upper()


def _ubicar_balance_xlsm(ruta: Path) -> Path:
    """Si `ruta` es un .zip de Plabacom, extrae el Balance_AAMM{D|P}.xlsm a un
    directorio temporal junto al zip y devuelve su ruta. Si ya es un .xlsm,
    lo devuelve tal cual."""
    if ruta.suffix.lower() == ".xlsm":
        return ruta

    if ruta.suffix.lower() != ".zip":
        raise ValueError(f"Formato no soportado: {ruta.suffix}. Se espera .xlsm o .zip")

    destino = ruta.parent / (ruta.stem + "_extraido")
    destino.mkdir(exist_ok=True)
    with zipfile.ZipFile(ruta) as zf:
        candidatos = [n for n in zf.namelist() if re.search(r"Balance_\d{4}[DP]\.xlsm$", n, re.IGNORECASE)]
        if not candidatos:
            raise ValueError(f"No encontré un Balance_AAMM{{D|P}}.xlsm dentro de {ruta.name}")
        nombre_interno = candidatos[0]
        zf.extract(nombre_interno, destino)
        return destino / nombre_interno


def leer_balance_fisico(ruta_xlsm: Path) -> pd.DataFrame:
    wb = openpyxl.load_workbook(ruta_xlsm, read_only=True, data_only=True)
    ws = wb["Balance Físico"]
    filas = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue  # encabezado
        filas.append(row[: len(COLUMNAS_BALANCE_FISICO)])
    df = pd.DataFrame(filas, columns=COLUMNAS_BALANCE_FISICO)
    df["medida_kwh"] = pd.to_numeric(df["medida1"], errors="coerce").fillna(0.0)
    return df


def construir_tabla_retiros(df: pd.DataFrame, periodo: str, tipo_balance: str) -> pd.DataFrame:
    """Deja una fila por punto de medida con la energía en valor absoluto
    (el signo negativo de Plabacom indica retiro; para esta vista solo nos
    interesa la magnitud) y el tipo de medidor normalizado."""
    out = df.copy()
    out["periodo"] = periodo
    out["tipo_balance"] = tipo_balance  # D = Definitivo, P = Preliminar
    out["energia_kwh_abs"] = out["medida_kwh"].abs()
    out["propietario_medidor"] = out["propietario_medidor"].astype(str).str.strip()
    out["nombre_medidor"] = out["nombre_medidor"].astype(str).str.strip()
    out["tipo_medidor"] = out["tipo_medidor"].astype(str).str.strip()
    return out[[
        "periodo", "tipo_balance", "barra", "nombre_medidor", "clave_medidor",
        "propietario_medidor", "rut", "tipo_medidor", "zona",
        "energia_kwh_abs", "medida_kwh",
    ]]


def build_database(tabla_retiros: pd.DataFrame, periodo: str, tipo_balance: str):
    con = duckdb.connect(str(DB_PATH))
    con.register("tabla_retiros_df", tabla_retiros)

    con.execute("""
        CREATE TABLE IF NOT EXISTS retiros_balance (
            periodo VARCHAR, tipo_balance VARCHAR, barra VARCHAR,
            nombre_medidor VARCHAR, clave_medidor VARCHAR,
            propietario_medidor VARCHAR, rut VARCHAR, tipo_medidor VARCHAR,
            zona VARCHAR, energia_kwh_abs DOUBLE, medida_kwh DOUBLE
        )
    """)
    # Reemplaza el período si ya existía (permite re-procesar Preliminar -> Definitivo)
    con.execute(
        "DELETE FROM retiros_balance WHERE periodo = ? AND tipo_balance = ?",
        [periodo, tipo_balance],
    )
    con.execute("INSERT INTO retiros_balance SELECT * FROM tabla_retiros_df")

    con.execute("""
        CREATE TABLE IF NOT EXISTS meta_actualizaciones_balance (
            periodo VARCHAR, tipo_balance VARCHAR, actualizado_en TIMESTAMP, n_filas BIGINT
        )
    """)
    con.execute(
        "DELETE FROM meta_actualizaciones_balance WHERE periodo = ? AND tipo_balance = ?",
        [periodo, tipo_balance],
    )
    con.execute(
        "INSERT INTO meta_actualizaciones_balance VALUES (?, ?, now(), ?)",
        [periodo, tipo_balance, len(tabla_retiros)],
    )
    con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archivo", help="Ruta al Balance_AAMM{D|P}.xlsm o al .zip descargado de Plabacom")
    args = parser.parse_args()

    ruta = Path(args.archivo)
    if not ruta.exists():
        print(f"No existe el archivo: {ruta}", file=sys.stderr)
        sys.exit(1)

    xlsm = _ubicar_balance_xlsm(ruta)
    periodo, tipo_balance = _detectar_periodo(xlsm.name)
    print(f"Procesando período {periodo} ({tipo_balance}) desde {xlsm.name}...")

    df = leer_balance_fisico(xlsm)
    print(f"  {len(df)} filas leídas de 'Balance Físico'")

    tabla = construir_tabla_retiros(df, periodo, tipo_balance)
    build_database(tabla, periodo, tipo_balance)
    print(f"  Guardado en {DB_PATH} (tabla retiros_balance)")


if __name__ == "__main__":
    main()
