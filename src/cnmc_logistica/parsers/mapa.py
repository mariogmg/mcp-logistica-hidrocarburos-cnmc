"""Parser de la portada y de los mapas por comunidad autónoma."""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from ..portal import id_de_enlace
from .lineas import limpiar


@dataclass
class InstalacionMapa:
    id_tanque: int
    titulo: str
    icono: str
    tipo: str  # "almacenamiento" | "aeroportuaria" | "desconocido"


def comunidades_de_portada(html: str) -> list[str]:
    """Códigos de comunidad enlazados desde la portada (no se fijan en el código)."""
    sopa = BeautifulSoup(html, "lxml")
    codigos = []
    # Los enlaces de la portada son <area> de un mapa de imagen, no <a>.
    for a in sopa.find_all(href=True):
        m = re.search(r"instalaciones_comunidad\.htm\?idcomunidad=(\w+)", a["href"])
        if m and m.group(1) not in codigos:
            codigos.append(m.group(1))
    return codigos


def tipo_de_icono(icono: str) -> str:
    """El tipo no viene como texto: solo lo indica el icono del mapa.

    «N_aer.png» → aeroportuaria; «N.png» → almacenamiento (N es el color del
    operador). Cualquier otro nombre se marca como desconocido, sin adivinar.
    """
    nombre = icono.rsplit("/", 1)[-1].lower()
    if re.fullmatch(r"\d+_aer\.png", nombre):
        return "aeroportuaria"
    if re.fullmatch(r"\d+\.png", nombre):
        return "almacenamiento"
    return "desconocido"


def parsear_mapa(html: str) -> list[InstalacionMapa]:
    sopa = BeautifulSoup(html, "lxml")
    salida: dict[int, InstalacionMapa] = {}
    for a in sopa.find_all("a", href=True):
        if "instalacion.htm" not in a["href"]:
            continue
        id_tanque = id_de_enlace(a["href"], "idTanque")
        img = a.find("img")
        if id_tanque is None or img is None:
            continue
        icono = img.get("src", "")
        titulo = limpiar(img.get("title") or img.get("alt") or "")
        salida[id_tanque] = InstalacionMapa(id_tanque, titulo, icono, tipo_de_icono(icono))
    return list(salida.values())
