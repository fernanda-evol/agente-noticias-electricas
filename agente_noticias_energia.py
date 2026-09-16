import requests
import sqlite3
import json
import os
import re
import urllib3
from datetime import datetime, timezone
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DB_NAME = "noticias_energia.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/json,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

def inicializar_bd():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # RESET=FALSE: NUNCA BORRAMOS LA TABLA. El historial se respeta siempre.
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
    if "acenor" in contenido: actores.append("Acenor")
    if "cne" in contenido: actores.append("CNE")
    if "coordinador" in contenido: actores.append("Coordinador Eléctrico")
    if "enel" in contenido: actores.append("Enel")
    if "colbún" in contenido or "colbun" in contenido: actores.append("Colbún")

    return {
        "categoria": cat,
        "resumen_ejecutivo": (texto[:220] + "...") if len(texto) > 50 else titulo,
        "impacto_mercado": impacto,
        "actores_mencionados": actores,
        "sentimiento": "Neutro"
    }

def obtener_noticias():
    noticias = []
    
    # 1. Revista EI (API WP)
    try:
        resp = requests.get("https://www.revistaei.cl/wp-json/wp/v2/posts?per_page=30", headers=HEADERS, timeout=15, verify=False)
        if resp.status_code == 200:
            for post in resp.json():
                titulo = limpiar_html(post.get("title", {}).get("rendered", ""))
                url = post.get("link", "").strip()
                texto = limpiar_html(post.get("content", {}).get("rendered", ""))
                if titulo and url:
                    noticias.append({"fuente": "Revista EI", "titulo": titulo, "url": url, "texto": texto, "fecha": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')})
    except Exception as e:
        print(f"Error Revista EI: {e}")

    # 2. ElectroMinería (API WP)
    try:
        resp = requests.get("https://electromineria.cl/wp-json/wp/v2/posts?per_page=30", headers=HEADERS, timeout=15, verify=False)
        if resp.status_code == 200:
            for post in resp.json():
                titulo = limpiar_html(post.get("title", {}).get("rendered", ""))
                url = post.get("link", "").strip()
                texto = limpiar_html(post.get("content", {}).get("rendered", ""))
                if titulo and url:
                    noticias.append({"fuente": "ElectroMinería", "titulo": titulo, "url": url, "texto": texto, "fecha": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')})
    except Exception as e:
        print(f"Error ElectroMinería: {e}")

    return noticias

def ejecutar_agente():
    inicializar_bd()
    noticias = obtener_noticias()
    print(f"Total de noticias obtenidas: {len(noticias)}")
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    guardadas = 0
    for item in noticias:
        cursor.execute("SELECT id FROM noticias WHERE url = ?", (item["url"],))
        if cursor.fetchone():
            continue
            
        analisis = clasificar_localmente(item["titulo"], item["texto"])
        
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
    print(f"Proceso finalizado. {guardadas} nuevas noticias guardadas.")

if __name__ == "__main__":
    ejecutar_agente()
