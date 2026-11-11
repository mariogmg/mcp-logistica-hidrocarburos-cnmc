"""Convierte una página en una lista de líneas de texto.

Los parsers no navegan por clases CSS: recorren estas líneas y se guían por
las etiquetas visibles («PRODUCTOS:», «Incluye:», «CONDICIONES»…). Si la CNMC
cambia la maquetación pero no el texto, siguen funcionando.

Una línea termina en cada elemento de bloque y en cada <br>. De cada línea se
conserva si está dentro de un <li>, si ese <li> está sangrado (subapartado) y
los enlaces que contiene.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

_BLOQUE = {
    "address", "blockquote", "body", "center", "dd", "div", "dl", "dt", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "html", "li", "ol", "option", "p", "select", "table", "tbody",
    "td", "tfoot", "th", "thead", "tr", "ul",
}  # fmt: skip
_IGNORAR = {"script", "style", "head", "noscript"}


@dataclass
class Linea:
    texto: str
    en_li: bool = False
    sangrada: bool = False
    enlaces: list[tuple[str, str]] = field(default_factory=list)  # (texto, href)


def limpiar(texto: str) -> str:
    """Colapsa espacios (incluidos los &nbsp;) y recorta."""
    return re.sub(r"\s+", " ", texto.replace("\xa0", " ")).strip()


def _sangrado(li: Tag) -> bool:
    # El portal anida <li> dentro de <li> sin <ul>; lxml puede dejarlos como
    # hermanos. Lo único que distingue un subapartado es su margen izquierdo.
    return "margin-left" in (li.get("style") or "").replace(" ", "").lower()


def lineas_de(html: str) -> list[Linea]:
    sopa = BeautifulSoup(html, "lxml")
    salida: list[Linea] = []
    trozos: list[str] = []
    enlaces: list[tuple[str, str]] = []
    pila_li: list[Tag] = []

    def cerrar() -> None:
        texto = limpiar("".join(trozos))
        if texto or enlaces:
            li = pila_li[-1] if pila_li else None
            salida.append(Linea(texto, li is not None, bool(li and _sangrado(li)), list(enlaces)))
        trozos.clear()
        enlaces.clear()

    def recorrer(nodo: Tag) -> None:
        for hijo in nodo.children:
            if isinstance(hijo, Comment):
                continue
            if isinstance(hijo, NavigableString):
                trozos.append(str(hijo))
            elif isinstance(hijo, Tag):
                nombre = hijo.name.lower()
                if nombre in _IGNORAR:
                    continue
                if nombre == "br":
                    cerrar()
                elif nombre in _BLOQUE:
                    cerrar()
                    if nombre == "li":
                        pila_li.append(hijo)
                    recorrer(hijo)
                    cerrar()
                    if nombre == "li":
                        pila_li.pop()
                else:
                    if nombre == "a" and hijo.get("href"):
                        enlaces.append((limpiar(hijo.get_text(" ")), hijo["href"]))
                    recorrer(hijo)

    recorrer(sopa)
    cerrar()
    return salida
