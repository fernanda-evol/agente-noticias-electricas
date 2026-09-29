"""Construye el diccionario maestro de empresas (empresa_id único por
compañía real) a partir de las 4 fuentes de datos, para dejar de resolver
el cruce entre tablas con una heurística distinta en cada pestaña.

Estrategia:
  1. Contratos (Plataforma de Contratos) y Balance (Plabacom) SÍ traen
     RUT -> se agrupan por RUT normalizado. El RUT pasa a ser el
     empresa_id canónico para todo lo que aparece en cualquiera de esas
     dos fuentes. El nombre canónico es el más frecuente entre sus alias.
  2. SIPUB (capacidad_central.propietario y empresas_infotecnica_raw.nombre)
     NUNCA trae RUT -> se intenta calzar por nombre (tokens, sin
     stopwords de razón social) contra el pool de nombres canónicos ya
     anclados por RUT:
       - Coincidencia exacta de tokens -> confianza "alta"
       - Buen solape (Jaccard >= 0.6) y sin ambigüedad -> confianza "media"
       - Si no hay candidato razonable -> queda con un empresa_id
         temporal (prefijo "SIPUB::") y confianza "pendiente"
  3. Se escriben dos tablas en mercado.duckdb:
       - diccionario_empresas (empresa_id, nombre_canonico, n_fuentes)
       - diccionario_alias (empresa_id, fuente, nombre_original,
         identificador_original, confianza)
     y se exporta un Excel con los "pendiente" para que Fernanda los
     revise y confirme (o corrija) manualmente.

Este script es de construcción/actualización puntual -- se corre a mano
cuando se quiera refrescar el diccionario, no forma parte de ningún
pipeline automático.
"""

import re
import unicodedata
from collections import Counter
from pathlib import Path

import duckdb
import pandas as pd

DB_PATH = Path(__file__).parent / "mercado.duckdb"
SALIDA_PENDIENTES = Path(__file__).parent / "diccionario_pendientes_revision.xlsx"

STOPWORDS = {
    "SA", "S.A.", "SPA", "LTDA", "LIMITADA", "SOCIEDAD", "COMPANIA",
    "COMPAÑIA", "CONTRACTUAL", "CIA", "DE", "DEL", "LA", "EL", "LOS",
    "LAS", "Y", "S", "A", "EIRL", "GENERACION", "ENERGIA", "CHILE",
}


def _normalizar_rut(rut) -> str:
    if not rut or str(rut).strip().lower() in ("nan", "none", ""):
        return ""
    return str(rut).strip().replace(".", "").replace(" ", "").upper()


def _tokens(nombre: str) -> frozenset:
    if not nombre:
        return frozenset()
    t = unicodedata.normalize("NFKD", str(nombre)).encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9 ]", " ", t).upper()
    return frozenset(tok for tok in t.split() if tok not in STOPWORDS and len(tok) > 2)


def cargar_fuentes():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    contratos_sum = con.execute(
        "SELECT DISTINCT rut_suministrador AS rut, suministrador_nombre AS nombre FROM contratos_publica_raw"
    ).df()
    contratos_cli = con.execute(
        "SELECT DISTINCT rut_cliente AS rut, cliente_nombre AS nombre FROM contratos_publica_raw"
    ).df()
    balance = con.execute(
        "SELECT DISTINCT rut, propietario_medidor AS nombre FROM retiros_balance WHERE rut IS NOT NULL"
    ).df()
    capacidad = con.execute("SELECT DISTINCT propietario AS nombre FROM capacidad_central").df()
    infotecnica = con.execute("SELECT DISTINCT nombre, mnemotecnico FROM empresas_infotecnica_raw").df()
    con.close()

    contratos_sum["fuente"] = "contratos_suministrador"
    contratos_cli["fuente"] = "contratos_cliente"
    balance["fuente"] = "balance"
    return contratos_sum, contratos_cli, balance, capacidad, infotecnica


def construir_pool_con_rut(contratos_sum, contratos_cli, balance) -> pd.DataFrame:
    partes = []
    for df in (contratos_sum, contratos_cli, balance):
        d = df.copy()
        d["rut_norm"] = d["rut"].apply(_normalizar_rut)
        d = d[d["rut_norm"] != ""]
        d = d[d["nombre"].notna() & (d["nombre"].astype(str).str.strip() != "")]
        partes.append(d[["rut_norm", "nombre", "fuente"]])
    pool = pd.concat(partes, ignore_index=True)
    return pool


def elegir_nombre_canonico(nombres: list) -> str:
    # El más largo entre los más frecuentes: suele ser la razón social
    # completa en vez de una sigla o nombre de sitio.
    conteo = Counter(nombres)
    max_frec = max(conteo.values())
    candidatos = [n for n, c in conteo.items() if c == max_frec]
    return max(candidatos, key=len)


def construir_diccionario_base(pool: pd.DataFrame):
    empresas = []
    alias = []
    for rut_norm, grupo in pool.groupby("rut_norm"):
        nombre_canonico = elegir_nombre_canonico(grupo["nombre"].tolist())
        empresas.append({
            "empresa_id": rut_norm,
            "nombre_canonico": nombre_canonico,
            "n_fuentes": grupo["fuente"].nunique(),
            "n_alias": grupo["nombre"].nunique(),
        })
        for _, fila in grupo.drop_duplicates(["nombre", "fuente"]).iterrows():
            alias.append({
                "empresa_id": rut_norm, "fuente": fila["fuente"],
                "nombre_original": fila["nombre"], "identificador_original": rut_norm,
                "confianza": "alta (RUT)",
            })
    return pd.DataFrame(empresas), pd.DataFrame(alias), {r["empresa_id"]: _tokens(r["nombre_canonico"]) for r in empresas}


def calzar_sipub(nombres_sipub: pd.DataFrame, fuente: str, id_col_extra: str, tokens_canonicos: dict, umbral=0.6):
    resultados = []
    for _, fila in nombres_sipub.iterrows():
        nombre = fila["nombre"]
        tok = _tokens(nombre)
        if not tok:
            continue
        mejor_id, mejor_score, empate = None, 0.0, False
        for empresa_id, tok_canon in tokens_canonicos.items():
            if not tok_canon:
                continue
            inter = len(tok & tok_canon)
            if inter == 0:
                continue
            union = len(tok | tok_canon)
            score = inter / union
            if score > mejor_score:
                mejor_score, mejor_id, empate = score, empresa_id, False
            elif score == mejor_score and empresa_id != mejor_id:
                empate = True

        if mejor_id and tok == tokens_canonicos[mejor_id]:
            confianza = "alta (nombre exacto)"
        elif mejor_id and mejor_score >= umbral and not empate:
            confianza = "media (nombre similar)"
        else:
            mejor_id, confianza = None, "pendiente"

        empresa_id_final = mejor_id if mejor_id else f"SIPUB::{'|'.join(sorted(tok))}"
        resultados.append({
            "empresa_id": empresa_id_final, "fuente": fuente,
            "nombre_original": nombre,
            "identificador_original": fila.get(id_col_extra, "") if id_col_extra else "",
            "confianza": confianza,
            "score": round(mejor_score, 2) if mejor_id else 0.0,
        })
    return pd.DataFrame(resultados)


def main():
    contratos_sum, contratos_cli, balance, capacidad, infotecnica = cargar_fuentes()
    pool = construir_pool_con_rut(contratos_sum, contratos_cli, balance)
    print(f"{len(pool)} pares (RUT, nombre) desde Contratos + Balance")

    empresas_df, alias_rut_df, tokens_canonicos = construir_diccionario_base(pool)
    print(f"{len(empresas_df)} empresas ancladas por RUT")

    capacidad["_dummy"] = ""
    infotecnica_conmn = infotecnica.rename(columns={"mnemotecnico": "mnemotecnico"})

    alias_capacidad = calzar_sipub(capacidad, "capacidad_central", None, tokens_canonicos)
    alias_infotecnica = calzar_sipub(infotecnica_conmn, "empresas_infotecnica", "mnemotecnico", tokens_canonicos)

    alias_sipub = pd.concat([alias_capacidad, alias_infotecnica], ignore_index=True)
    print(f"{len(alias_sipub)} nombres SIPUB procesados:")
    print(alias_sipub["confianza"].value_counts().to_string())

    # Empresas SIPUB que no calzaron con ninguna del pool RUT: se agregan
    # como entidades propias (empresa_id temporal) para que también
    # queden en el diccionario, marcadas como pendientes de revisar.
    pendientes_ids = alias_sipub[alias_sipub["confianza"] == "pendiente"]["empresa_id"].unique()
    extra_empresas = []
    for eid in pendientes_ids:
        nombre = alias_sipub[alias_sipub["empresa_id"] == eid]["nombre_original"].iloc[0]
        extra_empresas.append({"empresa_id": eid, "nombre_canonico": nombre, "n_fuentes": 1, "n_alias": 1})
    empresas_df = pd.concat([empresas_df, pd.DataFrame(extra_empresas)], ignore_index=True)

    alias_final = pd.concat([
        alias_rut_df, alias_sipub.drop(columns=["score"])
    ], ignore_index=True)

    con = duckdb.connect(str(DB_PATH))
    con.execute("CREATE OR REPLACE TABLE diccionario_empresas AS SELECT * FROM empresas_df")
    con.execute("CREATE OR REPLACE TABLE diccionario_alias AS SELECT * FROM alias_final")
    con.close()
    print(f"\nGuardado en {DB_PATH}: diccionario_empresas ({len(empresas_df)} filas), diccionario_alias ({len(alias_final)} filas)")

    pendientes = alias_sipub[alias_sipub["confianza"] == "pendiente"].sort_values("nombre_original")
    medias = alias_sipub[alias_sipub["confianza"] == "media (nombre similar)"].sort_values("score", ascending=False)
    with pd.ExcelWriter(SALIDA_PENDIENTES) as writer:
        pendientes[["nombre_original", "fuente", "identificador_original"]].to_excel(
            writer, sheet_name="Sin candidato (pendiente)", index=False
        )
        medias[["nombre_original", "fuente", "empresa_id", "score"]].rename(
            columns={"empresa_id": "RUT candidato / nombre canónico calzado"}
        ).to_excel(writer, sheet_name="Candidato dudoso (revisar)", index=False)
    print(f"Exportado para revisión: {SALIDA_PENDIENTES}")
    print(f"  - {len(pendientes)} sin ningún candidato razonable")
    print(f"  - {len(medias)} con candidato pero no exacto (revisar si el calce es correcto)")


if __name__ == "__main__":
    main()
