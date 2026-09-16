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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DB_NAME = "noticias_energia.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
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

def clasificar_inteligente(titulo, texto):
    contenido = f"{titulo} {texto}".lower()
    
    # Categorías técnicas
    cat = "Generación/ERNC"
    if any(k in contenido for k in ["bess", "almacenamiento", "batería", "baterias"]):
        cat = "Almacenamiento (BESS)"
    elif any(k in contenido for k in ["transmisión", "transmision", "línea", "subestación", "subestacion"]):
        cat = "Transmisión"
    elif any(k in contenido for k in ["cne", "coordinador", "regulación", "norma", "ley", "decreto", "tarifas", "comisión nacional de energía"]):
        cat = "Regulación/Normativa"
    elif any(k in contenido for k in ["pmgd", "distribución", "distribucion"]):
        cat = "PMGD/Distribución"
    elif any(k in contenido for k in ["hidrógeno", "hidrogeno", "descarbonización", "descarbonizacion"]):
        cat = "Hidrógeno Verde/Descarbonización"
    elif any(k in contenido for k in ["precio", "spot", "cmg", "costo marginal", "mayorista"]):
        cat = "Mercado Mayorista/Precios"

    # Niveles de impacto con criterios objetivos
    impacto = "Medio"
    if any(k in contenido for k in ["cne", "coordinador eléctrico", "decreto", "ley", "resolución", "vertimiento", "insolvencia", "licitación", "mw", "us$", "millones"]):
        impacto = "Alto"
    elif any(k in contenido for k in ["nombramiento", "premio", "evento", "reconocimiento", "aniversario", "feria"]):
        impacto = "Bajo"

    # Actores clave del mercado chileno
    actores = []
    if "acenor" in contenido: actores.append("Acenor")
    if "cne" in contenido: actores.append("CNE")
    if "coordinador" in contenido: actores.append("Coordinador Eléctrico")
    if "enel" in contenido: actores.append("Enel")
    if "colbún" in contenido or "colbun" in contenido: actores.append("Colbún")
    if "transelec" in contenido: actores.append("Transelec")
    if "cge" in contenido: actores.append("CGE")
    if "sec" in contenido: actores.append("SEC")

    return {
        "es_relevante": True,
        "categoria": cat,
        "resumen_ejecutivo": (texto[:250] + "...") if len(texto) > 50 else titulo,
        "impacto_mercado": impacto,
        "actores_mencionados": actores,
        "sentimiento": "Neutro"
    }

def obtener_noticias():
    noticias_map = {}
    hace_30_dias = datetime.now(timezone.utc) - timedelta(days=30)
    
    # 1. Extracción ElectroMinería (API REST WordPress + Scraper de sección)
    print("📡 Consultando fuente: ElectroMinería...")
    try:
        resp_em = requests.get("https://electromineria.cl/wp-json/wp/v2/posts?per_page=30", headers=HEADERS, timeout=15, verify=False)
        if resp_em.status_code == 200:
            posts_em = resp_em.json()
            if isinstance(posts_em, list):
                print(f"   [API WP] {len(posts_em)} artículos obtenidos de ElectroMinería.")
                for post in posts_em:
                    titulo = limpiar_html(post.get("title", {}).get("rendered", ""))
                    url_oficial = normalizar_url(post.get("link", ""))
                    if not url_oficial or es_oferta_empleo(titulo, ""):
                        continue
                    texto_limpio = limpiar_html(post.get("content", {}).get("rendered", ""))
                    if titulo and url_oficial not in noticias_map:
                        noticias_map[url_oficial] = {
                            "fuente": "ElectroMinería",
                            "titulo": titulo,
                            "url": url_oficial,
                            "fecha": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                            "texto": texto_limpio
                        }
    except Exception as e:
        print(f"   ⚠️ Error API ElectroMinería: {e}")

    # Scraper de respaldo directo para la sección Panorama Energético de ElectroMinería
    try:
        url_sec = "https://electromineria.cl/category/panorama-energetico/"
        resp_sec = requests.get(url_sec, headers=HEADERS, timeout=15, verify=False)
        if resp_sec.status_code == 200:
            soup = BeautifulSoup(resp_sec.content, "html.parser")
            for a_tag in soup.find_all("a", href=True):
                href = normalizar_url(a_tag["href"])
                titulo = limpiar_html(a_tag.get_text())
                if href.startswith("https://electromineria.cl/") and not any(x in href for x in ["/category/", "/tag/", "/page/", "/author/", "#", "feed"]):
                    if len(titulo) > 20 and not es_oferta_empleo(titulo, ""):
                        if href not in noticias_map:
                            noticias_map[href] = {
                                "fuente": "ElectroMinería",
                                "titulo": titulo,
                                "url": href,
                                "fecha": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                                "texto": titulo
                            }
            print(f"   [Scraper HTML] Artículos totales de ElectroMinería actualizados.")
    except Exception as e:
        print(f"   ⚠️ Error Scraper ElectroMinería: {e}")

    # 2. Extracción Revista EI (API WP + RSS)
    print("📡 Consultando fuente: Revista EI...")
    try:
        resp_ei = requests.get("https://www.revistaei.cl/wp-json/wp/v2/posts?per_page=30", headers=HEADERS, timeout=15, verify=False)
        if resp_ei.status_code == 200:
            posts_ei = resp_ei.json()
            if isinstance(posts_ei, list):
                print(f"   [API WP] {len(posts_ei)} artículos obtenidos de Revista EI.")
                for post in posts_ei:
                    titulo = limpiar_html(post.get("title", {}).get("rendered", ""))
                    url_oficial = normalizar_url(post.get("link", ""))
                    if not url_oficial or es_oferta_empleo(titulo, ""):
                        continue
                    texto_limpio = limpiar_html(post.get("content", {}).get("rendered", ""))
                    if titulo and url_oficial not in noticias_map:
                        noticias_map[url_oficial] = {
                            "fuente": "Revista EI",
                            "titulo": titulo,
                            "url": url_oficial,
                            "fecha": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                            "texto": texto_limpio
                        }
    except Exception as e:
        print(f"   ⚠️ Error Revista EI: {e}")

    try:
        resp_rss = requests.get("https://www.revistaei.cl/feed/", headers=HEADERS, timeout=15, verify=False)
        if resp_rss.status_code == 200:
            feed = feedparser.parse(resp_rss.content)
            for entry in feed.entries:
                titulo = getattr(entry, 'title', '').strip()
                url_oficial = normalizar_url(getattr(entry, 'link', ''))
                if not url_oficial or es_oferta_empleo(titulo, ""):
                    continue
                if url_oficial not in noticias_map:
                    content_raw = entry.content[0].value if "content" in entry and len(entry.content) > 0 else getattr(entry, 'summary', '')
                    noticias_map[url_oficial] = {
                        "fuente": "Revista EI",
                        "titulo": titulo,
                        "url": url_oficial,
                        "fecha": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                        "texto": limpiar_html(content_raw)
                    }
    except Exception as e:
        print(f"   ⚠️ Error RSS Revista EI: {e}")

    return list(noticias_map.values())

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
            
        print(f"🧠 [{idx+1}/{len(noticias)}] Procesando ({item['fuente']}): {item['titulo'][:40]}...")
        analisis = clasificar_inteligente(item["titulo"], item["texto"])
        
        if analisis and analisis.get("es_relevante", True):
            actores_list = analisis.get("actores_mencionados", [])
            
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
            print(f"✅ Guardada -> Fuente: {item['fuente']} | Cat: {analisis.get('categoria')} | Impacto: {analisis.get('impacto_mercado')}")

    conn.close()
    print(f"\n🚀 Proceso finalizado. {noticias_guardadas} noticias agregadas a {DB_NAME}.")

if __name__ == "__main__":
    ejecutar_agente()
