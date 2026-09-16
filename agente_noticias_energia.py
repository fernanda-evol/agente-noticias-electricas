import feedparser
import requests
from bs4 import BeautifulSoup
import sqlite3
import json
import os
import re
import calendar
from datetime import datetime, timedelta, timezone
import time
from google import genai

FUENTES = [
    {"nombre": "Revista EI", "url_rss": "https://www.revistaei.cl/feed/"},
    {"nombre": "ElectroMinería", "url_rss": "https://electromineria.cl/feed/"}
]

DB_NAME = "noticias_energia.db"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

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
    soup = BeautifulSoup(html_content, "html.parser")
    for script in soup(["script", "style"]):
        script.decompose()
    texto = soup.get_text(separator=" ")
    return re.sub(r'\s+', ' ', texto).strip()

def obtener_noticias_recientes_7dias():
    noticias = []
    hace_7_dias = datetime.now(timezone.utc) - timedelta(days=7)
    
    for fuente in FUENTES:
        print(f"📡 Leyendo feed de {fuente['nombre']}...")
        feed = feedparser.parse(fuente["url_rss"], agent=USER_AGENT)
        print(f"   Entradas encontradas: {len(feed.entries)}")
        
        for entry in feed.entries:
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                timestamp = calendar.timegm(entry.published_parsed)
                fecha_noticia = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            else:
                fecha_noticia = datetime.now(timezone.utc)

            if fecha_noticia < hace_7_dias:
                continue
                
            url = entry.link
            titulo = entry.title
            
            contenido_raw = ""
            if "content" in entry and len(entry.content) > 0:
                contenido_raw = entry.content[0].value
            elif "summary" in entry:
                contenido_raw = entry.summary
                
            contenido_limpio = limpiar_html(contenido_raw)
            
            noticias.append({
                "fuente": fuente["nombre"],
                "titulo": titulo,
                "url": url,
                "fecha": fecha_noticia.strftime('%Y-%m-%d %H:%M:%S'),
                "texto": contenido_limpio
            })
            
    return noticias

def analizar_con_llm(titulo, texto, fuente):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Error: No se encontró la variable GEMINI_API_KEY en el entorno.")

    client = genai.Client(api_key=api_key)
    
    prompt = f"""
    Eres un analista experto en el mercado eléctrico chileno (CNE, Coordinador Eléctrico, BESS, Transmisión, Regulación, PMGD, Precios Spot/Barra).
    
    Analiza el siguiente artículo publicado en {fuente}:
    
    Título: {titulo}
    Texto: {texto[:3000]}
    
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
            model="gemini-2.0-flash",
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"❌ Error procesando con LLM para '{titulo[:30]}': {e}")
        return None

def ejecutar_agente():
    inicializar_bd()
    noticias = obtener_noticias_recientes_7dias()
    print(f"📰 Total de noticias recuperadas (últimos 7 días): {len(noticias)}")
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    noticias_guardadas = 0
    for item in noticias:
        cursor.execute("SELECT id FROM noticias WHERE url = ?", (item["url"],))
        if cursor.fetchone():
            print(f"⏭️ Noticia ya registrada: {item['titulo'][:40]}...")
            continue
            
        print(f"🧠 Analizando: {item['titulo'][:50]}...")
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
                analisis.get("categoria", "Sin Categoria"),
                analisis.get("resumen_ejecutivo", ""),
                analisis.get("impacto_mercado", "Bajo"),
                json.dumps(analisis.get("actores_mencionados", []), ensure_ascii=False),
                analisis.get("sentimiento", "Neutro")
            ))
            conn.commit()
            noticias_guardadas += 1
            print(f"✅ Guardada correctamente.")
            
    conn.close()
    print(f"\n🚀 Pipeline completado. Se guardaron {noticias_guardadas} noticias nuevas.")

if __name__ == "__main__":
    ejecutar_agente()
