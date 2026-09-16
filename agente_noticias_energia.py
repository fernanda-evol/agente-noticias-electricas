import requests
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
        "url_api": "https://www.revistaei.cl/wp-json/wp/v2/posts"
    },
    {
        "nombre": "ElectroMinería", 
        "url_api": "https://www.electromineria.cl/wp-json/wp/v2/posts"
    }
]

DB_NAME = "noticias_energia.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json"
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

def obtener_noticias_wp_api():
    noticias = []
    hace_30_dias = datetime.now(timezone.utc) - timedelta(days=30)
    
    # Consultamos tanto entradas estándar como tipos de contenido adicionales
    endpoints = ["posts", "noticias", "reportajes"]
    
    for fuente in FUENTES:
        print(f"📡 Consultando API de {fuente['nombre']}...")
        base_url = fuente["url_api"].rsplit("/posts", 1)[0]
        urls_procesadas_en_ejecucion = set()
        
        for ep in endpoints:
            url_consulta = f"{base_url}/{ep}?per_page=30"
            try:
                resp = requests.get(url_consulta, headers=HEADERS, timeout=15)
                if resp.status_code == 200:
                    posts = resp.json()
                    if isinstance(posts, list):
                        print(f"   Subsección '{ep}': {len(posts)} artículos encontrados.")
                        for post in posts:
                            titulo = limpiar_html(post.get("title", {}).get("rendered", ""))
                            # Normalización de URL para evitar duplicados por '/' final
                            url = post.get("link", "").strip().rstrip("/")
                            
                            if not url or url in urls_procesadas_en_ejecucion:
                                continue
                            urls_procesadas_en_ejecucion.add(url)
                            
                            date_str = post.get("date_gmt", "") or post.get("date", "")
                            
                            # Parser flexible de fecha ISO
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
                                
                            content_raw = post.get("content", {}).get("rendered", "") or post.get("excerpt", {}).get("rendered", "")
                            texto_limpio = limpiar_html(content_raw)
                            
                            if titulo and url:
                                noticias.append({
                                    "fuente": fuente["nombre"],
                                    "titulo": titulo,
                                    "url": url,
                                    "fecha": fecha_dt.strftime('%Y-%m-%d %H:%M:%S'),
                                    "texto": texto_limpio
                                })
            except Exception as e:
                print(f"   ⚠️ No se pudo consultar '{ep}' en {fuente['nombre']}: {e}")
                
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
    noticias = obtener_noticias_wp_api()
    print(f"\n📰 Total de noticias recuperadas para procesar: {len(noticias)}")
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    noticias_guardadas = 0
    for item in noticias:
        # Consulta flexible que revisa la URL tanto con como sin '/' final
        cursor.execute("SELECT id FROM noticias WHERE url = ? OR url = ?", (item["url"], item["url"] + "/"))
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
