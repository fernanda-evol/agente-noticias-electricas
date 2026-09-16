import requests
import feedparser
from bs4 import BeautifulSoup
import sqlite3
import json
import os
import re
from datetime import datetime, timedelta, timezone
from google import genai

FUENTES = [
    {
        "nombre": "Revista EI", 
        "url_api": "https://www.revistaei.cl/wp-json/wp/v2/posts?per_page=50",
        "url_rss": "https://www.revistaei.cl/feed/"
    },
    {
        "nombre": "ElectroMinería", 
        "url_api": "https://www.electromineria.cl/wp-json/wp/v2/posts?per_page=50",
        "url_rss": "https://www.electromineria.cl/feed/"
    }
]

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
    # Limpieza de publicaciones de empleo y URLs mal formadas
    cursor.execute("DELETE FROM noticias WHERE LOWER(titulo) LIKE '%ofertas de empleo%' OR LOWER(titulo) LIKE '%vacantes%'")
    cursor.execute("DELETE FROM noticias WHERE url NOT LIKE 'http%'")
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

def obtener_noticias_hibridas():
    noticias_map = {}
    hace_30_dias = datetime.now(timezone.utc) - timedelta(days=30)
    
    for fuente in FUENTES:
        print(f"📡 Consultando fuente: {fuente['nombre']}...")
        
        # 1. Consulta REST API
        try:
            resp = requests.get(fuente["url_api"], headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                posts = resp.json()
                if isinstance(posts, list):
                    for post in posts:
                        titulo = limpiar_html(post.get("title", {}).get("rendered", ""))
                        url_real = post.get("link", "").strip()
                        
                        if es_oferta_empleo(titulo, "") or not url_real:
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
                        
                        if titulo and url_real not in noticias_map:
                            noticias_map[url_real] = {
                                "fuente": fuente["nombre"],
                                "titulo": titulo,
                                "url": url_real,
                                "fecha": fecha_dt.strftime('%Y-%m-%d %H:%M:%S'),
                                "texto": texto_limpio
                            }
        except Exception as e:
            print(f"   ⚠️ Error en API WP para {fuente['nombre']}: {e}")

        # 2. Respaldo RSS
        try:
            resp_rss = requests.get(fuente["url_rss"], headers=HEADERS, timeout=15)
            if resp_rss.status_code == 200:
                feed = feedparser.parse(resp_rss.content)
                for entry in feed.entries:
                    titulo = getattr(entry, 'title', '').strip()
                    url_real = getattr(entry, 'link', '').strip()

                    if es_oferta_empleo(titulo, "") or not url_real:
                        continue

                    if url_real not in noticias_map:
                        parsed_time = getattr(entry, 'published_parsed', None)
                        fecha_dt = datetime.now(timezone.utc)
                        if parsed_time:
                            fecha_dt = datetime.fromtimestamp(requests.utils.calendar.timegm(parsed_time), tz=timezone.utc)

                        if fecha_dt < hace_30_dias:
                            continue

                        content_raw = ""
                        if "content" in entry and len(entry.content) > 0:
                            content_raw = entry.content[0].value
                        elif "summary" in entry:
                            content_raw = entry.summary

                        noticias_map[url_real] = {
                            "fuente": fuente["nombre"],
                            "titulo": titulo,
                            "url": url_real,
                            "fecha": fecha_dt.strftime('%Y-%m-%d %H:%M:%S'),
                            "texto": limpiar_html(content_raw)
                        }
        except Exception as e:
            print(f"   ⚠️ Error en RSS para {fuente['nombre']}: {e}")

    return list(noticias_map.values())

def analizar_con_llm(titulo, texto, fuente):
    if es_oferta_empleo(titulo, texto):
        return {"es_relevante": False}

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("❌ Error: Variable GEMINI_API_KEY no encontrada.")
        return None

    client = genai.Client(api_key=api_key)
    
    prompt = f"""
    Eres un analista experto en el mercado eléctrico chileno (CNE, Coordinador Eléctrico, BESS, Transmisión, Regulación, PMGD, Precios Spot/Barra).
    
    Analiza el siguiente artículo publicado en {fuente}:
    
    Título: {titulo}
    Texto: {texto[:2500]}
    
    INSTRUCCIÓN DE FILTRADO: Si la noticia es una oferta de empleo, anuncio de trabajo o no contiene información de mercado eléctrico, responde exactamente:
    {{"es_relevante": false}}

    De lo contrario, responde en JSON estricto con las siguientes llaves:
    1. "es_relevante": true
    2. "categoria": Elige entre ["Transmisión", "Almacenamiento (BESS)", "Generación/ERNC", "Regulación/Normativa", "Mercado Mayorista/Precios", "PMGD/Distribución", "Hidrógeno Verde/Descarbonización"].
    3. "resumen_ejecutivo": Un párrafo técnico de 2-3 oraciones sintetizando el impacto.
    4. "impacto_mercado": "Alto", "Medio", o "Bajo".
    5. "actores_mencionados": Lista con nombres de empresas, instituciones o reguladores mencionados.
    6. "sentimiento": "Positivo", "Neutro", o "Riesgo/Negativo".

    Responde ÚNICAMENTE con el objeto JSON.
    """
    
    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"❌ Error Gemini para '{titulo[:30]}': {e}")
        return None

def ejecutar_agente():
    inicializar_bd()
    noticias = obtener_noticias_hibridas()
    print(f"\n📰 Total de noticias únicas recuperadas para procesar: {len(noticias)}")
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    noticias_guardadas = 0
    for item in noticias:
        url_actual = item["url"]
        cursor.execute("SELECT id FROM noticias WHERE url = ?", (url_actual,))
        if cursor.fetchone():
            continue
            
        print(f"🧠 Analizando con Gemini: {item['titulo'][:50]}...")
        analisis = analizar_con_llm(item["titulo"], item["texto"], item["fuente"])
        
        if analisis and analisis.get("es_relevante", True):
            cursor.execute('''
                INSERT INTO noticias 
                (fuente, titulo, url, fecha_publicacion, categoria, resumen_ejecutivo, impacto_mercado, actores_mencionados, sentimiento)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                item["fuente"],
                item["titulo"],
                url_actual,
                item["fecha"],
                analisis.get("categoria", "Sin Categoría"),
                analisis.get("resumen_ejecutivo", ""),
                analisis.get("impacto_mercado", "Bajo"),
                json.dumps(analisis.get("actores_mencionados", []), ensure_ascii=False),
                analisis.get("sentimiento", "Neutro")
            ))
            conn.commit()
            noticias_guardadas += 1
            print(f"✅ Guardada en BD.")
            
    conn.close()
    print(f"\n🚀 Proceso finalizado. {noticias_guardadas} noticias agregadas a {DB_NAME}.")

if __name__ == "__main__":
    ejecutar_agente()
