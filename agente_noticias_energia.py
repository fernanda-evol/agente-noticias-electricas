import requests
import feedparser
from bs4 import BeautifulSoup
import sqlite3
import json
import os
import re
import calendar
import time
import urllib3
from datetime import datetime, timedelta, timezone
from google import genai

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FUENTES = [
    {
        "nombre": "Revista EI", 
        "url_api": "https://www.revistaei.cl/wp-json/wp/v2/posts?per_page=30",
        "url_rss": "https://www.revistaei.cl/feed/"
    },
    {
        "nombre": "ElectroMinería", 
        "url_api": "https://electromineria.cl/wp-json/wp/v2/posts?per_page=30",
        "url_rss": "https://electromineria.cl/category/panorama-energetico/feed/"
    }
]

DB_NAME = "noticias_energia.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/json,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

PALABRAS_EXCLUIR_EMPLEO = [
    "ofertas de empleo", "oferta de empleo", "vacantes disponibles", 
    "vacantes", "reclutamiento", "oferta laboral", "ofertas laborales", 
    "trabaje con nosotros", "postulaciones abiertas", "buscamos profesional", 
    "bolsa de trabajo", "oportunidad laboral"
]

def inicializar_bd(reset=True):
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

def clasificar_localmente(titulo, texto):
    contenido = f"{titulo} {texto}".lower()
    
    # Categoría
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

    # Impacto
    impacto = "Medio"
    if any(k in contenido for k in ["cne", "coordinador eléctrico", "decreto", "ley", "resolución", "vertimiento", "insolvencia", "licitación", "mw", "us$"]):
        impacto = "Alto"
    elif any(k in contenido for k in ["nombramiento", "premio", "evento", "reconocimiento", "aniversario"]):
        impacto = "Bajo"

    # Actores
    actores = []
    if "acenor" in contenido: actores.append("Acenor")
    if "cne" in contenido: actores.append("CNE")
    if "coordinador" in contenido: actores.append("Coordinador Eléctrico")
    if "enel" in contenido: actores.append("Enel")
    if "colbún" in contenido or "colbun" in contenido: actores.append("Colbún")

    return {
        "es_relevante": True,
        "categoria": cat,
        "resumen_ejecutivo": (texto[:220] + "...") if len(texto) > 50 else titulo,
        "impacto_mercado": impacto,
        "actores_mencionados": actores,
        "sentimiento": "Neutro"
    }

def obtener_noticias():
    noticias_map = {}
    hace_30_dias = datetime.now(timezone.utc) - timedelta(days=30)
    
    for fuente in FUENTES:
        print(f"📡 Consultando fuente: {fuente['nombre']}...")
        
        # 1. RSS Feed
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

        # 2. API WP
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

    return list(noticias_map.values())

def analizar_con_llm(titulo, texto, fuente):
    if es_oferta_empleo(titulo, texto):
        return {"es_relevante": False}

    fallback_local = clasificar_localmente(titulo, texto)
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return fallback_local

    client = genai.Client(api_key=api_key)
    
    prompt = f"""
    Eres un analista experto en el mercado eléctrico chileno.
    Analiza la noticia publicada en {fuente}:
    
    Título: {titulo}
    Texto: {texto[:2500]}
    
    SI ES OFERTA DE EMPLEO O PUBLICIDAD, RESPONDE STRICTAMENTE: {{"es_relevante": false}}

    CLASIFICACIÓN OBLIGATORIA DE "impacto_mercado":
    - "Alto": Proyectos >US$ 50M o >100 MW (BESS, Transmisión, ERNC), decretos/resoluciones de CNE/Coordinador Eléctrico/Ministerio, alzas tarifarias, vertimientos masivos o insolvencias.
    - "Medio": Declaraciones/estudios de gremios (Acenor, Generadoras, ACERA), proyectos PMGD, hitos de obra, contratos PPA, aprobaciones ambientales.
    - "Bajo": Nombramientos corporativos, eventos, ferias, premiaciones, RSE.

    Responde ÚNICAMENTE en JSON estricto con la siguiente estructura:
    1. "es_relevante": true
    2. "categoria": Selección estricta entre ["Transmisión", "Almacenamiento (BESS)", "Generación/ERNC", "Regulación/Normativa", "Mercado Mayorista/Precios", "PMGD/Distribución", "Hidrógeno Verde/Descarbonización"].
    3. "resumen_ejecutivo": Párrafo técnico de 2-3 oraciones sintetizando el impacto real.
    4. "impacto_mercado": Evalúa si es "Alto", "Medio" o "Bajo".
    5. "actores_mencionados": Lista con nombres exactos de empresas u organismos citados (ej. ["Acenor", "CNE", "Coordinador Eléctrico"]).
    6. "sentimiento": Elige entre ["Positivo", "Neutro", "Riesgo/Negativo"].
    """

    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
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
        print(f"⚠️ Limit de cuota Gemini o Error. Usando fallback inteligente local: {e}")

    return fallback_local

def ejecutar_agente():
    inicializar_bd(reset=True)
    
    noticias = obtener_noticias()
    print(f"\n📰 Total de noticias únicas recuperadas para procesar: {len(noticias)}")
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    noticias_guardadas = 0
    for idx, item in enumerate(noticias):
        url_oficial = item["url"]
        
        cursor.execute("SELECT id FROM noticias WHERE url = ?", (url_oficial,))
        if cursor.fetchone():
            continue
            
        print(f"🧠 [{idx+1}/{len(noticias)}] Analizando ({item['fuente']}): {item['titulo'][:40]}...")
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
                analisis.get("categoria", "Generación/ERNC"),
                analisis.get("resumen_ejecutivo", item["titulo"]),
                analisis.get("impacto_mercado", "Medio"),
                json.dumps(actores_list, ensure_ascii=False),
                analisis.get("sentimiento", "Neutro")
            ))
            conn.commit()
            noticias_guardadas += 1
            print(f"✅ Guardada -> Cat: {analisis.get('categoria')} | Impacto: {analisis.get('impacto_mercado')}")
            
        time.sleep(3.5)

    conn.close()
    print(f"\n🚀 Proceso finalizado. {noticias_guardadas} noticias agregadas a {DB_NAME}.")

if __name__ == "__main__":
    ejecutar_agente()
