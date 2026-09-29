"""Pipeline de ingesta de "Contratos_publica.xlsx", el export de la
Plataforma de Contratos del Coordinador (no confundir con
`/api/v2/recursos/contratos_de_suministro_vigentes` de SIPUB, que este
pipeline REEMPLAZA).

Por qué el reemplazo (2026-09-29): comparado con el pipeline anterior
(pipeline_contratos.py, todavía en el repo por si se necesita volver
atrás), este archivo trae:
  - 233 suministradoras vs 23 que veíamos por la API
  - 2.950 clientes distintos vs 52
  - RUT directo de suministrador y cliente (SIPUB infotecnica NUNCA trae
    RUT -- 0 de 1.427 empresas lo tenían poblado)
  - Estado del contrato explícito ("Vigente"/"No vigente"), no inferido
    de fechas
  - Puntos de retiro con nombre de barra/S.E., cruzable con la barra de
    retiros_balance (Plabacom)

Como Plabacom, esta plataforma NO tiene API pública -- Fernanda descarga
el .xlsx manualmente y lo sube al chat cada vez que se necesite refrescar
("Contratos_publica.xlsx", sheet "contratos", filas 1-2 son encabezado
de dos niveles, formato ancho: una columna por año 1986-2060 para
Energía [MWh] y otra para Potencia [MW]).

Este script:
  1. Lee la hoja "contratos" (fila 2 = nombres de columna reales).
  2. Separa los metadatos del contrato (una fila por contrato) de las
     ~150 columnas año-a-año.
  3. "Derrite" (melt) Energía y Potencia a formato largo (una fila por
     contrato + año), descartando años en 0/nulos -- si no, el largo
     completo sería 11.116 contratos x 75 años ≈ 833.700 filas; filtrando
     ceros queda un orden de magnitud más chico y evita inflar
     mercado.duckdb sin necesidad.
  4. Escribe dos tablas: contratos_publica_raw (metadata, 1 fila/contrato)
     y contratos_publica_anual (1 fila por contrato+año con datos).
"""

import argparse
import sys
from pathlib import Path

import duckdb
import openpyxl
import pandas as pd

DB_PATH = Path(__file__).parent / "mercado.duckdb"

COLUMNAS_META = {
    "Id Contrato": "id_contrato",
    "Empresa Suministradora": "suministrador_nombre",
    "Rut Suministradora": "rut_suministrador",
    "Tipo Suministrador": "tipo_suministrador",
    "Tipo Contrato": "tipo_contrato",
    "Red Distribuidora": "red_distribuidora",
    "Tipo Cliente": "tipo_cliente",
    "Empresa Cliente": "cliente_nombre",
    "Rut Empresa Cliente": "rut_cliente",
    "Suscripción": "fecha_suscripcion",
    "Inicio": "fecha_inicio",
    "Término": "fecha_termino",
    "Fecha Renovación": "fecha_renovacion",
    "Fecha Inicio Renovación": "fecha_inicio_renovacion",
    "Fecha Término Renovación": "fecha_termino_renovacion",
    "Estado Contrato": "estado_contrato",
    "Afecto Ley": "afecto_ley_ernc",
    "Régimen Compra Potencia": "regimen_compra_potencia",
    "Región": "region",
    "Dirección Comercial": "direccion_comercial",
    "Sector Económico": "sector_economico",
    "Subsector Económico": "subsector_economico",
    "Listado de puntos de Suministro": "puntos_de_suministro",
    "Listado de puntos de Retiro": "puntos_de_retiro",
    "Listado de Potencia Conectada [MW]": "potencia_conectada_listado",
    "Numero Cliente": "numero_cliente",
    "Distribuidora Conectada": "distribuidora_conectada",
}


def leer_excel(ruta: Path) -> pd.DataFrame:
    wb = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
    ws = wb["contratos"]
    filas = list(ws.iter_rows(values_only=True))
    encabezado = filas[1]  # fila 0 son títulos de sección fusionados, fila 1 son los nombres reales
    datos = filas[2:]
    columnas = [c if c else f"col_{i}" for i, c in enumerate(encabezado)]
    df = pd.DataFrame(datos, columns=columnas)
    return df


def construir_meta(df: pd.DataFrame) -> pd.DataFrame:
    presentes = {k: v for k, v in COLUMNAS_META.items() if k in df.columns}
    meta = df[list(presentes.keys())].rename(columns=presentes).copy()
    for col in ("fecha_suscripcion", "fecha_inicio", "fecha_termino",
                "fecha_renovacion", "fecha_inicio_renovacion", "fecha_termino_renovacion"):
        if col in meta.columns:
            meta[col] = pd.to_datetime(meta[col], errors="coerce", dayfirst=True)
    for col in ("rut_suministrador", "rut_cliente"):
        if col in meta.columns:
            meta[col] = meta[col].astype(str).str.replace(".", "", regex=False).str.strip()
    return meta


def construir_anual(df: pd.DataFrame) -> pd.DataFrame:
    id_col = "Id Contrato"
    cols_energia = [c for c in df.columns if isinstance(c, str) and c.startswith("Energía ")]
    cols_potencia = [c for c in df.columns if isinstance(c, str) and c.startswith("Potencia ")]

    energia = df[[id_col] + cols_energia].melt(id_vars=id_col, var_name="col", value_name="energia_mwh")
    energia["año"] = energia["col"].str.extract(r"(\d{4})").astype(int)
    energia = energia.drop(columns="col")

    potencia = df[[id_col] + cols_potencia].melt(id_vars=id_col, var_name="col", value_name="potencia_mw")
    potencia["año"] = potencia["col"].str.extract(r"(\d{4})").astype(int)
    potencia = potencia.drop(columns="col")

    anual = pd.merge(energia, potencia, on=[id_col, "año"], how="outer")
    anual["energia_mwh"] = pd.to_numeric(anual["energia_mwh"], errors="coerce").fillna(0.0)
    anual["potencia_mw"] = pd.to_numeric(anual["potencia_mw"], errors="coerce").fillna(0.0)
    anual = anual[(anual["energia_mwh"] != 0) | (anual["potencia_mw"] != 0)]
    anual = anual.rename(columns={id_col: "id_contrato"})
    anual["energia_contratada_gwh"] = anual["energia_mwh"] / 1000.0
    return anual[["id_contrato", "año", "energia_contratada_gwh", "potencia_mw"]]


def build_database(meta: pd.DataFrame, anual: pd.DataFrame):
    con = duckdb.connect(str(DB_PATH))
    con.register("meta_df", meta)
    con.register("anual_df", anual)

    con.execute("CREATE OR REPLACE TABLE contratos_publica_raw AS SELECT * FROM meta_df")
    con.execute("CREATE OR REPLACE TABLE contratos_publica_anual AS SELECT * FROM anual_df")

    con.execute("""
        CREATE OR REPLACE VIEW contratos_con_nombres AS
        SELECT
            a.id_contrato AS contrato_row_id,
            m.fecha_suscripcion, m.fecha_inicio, m.fecha_termino,
            m.puntos_de_suministro, m.puntos_de_retiro,
            NULL::INTEGER AS potencia_conectada,
            a.año,
            a.energia_contratada_gwh AS energia_contratada,
            a.potencia_mw AS potencia_contratada,
            NULL::DOUBLE AS potencia_contratada_horapunta,
            NULL::DOUBLE AS potencia_contratada_no_horapunta,
            m.distribuidora_conectada AS nombre_distribuidora,
            CASE
                WHEN m.tipo_contrato = 'LIBRE' THEN 'L'
                WHEN m.tipo_contrato = 'LIBRE EN DISTRIBUCIÓN' THEN 'L_D'
                WHEN m.tipo_contrato = 'REGULADO' THEN 'R'
                ELSE 'C'
            END AS tipo,
            m.afecto_ley_ernc = 'Sí' AS afecto_obligacion_ernc,
            NULL::VARCHAR AS enlace,
            m.fecha_renovacion AS fecha_suscripcion_renovacion,
            m.fecha_inicio_renovacion, m.fecha_termino_renovacion,
            NULL::BIGINT AS suministrador, NULL::BIGINT AS cliente,
            m.suministrador_nombre,
            NULL::VARCHAR AS suministrador_mnemotecnico_resuelto,
            m.cliente_nombre,
            NULL::VARCHAR AS cliente_mnemotecnico_resuelto,
            m.rut_suministrador, m.rut_cliente,
            m.estado_contrato, m.tipo_suministrador, m.tipo_cliente,
            m.region, m.sector_economico, m.subsector_economico
        FROM contratos_publica_anual a
        JOIN contratos_publica_raw m ON m.id_contrato = a.id_contrato
    """)

    con.execute("""
        CREATE OR REPLACE TABLE meta_actualizaciones_contratos (
            corrida_utc TIMESTAMP, n_contratos BIGINT, n_filas_anuales BIGINT, fuente VARCHAR
        )
    """)
    con.execute(
        "INSERT INTO meta_actualizaciones_contratos VALUES (now(), ?, ?, 'Plataforma de Contratos (manual)')",
        [len(meta), len(anual)],
    )
    con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archivo", help="Ruta al Contratos_publica.xlsx descargado de la Plataforma de Contratos")
    args = parser.parse_args()

    ruta = Path(args.archivo)
    if not ruta.exists():
        print(f"No existe el archivo: {ruta}", file=sys.stderr)
        sys.exit(1)

    df = leer_excel(ruta)
    print(f"{len(df)} contratos leídos de {ruta.name}")

    meta = construir_meta(df)
    anual = construir_anual(df)
    print(f"{len(anual)} filas contrato-año con energía o potencia distinta de cero")

    build_database(meta, anual)
    print(f"Guardado en {DB_PATH}: contratos_publica_raw, contratos_publica_anual, y vista contratos_con_nombres")


if __name__ == "__main__":
    main()
