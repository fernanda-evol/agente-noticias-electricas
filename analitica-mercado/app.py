import importlib.util
import os

import streamlit as st


def _cargar_tab(nombre_archivo: str):
    """Carga un módulo de tabs/ por ruta de archivo. Los archivos de tabs
    llevan prefijo numérico (01_, 02_, ...) para fijar el orden en que se
    escribieron; eso no es un nombre de módulo válido para un `import`
    normal, así que se cargan así en vez de por nombre."""
    ruta = os.path.join(os.path.dirname(__file__), "tabs", nombre_archivo)
    spec = importlib.util.spec_from_file_location(nombre_archivo[:-3], ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


capacidad_instalada = _cargar_tab("01_capacidad_instalada.py")
contratos = _cargar_tab("02_contratos.py")

st.set_page_config(
    page_title="Analítica de Mercado Eléctrico · EVOL",
    page_icon="📊",
    layout="wide",
)

AZUL_OSCURO = "#00629B"

st.markdown(f"""
<style>
    html, body, [class*="css"], .stApp {{
        font-family: 'Calibri', 'Carlito', sans-serif;
    }}
    h1, h2, h3 {{ color: {AZUL_OSCURO}; }}
    [data-testid="stMetricValue"] {{ color: {AZUL_OSCURO}; }}
    .stTabs [aria-selected="true"] {{ color: {AZUL_OSCURO}; border-bottom-color: {AZUL_OSCURO} !important; }}
</style>
""", unsafe_allow_html=True)

st.title("📊 Analítica de Mercado Eléctrico Chileno")
st.caption("Datos públicos del Coordinador Eléctrico Nacional (API SIPUB)")

tab_capacidad, tab_contratos = st.tabs(["⚡ Capacidad Instalada", "📄 Contratos de Suministro"])
# Los próximos tabs (costo marginal, etc.) se agregan aquí como
# tabs/03_....py + una entrada más en la lista de arriba.

with tab_capacidad:
    capacidad_instalada.render()

with tab_contratos:
    contratos.render()
