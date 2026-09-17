import requests
import feedparser
from bs4 import BeautifulSoup
import sqlite3
import json
import os
import re
import random
import urllib3
import urllib.parse
import time
from datetime import datetime, timezone
from google import genai
from google.genai import types

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DB_NAME = "noticias_energia.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
}

# --- Configuración de Gemini ---
# gemini-2.5-flash-lite: es el modelo con cuota gratuita más generosa (del
# orden de 1.000-1.500 solicitudes/día) y de sobra para clasificar/resumir
# noticias cortas. Con el volumen diario de este agente (decenas de
# noticias) el riesgo real de tope de cuota es bajo, pero igual se deja un
# límite de llamadas por corrida y una caída a reglas locales por si acaso
# (falla de red, cuota agotada, respuesta inválida).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
MAX_GEMINI_LLAMADAS_POR_CORRIDA = int(os.getenv("MAX_GEMINI_CALLS_PER_RUN", "60"))
GEMINI_ESPERA_SEGUNDOS = float(os.getenv("GEMINI_SLEEP_SECONDS", "3"))

CATEGORIAS_VALIDAS = [
    "Generación/ERNC", "Almacenamiento (BESS)", "Transmisión",
    "Regulación/Normativa", "PMGD/Distribución",
    "Hidrógeno Verde/Descarbonización", "Mercado Mayorista/Precios",
]
IMPACTOS_VALIDOS = ["Alto", "Medio", "Bajo"]
SENTIMIENTOS_VALIDOS = ["Positivo", "Negativo", "Neutro"]

# Catálogo de actores de referencia del mercado eléctrico chileno. No es una
# lista cerrada (Gemini puede mencionar otros actores relevantes que
# aparezcan en la noticia), pero asegura que reconozca y use el nombre
# canónico de estos, incluso si el artículo los menciona de forma indirecta
# (p. ej. "el Coordinador" -> "Coordinador Eléctrico Nacional").
ACTORES_REFERENCIA = """
Instituciones del sector: CNE (Comisión Nacional de Energía), CEN / Coordinador Eléctrico Nacional, SEA (Servicio de Evaluación Ambiental), SEIA, Panel de Expertos, SEC (Superintendencia de Electricidad y Combustibles)
Gremios: ACERA, ACENOR, ACEN, ANESCO, WEC, ACESOL, GIE, EEAG, Generadoras de Chile, Transmisoras de Chile, Chile Data Center
Big4 (generadoras principales): Enel, Engie, AES (AES Andes), Colbún
Otras generadoras/comercializadoras: Grenergy, Atlas Renewable Energy, Zelestra, Lipigas, EVOL, EMOAC, Cinergia, GM / Generadora Metropolitana
Transmisión: Transelec, ISA
Distribución: Enel Distribución, Chilquinta, CGE
"""

# Proxies públicos de lectura (gratuitos). Se usan como respaldo cuando la
# petición directa falla con 403/timeout — algo frecuente porque las IPs de
# los runners de GitHub Actions son rangos de datacenter conocidos que varios
# WAFs (Wordfence, Sucuri, Cloudflare) bloquean por defecto. El proxy hace la
# petición desde su propia IP y nos devuelve el HTML/XML crudo tal cual.
# Son servicios gratuitos sin garantía de disponibilidad, por eso hay dos en
# cascada y cada uno se reintenta antes de pasar al siguiente. (corsproxy.io
# quedó afuera: desde hace poco exige API key paga para cualquier uso que no
# sea localhost, así que nunca va a funcionar desde un runner de Actions.)
PROXIES_LECTURA = [
    "https://api.allorigins.win/raw?url={url}",
    "https://api.codetabs.com/v1/proxy?quest={url}",
]

REINTENTOS_POR_PROXY = 2
ESPERA_ENTRE_REINTENTOS = 3


def get_con_resiliencia(session, url, timeout=20, minimo_bytes=200):
    """GET con reintento automático vía proxy si la petición directa falla.
    Cada proxy se reintenta un par de veces (los fallos gratuitos suelen ser
    transitorios: saturación momentánea, timeout puntual). Devuelve el
    objeto Response (directo o vía proxy) o None si todo falló."""
    try:
        resp = session.get(url, timeout=timeout, verify=False)
        if resp.status_code == 200 and len(resp.content) >= minimo_bytes:
            return resp
        print(f"Directo a {url} -> HTTP {resp.status_code} ({len(resp.content)} bytes), probando proxy...")
    except Exception as e:
        print(f"Directo a {url} falló ({e}), probando proxy...")

    for plantilla in PROXIES_LECTURA:
        proxy_url = plantilla.format(url=urllib.parse.quote(url, safe=""))
        nombre_proxy = urllib.parse.urlparse(proxy_url).netloc
        for intento in range(1, REINTENTOS_POR_PROXY + 1):
            try:
                resp = session.get(proxy_url, timeout=timeout, verify=False)
                if resp.status_code == 200 and len(resp.content) >= minimo_bytes:
                    print(f"Proxy {nombre_proxy} -> OK ({len(resp.content)} bytes, intento {intento})")
                    return resp
                print(f"Proxy {nombre_proxy} -> HTTP {resp.status_code} ({len(resp.content)} bytes, intento {intento}/{REINTENTOS_POR_PROXY})")
            except Exception as e:
                print(f"Proxy {nombre_proxy} falló (intento {intento}/{REINTENTOS_POR_PROXY}): {e}")
            if intento < REINTENTOS_POR_PROXY:
                time.sleep(ESPERA_ENTRE_REINTENTOS)

    return None

def inicializar_bd():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS noticias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fuente TEXT,
            titulo TEXT,
            url TEXT UNIQUE,
            fecha_publicacion TEXT,
            categoria TEXT,
            resumen_ejecutivo TEXT,
            impacto_mercado TEXT,
            actores_mencionados TEXT,
            sentimiento TEXT,
            procesado_el TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def limpiar_html(html_content):
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    for script in soup(["script", "style"]):
        script.decompose()
    return re.sub(r'\s+', ' ', soup.get_text(separator=" ")).strip()

def clasificar_localmente(titulo, texto):
    contenido = f"{titulo} {texto}".lower()
    
    cat = "Generación/ERNC"
    if any(k in contenido for k in ["bess", "almacenamiento", "batería", "baterias"]):
        cat = "Almacenamiento (BESS)"
    elif any(k in contenido for k in ["transmisión", "transmision", "línea", "subestación"]):
        cat = "Transmisión"
    elif any(k in contenido for k in ["cne", "coordinador", "regulación", "norma", "ley", "decreto", "tarifas"]):
        cat = "Regulación/Normativa"
    elif any(k in contenido for k in ["pmgd", "distribución", "distribucion"]):
        cat = "PMGD/Distribución"
    elif any(k in contenido for k in ["hidrógeno", "hidrogeno", "descarbonización"]):
        cat = "Hidrógeno Verde/Descarbonización"
    elif any(k in contenido for k in ["precio", "spot", "cmg", "costo marginal", "mayorista"]):
        cat = "Mercado Mayorista/Precios"

    impacto = "Medio"
    if any(k in contenido for k in ["cne", "coordinador eléctrico", "decreto", "ley", "resolución", "vertimiento", "insolvencia", "licitación", "mw", "us$"]):
        impacto = "Alto"
    elif any(k in contenido for k in ["nombramiento", "premio", "evento", "reconocimiento", "aniversario"]):
        impacto = "Bajo"

    actores = []
    actores_reglas = {
        "cne": "CNE", "coordinador": "Coordinador Eléctrico Nacional",
        "panel de expertos": "Panel de Expertos", "sec": "SEC",
        "sea": "SEA", "seia": "SEIA",
        "acera": "ACERA", "acenor": "ACENOR", "anesco": "ANESCO",
        "generadoras de chile": "Generadoras de Chile", "transmisoras de chile": "Transmisoras de Chile",
        "enel": "Enel", "engie": "Engie", "aes": "AES Andes", "colbún": "Colbún", "colbun": "Colbún",
        "grenergy": "Grenergy", "atlas renewable": "Atlas Renewable Energy", "zelestra": "Zelestra",
        "lipigas": "Lipigas", "evol": "EVOL", "emoac": "EMOAC", "cinergia": "Cinergia",
        "transelec": "Transelec", "isa": "ISA",
        "chilquinta": "Chilquinta", "cge": "CGE",
    }
    for clave, nombre_canonico in actores_reglas.items():
        if clave in contenido and nombre_canonico not in actores:
            actores.append(nombre_canonico)

    return {
        "categoria": cat,
        "resumen_ejecutivo": (texto[:220] + "...") if len(texto) > 50 else titulo,
        "impacto_mercado": impacto,
        "actores_mencionados": actores,
        "sentimiento": "Neutro"
    }


class CuotaGeminiAgotada(Exception):
    """Señal interna: Gemini devolvió 429 de forma persistente en esta
    corrida. El llamador debe dejar de intentar con Gemini y usar reglas
    locales para el resto de las noticias, sin seguir insistiendo."""
    pass


def _esquema_analisis_gemini():
    return {
        "type": "object",
        "properties": {
            "categoria": {"type": "string", "enum": CATEGORIAS_VALIDAS},
            "resumen_ejecutivo": {"type": "string"},
            "impacto_mercado": {"type": "string", "enum": IMPACTOS_VALIDOS},
            "actores_mencionados": {"type": "array", "items": {"type": "string"}},
            "sentimiento": {"type": "string", "enum": SENTIMIENTOS_VALIDOS},
        },
        "required": ["categoria", "resumen_ejecutivo", "impacto_mercado", "actores_mencionados", "sentimiento"],
    }


def clasificar_con_gemini(client, titulo, texto, contador_llamadas):
    """Analiza una noticia con Gemini (categoría, resumen, impacto, actores,
    sentimiento) usando salida JSON estructurada. Devuelve None si la
    respuesta no se pudo usar (el llamador debe caer a clasificar_localmente
    en ese caso). Lanza CuotaGeminiAgotada si detecta un 429 persistente."""
    if contador_llamadas[0] >= MAX_GEMINI_LLAMADAS_POR_CORRIDA:
        raise CuotaGeminiAgotada("Presupuesto de llamadas a Gemini agotado para esta corrida")

    prompt = (
        "Eres un analista del mercado eléctrico chileno. Analiza la siguiente "
        "noticia y responde solo con el JSON solicitado, sin texto adicional ni markdown.\n\n"
        f"Categorías válidas (elige exactamente una): {', '.join(CATEGORIAS_VALIDAS)}\n\n"
        f"Título: {titulo}\n"
        f"Contenido: {texto[:3000]}\n\n"
        "resumen_ejecutivo: máximo 40 palabras, en español, con la información "
        "más relevante para un analista comercial del mercado eléctrico.\n\n"
        "actores_mencionados: identifica TODOS los actores del mercado eléctrico "
        "chileno mencionados en el título o el contenido, incluso si aparecen "
        "solo de forma indirecta o abreviada (por ejemplo, \"el Coordinador\" "
        "debe registrarse como \"Coordinador Eléctrico Nacional\"). Usa siempre "
        "el nombre oficial o más reconocible del actor, no la forma abreviada "
        "tal como aparece en el texto. Estos son los actores de referencia más "
        "relevantes para este análisis (la lista no es cerrada: si aparece un "
        "actor del sector eléctrico chileno que no está aquí, inclúyelo igual "
        "con su nombre más reconocible):\n"
        f"{ACTORES_REFERENCIA}\n"
        "Si ninguno de estos ni otro actor del sector aparece mencionado, "
        "devuelve una lista vacía."
    )

    max_reintentos = 3
    for intento in range(1, max_reintentos + 1):
        contador_llamadas[0] += 1
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_esquema_analisis_gemini(),
                    temperature=0.2,
                ),
            )
        except Exception as e:
            mensaje = str(e)
            if "429" in mensaje or "RESOURCE_EXHAUSTED" in mensaje.upper():
                print(f"Gemini: 429 (intento {intento}/{max_reintentos})")
                if intento == max_reintentos:
                    raise CuotaGeminiAgotada("Gemini devolvió 429 de forma persistente")
                time.sleep((2 ** intento) + random.uniform(0, 1))
                continue
            print(f"Gemini: error de API ({e}), se usan reglas locales para esta noticia")
            return None

        try:
            resultado = json.loads(resp.text)
        except (ValueError, AttributeError) as e:
            print(f"Gemini: respuesta no parseable ({e}), se usan reglas locales para esta noticia")
            return None

        categoria = resultado.get("categoria")
        impacto = resultado.get("impacto_mercado")
        sentimiento = resultado.get("sentimiento")
        actores = resultado.get("actores_mencionados")
        return {
            "categoria": categoria if categoria in CATEGORIAS_VALIDAS else "Generación/ERNC",
            "resumen_ejecutivo": (resultado.get("resumen_ejecutivo") or titulo)[:400],
            "impacto_mercado": impacto if impacto in IMPACTOS_VALIDOS else "Medio",
            "actores_mencionados": actores if isinstance(actores, list) else [],
            "sentimiento": sentimiento if sentimiento in SENTIMIENTOS_VALIDOS else "Neutro",
        }

    return None

def _contenedor_propio_del_post(h2):
    """Sube por los ancestros de un <h2> hasta encontrar el contenedor que
    envuelve SOLO esa noticia (no toda la lista). Se detecta sin depender del
    nombre de la clase CSS del tema: el contenedor correcto es el último
    ancestro que sigue teniendo un único <h2> adentro — apenas un ancestro
    tiene más de uno, ya nos pasamos al bloque que agrupa varias noticias."""
    candidato = h2.parent
    anterior = h2
    for _ in range(8):
        if candidato is None:
            break
        if len(candidato.find_all("h2")) > 1:
            break
        anterior = candidato
        candidato = candidato.parent
    return anterior


def _extraer_bajada(contenedor, titulo):
    """Primer párrafo con contenido real dentro del contenedor de la noticia
    (evita reventar categoría/fecha/"Leer más", que son muy cortos)."""
    for p in contenedor.find_all("p"):
        texto = p.get_text(" ", strip=True)
        if len(texto) > 25 and texto.lower() != titulo.lower():
            return texto
    return ""


def obtener_texto_y_fecha_articulo(session, url, max_chars=4000):
    """Descarga SOLO la fecha real de publicación desde el meta tag del
    artículo. (El cuerpo del artículo ya no se usa como fuente de resumen:
    el selector genérico de respaldo terminaba agarrando bloques de
    "artículos relacionados" del tema y mezclando resúmenes entre noticias.
    Ver obtener_electromineria_categoria, que ahora saca la bajada del
    propio listado de la categoría.)"""
    fecha_default = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    try:
        resp = get_con_resiliencia(session, url, timeout=15)
        if resp is None:
            return fecha_default
        soup = BeautifulSoup(resp.text, "html.parser")
        meta_fecha = soup.find("meta", property="article:published_time")
        return meta_fecha["content"] if meta_fecha and meta_fecha.get("content") else fecha_default
    except Exception:
        return fecha_default


CATEGORIA_ENERGIA_URL = "https://electromineria.cl/category/panorama-energetico/"


def obtener_electromineria_categoria(session, limite=25):
    """Extrae noticias directamente del listado de la categoría 'Panorama
    Energético' — la sección específica de energía del sitio (a diferencia
    del RSS general, que mezcla noticias de minería con las de energía).
    La bajada de cada noticia se saca del propio bloque del listado (no de
    visitar cada artículo), así queda garantizado que no se mezcla con la de
    otra noticia. Solo se visita el artículo individual para confirmar la
    fecha exacta de publicación."""
    try:
        resp = get_con_resiliencia(session, CATEGORIA_ENERGIA_URL, timeout=20)
        if resp is None:
            print("ElectroMinería categoría Panorama Energético -> falló directo y por proxy")
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        vistos, noticias = set(), []
        for h2 in soup.select("h2"):
            a = h2.find("a", href=True)
            if not a:
                continue
            url = a.get("href", "").strip()
            titulo = a.get_text(strip=True)
            if not (url.startswith("https://electromineria.cl/") and titulo):
                continue
            if "/category/" in url or "/tag/" in url or url in vistos:
                continue
            vistos.add(url)

            contenedor = _contenedor_propio_del_post(h2)
            texto = _extraer_bajada(contenedor, titulo)
            fecha_pub = obtener_texto_y_fecha_articulo(session, url)
            noticias.append({"fuente": "ElectroMinería", "titulo": titulo, "url": url, "texto": texto, "fecha": fecha_pub})
            if len(noticias) >= limite:
                break
        print(f"ElectroMinería categoría Panorama Energético -> {len(noticias)} noticias")
        return noticias
    except Exception as e:
        print(f"Error scraping categoría ElectroMinería: {e}")
        return []


def obtener_electromineria_via_html(session, limite=20):
    """Último recurso si la categoría y el RSS también fallan: scraping del home."""
    try:
        resp = get_con_resiliencia(session, "https://electromineria.cl/", timeout=20)
        if resp is None:
            print("ElectroMinería HTML (fallback) -> falló directo y por proxy")
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        vistos, noticias = set(), []
        for h2 in soup.select("h2"):
            a = h2.find("a", href=True)
            if not a:
                continue
            url = a.get("href", "").strip()
            titulo = a.get_text(strip=True)
            if not (url.startswith("https://electromineria.cl/") and titulo) or url in vistos:
                continue
            vistos.add(url)
            contenedor = _contenedor_propio_del_post(h2)
            texto = _extraer_bajada(contenedor, titulo)
            fecha_pub = obtener_texto_y_fecha_articulo(session, url)
            noticias.append({"fuente": "ElectroMinería", "titulo": titulo, "url": url, "texto": texto, "fecha": fecha_pub})
            if len(noticias) >= limite:
                break
        return noticias
    except Exception as e:
        print(f"Error scraping HTML ElectroMinería: {e}")
        return []


def obtener_noticias():
    noticias = []

    # 1. Revista EI (API WP)
    session_ei = requests.Session()
    session_ei.headers.update(HEADERS)
    resp = get_con_resiliencia(session_ei, "https://www.revistaei.cl/wp-json/wp/v2/posts?per_page=30", timeout=15)
    if resp is not None:
        try:
            for post in resp.json():
                titulo = limpiar_html(post.get("title", {}).get("rendered", ""))
                url = post.get("link", "").strip()
                texto = limpiar_html(post.get("content", {}).get("rendered", ""))
                fecha = post.get("date") or datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                if titulo and url:
                    noticias.append({"fuente": "Revista EI", "titulo": titulo, "url": url, "texto": texto, "fecha": fecha})
        except Exception as e:
            print(f"Error parseando Revista EI: {e}")
    else:
        print("Revista EI -> falló directo y por proxy")

    # 2. ElectroMinería — el REST API de este sitio (/wp-json/wp/v2/posts) está
    # filtrado por un plugin de seguridad y siempre devuelve [] con HTTP 200,
    # así que no se usa como fuente. La fuente principal es el listado HTML de
    # la categoría "Panorama Energético" (la sección de energía del sitio),
    # con el RSS general y el home como respaldos si esa página cambia.
    session_em = requests.Session()
    session_em.headers.update({
        **HEADERS,
        "Referer": "https://electromineria.cl/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    try:
        # "Calentar" la sesión visitando el home primero: algunos WAFs exigen
        # una cookie de sesión antes de servir contenido a clientes no-navegador.
        session_em.get("https://electromineria.cl/", timeout=15, verify=False)
    except Exception as e:
        print(f"Aviso: no se pudo precalentar sesión ElectroMinería: {e}")

    noticias_em = obtener_electromineria_categoria(session_em)

    if not noticias_em:
        resp_rss = get_con_resiliencia(session_em, "https://electromineria.cl/feed/", timeout=20)
        if resp_rss is not None:
            try:
                feed = feedparser.parse(resp_rss.content)
                print(f"ElectroMinería RSS (respaldo) -> {len(feed.entries)} entradas parseadas")
                for entry in feed.entries:
                    titulo = getattr(entry, 'title', '').strip()
                    url = getattr(entry, 'link', '').strip()
                    if not (titulo and url):
                        continue
                    fecha_pub = obtener_texto_y_fecha_articulo(session_em, url)
                    content_raw = entry.content[0].value if "content" in entry and len(entry.content) > 0 else getattr(entry, 'summary', '')
                    texto = limpiar_html(content_raw)
                    noticias_em.append({"fuente": "ElectroMinería", "titulo": titulo, "url": url, "texto": texto, "fecha": fecha_pub})
            except Exception as e:
                print(f"Error parseando RSS ElectroMinería: {e}")
        else:
            print("ElectroMinería RSS (respaldo) -> falló directo y por proxy")

    # Último recurso: scraping del home si categoría y RSS fallaron.
    if not noticias_em:
        noticias_em = obtener_electromineria_via_html(session_em)

    noticias.extend(noticias_em)
    return noticias

def ejecutar_agente():
    inicializar_bd()
    noticias = obtener_noticias()
    print(f"Total de noticias obtenidas: {len(noticias)}")

    cliente_gemini = None
    cuota_agotada = not bool(GEMINI_API_KEY)
    contador_llamadas = [0]
    if GEMINI_API_KEY:
        try:
            cliente_gemini = genai.Client(api_key=GEMINI_API_KEY)
        except Exception as e:
            print(f"No se pudo inicializar el cliente de Gemini ({e}); se usarán reglas locales para todo.")
            cuota_agotada = True
    else:
        print("GEMINI_API_KEY no configurada; se usarán reglas locales para todo.")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    guardadas = 0
    origen_gemini = 0
    origen_reglas = 0
    for item in noticias:
        cursor.execute("SELECT id FROM noticias WHERE url = ?", (item["url"],))
        if cursor.fetchone():
            continue

        analisis = None
        if not cuota_agotada:
            try:
                analisis = clasificar_con_gemini(cliente_gemini, item["titulo"], item["texto"], contador_llamadas)
                if analisis is not None:
                    origen_gemini += 1
                    time.sleep(GEMINI_ESPERA_SEGUNDOS)
            except CuotaGeminiAgotada as e:
                print(f"{e} — de acá en adelante se usan reglas locales para esta corrida.")
                cuota_agotada = True

        if analisis is None:
            analisis = clasificar_localmente(item["titulo"], item["texto"])
            origen_reglas += 1

        cursor.execute('''
            INSERT OR IGNORE INTO noticias 
            (fuente, titulo, url, fecha_publicacion, categoria, resumen_ejecutivo, impacto_mercado, actores_mencionados, sentimiento)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            item["fuente"],
            item["titulo"],
            item["url"],
            item["fecha"],
            analisis["categoria"],
            analisis["resumen_ejecutivo"],
            analisis["impacto_mercado"],
            json.dumps(analisis["actores_mencionados"], ensure_ascii=False),
            analisis["sentimiento"]
        ))
        conn.commit()
        guardadas += 1

    conn.close()
    print(f"Proceso finalizado. {guardadas} nuevas noticias guardadas "
          f"({origen_gemini} con Gemini, {origen_reglas} con reglas locales).")

if __name__ == "__main__":
    ejecutar_agente()
