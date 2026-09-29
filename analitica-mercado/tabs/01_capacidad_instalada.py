"""
Tab: Capacidad instalada por tecnología, estado y empresa.

Lee la vista `capacidad_central` de mercado.duckdb (armada por pipeline.py
a partir de los endpoints /capacidad-instalada y /centrales de SIPUB).
Nunca llama a la API en vivo — si pipeline.py no ha corrido todavía,
muestra un aviso en vez de fallar.

Si además existe `empresas_infotecnica_raw` (armada por
pipeline_contratos.py a partir de infotecnica/empresas, API v2), se cruza
por nombre de empresa para agrupar por "holding" -- el grupo económico de
SIPUB (campo `grupo`), que agrupa filiales y SPAs de proyecto individuales
bajo un mismo dueño real (ver notas en pipeline_contratos.py sobre por qué
`grupo` es la unidad correcta y no `numero`). Si esa tabla no existe, el
agrupamiento por holding simplemente no está disponible y todo sigue
funcionando por empresa individual.

PMGD (Pequeños Medios de Generación Distribuida) no viene marcado como tal
en la API -- se aproxima por la definición regulatoria (potencia ≤ 9 MW),
que es exactamente el corte que se observa en los datos: todas las
centrales cuyo nombre empieza con "PMGD" tienen 9.0 MW como máximo.
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
VERDE = "#43A047"

PMGD_MW_MAX = 9.0  # umbral regulatorio (Ley 20.571 / NT de Conexión y Operación PMGD)


def _norm(nombre: str) -> str:
    if not nombre:
        return ""
    return " ".join(str(nombre).strip().upper().split())


@st.cache_data(ttl=3600)
def cargar_datos():
    if not os.path.exists(DB_PATH):
        return None, None
    con = duckdb.connect(DB_PATH, read_only=True)
    df = con.execute("SELECT * FROM capacidad_central").fetchdf()
    ultima_corrida = con.execute(
        "SELECT corrida_utc FROM meta_actualizaciones ORDER BY corrida_utc DESC LIMIT 1"
    ).fetchone()

    tablas = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    df["holding"] = df["propietario"]
    if "empresas_infotecnica_raw" in tablas:
        emp = con.execute(
            "SELECT nombre, grupo FROM empresas_infotecnica_raw WHERE mnemotecnico LIKE 'G%'"
        ).fetchdf()
        emp["empresa_norm"] = emp["nombre"].map(_norm)
        emp = emp.dropna(subset=["grupo"]).drop_duplicates("empresa_norm")

        df["empresa_norm"] = df["propietario"].map(_norm)
        df = df.merge(emp[["empresa_norm", "grupo"]], on="empresa_norm", how="left")

        # Nombre del holding = la empresa con más MW dentro de su mismo grupo
        # económico (ej. grupo de Colbún -> "COLBÚN S.A.", aunque incluya
        # también a Río Tranquilo S.A.). Sin grupo asignado, el holding es
        # la empresa misma.
        con_grupo = df.dropna(subset=["grupo"])
        if not con_grupo.empty:
            lider_por_grupo = (
                con_grupo.groupby(["grupo", "propietario"])["capacidad_mw_centrales"].sum()
                .reset_index()
                .sort_values("capacidad_mw_centrales", ascending=False)
                .drop_duplicates("grupo")
                .set_index("grupo")["propietario"]
            )
            df["holding"] = df["grupo"].map(lider_por_grupo).fillna(df["propietario"])
        df = df.drop(columns=["empresa_norm", "grupo"], errors="ignore")

    df["es_pmgd"] = df["capacidad_mw_centrales"] <= PMGD_MW_MAX
    con.close()
    return df, (ultima_corrida[0] if ultima_corrida else None)


def _grafico_mix(df_in: pd.DataFrame, col_empresa: str, top_n: int, key_suffix: str):
    empresas_top = (
        df_in.groupby(col_empresa)["capacidad_mw_centrales"].sum()
        .sort_values(ascending=False).head(top_n).index
    )
    df_mix = df_in[df_in[col_empresa].isin(empresas_top)].copy()
    if df_mix.empty:
        st.info("No hay datos para armar el mix tecnológico con el filtro actual.")
        return
    mix = df_mix.groupby([col_empresa, "tipo_tecnologia"], as_index=False)["capacidad_mw_centrales"].sum()
    mix["pct"] = mix.groupby(col_empresa)["capacidad_mw_centrales"].transform(lambda s: 100 * s / s.sum())
    orden = list(empresas_top)[::-1]
    fig = px.bar(
        mix, x="pct", y=col_empresa, color="tipo_tecnologia", orientation="h",
        labels={"pct": "% de la capacidad", col_empresa: "", "tipo_tecnologia": "Tecnología"},
        category_orders={col_empresa: orden},
        custom_data=["tipo_tecnologia", "capacidad_mw_centrales"],
    )
    fig.update_traces(hovertemplate="%{customdata[0]}<br>%{x:.1f}% · %{customdata[1]:,.0f} MW<extra></extra>")
    fig.update_layout(
        barmode="stack", height=max(380, 28 * len(empresas_top)), xaxis_ticksuffix="%",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig, use_container_width=True, key=f"mix_{key_suffix}")


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

    hay_holding = not df["holding"].equals(df["propietario"])

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

    # --- Ranking y mix tecnológico por empresa / holding ---
    if hay_holding:
        agrupar_holding = st.toggle(
            "Agrupar por holding (grupo económico)", value=True,
            help=(
                "Suma filiales y SPAs de proyecto bajo el mismo dueño real "
                "(ej. Colbún S.A. + Río Tranquilo S.A.), usando el grupo económico "
                "de infotecnica/empresas en vez de la razón social de cada central."
            ),
        )
        col_empresa = "holding" if agrupar_holding else "propietario"
        etiqueta = "holding" if agrupar_holding else "empresa"
    else:
        col_empresa = "propietario"
        etiqueta = "empresa"

    st.subheader(f"Ranking de {etiqueta}s por capacidad instalada")
    por_empresa = (
        df_f.groupby(col_empresa, as_index=False)
        .agg(mw=("capacidad_mw_centrales", "sum"), n_centrales=("id_central", "nunique"))
        .sort_values("mw", ascending=False)
        .head(20)
    )
    fig = px.bar(
        por_empresa, x="mw", y=col_empresa, orientation="h",
        labels={"mw": "MW", col_empresa: ""},
        color_discrete_sequence=[AZUL_OSCURO],
    )
    fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=560)
    st.plotly_chart(fig, use_container_width=True)

    st.divider()

    st.subheader(f"Mix tecnológico por {etiqueta}")
    st.caption(f"% de la capacidad de cada {etiqueta} que corresponde a cada tecnología (dentro del filtro actual).")
    top_n_mix = st.slider(
        f"{etiqueta.capitalize()}s a mostrar (ordenados por capacidad total)", 5, 40, 15, key="mix_top_n"
    )
    _grafico_mix(df_f, col_empresa, top_n_mix, key_suffix="empresa")

    st.divider()

    # --- PMGD ---
    st.subheader("PMGD — Pequeños Medios de Generación Distribuida")
    st.caption(
        "Aproximado por el umbral regulatorio (≤ 9 MW por central); SIPUB no trae una marca "
        "explícita de PMGD, pero el corte calza con las centrales nombradas 'PMGD ...'."
    )
    df_pmgd = df_f[df_f["es_pmgd"]].copy()
    if df_pmgd.empty:
        st.info("No hay centrales PMGD dentro del filtro actual.")
    else:
        mw_pmgd = df_pmgd["capacidad_mw_centrales"].sum()
        pct_del_total = 100 * mw_pmgd / mw_total if mw_total else 0
        p1, p2, p3 = st.columns(3)
        p1.metric("Capacidad PMGD", f"{mw_pmgd:,.0f} MW", f"{pct_del_total:.1f}% del total filtrado")
        p2.metric("Centrales PMGD", f"{df_pmgd['id_central'].nunique():,}")
        p3.metric(f"{etiqueta.capitalize()}s con PMGD", f"{df_pmgd[col_empresa].nunique():,}")

        cpm1, cpm2 = st.columns(2)
        with cpm1:
            st.markdown("###### Mix tecnológico dentro de PMGD")
            por_tec_pmgd = (
                df_pmgd.groupby("tipo_tecnologia", as_index=False)["capacidad_mw_centrales"]
                .sum().sort_values("capacidad_mw_centrales", ascending=False)
            )
            fig = px.bar(
                por_tec_pmgd, x="capacidad_mw_centrales", y="tipo_tecnologia", orientation="h",
                labels={"capacidad_mw_centrales": "MW", "tipo_tecnologia": ""},
                color_discrete_sequence=[VERDE],
            )
            fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=340)
            st.plotly_chart(fig, use_container_width=True)
        with cpm2:
            st.markdown(f"###### Top {etiqueta}s con más capacidad PMGD")
            top_pmgd = (
                df_pmgd.groupby(col_empresa, as_index=False)["capacidad_mw_centrales"].sum()
                .sort_values("capacidad_mw_centrales", ascending=False).head(10)
            )
            fig = px.bar(
                top_pmgd, x="capacidad_mw_centrales", y=col_empresa, orientation="h",
                labels={"capacidad_mw_centrales": "MW", col_empresa: ""},
                color_discrete_sequence=[VERDE],
            )
            fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=340)
            st.plotly_chart(fig, use_container_width=True)

        with st.expander(f"Ver las {len(df_pmgd):,} centrales PMGD del filtro actual"):
            st.dataframe(
                df_pmgd[["nombre_central", col_empresa, "tipo_tecnologia", "capacidad_mw_centrales", "region", "comuna"]]
                .sort_values("capacidad_mw_centrales", ascending=False),
                use_container_width=True, hide_index=True,
                column_config={
                    "nombre_central": "Central",
                    col_empresa: etiqueta.capitalize(),
                    "tipo_tecnologia": "Tecnología",
                    "capacidad_mw_centrales": st.column_config.NumberColumn("MW", format="%.2f"),
                    "region": "Región",
                    "comuna": "Comuna",
                },
            )

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
