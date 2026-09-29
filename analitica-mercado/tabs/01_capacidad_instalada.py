"""
Tab: Capacidad instalada por tecnología, estado y empresa.

Lee la vista `capacidad_central` de mercado.duckdb (armada por pipeline.py
a partir de los endpoints /capacidad-instalada y /centrales de SIPUB).
Nunca llama a la API en vivo — si pipeline.py no ha corrido todavía,
muestra un aviso en vez de fallar.
"""

import os

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mercado.duckdb")

AZUL_OSCURO = "#00629B"
CELESTE = "#16A7E5"
AMARILLO = "#FCDB00"


@st.cache_data(ttl=3600)
def cargar_datos():
    if not os.path.exists(DB_PATH):
        return None, None
    con = duckdb.connect(DB_PATH, read_only=True)
    df = con.execute("SELECT * FROM capacidad_central").fetchdf()
    ultima_corrida = con.execute(
        "SELECT corrida_utc FROM meta_actualizaciones ORDER BY corrida_utc DESC LIMIT 1"
    ).fetchone()
    con.close()
    return df, (ultima_corrida[0] if ultima_corrida else None)


def render():
    df, ultima_corrida = cargar_datos()

    if df is None:
        st.warning(
            "Todavía no hay datos locales (`mercado.duckdb` no existe). "
            "Corre `python pipeline.py` una vez (con SIPUB_API_KEY configurada) "
            "o dispara el workflow 'Actualizar Capacidad Instalada' manualmente "
            "desde la pestaña Actions de GitHub."
        )
        return

    if ultima_corrida is not None:
        st.caption(f"Datos actualizados desde SIPUB el {ultima_corrida:%d-%m-%Y %H:%M} UTC")

    # --- Filtros ---
    col_f1, col_f2, col_f3 = st.columns(3)
    estados_disp = sorted(e for e in df["estado"].dropna().unique() if e)
    tecnologias_disp = sorted(t for t in df["tipo_tecnologia"].dropna().unique() if t)
    regiones_disp = sorted(r for r in df["region"].dropna().unique() if r)

    with col_f1:
        estados_sel = st.multiselect("Estado", estados_disp, default=estados_disp)
    with col_f2:
        tecnologias_sel = st.multiselect("Tecnología", tecnologias_disp, default=tecnologias_disp)
    with col_f3:
        regiones_sel = st.multiselect("Región", regiones_disp, default=regiones_disp)

    df_f = df[
        df["estado"].isin(estados_sel)
        & df["tipo_tecnologia"].isin(tecnologias_sel)
        & df["region"].isin(regiones_sel)
    ].copy()

    # --- KPIs ---
    mw_total = df_f["capacidad_mw_centrales"].sum()
    n_centrales = df_f["id_central"].nunique()
    n_empresas = df_f["propietario"].nunique()
    mw_operativa = df_f.loc[df_f["estado"] == "Operativa", "capacidad_mw_centrales"].sum()

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Capacidad total (filtro actual)", f"{mw_total:,.0f} MW")
    k2.metric("De las cuales operativas", f"{mw_operativa:,.0f} MW")
    k3.metric("Centrales", f"{n_centrales:,}")
    k4.metric("Empresas propietarias", f"{n_empresas:,}")

    st.divider()

    # --- Mix tecnológico y por estado ---
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Capacidad por tecnología")
        por_tec = (
            df_f.groupby("tipo_tecnologia", as_index=False)["capacidad_mw_centrales"]
            .sum()
            .sort_values("capacidad_mw_centrales", ascending=False)
        )
        fig = px.bar(
            por_tec, x="capacidad_mw_centrales", y="tipo_tecnologia", orientation="h",
            labels={"capacidad_mw_centrales": "MW", "tipo_tecnologia": ""},
            color_discrete_sequence=[AZUL_OSCURO],
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=420)
        st.plotly_chart(fig, use_container_width=True)

    with c2:
        st.subheader("Centrales por estado")
        por_estado = df_f["estado"].replace("", "Sin informar").value_counts().reset_index()
        por_estado.columns = ["estado", "n"]
        fig = px.pie(
            por_estado, names="estado", values="n", hole=0.45,
            color_discrete_sequence=[AZUL_OSCURO, CELESTE, AMARILLO, "#B0BEC5", "#78909C"],
        )
        fig.update_layout(height=420)
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # --- Ranking de empresas ---
    st.subheader("Ranking de empresas por capacidad instalada")
    por_empresa = (
        df_f.groupby("propietario", as_index=False)
        .agg(mw=("capacidad_mw_centrales", "sum"), n_centrales=("id_central", "nunique"))
        .sort_values("mw", ascending=False)
        .head(20)
    )
    fig = px.bar(
        por_empresa, x="mw", y="propietario", orientation="h",
        labels={"mw": "MW", "propietario": ""},
        color_discrete_sequence=[AZUL_OSCURO],
    )
    fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=560)
    st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # --- Mix tecnológico por empresa ---
    st.subheader("Mix tecnológico por empresa")
    st.caption("% de la capacidad de cada empresa que corresponde a cada tecnología (dentro del filtro actual).")
    top_n_mix = st.slider("Empresas a mostrar (ordenadas por capacidad total)", 5, 40, 15, key="mix_top_n")
    empresas_top_mix = (
        df_f.groupby("propietario")["capacidad_mw_centrales"].sum()
        .sort_values(ascending=False).head(top_n_mix).index
    )
    df_mix = df_f[df_f["propietario"].isin(empresas_top_mix)].copy()
    if not df_mix.empty:
        mix = (
            df_mix.groupby(["propietario", "tipo_tecnologia"], as_index=False)["capacidad_mw_centrales"].sum()
        )
        mix["pct"] = mix.groupby("propietario")["capacidad_mw_centrales"].transform(lambda s: 100 * s / s.sum())
        orden_empresas = list(empresas_top_mix)[::-1]
        fig = px.bar(
            mix, x="pct", y="propietario", color="tipo_tecnologia", orientation="h",
            labels={"pct": "% de la capacidad de la empresa", "propietario": "", "tipo_tecnologia": "Tecnología"},
            category_orders={"propietario": orden_empresas},
            custom_data=["tipo_tecnologia", "capacidad_mw_centrales"],
        )
        fig.update_traces(
            hovertemplate="%{customdata[0]}<br>%{x:.1f}% · %{customdata[1]:,.0f} MW<extra></extra>"
        )
        fig.update_layout(
            barmode="stack", height=max(380, 28 * top_n_mix), xaxis_ticksuffix="%",
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No hay datos para armar el mix tecnológico con el filtro actual.")

    st.divider()

    # --- Tabla de detalle ---
    st.subheader("Detalle por central")
    st.dataframe(
        df_f[[
            "nombre_central", "propietario", "estado", "tipo_tecnologia", "conv_ernc",
            "capacidad_mw_centrales", "region", "comuna", "fecha_ent_oper",
        ]].sort_values("capacidad_mw_centrales", ascending=False),
        use_container_width=True,
        hide_index=True,
        column_config={
            "nombre_central": "Central",
            "propietario": "Propietario",
            "estado": "Estado",
            "tipo_tecnologia": "Tecnología",
            "conv_ernc": "Conv./ERNC",
            "capacidad_mw_centrales": st.column_config.NumberColumn("MW", format="%.2f"),
            "region": "Región",
            "comuna": "Comuna",
            "fecha_ent_oper": "Entrada en operación",
        },
    )
