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

FUENTES_API = [
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

URL_ELECTROMINERIA_ENERGIA = "https://electromineria.cl/category/panorama-energetico/"
DB_NAME = "noticias_energia.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

PALABRAS_EXCLUIR_EMPLEO = [
    "ofertas de empleo", "oferta de empleo", "vacantes disponibles", 
    "vacantes", "reclutamiento", "oferta laboral", "ofertas laborales", 
    "trabaje con nosotros", "postulaciones abiertas", "buscamos profesional", 
    "bolsa de trabajo", "oportunidad laboral"
]

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
    cursor.execute("DELETE FROM noticias WHERE LOWER(titulo) LIKE '%ofertas de empleo%' OR LOWER(titulo) LIKE '%vacantes%'")
    conn.commit()
    conn.close()

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

def extraer_panorama_energetico_electromineria():
    noticias = []
    try:
        print(f"🕸️ Extrayendo directamente desde categoría: {URL_ELECTROMINERIA_ENERGIA}")
        resp = requests.get(URL_ELECTROMINERIA_ENERGIA, headers=HEADERS, timeout=15, verify=False)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "html.parser")
            articulos = soup.find_all(["article", "div"], class_=re.compile(r'post|entry|item|article'))
            
            for art in articulos:
                a_tag = art.find("a", href=True)
                h_tag = art.find(["h1", "h2", "h3", "h4"]) or a_tag
                
                if a_tag and h_tag:
                    url = a_tag["href"].strip()
                    titulo = limpiar_html(h_tag.get_text())
                    
                    if titulo and url and "electromineria.cl" in url and not es_oferta_empleo(titulo, ""):
                        noticias.append({
                            "fuente": "ElectroMinería",
                            "titulo": titulo,
                            "url": url,
                            "fecha": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                            "texto": titulo
                        })
            print(f"   [Web Scraper] {len(noticias)} artículos encontrados.")
    except Exception as e:
        print(f"⚠️ Error Scraper ElectroMinería: {e}")
    return noticias

def obtener_noticias():
    noticias_map = {}
    hace_30_dias = datetime.now(timezone.utc) - timedelta(days=30)
    
    # 1. Scraper Panorama Energético
    for item in extraer_panorama_energetico_electromineria():
        noticias_map[item["url"]] = item

    # 2. APIs REST y RSS
    for fuente in FUENTES_API:
        print(f"📡 Consultando API/RSS: {fuente['nombre']}...")
        try:
            resp = requests.get(fuente["url_api"], headers=HEADERS, timeout=15, verify=False)
            if resp.status_code == 200:
                posts = resp.json()
                if isinstance(posts, list):
                    for post in posts:
                        titulo = limpiar_html(post.get("title", {}).get("rendered", ""))
                        url_oficial = post.get("link", "").strip()
                        
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

    return list(noticias_map.values())

def analizar_con_llm(titulo, texto, fuente):
    if es_oferta_empleo(titulo, texto):
        return {"es_relevante": False}

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    client = genai.Client(api_key=api_key)
    
    prompt = f"""
    Eres un analista experto del mercado eléctrico chileno.
    Analiza la noticia de {fuente}:
    
    Título: {titulo}
    Texto: {texto[:2500]}
    
    SI ES OFERTA DE EMPLEO, RESPONDE: {{"es_relevante": false}}

    CLASIFICACIÓN ESTRICTA DE "impacto_mercado":
    - "Alto": Leyes, reglamentos, resoluciones CNE/Coordinador, proyectos BESS o Transmisión >US$50M o >100MW, vertimientos masivos o alzas tarifarias.
    - "Medio": Posicionamiento de gremios (Acenor, Generadoras), proyectos PMGD, hitos de construcción/ingreso ambiental, contratos PPA.
    - "Bajo": Nombramientos de ejecutivos, eventos, ferias, actividades RSE.

    Responde ÚNICAMENTE en JSON estricto:
    1. "es_relevante": true
    2. "categoria": Selección estricta entre ["Transmisión", "Almacenamiento (BESS)", "Generación/ERNC", "Regulación/Normativa", "Mercado Mayorista/Precios", "PMGD/Distribución", "Hidrógeno Verde/Descarbonización"].
    3. "resumen_ejecutivo": Párrafo técnico de 2-3 oraciones sintetizando el impacto operativo/regulatorio.
    4. "impacto_mercado": "Alto", "Medio" o "Bajo".
    5. "actores_mencionados": Lista con nombres exactos de empresas u organismos citados (ej. ["Acenor", "CNE", "Coordinador Eléctrico", "Enel"]).
    6. "sentimiento": "Positivo", "Neutro", o "Riesgo/Negativo".
    """

    for model_name in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]:
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
        except Exception:
            continue

    return None

def ejecutar_agente():
    inicializar_bd()
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # FASE 1: RE-ANALIZAR REGISTROS GENÉRICOS ANTIGUOS
    cursor.execute("SELECT id, fuente, titulo, url, resumen_ejecutivo FROM noticias WHERE actores_mencionados = '[]' OR actores_mencionados = '\"[]\"'")
    filas_a_reparar = cursor.fetchall()
    
    if filas_a_reparar:
        print(f"🛠️ Re-analizando {len(filas_a_reparar)} registros genéricos...")
        for row_id, fuente, titulo, url, resumen_db in filas_a_reparar:
            analisis = analizar_con_llm(titulo, resumen_db, fuente)
            if analisis and analisis.get("es_relevante", True):
                actores_list = analisis.get("actores_mencionados", [])
                if not isinstance(actores_list, list):
                    actores_list = [str(actores_list)] if actores_list else []
                    
                cursor.execute('''
                    UPDATE noticias 
                    SET categoria = ?, resumen_ejecutivo = ?, impacto_mercado = ?, actores_mencionados = ?, sentimiento = ?
                    WHERE id = ?
                ''', (
                    analisis.get("categoria", "Regulación/Normativa"),
                    analisis.get("resumen_ejecutivo", titulo),
                    analisis.get("impacto_mercado", "Medio"),
                    json.dumps(actores_list, ensure_ascii=False),
                    analisis.get("sentimiento", "Neutro"),
                    row_id
                ))
                conn.commit()

    # FASE 2: PROCESAR PUBLICACIONES NUEVAS
    noticias = obtener_noticias()
    print(f"\n📰 Total de noticias únicas recuperadas: {len(noticias)}")

    noticias_guardadas = 0
    for item in noticias:
        url_oficial = item["url"]
        
        cursor.execute("SELECT id FROM noticias WHERE url = ?", (url_oficial,))
        if cursor.fetchone():
            continue
            
        print(f"🧠 Analizando noticia nueva: {item['titulo'][:50]}...")
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
            
    conn.close()
    print(f"\n🚀 Proceso finalizado. {noticias_guardadas} noticias agregadas a {DB_NAME}.")

if __name__ == "__main__":
    ejecutar_agente()
