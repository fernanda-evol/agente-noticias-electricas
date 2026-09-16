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
    {"nombre": "Revista EI", "url_rss": "https://www.revistaei.cl/feed/"},
    {"nombre": "ElectroMinería", "url_rss": "https://www.electromineria.cl/feed/"}
]

DB_NAME = "noticias_energia.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

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
    texto = soup.get_text(separator=" ")
    return re.sub(r'\s+', ' ', texto).strip()

def obtener_items_feed(url_rss):
    """
    Intenta obtener las noticias vía API rss2json. Si falla, hace petición directa con feedparser.
    """
    api_url = f"https://api.rss2json.com/v1/api.json?rss_url={requests.utils.quote(url_rss)}"
    try:
        resp = requests.get(api_url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "ok" and "items" in data and len(data["items"]) > 0:
                print("   ✔ Recuperado vía API rss2json")
                return [
                    {
                        "title": item.get("title", "").strip(),
                        "link": item.get("link", "").strip(),
                        "pubDate": item.get("pubDate", ""),
                        "content": item.get("content") or item.get("description") or ""
                    }
                    for item in data["items"]
                ]
    except Exception as e:
        print(f"   ⚠️ rss2json falló: {e}")

    # Método de respaldo directo
    try:
        resp_direct = requests.get(url_rss, headers=HEADERS, timeout=15, allow_redirects=True)
        if resp_direct.status_code == 200:
            feed = feedparser.parse(resp_direct.content)
            print(f"   ✔ Recuperado vía feedparser directo ({len(feed.entries)} entradas)")
            items = []
            for entry in feed.entries:
                pub_date = getattr(entry, 'published', '') or getattr(entry, 'updated', '')
                content = ""
                if "content" in entry and len(entry.content) > 0:
                    content = entry.content[0].value
                elif "summary" in entry:
                    content = entry.summary
                    
                items.append({
                    "title": entry.get("title", "").strip(),
                    "link": entry.get("link", "").strip(),
                    "pubDate": pub_date,
                    "content": content
                })
            return items
    except Exception as e:
        print(f"   ❌ Error directo: {e}")

    return []

def obtener_noticias_recientes():
    noticias = []
    hace_30_dias = datetime.now(timezone.utc) - timedelta(days=30)
    
    for fuente in FUENTES:
        print(f"📡 Recuperando noticias de {fuente['nombre']} ({fuente['url_rss']})...")
        items = obtener_items_feed(fuente["url_rss"])
        print(f"   Artículos encontrados: {len(items)}")
        
        for item in items:
            titulo = item.get("title", "")
            url = item.get("link", "")
            pub_date_str = item.get("pubDate", "")
            
            fecha_dt = datetime.now(timezone.utc)
            if pub_date_str:
                for fmt in ["%Y-%m-%d %H:%M:%S", "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"]:
                    try:
                        fecha_dt = datetime.strptime(pub_date_str, fmt)
                        if fecha_dt.tzinfo is None:
                            fecha_dt = fecha_dt.replace(tzinfo=timezone.utc)
                        break
                    except:
                        pass
                
            if fecha_dt < hace_30_dias:
                continue
                
            texto_limpio = limpiar_html(item.get("content", ""))
            
            if titulo and url:
                noticias.append({
                    "fuente": fuente["nombre"],
                    "titulo": titulo,
                    "url": url,
                    "fecha": fecha_dt.strftime('%Y-%m-%d %H:%M:%S'),
                    "texto": texto_limpio
                })
                
    return noticias

def analizar_con_llm(titulo, texto, fuente):
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
    
    Genera un análisis en formato JSON estricto con las siguientes llaves:
    1. "categoria": Elige la más precisa entre ["Transmisión", "Almacenamiento (BESS)", "Generación/ERNC", "Regulación/Normativa", "Mercado Mayorista/Precios", "PMGD/Distribución", "Hidrógeno Verde/Descarbonización"].
    2. "resumen_ejecutivo": Un párrafo técnico de 2-3 oraciones sintetizando el impacto.
    3. "impacto_mercado": "Alto", "Medio", o "Bajo".
    4. "actores_mencionados": Lista con nombres de empresas, instituciones o reguladores mencionados.
    5. "sentimiento": "Positivo", "Neutro", o "Riesgo/Negativo".

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
    noticias = obtener_noticias_recientes()
    print(f"\n📰 Total de noticias recuperadas para procesar: {len(noticias)}")
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    noticias_guardadas = 0
    for item in noticias:
        cursor.execute("SELECT id FROM noticias WHERE url = ?", (item["url"],))
        if cursor.fetchone():
            print(f"⏭️ Ya existe en BD: {item['titulo'][:40]}...")
            continue
            
        print(f"🧠 Analizando con Gemini: {item['titulo'][:50]}...")
        analisis = analizar_con_llm(item["titulo"], item["texto"], item["fuente"])
        
        if analisis:
            cursor.execute('''
                INSERT INTO noticias 
                (fuente, titulo, url, fecha_publicacion, categoria, resumen_ejecutivo, impacto_mercado, actores_mencionados, sentimiento)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                item["fuente"],
                item["titulo"],
                item["url"],
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
