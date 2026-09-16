import streamlit as st
import sqlite3
import pandas as pd
import json
import plotly.express as px
from datetime import datetime

# ==========================================
# 1. CONFIGURACIÓN DE LA PÁGINA
# ==========================================
st.set_page_config(
    page_title="Dashboard Mercado Eléctrico Chile",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Estilos CSS personalizados para tarjetas visuales
st.markdown("""
    <style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        color: #1E3A8A;
        margin-bottom: 0.5rem;
    }
    .sub-header {
        font-size: 1.1rem;
        color: #4B5563;
        margin-bottom: 2rem;
    }
    .news-card {
        background-color: #FFFFFF;
        padding: 1.5rem;
        border-radius: 10px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        margin-bottom: 1.2rem;
        border: 1px solid #E5E7EB;
    }
    .badge-alto { background-color: #FEE2E2; color: #991B1B; padding: 4px 10px; border-radius: 6px; font-weight: bold; font-size: 0.85rem; }
    .badge-medio { background-color: #FEF3C7; color: #92400E; padding: 4px 10px; border-radius: 6px; font-weight: bold; font-size: 0.85rem; }
    .badge-bajo { background-color: #D1FAE5; color: #065F46; padding: 4px 10px; border-radius: 6px; font-weight: bold; font-size: 0.85rem; }
    .badge-cat { background-color: #E0E7FF; color: #3730A3; padding: 4px 10px; border-radius: 6px; font-weight: 600; font-size: 0.85rem; }
    </style>
""", unsafe_allow_html=True)

# ==========================================
# 2. CARGA DE DATOS DE SQLITE
# ==========================================
@st.cache_data(ttl=300)  # Se actualiza cada 5 minutos
def cargar_datos():
    try:
        conn = sqlite3.connect("noticias_energia.db")
        df = pd.read_sql_query("SELECT * FROM noticias ORDER BY fecha_publicacion DESC", conn)
        conn.close()
        return df
    except Exception as e:
        st.error(f"Error cargando la base de datos: {e}")
        return pd.DataFrame()

df_raw = cargar_datos()

# Encabezado Principal
st.markdown('<div class="main-header">⚡ Monitor & Análisis del Mercado Eléctrico Chileno</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">Agente automatizado con Inteligencia Artificial sobre Revista EI y ElectroMinería.</div>', unsafe_allow_html=True)

if df_raw.empty:
    st.info("Aún no hay noticias registradas. Ejecuta el agente para poblar la base de datos.")
else:
    # ==========================================
    # 3. FILTROS LATERALES
    # ==========================================
    st.sidebar.header("🔍 Filtros")
    
    # Filtro por Fuente
    fuentes_disponibles = list(df_raw['fuente'].unique())
    fuentes_sel = st.sidebar.multiselect("Fuente de Noticias", fuentes_disponibles, default=fuentes_disponibles)
    
    # Filtro por Categoría
    categorias_disponibles = list(df_raw['categoria'].unique())
    categorias_sel = st.sidebar.multiselect("Categoría", categorias_disponibles, default=categorias_disponibles)
    
    # Filtro por Nivel de Impacto
    impactos_disponibles = ["Alto", "Medio", "Bajo"]
    impacto_sel = st.sidebar.multiselect("Impacto de Mercado", impactos_disponibles, default=impactos_disponibles)
    
    # Búsqueda libre
    query_busqueda = st.sidebar.text_input("🔎 Buscar palabra clave", "")

    # Filtrar el DataFrame
    df_filtrado = df_raw[
        (df_raw['fuente'].isin(fuentes_sel)) &
        (df_raw['categoria'].isin(categorias_sel)) &
        (df_raw['impacto_mercado'].isin(impacto_sel))
    ]
    
    if query_busqueda:
        df_filtrado = df_filtrado[
            df_filtrado['titulo'].str.contains(query_busqueda, case=False, na=False) |
            df_filtrado['resumen_ejecutivo'].str.contains(query_busqueda, case=False, na=False)
        ]

    # ==========================================
    # 4. MÉTRICAS CLAVE (KPIs)
    # ==========================================
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Noticias Analizadas", len(df_filtrado))
    with col2:
        noticias_alto = len(df_filtrado[df_filtrado['impacto_mercado'] == 'Alto'])
        pct = (noticias_alto / len(df_filtrado) * 100) if len(df_filtrado) > 0 else 0
        st.metric("Noticias de Impacto Alto", noticias_alto, delta=f"{pct:.0f}% del total")
    with col3:
        cat_top = df_filtrado['categoria'].mode()[0] if not df_filtrado.empty else "N/A"
        st.metric("Tema Principal", cat_top)
    with col4:
        ult_fecha = df_filtrado['fecha_publicacion'].max() if not df_filtrado.empty else "N/A"
        st.metric("Última Publicación", str(ult_fecha)[:10])

    st.markdown("---")

    # ==========================================
    # 5. PESTAÑAS DE CONTENIDO
    # ==========================================
    tab1, tab2, tab3 = st.tabs(["📰 Feed de Análisis IA", "📊 Gráficos y Tendencias", "🗃️ Tabla de Datos"])

    # TAB 1: FEED DE NOTICIAS CON TARJETAS
    with tab1:
        st.subheader("Últimos Análisis de Mercado Generados por Gemini")
        for _, row in df_filtrado.iterrows():
            actores = []
            try:
                actores = json.loads(row['actores_mencionados']) if row['actores_mencionados'] else []
            except:
                pass
            
            imp = row['impacto_mercado']
            badge_class = "badge-alto" if imp == "Alto" else ("badge-medio" if imp == "Medio" else "badge-bajo")
            actores_str = " • ".join(actores) if actores else "Sin actores específicos"
            
            st.markdown(f"""
            <div class="news-card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <div>
                        <span class="{badge_class}">Impacto {imp}</span>
                        <span class="badge-cat" style="margin-left: 8px;">{row['categoria']}</span>
                    </div>
                    <span style="color: #6B7280; font-size: 0.85rem; font-weight: 500;">{row['fuente']} | {str(row['fecha_publicacion'])[:10]}</span>
                </div>
                <h3 style="margin-top: 5px; color: #111827; font-size: 1.2rem; font-weight: 600;">{row['titulo']}</h3>
                <p style="color: #374151; font-size: 0.95rem; line-height: 1.6; background-color: #F9FAFB; padding: 12px; border-radius: 8px; border-left: 4px solid #3B82F6;">
                    <strong>💡 Resumen Ejecutivo:</strong> {row['resumen_ejecutivo']}
                </p>
                <div style="margin-top: 10px; font-size: 0.88rem; color: #4B5563;">
                    <strong>🏛️ Actores / Empresas:</strong> {actores_str} &nbsp;|&nbsp; <strong>📊 Sentimiento:</strong> {row.get('sentimiento', 'Neutro')}
                </div>
                <div style="margin-top: 12px;">
                    <a href="{row['url']}" target="_blank" style="color: #2563EB; text-decoration: none; font-weight: 600;">🔗 Leer artículo completo en {row['fuente']} →</a>
                </div>
            </div>
            """, unsafe_allow_html=True)

    # TAB 2: GRÁFICOS E INTERACTIVOS
    with tab2:
        st.subheader("Estadísticas del Mercado")
        g_col1, g_col2 = st.columns(2)
        
        with g_col1:
            fig_cat = px.pie(
                df_filtrado, 
                names='categoria', 
                title='Distribución de Noticias por Categoría',
                hole=0.4,
                color_discrete_sequence=px.colors.qualitative.Pastel
            )
            st.plotly_chart(fig_cat, use_container_width=True)
            
        with g_col2:
            fig_imp = px.bar(
                df_filtrado['impacto_mercado'].value_counts().reset_index(),
                x='impacto_mercado',
                y='count',
                labels={'impacto_mercado': 'Nivel de Impacto', 'count': 'Cantidad'},
                title='Noticias por Nivel de Impacto',
                color='impacto_mercado',
                color_discrete_map={'Alto': '#EF4444', 'Medio': '#F59E0B', 'Bajo': '#10B981'}
            )
            st.plotly_chart(fig_imp, use_container_width=True)

        # Ranking de Actores Mencionados
        all_actores = []
        for a_str in df_filtrado['actores_mencionados']:
            try:
                all_actores.extend(json.loads(a_str))
            except:
                pass
                
        if all_actores:
            df_actores = pd.Series(all_actores).value_counts().head(10).reset_index()
            df_actores.columns = ['Actor / Empresa', 'Menciones']
            
            fig_actores = px.bar(
                df_actores,
                x='Menciones',
                y='Actor / Empresa',
                orientation='h',
                title='Top 10 Actores y Reguladores Más Mencionados',
                color='Menciones',
                color_continuous_scale='Blues'
            )
            fig_actores.update_layout(yaxis={'categoryorder': 'total ascending'})
            st.plotly_chart(fig_actores, use_container_width=True)

    # TAB 3: TABLA COMPLETA
    with tab3:
        st.subheader("Registros en Base de Datos")
        st.dataframe(df_filtrado[['fecha_publicacion', 'fuente', 'titulo', 'categoria', 'impacto_mercado', 'sentimiento']], use_container_width=True)
        
        csv = df_filtrado.to_csv(index=False).encode('utf-8')
        st.download_button(
            label="📥 Descargar datos filtrados en CSV",
            data=csv,
            file_name=f"noticias_mercado_electrico_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv"
        )
