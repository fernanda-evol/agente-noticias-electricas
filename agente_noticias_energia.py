import requests
import feedparser
from bs4 import BeautifulSoup
import sqlite3
import json
import os
import re
import calendar
import urllib3
from datetime import datetime, timedelta, timezone
from google import genai

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FUENTES = [
    {
        "nombre": "Revista EI", 
        "url_api": "https://www.revistaei.cl/wp-json/wp/v2/posts?per_page=50",
        "url_rss": "https://www.revistaei.cl/feed/"
    },
    {
        "nombre": "ElectroMinería", 
        "url_api": "https://electromineria.cl/wp-json/wp/v2/posts?per_page=50",
        "url_rss": "https://electromineria.cl/feed/"
    }
]

DB_NAME = "noticias_energia.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/xml, */*"
}

PALABRAS_EXCLUIR_EMPLEO = [
    "ofertas de empleo", "oferta de empleo", "vacantes disponibles", 
    "vacantes", "reclutamiento", "oferta laboral", "ofertas laborales", 
    "trabaje con nosotros", "postulaciones abiertas", "buscamos profesional", 
    "bolsa de trabajo", "oportunidad laboral"
]

def inicializar_bd(reset=False):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    if reset:
        cursor.execute("DROP TABLE IF EXISTS noticias")
        
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
    cursor.execute("DELETE FROM noticias WHERE LOWER(titulo) LIKE '%ofertas de empleo%' OR LOWER(titulo) LIKE '%vacantes%'")
    conn.commit()
    conn.close()

def normalizar_url(url):
    if not url:
        return ""
    u = url.strip().rstrip("/")
    u = u.replace("https://www.electromineria.cl", "https://electromineria.cl")
    u = u.replace("http://www.electromineria.cl", "https://electromineria.cl")
    u = u.replace("http://electromineria.cl", "https://electromineria.cl")
    return u

def limpiar_html(html_content):
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    for script in soup(["script", "style"]):
        script.decompose()
    texto = soup.get_text(separator=" ")
    return re.sub(r'\s+', ' ', texto).strip()

def es_oferta_empleo(titulo, texto):
    contenido = f"{titulo} {texto}".lower()
    return any(palabra in contenido for palabra in PALABRAS_EXCLUIR_EMPLEO)

def deducir_categoria(titulo, texto):
    contenido = f"{titulo} {texto}".lower()
    if any(k in contenido for k in ["bess", "almacenamiento", "batería", "baterias"]):
        return "Almacenamiento (BESS)"
    elif any(k in contenido for k in ["transmisión", "transmision", "línea", "subestación"]):
        return "Transmisión"
    elif any(k in contenido for k in ["cne", "coordinador", "regulación", "norma", "ley", "decreto"]):
        return "Regulación/Normativa"
    elif any(k in contenido for k in ["pmgd", "distribución", "distribucion"]):
        return "PMGD/Distribución"
    elif any(k in contenido for k in ["hidrógeno", "hidrogeno", "descarbonización"]):
        return "Hidrógeno Verde/Descarbonización"
    elif any(k in contenido for k in ["precio", "spot", "cmg", "costo marginal", "tarifa"]):
        return "Mercado Mayorista/Precios"
    return "Generación/ERNC"

def obtener_noticias():
    noticias_map = {}
    hace_30_dias = datetime.now(timezone.utc) - timedelta(days=30)
    
    for fuente in FUENTES:
        print(f"📡 Consultando fuente: {fuente['nombre']}...")
        
        # 1. Consulta vía API WordPress REST
        try:
            resp = requests.get(fuente["url_api"], headers=HEADERS, timeout=15, verify=False)
            if resp.status_code == 200:
                posts = resp.json()
                if isinstance(posts, list):
                    print(f"   [API WP] {len(posts)} artículos obtenidos de {fuente['nombre']}.")
                    for post in posts:
                        titulo = limpiar_html(post.get("title", {}).get("rendered", ""))
                        url_oficial = normalizar_url(post.get("link", ""))
                        
                        if not url_oficial or es_oferta_empleo(titulo, ""):
                            continue

                        date_str = post.get("date_gmt", "") or post.get("date", "")
                        fecha_dt = datetime.now(timezone.utc)
                        if date_str:
                            date_clean = re.sub(r'\.\d+', '', date_str.replace("Z", ""))
                            for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]:
                                try:
                                    fecha_dt = datetime.strptime(date_clean, fmt).replace(tzinfo=timezone.utc)
                                    break
                                except ValueError:
                                    pass

                        if fecha_dt < hace_30_dias:
                            continue

                        texto_limpio = limpiar_html(post.get("content", {}).get("rendered", ""))
                        
                        if titulo and url_oficial not in noticias_map:
                            noticias_map[url_oficial] = {
                                "fuente": fuente["nombre"],
                                "titulo": titulo,
                                "url": url_oficial,
                                "fecha": fecha_dt.strftime('%Y-%m-%d %H:%M:%S'),
                                "texto": texto_limpio
                            }
        except Exception as e:
            print(f"   ⚠️ Error API WP ({fuente['nombre']}): {e}")

        # 2. Respaldo vía Feed RSS
        try:
            resp_rss = requests.get(fuente["url_rss"], headers=HEADERS, timeout=15, verify=False)
            if resp_rss.status_code == 200:
                feed = feedparser.parse(resp_rss.content)
                print(f"   [RSS] {len(feed.entries)} artículos obtenidos de {fuente['nombre']}.")
                for entry in feed.entries:
                    titulo = getattr(entry, 'title', '').strip()
                    url_oficial = normalizar_url(getattr(entry, 'link', ''))

                    if not url_oficial or es_oferta_empleo(titulo, ""):
                        continue

                    if url_oficial not in noticias_map:
                        parsed_time = getattr(entry, 'published_parsed', None)
                        fecha_dt = datetime.now(timezone.utc)
                        if parsed_time:
                            fecha_dt = datetime.fromtimestamp(calendar.timegm(parsed_time), tz=timezone.utc)

                        if fecha_dt < hace_30_dias:
                            continue

                        content_raw = ""
                        if "content" in entry and len(entry.content) > 0:
                            content_raw = entry.content[0].value
                        elif "summary" in entry:
                            content_raw = entry.summary

                        noticias_map[url_oficial] = {
                            "fuente": fuente["nombre"],
                            "titulo": titulo,
                            "url": url_oficial,
                            "fecha": fecha_dt.strftime('%Y-%m-%d %H:%M:%S'),
                            "texto": limpiar_html(content_raw)
                        }
        except Exception as e:
            print(f"   ⚠️ Error RSS ({fuente['nombre']}): {e}")

    return list(noticias_map.values())

def analizar_con_llm(titulo, texto, fuente):
    if es_oferta_empleo(titulo, texto):
        return {"es_relevante": False}

    cat_fallback = deducir_categoria(titulo, texto)
    fallback_res = {
        "es_relevante": True,
        "categoria": cat_fallback,
        "resumen_ejecutivo": (texto[:220] + "...") if len(texto) > 50 else titulo,
        "impacto_mercado": "Medio",
        "actores_mencionados": [],
        "sentimiento": "Neutro"
    }

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("⚠️ GEMINI_API_KEY no encontrada. Usando análisis de respaldo.")
        return fallback_res

    client = genai.Client(api_key=api_key)
    
    prompt = f"""
    Eres un analista experto en el mercado eléctrico chileno.
    Analiza técnicamente el siguiente artículo publicado en {fuente}:
    
    Título: {titulo}
    Texto: {texto[:2500]}
    
    SI ES OFERTA DE EMPLEO, AVISO LABORAL O PUBLICIDAD, RESPONDE STRICTAMENTE:
    {{"es_relevante": false}}

    CLASIFICACIÓN DE "impacto_mercado":
    - "Alto": Proyectos >US$ 50M o >100 MW (BESS, Transmisión, ERNC), decretos/resoluciones de CNE/Coordinador Eléctrico/Ministerio, alzas tarifarias, vertimientos masivos o insolvencias.
    - "Medio": Declaraciones/estudios de gremios (Acenor, Generadoras, ACERA), proyectos PMGD, hitos de obra, contratos PPA, aprobaciones ambientales.
    - "Bajo": Nombramientos corporativos, eventos, ferias, premiaciones, RSE.

    Responde ÚNICAMENTE en JSON estricto con la siguiente estructura:
    1. "es_relevante": true
    2. "categoria": Selección estricta entre ["Transmisión", "Almacenamiento (BESS)", "Generación/ERNC", "Regulación/Normativa", "Mercado Mayorista/Precios", "PMGD/Distribución", "Hidrógeno Verde/Descarbonización"].
    3. "resumen_ejecutivo": Párrafo técnico de 2-3 oraciones sintetizando el impacto real.
    4. "impacto_mercado": "Alto", "Medio" o "Bajo".
    5. "actores_mencionados": Lista con nombres exactos de empresas u organismos citados (ej. ["Acenor", "CNE", "Coordinador Eléctrico", "Enel"]).
    6. "sentimiento": Elige entre ["Positivo", "Neutro", "Riesgo/Negativo"].
    """

    modelos_a_probar = ["gemini-3.6-flash"]
    
    for model_name in modelos_a_probar:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={"response_mime_type": "application/json"}
            )
            if response.text:
                cleaned = re.sub(r'^```json\s*', '', response.text.strip(), flags=re.MULTILINE)
                cleaned = re.sub(r'^```\s*', '', cleaned, flags=re.MULTILINE).strip()
                res_json = json.loads(cleaned)
                if isinstance(res_json, dict) and "categoria" in res_json:
                    return res_json
        except Exception as e:
            print(f"⚠️ Error Gemini ({model_name}): {e}")
            continue

    return fallback_res

def ejecutar_agente():
    inicializar_bd(reset=False)
    
    noticias = obtener_noticias()
    print(f"\n📰 Total de noticias únicas recuperadas: {len(noticias)}")
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    noticias_guardadas = 0
    for item in noticias:
        url_oficial = item["url"]
        
        cursor.execute("SELECT id FROM noticias WHERE url = ?", (url_oficial,))
        if cursor.fetchone():
            continue
            
        print(f"🧠 Analizando noticia de {item['fuente']}: {item['titulo'][:50]}...")
        analisis = analizar_con_llm(item["titulo"], item["texto"], item["fuente"])
        
        if analisis and analisis.get("es_relevante", True):
            actores_list = analisis.get("actores_mencionados", [])
            if not isinstance(actores_list, list):
                actores_list = [str(actores_list)] if actores_list else []
                
            cursor.execute('''
                INSERT INTO noticias 
                (fuente, titulo, url, fecha_publicacion, categoria, resumen_ejecutivo, impacto_mercado, actores_mencionados, sentimiento)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                item["fuente"],
                item["titulo"],
                url_oficial,
                item["fecha"],
                analisis.get("categoria", "Regulación/Normativa"),
                analisis.get("resumen_ejecutivo", item["titulo"]),
                analisis.get("impacto_mercado", "Medio"),
                json.dumps(actores_list, ensure_ascii=False),
                analisis.get("sentimiento", "Neutro")
            ))
            conn.commit()
            noticias_guardadas += 1
            print(f"✅ Noticia agregada.")
            
    conn.close()
    print(f"\n🚀 Proceso finalizado. {noticias_guardadas} noticias agregadas a {DB_NAME}.")

if __name__ == "__main__":
    ejecutar_agente()
