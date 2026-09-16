#!/usr/bin/env python3
"""
reparar_resumenes_electromineria.py
====================================
Script de UNA SOLA VEZ. No es parte del cron diario.

Corrige las noticias de ElectroMinería que ya quedaron guardadas en
noticias_energia.db con el resumen cruzado (bug del selector de HTML que
agarraba bloques de "artículos relacionados"). Reutiliza la extracción ya
corregida de agente_noticias_energia.py, pero en vez de insertar noticias
nuevas, hace UPDATE de las que ya existen por url.

Solo repara lo que sigue apareciendo en la categoría en este momento — si
una noticia ya salió del listado (quedó vieja), no hay de dónde sacar su
bajada corregida y se deja como está.

Uso: parado en la misma carpeta que agente_noticias_energia.py y
noticias_energia.db:
    python reparar_resumenes_electromineria.py
"""

import json
import sqlite3

import requests

from agente_noticias_energia import (
    DB_NAME,
    HEADERS,
    clasificar_localmente,
    obtener_electromineria_categoria,
)


def reparar():
    session = requests.Session()
    session.headers.update({
        **HEADERS,
        "Referer": "https://electromineria.cl/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    print("Descargando categoría Panorama Energético para obtener bajadas correctas...")
    noticias = obtener_electromineria_categoria(session, limite=50)
    print(f"{len(noticias)} noticias encontradas en el listado actual.")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    reparadas = 0
    no_encontradas = 0

    for n in noticias:
        cursor.execute("SELECT id, resumen_ejecutivo FROM noticias WHERE url = ?", (n["url"],))
        fila = cursor.fetchone()
        if not fila:
            no_encontradas += 1
            continue

        noticia_id, resumen_actual = fila
        analisis = clasificar_localmente(n["titulo"], n["texto"])

        # Evita tocar filas cuyo resumen ya coincide con el título (no
        # estaban rotas) para no gastar escrituras de más.
        if resumen_actual == analisis["resumen_ejecutivo"]:
            continue

        cursor.execute(
            """
            UPDATE noticias
            SET resumen_ejecutivo = ?, categoria = ?, impacto_mercado = ?, actores_mencionados = ?
            WHERE id = ?
            """,
            (
                analisis["resumen_ejecutivo"],
                analisis["categoria"],
                analisis["impacto_mercado"],
                json.dumps(analisis["actores_mencionados"], ensure_ascii=False),
                noticia_id,
            ),
        )
        reparadas += 1
        print(f"  Reparada: {n['titulo'][:70]}")

    conn.commit()
    conn.close()

    print(f"\nListo. {reparadas} noticias reparadas. "
          f"{no_encontradas} del listado actual no estaban en la base (normal si son nuevas y aún no corrió el cron).")
    print("Ahora hacé: git add noticias_energia.db && git commit -m 'Reparar resúmenes cruzados' && git push")


if __name__ == "__main__":
    reparar()
