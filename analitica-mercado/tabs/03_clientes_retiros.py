"""
Tab: Clientes por empresa, vía Balance de Energía de Plabacom.

Fuente: tabla retiros_balance (pipeline_balance.py), que carga la hoja
"Balance Físico" del Balance_AAMM{D|P}.xlsm que Fernanda descarga
manualmente de Plabacom (plabacom.coordinador.cl) -- no hay API pública
para esto, así que esta tabla se actualiza a mano, mes a mes, subiendo
el archivo al chat. No forma parte del workflow automático de GitHub
Actions.

Cada fila de retiros_balance es un punto de medida (barra + medidor) con
su propietario/empresa y un tipo:
  L    = Retiro Libre: cliente libre identificado individualmente
  L_D  = Retiro Libre vía Distribución
  L_S  = Retiro Libre, servicios auxiliares/propios
  R    = Retiro Regulado: agregado por distribuidora/zona, SIN cliente
         individual (así reporta el Coordinador a los clientes regulados)
  G / G_SAE = Inyección de generación (producción propia, no un cliente)
  C_FIS / C_FIN = Compraventa física/financiera ENTRE GENERADORAS -- esto
         es trading de energía entre pares de mercado, no una relación
         con un cliente final (confirmado en conversación 2026-09-29)
  T    = Transmisión
  N    = Neteo

Por eso la vista de "clientes" solo lista L, L_D y L_S (únicos tipos con
nombre de cliente reconocible); R se muestra solo agregado en el % de
composición, sin desglose de clientes.

Cruce con contratos: el nombre del punto de medida (nombre_medidor, p.ej.
"MIN_COLLAHUASI") casi nunca es igual al nombre legal del cliente en
contratos_con_nombres (p.ej. "COMPAÑÍA MINERA DOÑA INÉS DE COLLAHUASI
SCM"), así que el cruce es por coincidencia aproximada de texto (tokens
compartidos) y se marca explícitamente como "posible" -- no es una llave
exacta como el RUT, así que puede haber falsos negativos (contratos que
sí existen pero no calzan por nombre) y ocasionalmente falsos positivos.
"""

import os
import re
import unicodedata
from datetime import date, datetime

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mercado.duckdb")

AZUL_OSCURO = "#00629B"
CELESTE = "#16A7E5"
AMARILLO = "#FCDB00"
VERDE = "#43A047"
ROJO = "#E53935"
GRIS = "#9E9E9E"

TIPOS_CLIENTE = ["L", "L_D", "L_S"]
TIPOS_RETIRO = ["L", "L_D", "L_S", "R"]
ETIQUETAS_TIPO = {
    "L": "Libre",
    "L_D": "Libre vía Distribución",
    "L_S": "Libre (servicios propios)",
    "R": "Regulado",
}

STOPWORDS_RAZON_SOCIAL = {
    "SA", "S.A.", "SPA", "LTDA", "LIMITADA", "SOCIEDAD", "COMPANIA",
    "COMPAÑIA", "CONTRACTUAL", "MINERA", "CIA", "DE", "DEL", "LA", "EL",
    "LOS", "LAS", "Y", "S", "A", "EIRL",
}


def _normalizar(texto: str) -> set:
    if not texto:
        return set()
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9 ]", " ", t).upper()
    tokens = {tok for tok in t.split() if tok not in STOPWORDS_RAZON_SOCIAL and len(tok) > 2}
    return tokens


def _normalizar_rut(rut) -> str:
    """91.081.000-6 -> 91081000-6 (sin puntos, sin espacios). Ambas fuentes
    traen el RUT en formatos distintos (Balance con puntos, Contratos sin
    puntos); normalizado así, comparten formato y es una llave mucho más
    confiable que el nombre de la empresa."""
    if not rut or str(rut).strip().lower() in ("nan", "none", ""):
        return ""
    return str(rut).strip().replace(".", "").replace(" ", "").upper()


@st.cache_data(ttl=3600)
def cargar_datos():
    con = duckdb.connect(DB_PATH, read_only=True)
    retiros = con.execute("SELECT * FROM retiros_balance").df()
    try:
        contratos = con.execute("""
            SELECT suministrador_nombre, rut_suministrador, cliente_nombre, fecha_inicio,
                   fecha_termino, energia_contratada, tipo, estado_contrato
            FROM contratos_con_nombres
        """).df()
    except duckdb.CatalogException:
        contratos = pd.DataFrame()
    periodos = con.execute("""
        SELECT DISTINCT periodo, tipo_balance FROM retiros_balance ORDER BY periodo DESC, tipo_balance
    """).df()
    con.close()
    return retiros, contratos, periodos


def _preparar_matching(contratos: pd.DataFrame):
    contratos = contratos.copy()
    contratos["_rut_suministrador_norm"] = contratos["rut_suministrador"].apply(_normalizar_rut)
    contratos["_tokens_cliente"] = contratos["cliente_nombre"].apply(_normalizar)
    return contratos


def _buscar_contrato(nombre_medidor: str, empresa_tokens: set, contratos_prep: pd.DataFrame, rut_empresa: str = ""):
    """Busca, dentro de los contratos de la MISMA empresa suministradora,
    el cliente cuyo nombre comparta más tokens con nombre_medidor.

    La empresa suministradora se identifica primero por RUT (llave
    confiable, disponible desde que se reemplazó el pipeline de contratos
    por el export de la Plataforma de Contratos); si no hay RUT en
    Balance para ese punto, se cae a coincidencia de nombre como respaldo.
    Devuelve None si no hay ningún cliente con al menos 1 token en común."""
    if contratos_prep.empty:
        return None
    tokens_medidor = _normalizar(nombre_medidor)
    if not tokens_medidor:
        return None

    rut_norm = _normalizar_rut(rut_empresa)
    if rut_norm:
        candidatos = contratos_prep[contratos_prep["_rut_suministrador_norm"] == rut_norm]
    else:
        candidatos = pd.DataFrame()

    if candidatos.empty:
        # Respaldo: sin RUT (o RUT sin match), se intenta por nombre.
        candidatos = contratos_prep[
            contratos_prep["suministrador_nombre"].apply(lambda n: len(_normalizar(n) & empresa_tokens) > 0)
        ]
    if candidatos.empty:
        return None

    mejor = None
    mejor_score = 0
    for _, fila in candidatos.iterrows():
        score = len(fila["_tokens_cliente"] & tokens_medidor)
        if score > mejor_score:
            mejor_score = score
            mejor = fila
    if mejor is None or mejor_score == 0:
        return None
    return mejor


def render():
    st.subheader("🔌 Clientes por Empresa (Balance de Energía Plabacom)")
    st.caption(
        "Fuente: Balance de Energía descargado manualmente de Plabacom (no automatizado). "
        "Cruce con contratos por coincidencia aproximada de nombre — revisar antes de usar como dato definitivo."
    )

    retiros, contratos, periodos = cargar_datos()

    if retiros.empty:
        st.info(
            "Todavía no se ha cargado ningún Balance de Energía. "
            "Sube el archivo Balance_AAMM{D|P}.xlsm descargado de Plabacom y se procesa con pipeline_balance.py."
        )
        return

    # --- Selector de período ---
    periodos["etiqueta"] = periodos["periodo"] + " " + periodos["tipo_balance"].map({"D": "(Definitivo)", "P": "(Preliminar)"})
    opciones_periodo = periodos["etiqueta"].tolist()
    periodo_sel_etq = st.selectbox("Período", opciones_periodo, index=0)
    fila_periodo = periodos[periodos["etiqueta"] == periodo_sel_etq].iloc[0]
    df = retiros[
        (retiros["periodo"] == fila_periodo["periodo"]) & (retiros["tipo_balance"] == fila_periodo["tipo_balance"])
    ].copy()

    # --- Selector de empresa (solo empresas con al menos un tipo de retiro) ---
    empresas_retiro = (
        df[df["tipo_medidor"].isin(TIPOS_RETIRO)]
        .groupby("propietario_medidor")["energia_kwh_abs"].sum()
        .sort_values(ascending=False)
    )
    empresas_retiro = empresas_retiro[empresas_retiro.index.notna() & (empresas_retiro.index != "None")]
    if empresas_retiro.empty:
        st.warning("No hay filas de tipo retiro (L, L_D, L_S, R) en este período.")
        return

    empresa_sel = st.selectbox(
        "Empresa generadora / suministradora",
        empresas_retiro.index.tolist(),
        format_func=lambda e: f"{e}  —  {empresas_retiro[e]/1000:,.0f} MWh retirados",
    )

    df_emp = df[df["propietario_medidor"] == empresa_sel]

    # --- % de composición de retiros ---
    st.markdown("#### Composición de retiros")
    comp = (
        df_emp[df_emp["tipo_medidor"].isin(TIPOS_RETIRO)]
        .groupby("tipo_medidor")["energia_kwh_abs"].sum()
        .reindex(TIPOS_RETIRO)
        .fillna(0)
    )
    total_retiro = comp.sum()
    if total_retiro > 0:
        col1, col2 = st.columns([1, 1])
        with col1:
            comp_df = pd.DataFrame({
                "Tipo": [ETIQUETAS_TIPO[t] for t in comp.index],
                "Energía (MWh)": (comp.values / 1000).round(1),
                "%": (comp.values / total_retiro * 100).round(1),
            })
            st.dataframe(comp_df, hide_index=True, use_container_width=True)
        with col2:
            fig = px.pie(
                comp_df[comp_df["Energía (MWh)"] > 0], names="Tipo", values="Energía (MWh)",
                hole=0.5, color_discrete_sequence=[AZUL_OSCURO, CELESTE, AMARILLO, GRIS],
            )
            fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=280)
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Esta empresa no tiene retiros (L/L_D/L_S/R) en este período; probablemente solo inyecta generación o transa (C_FIS/C_FIN).")

    # --- Listado de clientes (solo L, L_D, L_S -- tienen nombre individual) ---
    st.markdown("#### Clientes (retiro libre)")
    df_clientes = (
        df_emp[df_emp["tipo_medidor"].isin(TIPOS_CLIENTE)]
        .groupby(["nombre_medidor", "barra", "tipo_medidor"], as_index=False)["energia_kwh_abs"].sum()
        .sort_values("energia_kwh_abs", ascending=False)
    )

    if df_clientes.empty:
        st.info("Esta empresa no tiene clientes libres identificados individualmente en este período (solo Regulado, o solo trading/inyección).")
    else:
        contratos_prep = _preparar_matching(contratos) if not contratos.empty else contratos
        empresa_tokens = _normalizar(empresa_sel)
        rut_empresa = df_emp["rut"].dropna().iloc[0] if df_emp["rut"].notna().any() else ""

        estados, f_inicio, f_termino, tipo_contrato = [], [], [], []
        for _, fila in df_clientes.iterrows():
            match = (
                _buscar_contrato(fila["nombre_medidor"], empresa_tokens, contratos_prep, rut_empresa)
                if not contratos_prep.empty else None
            )
            if match is None:
                estados.append("Sin contrato identificado")
                f_inicio.append(None)
                f_termino.append(None)
                tipo_contrato.append(None)
            else:
                estado_real = match.get("estado_contrato")
                if estado_real in ("Vigente", "No vigente"):
                    estados.append(f"{estado_real} (match posible)")
                else:
                    termino = match["fecha_termino"]
                    try:
                        vigente = termino is None or pd.isna(termino) or datetime.strptime(str(termino)[:10], "%Y-%m-%d").date() >= date.today()
                    except ValueError:
                        vigente = True
                    estados.append("Contrato vigente (posible)" if vigente else "Contrato vencido (posible)")
                f_inicio.append(match["fecha_inicio"])
                f_termino.append(match["fecha_termino"])
                tipo_contrato.append(match["tipo"])

        df_clientes["Energía (MWh)"] = (df_clientes["energia_kwh_abs"] / 1000).round(1)
        df_clientes["Tipo retiro"] = df_clientes["tipo_medidor"].map(ETIQUETAS_TIPO)
        df_clientes["Estado contrato"] = estados
        df_clientes["Inicio (posible)"] = f_inicio
        df_clientes["Término (posible)"] = f_termino

        st.caption(
            f"{len(df_clientes)} puntos de retiro libre. El cruce con contratos es por nombre aproximado "
            "(no hay RUT en común entre ambas fuentes) -- \"posible\" significa que el nombre calzó, no que esté verificado."
        )
        st.dataframe(
            df_clientes[[
                "nombre_medidor", "barra", "Tipo retiro", "Energía (MWh)",
                "Estado contrato", "Inicio (posible)", "Término (posible)",
            ]].rename(columns={"nombre_medidor": "Cliente / punto de retiro", "barra": "Barra"}),
            hide_index=True, use_container_width=True, height=min(35 * (len(df_clientes) + 1), 600),
        )

        n_con_contrato = sum(1 for e in estados if e != "Sin contrato identificado")
        st.caption(f"{n_con_contrato} de {len(df_clientes)} puntos calzaron con algún contrato en SIPUB (por nombre).")
