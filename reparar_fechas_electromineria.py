#!/usr/bin/env python3
"""
reparar_fechas_electromineria.py
=================================
Script de UNA SOLA VEZ. No es parte del cron diario.

Corrige fechas de noticias de ElectroMinería que quedaron mal guardadas por
fallas de acceso ANTERIORES al fix que hace que el agente omita (en vez de
inventar una fecha) cuando no puede confirmar la fecha real de un artículo.

A diferencia de reparar_resumenes_electromineria.py, este SÍ puede revisar
todo el historial (no solo lo que aparece hoy en el listado de la
categoría), porque solo necesita volver a visitar la página de cada
artículo — no depende de que siga apareciendo en el listado.

Uso: parado en la misma carpeta que agente_noticias_energia.py y
noticias_energia.db:
    python reparar_fechas_electromineria.py
"""

import sqlite3

import requests

from agente_noticias_energia import (
    DB_NAME,
    HEADERS,
    obtener_texto_y_fecha_articulo,
)


def reparar():
    session = requests.Session()
    session.headers.update({
        **HEADERS,
        "Referer": "https://electromineria.cl/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, url, titulo, fecha_publicacion FROM noticias WHERE fuente = 'ElectroMinería'")
    filas = cursor.fetchall()
    print(f"{len(filas)} noticias de ElectroMinería a revisar.")

    reparadas = 0
    sin_confirmar = 0

    for noticia_id, url, titulo, fecha_actual in filas:
        fecha_real = obtener_texto_y_fecha_articulo(session, url)
        if fecha_real is None:
            print(f"  No se pudo confirmar la fecha de \"{titulo[:60]}\" ahora mismo — se deja como está.")
            sin_confirmar += 1
            continue
        if fecha_real != fecha_actual:
            cursor.execute(
                "UPDATE noticias SET fecha_publicacion = ? WHERE id = ?",
                (fecha_real, noticia_id),
            )
            reparadas += 1
            print(f"  Corregida: \"{titulo[:60]}\" -> {fecha_actual} => {fecha_real}")

    conn.commit()
    conn.close()

    print(f"\nListo. {reparadas} fechas corregidas. "
          f"{sin_confirmar} no se pudieron confirmar en este momento (se pueden reintentar corriendo el script de nuevo).")
    print("Ahora hacé: git add noticias_energia.db && git commit -m 'Reparar fechas de ElectroMineria' && git push")


if __name__ == "__main__":
    reparar()
