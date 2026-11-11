"""Parser de buscador.htm: desplegables y tabla de resultados paginada."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ..portal import id_de_enlace
from .lineas import limpiar


@dataclass
class Formulario:
    almacenistas: dict[str, str]  # código → nombre
    comunidades: dict[str, str]  # código → nombre


@dataclass
class FilaBuscador:
    id_tanque_instalacion: int
    comunidad: str  # nombre, tal como lo muestra la tabla
    instalacion: str
    producto: str


@dataclass
class PaginaBuscador:
    filas: list[FilaBuscador] = field(default_factory=list)
    total: int | None = None  # filas que anuncia el portal para toda la búsqueda
    paginas: int = 1
    parametro_pagina: str | None = None  # p. ej. «d-49653-p»


def _opciones(sopa: BeautifulSoup, nombre: str) -> dict[str, str]:
    select = sopa.find("select", attrs={"name": nombre})
    if select is None:
        return {}
    salida = {}
    for op in select.find_all("option"):
        valor = (op.get("value") or "").strip()
        if valor and valor != "0":
            salida[valor] = limpiar(op.get_text())
    return salida


def parsear_formulario(html: str) -> Formulario:
    sopa = BeautifulSoup(html, "lxml")
    return Formulario(_opciones(sopa, "almacenista"), _opciones(sopa, "comunidad"))


def parsear_resultados(html: str) -> PaginaBuscador:
    sopa = BeautifulSoup(html, "lxml")
    pagina = PaginaBuscador()
    for tr in sopa.find_all("tr"):
        celdas = tr.find_all("td", recursive=False)
        enlace = tr.find("a", href=re.compile(r"detalle\.htm\?idTanqueInstalacion=\d+"))
        if enlace is None or len(celdas) < 4:
            continue
        pagina.filas.append(
            FilaBuscador(
                id_tanque_instalacion=id_de_enlace(enlace["href"], "idTanqueInstalacion"),
                comunidad=limpiar(celdas[0].get_text()),
                instalacion=limpiar(celdas[1].get_text()),
                producto=limpiar(celdas[2].get_text()),
            )
        )
    texto = limpiar(sopa.get_text(" "))
    if m := re.search(r"(\d+) filas encontradas", texto):
        pagina.total = int(m.group(1))
    elif re.search(r"\bUna? fila encontrada", texto, re.I):
        pagina.total = 1
    elif "No se han encontrado filas" in texto:
        pagina.total = 0
    for a in sopa.find_all("a", href=True):
        if m := re.search(r"[?&](d-\d+-p)=(\d+)", a["href"].replace("&amp;", "&")):
            pagina.parametro_pagina = m.group(1)
            pagina.paginas = max(pagina.paginas, int(m.group(2)))
    return pagina
