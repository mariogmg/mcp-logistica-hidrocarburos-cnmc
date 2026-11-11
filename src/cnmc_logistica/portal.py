"""URLs del portal y claves de recurso. Único sitio donde se construyen."""

from __future__ import annotations

import re
from urllib.parse import quote

from .almacen import Recurso
from .config import BASE_PORTAL


def portada() -> Recurso:
    return Recurso("portada", "portada", f"{BASE_PORTAL}/instalaciones_nacionales.htm")


def mapa(comunidad: str) -> Recurso:
    url = f"{BASE_PORTAL}/instalaciones_comunidad.htm?idcomunidad={quote(comunidad)}"
    return Recurso("mapa", comunidad, url)


def formulario_buscador() -> Recurso:
    return Recurso("buscador", "formulario", f"{BASE_PORTAL}/buscador.htm")


def buscador_almacenista(codigo: str, pagina: int = 1, parametro_pagina: str | None = None):
    """Resultados del buscador para un almacenista.

    La página 1 se pide sin parámetro de paginación; el nombre del parámetro
    (hoy «d-49653-p») lo genera el portal y se lee de los enlaces de esa página.
    """
    url = f"{BASE_PORTAL}/buscador.htm?comunidad=0&almacenista={quote(codigo)}"
    if pagina > 1:
        if not parametro_pagina:
            raise ValueError("falta el nombre del parámetro de paginación")
        url += f"&{parametro_pagina}={pagina}"
    slug = re.sub(r"\W+", "_", codigo).strip("_")
    return Recurso("buscador", f"almacenista_{slug}_p{pagina}", url)


def ficha(id_tanque: int) -> Recurso:
    return Recurso("ficha", str(id_tanque), f"{BASE_PORTAL}/instalacion.htm?idTanque={id_tanque}")


def detalle(id_ti: int) -> Recurso:
    url = f"{BASE_PORTAL}/detalle.htm?idTanqueInstalacion={id_ti}"
    return Recurso("detalle", str(id_ti), url)


def origen_destino(id_ti: int) -> Recurso:
    url = f"{BASE_PORTAL}/precio-producto.htm?idTanqueInstalacion={id_ti}"
    return Recurso("origen_destino", str(id_ti), url)


def pdf(directorio_hijo: int) -> Recurso:
    url = f"{BASE_PORTAL}/downloadfile.htm?directorioPadre=pdfpub&directorioHijo={directorio_hijo}"
    return Recurso("pdf", str(directorio_hijo), url)


def pdf_capacidades() -> Recurso:
    url = f"{BASE_PORTAL}/downloadfile.htm?directorioPadre=carpdfpub"
    return Recurso("pdf", "capacidades", url)


def id_de_enlace(href: str, parametro: str) -> int | None:
    """Extrae un id numérico de un enlace: id_de_enlace(h, 'idTanque')."""
    m = re.search(rf"[?&]{re.escape(parametro)}=(\d+)", href.replace("&amp;", "&"))
    return int(m.group(1)) if m else None
