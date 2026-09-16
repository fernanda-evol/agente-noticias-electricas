import streamlit as st
import sqlite3
import pandas as pd
import json
from datetime import datetime, timedelta

st.set_page_config(
    page_title="Monitor Mercado Eléctrico Chileno",
    page_icon="⚡",
    layout="wide"
)

# Estilos CSS para Badges y Tarjetas
st.markdown("""
<style>
    .badge-alto { background-color: #ff4b4b; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; }
    .badge-medio { background-color: #ffa726; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; }
    .badge-bajo { background-color: #66bb6a; color: white; padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; }
    .badge-cat { background-color: #e0f7fa; color: #006064; padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; }
    .card-news { background-color: #ffffff; padding: 18px; border-radius: 8px; border: 1px solid #e0e0e0; margin-bottom: 15px; }
</style>
""", unsafe_allow_html=True)

DB_NAME = "noticias_energia.db"

@st.cache_data(ttl=60)
def cargar_datos():
    try:
        conn = sqlite3.connect(DB_NAME)
        df = pd.read_sql_query("SELECT * FROM noticias ORDER BY fecha_publicacion DESC", conn)
        conn.close()
        if not df.empty:
            df['fecha_dt'] = pd.to_datetime(df['fecha_publicacion'], errors='coerce')
        return df
    except Exception:
        return pd.DataFrame()

df = cargar_datos()

st.sidebar.title("⚙️ Opciones")
if st.sidebar.button("🔄 Actualizar Datos / Limpiar Caché"):
    st.cache_data.clear()
    st.rerun()

st.sidebar.title("🔍 Filtros")

if df.empty:
    st.warning("Aún no hay noticias registradas. Ejecuta el agente para poblar la base de datos.")
else:
    fechas_validas = df['fecha_dt'].dropna()
    min_date = fechas_validas.min().date() if not fechas_validas.empty else datetime.now().date() - timedelta(days=30)
    max_date = fechas_validas.max().date() if not fechas_validas.empty else datetime.now().date()

    rango_fechas = st.sidebar.date_input(
        "📅 Rango de Fechas",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date
    )

    fuentes_disponibles = sorted(list(df['fuente'].unique()))
    fuentes_sel = st.sidebar.multiselect("Fuente de Noticias", fuentes_disponibles, default=fuentes_disponibles)

    cat_disponibles = sorted(list(df['categoria'].unique()))
    cat_sel = st.sidebar.multiselect("Categoría", cat_disponibles, default=cat_disponibles)

    impacto_disponibles = ["Alto", "Medio", "Bajo"]
    impacto_presente = [i for i in impacto_disponibles if i in df['impacto_mercado'].unique()]
    impacto_sel = st.sidebar.multiselect("Impacto de Mercado", impacto_presente, default=impacto_presente)

    busqueda = st.sidebar.text_input("🔎 Buscar palabra clave")

    df_filtrado = df.copy()

    if isinstance(rango_fechas, (list, tuple)):
        if len(rango_fechas) == 2:
            df_filtrado = df_filtrado[
                (df_filtrado['fecha_dt'].dt.date >= rango_fechas[0]) & 
                (df_filtrado['fecha_dt'].dt.date <= rango_fechas[1])
            ]
        elif len(rango_fechas) == 1:
            df_filtrado = df_filtrado[df_filtrado['fecha_dt'].dt.date == rango_fechas[0]]

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

    st.title("⚡ Monitor & Análisis del Mercado Eléctrico Chileno")
    st.caption("Agente automatizado con Inteligencia Artificial sobre Revista EI y ElectroMinería.")

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
                imp = row['impacto_mercado']
                badge_class = "badge-alto" if imp == "Alto" else ("badge-medio" if imp == "Medio" else "badge-bajo")
                
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

                st.markdown(f"""
                <div class="card-news">
                    <span class="{badge_class}">Impacto {imp}</span> 
                    <span class="badge-cat">{row['categoria']}</span>
                    <span style="float: right; color: #757575; font-size: 13px;"><b>{row['fuente']}</b> | {fecha_corta}</span>
                    <h3 style="margin-top: 10px; margin-bottom: 10px;">{row['titulo']}</h3>
                    <div style="background-color: #f5f5f5; padding: 12px; border-left: 4px solid #1976d2; margin-bottom: 10px; font-size: 14px;">
                        💡 <b>Resumen Ejecutivo:</b> {row['resumen_ejecutivo']}
                    </div>
                    <p style="font-size: 13px; color: #424242; margin-bottom: 5px;">
                        🏛️ <b>Actores / Empresas:</b> {actores_str} &nbsp;|&nbsp; 📊 <b>Sentimiento:</b> {row['sentimiento']}
                    </p>
                    <a href="{row['url']}" target="_blank" style="font-size: 13px; text-decoration: none; color: #1976d2; font-weight: bold;">Leer artículo completo en {row['fuente']} →</a>
                </div>
                """, unsafe_allow_html=True)

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
