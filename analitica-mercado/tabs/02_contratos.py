"""
Tab: Contratos de suministro y evolución por empresa.

Cruza capacidad_central (capacidad instalada, de pipeline.py) con
contratos_con_nombres (contratos de suministro vigentes, de
pipeline_contratos.py) para ver, por empresa generadora: cuánta
capacidad tiene, a quién le vende, cuánta energía/potencia contratada
tiene comprometida por año, y cuándo empiezan a vencer esos contratos.

El cruce entre ambas fuentes es por nombre de empresa (texto), porque
capacidad_central viene de /centrales (v4) con el nombre completo del
propietario, y contratos_con_nombres viene de infotecnica (v2) con su
propio nombre — no comparten un ID común. Se normaliza a mayúsculas y
sin espacios extra antes de cruzar; empresas que no calcen quedan sin
capacidad asociada (se avisa en la UI en vez de fallar en silencio).

PMGD se aproxima igual que en la pestaña de Capacidad Instalada: una
empresa se considera "PMGD" si TODAS sus centrales operativas tienen
9 MW o menos (umbral regulatorio). La mayoría de las PMGD venden a
precio estabilizado y no aparecen con contratos bilaterales -- la
sección de PMGD de esta pestaña puede salir corta, y eso es esperable,
no un error.
"""

import os

import duckdb
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mercado.duckdb")

AZUL_OSCURO = "#00629B"
CELESTE = "#16A7E5"
AMARILLO = "#FCDB00"
VERDE = "#43A047"
ROJO = "#E53935"

TIPO_LABELS = {"R": "Cliente regulado", "L": "Cliente libre", "C": "Entre generadores"}
PMGD_MW_MAX = 9.0


def _norm(nombre: str) -> str:
    if not nombre:
        return ""
    return " ".join(str(nombre).strip().upper().split())


@st.cache_data(ttl=3600)
def cargar_datos():
    con = duckdb.connect(DB_PATH, read_only=True)
    tablas = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if "contratos_con_nombres" not in tablas:
        con.close()
        return None

    contratos = con.execute("SELECT * FROM contratos_con_nombres").fetchdf()

    capacidad = pd.DataFrame()
    if "capacidad_central" in tablas:
        capacidad = con.execute("""
            SELECT propietario, estado, capacidad_mw_centrales
            FROM capacidad_central
        """).fetchdf()

    ultima = None
    if "meta_actualizaciones_contratos" in tablas:
        ultima = con.execute("SELECT MAX(corrida_utc) FROM meta_actualizaciones_contratos").fetchone()[0]
    con.close()
    return contratos, capacidad, ultima


def render():
    st.subheader("Contratos de suministro y evolución por empresa")
    st.caption(
        "Fuente: /api/v2/recursos/contratos_de_suministro_vigentes (Coordinador, SIPUB) — "
        "una fila por contrato **y año de vigencia**, cruzada con capacidad instalada."
    )

    datos = cargar_datos()
    if datos is None:
        st.warning(
            "Todavía no existe `contratos_con_nombres` en la base de datos. "
            "Corre `python pipeline_contratos.py` (o el workflow correspondiente) para generarla."
        )
        return

    contratos, capacidad, ultima = datos
    if contratos.empty:
        st.warning("La tabla de contratos está vacía.")
        return

    if ultima is not None:
        st.caption(f"Datos actualizados desde SIPUB el {ultima}")

    # Capacidad por empresa (nombre normalizado -> MW), toda y solo operativa,
    # y si toda su capacidad operativa es PMGD (<= 9 MW por central).
    cap_por_empresa = pd.Series(dtype=float)
    cap_por_empresa_operativa = pd.Series(dtype=float)
    es_pmgd_empresa = pd.Series(dtype=bool)
    nombre_original = {}
    if not capacidad.empty:
        cap = capacidad.copy()
        cap["empresa_norm"] = cap["propietario"].map(_norm)
        cap_por_empresa = cap.groupby("empresa_norm")["capacidad_mw_centrales"].sum()
        cap_op = cap[cap["estado"].str.contains("Operativ", case=False, na=False)]
        cap_por_empresa_operativa = cap_op.groupby("empresa_norm")["capacidad_mw_centrales"].sum()
        es_pmgd_empresa = cap_op.groupby("empresa_norm")["capacidad_mw_centrales"].max() <= PMGD_MW_MAX
        # nombre "bonito" (tal como aparece en capacidad_central) por cada norm,
        # para mostrar en el selector el mismo texto que en la pestaña de Capacidad.
        nombre_original = cap.drop_duplicates("empresa_norm").set_index("empresa_norm")["propietario"].to_dict()

    contratos = contratos.copy()
    contratos["suministrador_norm"] = contratos["suministrador_nombre"].map(_norm)

    # ---------- selector de empresa: mismo universo que la pestaña de Capacidad Instalada ----------
    if cap_por_empresa.empty:
        st.warning("No hay datos de capacidad instalada cargados todavía para armar el listado de empresas.")
        return

    empresas_norm = cap_por_empresa.sort_values(ascending=False).index.tolist()
    empresas = [nombre_original.get(n, n) for n in empresas_norm]

    empresa_sel = st.selectbox("Empresa generadora", empresas)
    empresa_sel_norm = _norm(empresa_sel)
    df_emp = contratos[contratos["suministrador_norm"] == empresa_sel_norm].copy()
    cap_mw = cap_por_empresa_operativa.get(empresa_sel_norm)

    if df_emp.empty:
        st.info(f"**{empresa_sel}** no tiene contratos de suministro vigentes registrados en SIPUB.")
        if pd.notna(cap_mw):
            st.metric("Capacidad operativa instalada", f"{cap_mw:,.0f} MW")
    else:
        _render_detalle_empresa(df_emp, empresa_sel, cap_mw)

    st.divider()

    # ---------- ranking global de suministradores ----------
    st.markdown("#### Ranking de suministradores por energía contratada (último año disponible)")
    anio_global = int(contratos["año"].max()) if contratos["año"].notna().any() else None
    if anio_global:
        rank = (
            contratos[contratos["año"] == anio_global]
            .groupby("suministrador_nombre")["energia_contratada"].sum()
            .sort_values(ascending=False).head(15).reset_index()
        )
        fig3 = go.Figure(go.Bar(
            x=rank["energia_contratada"], y=rank["suministrador_nombre"],
            orientation="h", marker_color=CELESTE,
        ))
        fig3.update_layout(
            height=420, margin=dict(l=10, r=10, t=10, b=10),
            xaxis_title=f"Energía contratada {anio_global} (GWh/año)", yaxis=dict(autorange="reversed"),
        )
        st.plotly_chart(fig3, use_container_width=True)

    st.divider()

    _render_seccion_pmgd(contratos, es_pmgd_empresa)


def _render_detalle_empresa(df_emp: pd.DataFrame, empresa_sel: str, cap_mw):
    # ---------- KPIs ----------
    anio_max = int(df_emp["año"].max()) if df_emp["año"].notna().any() else None
    df_anio_actual = df_emp[df_emp["año"] == anio_max] if anio_max else df_emp.iloc[0:0]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric(
        "Capacidad operativa instalada",
        f"{cap_mw:,.0f} MW" if pd.notna(cap_mw) else "sin cruce",
    )
    col2.metric(
        f"Energía contratada ({anio_max})" if anio_max else "Energía contratada",
        f"{df_anio_actual['energia_contratada'].sum():,.1f} GWh/año" if not df_anio_actual.empty else "—",
    )
    col3.metric(
        f"Potencia contratada ({anio_max})" if anio_max else "Potencia contratada",
        f"{df_anio_actual['potencia_contratada'].sum():,.1f} MW" if not df_anio_actual.empty else "—",
    )
    col4.metric("Clientes distintos", f"{df_emp['cliente_nombre'].nunique():,}")

    if pd.isna(cap_mw):
        st.caption("⚠️ Esta empresa no tiene centrales en estado Operativa registradas en `capacidad_central`.")

    # ---------- evolución en el tiempo ----------
    st.markdown("#### Evolución de energía y potencia contratada")
    por_anio = (
        df_emp.dropna(subset=["año"])
        .groupby("año")
        .agg(energia_gwh=("energia_contratada", "sum"), potencia_mw=("potencia_contratada", "sum"))
        .reset_index()
        .sort_values("año")
    )
    if not por_anio.empty:
        fig = go.Figure()
        fig.add_bar(x=por_anio["año"], y=por_anio["energia_gwh"], name="Energía contratada (GWh/año)", marker_color=AZUL_OSCURO)
        fig.add_trace(go.Scatter(
            x=por_anio["año"], y=por_anio["potencia_mw"], name="Potencia contratada (MW)",
            yaxis="y2", mode="lines+markers", line=dict(color=AMARILLO, width=3),
        ))
        if pd.notna(cap_mw):
            fig.add_hline(y=cap_mw, line_dash="dot", line_color=VERDE, yref="y2",
                           annotation_text=f"Capacidad instalada ({cap_mw:,.0f} MW)", annotation_position="top left")
        fig.update_layout(
            height=380, margin=dict(l=10, r=10, t=30, b=10),
            xaxis_title="Año", yaxis_title="Energía (GWh/año)",
            yaxis2=dict(title="Potencia (MW)", overlaying="y", side="right"),
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Esta empresa no tiene filas con año identificado.")

    # ---------- vencimientos ----------
    st.markdown("#### Vencimiento de contratos vigentes")
    vig = df_emp.dropna(subset=["fecha_termino"]).drop_duplicates(
        subset=["cliente_nombre", "fecha_inicio", "fecha_termino", "tipo"]
    ).copy()
    if not vig.empty:
        vig["fecha_termino_dt"] = pd.to_datetime(vig["fecha_termino"], errors="coerce")
        vig["año_termino"] = vig["fecha_termino_dt"].dt.year
        por_venc = vig.groupby("año_termino").agg(
            n_contratos=("cliente_nombre", "count"),
            energia_gwh=("energia_contratada", "sum"),
        ).reset_index().sort_values("año_termino")
        fig2 = go.Figure()
        fig2.add_bar(x=por_venc["año_termino"], y=por_venc["n_contratos"], marker_color=ROJO, name="Contratos que vencen")
        fig2.update_layout(
            height=280, margin=dict(l=10, r=10, t=20, b=10),
            xaxis_title="Año de término", yaxis_title="N° de contratos",
        )
        st.plotly_chart(fig2, use_container_width=True)

        st.dataframe(
            vig.sort_values("fecha_termino_dt")[
                ["cliente_nombre", "tipo", "fecha_inicio", "fecha_termino", "energia_contratada", "potencia_contratada"]
            ],
            hide_index=True,
            use_container_width=True,
            column_config={
                "cliente_nombre": "Cliente",
                "tipo": st.column_config.TextColumn("Tipo", help="R: regulado · L: libre · C: entre generadores"),
                "fecha_inicio": "Inicio",
                "fecha_termino": "Término",
                "energia_contratada": st.column_config.NumberColumn("Energía (GWh/año)", format="%.1f"),
                "potencia_contratada": st.column_config.NumberColumn("Potencia (MW)", format="%.1f"),
            },
        )
    else:
        st.info("No hay fechas de término registradas para esta empresa.")


def _render_seccion_pmgd(contratos: pd.DataFrame, es_pmgd_empresa: pd.Series):
    st.markdown("#### PMGD — contratos de suministradores clasificados como PMGD")
    st.caption(
        "Empresas cuyas centrales operativas tienen todas 9 MW o menos (umbral regulatorio). "
        "La mayoría de las PMGD venden a precio estabilizado y no figuran con contratos "
        "bilaterales, así que es normal que esta lista salga corta."
    )
    if es_pmgd_empresa.empty:
        st.info("No hay datos de capacidad cargados para clasificar qué suministradores son PMGD.")
        return

    pmgd_norms = set(es_pmgd_empresa[es_pmgd_empresa].index)
    df_pmgd = contratos[contratos["suministrador_norm"].isin(pmgd_norms)].copy()

    if df_pmgd.empty:
        st.info("Ningún suministrador clasificado como PMGD tiene contratos vigentes registrados en SIPUB.")
        return

    resumen = (
        df_pmgd.groupby("suministrador_nombre")
        .agg(
            n_contratos=("cliente_nombre", "count"),
            n_clientes=("cliente_nombre", "nunique"),
            energia_gwh=("energia_contratada", "sum"),
        )
        .sort_values("energia_gwh", ascending=False)
        .reset_index()
    )
    q1, q2 = st.columns(2)
    q1.metric("Suministradores PMGD con contratos", f"{resumen.shape[0]:,}")
    q2.metric("Energía contratada total (todas las filas/años)", f"{resumen['energia_gwh'].sum():,.1f} GWh/año")

    fig = px.bar(
        resumen, x="energia_gwh", y="suministrador_nombre", orientation="h",
        labels={"energia_gwh": "Energía contratada (GWh/año, todas las filas)", "suministrador_nombre": ""},
        color_discrete_sequence=[VERDE],
    )
    fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=max(240, 40 * len(resumen)))
    st.plotly_chart(fig, use_container_width=True)

    st.dataframe(
        resumen, use_container_width=True, hide_index=True,
        column_config={
            "suministrador_nombre": "Suministrador",
            "n_contratos": "N° filas de contrato",
            "n_clientes": "Clientes distintos",
            "energia_gwh": st.column_config.NumberColumn("Energía (GWh/año)", format="%.1f"),
        },
    )
