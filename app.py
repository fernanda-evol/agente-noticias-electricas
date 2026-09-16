import streamlit as st
import sqlite3
import pandas as pd
import json
from datetime import datetime, timedelta

st.set_page_config(
    page_title="Monitor Mercado Eléctrico",
    page_icon="⚡",
    layout="wide"
)

DB_NAME = "noticias_energia.db"

@st.cache_data(ttl=300)
def cargar_datos():
    try:
        conn = sqlite3.connect(DB_NAME)
        query = "SELECT * FROM noticias ORDER BY fecha_publicacion DESC"
        df = pd.read_sql_query(query, conn)
        conn.close()
        if not df.empty:
            df['fecha_dt'] = pd.to_datetime(df['fecha_publicacion'], errors='coerce')
        return df
    except Exception:
        return pd.DataFrame()

df = cargar_datos()

# Panel Lateral
st.sidebar.title("⚙️ Opciones")
if st.sidebar.button("🔄 Actualizar Datos / Limpiar Caché"):
    st.cache_data.clear()
    st.rerun()

st.sidebar.title("🔍 Filtros")

if df.empty:
    st.warning("Aún no hay noticias registradas. Ejecuta el agente para poblar la base de datos.")
else:
    # 1. Filtro por Rango de Fechas
    fechas_validas = df['fecha_dt'].dropna()
    min_date = fechas_validas.min().date() if not fechas_validas.empty else datetime.now().date() - timedelta(days=30)
    max_date = fechas_validas.max().date() if not fechas_validas.empty else datetime.now().date()

    rango_fechas = st.sidebar.date_input(
        "📅 Rango de Fechas",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date
    )

    # 2. Filtros Dinámicos
    fuentes_disponibles = sorted(list(df['fuente'].unique()))
    fuentes_sel = st.sidebar.multiselect("Fuente de Noticias", fuentes_disponibles, default=fuentes_disponibles)

    cat_disponibles = sorted(list(df['categoria'].unique()))
    cat_sel = st.sidebar.multiselect("Categoría", cat_disponibles, default=cat_disponibles)

    impacto_disponibles = ["Alto", "Medio", "Bajo"]
    impacto_presente = [i for i in impacto_disponibles if i in df['impacto_mercado'].unique()]
    impacto_sel = st.sidebar.multiselect("Impacto de Mercado", impacto_presente, default=impacto_presente)

    busqueda = st.sidebar.text_input("🔎 Buscar palabra clave")

    # Aplicación de Filtros
    df_filtrado = df.copy()

    # Filtrar Fechas
    if isinstance(rango_fechas, (list, tuple)):
        if len(rango_fechas) == 2:
            f_inicio, f_fin = rango_fechas
            df_filtrado = df_filtrado[
                (df_filtrado['fecha_dt'].dt.date >= f_inicio) & 
                (df_filtrado['fecha_dt'].dt.date <= f_fin)
            ]
        elif len(rango_fechas) == 1:
            f_inicio = rango_fechas[0]
            df_filtrado = df_filtrado[df_filtrado['fecha_dt'].dt.date == f_inicio]

    # Filtrar Selectores
    if fuentes_sel:
        df_filtrado = df_filtrado[df_filtrado['fuente'].isin(fuentes_sel)]
    if cat_sel:
        df_filtrado = df_filtrado[df_filtrado['categoria'].isin(cat_sel)]
    if impacto_sel:
        df_filtrado = df_filtrado[df_filtrado['impacto_mercado'].isin(impacto_sel)]

    if busqueda:
        b_lower = busqueda.lower()
        df_filtrado = df_filtrado[
            df_filtrado['titulo'].str.lower().str.contains(b_lower) |
            df_filtrado['resumen_ejecutivo'].str.lower().str.contains(b_lower)
        ]

    # Encabezado Principal
    st.title("⚡ Monitor & Análisis del Mercado Eléctrico Chileno")
    st.caption("Agente automatizado con Inteligencia Artificial sobre Revista EI y ElectroMinería.")

    # Métricas KPI
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Noticias Analizadas", len(df_filtrado))
    alto_count = len(df_filtrado[df_filtrado['impacto_mercado'] == 'Alto'])
    pct_alto = round((alto_count / len(df_filtrado) * 100)) if len(df_filtrado) > 0 else 0
    c2.metric("Noticias de Impacto Alto", alto_count, f"{pct_alto}% del total")
    top_cat = df_filtrado['categoria'].mode()[0] if not df_filtrado.empty and not df_filtrado['categoria'].mode().empty else "N/A"
    c3.metric("Tema Principal", top_cat)
    ultima_f = str(df_filtrado['fecha_publicacion'].max())[:10] if not df_filtrado.empty else "N/A"
    c4.metric("Última Publicación", ultima_f)

    st.markdown("---")

    tab1, tab2, tab3 = st.tabs(["📰 Feed de Análisis IA", "📊 Gráficos y Tendencias", "📋 Tabla de Datos"])

    with tab1:
        st.subheader("Últimos Análisis de Mercado Generados por Gemini")
        if df_filtrado.empty:
            st.info("No hay noticias que coincidan con los filtros seleccionados.")
        else:
            for _, row in df_filtrado.iterrows():
                badge_color = "🔴" if row['impacto_mercado'] == "Alto" else ("🟡" if row['impacto_mercado'] == "Medio" else "🟢")
                
                # Formatear lista de actores
                actores_str = "Sin actores específicos"
                if row['actores_mencionados']:
                    try:
                        actores = json.loads(row['actores_mencionados'])
                        if isinstance(actores, list) and len(actores) > 0:
                            actores_str = ", ".join(actores)
                        elif isinstance(actores, str) and actores:
                            actores_str = actores
                    except:
                        actores_str = str(row['actores_mencionados'])

                fecha_corta = str(row['fecha_publicacion'])[:10]

                with st.container():
                    st.markdown(f"### {row['titulo']}")
                    st.caption(f"{badge_color} **Impacto {row['impacto_mercado']}** | 🏷️ **{row['categoria']}** | 📰 **{row['fuente']}** | 📅 **{fecha_corta}**")
                    st.info(f"💡 **Resumen Ejecutivo:** {row['resumen_ejecutivo']}")
                    st.write(f"🏛️ **Actores / Empresas:** {actores_str} | 📊 **Sentimiento:** {row['sentimiento']}")
                    st.markdown(f"🔗 [Leer artículo completo en {row['fuente']}]({row['url']})")
                    st.markdown("---")

    with tab2:
        st.subheader("Estadísticas del Mercado")
        if not df_filtrado.empty:
            col_g1, col_g2 = st.columns(2)
            with col_g1:
                st.write("**Distribución por Categoría**")
                st.bar_chart(df_filtrado['categoria'].value_counts())
            with col_g2:
                st.write("**Distribución por Nivel de Impacto**")
                st.bar_chart(df_filtrado['impacto_mercado'].value_counts())

    with tab3:
        st.subheader("Tabla de Datos Completa")
        st.dataframe(
            df_filtrado[['fecha_publicacion', 'fuente', 'categoria', 'impacto_mercado', 'titulo', 'url']],
            use_container_width=True
        )
