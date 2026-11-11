"""Parser de precio-producto.htm (precios origen → destino de un detalle).

Es la única página sin etiquetas de texto que separen las celdas: cada precio
es una caja con el origen y el valor, distinguibles solo por su clase CSS
(`caja_datos_titulo` / `caja_datos_texto`), bajo un título de modalidad
(`titulo_texto_detalle`). Por eso aquí sí se usan clases; los tests sobre
páginas reales avisarán si la CNMC las cambia. El destino no se identifica
por nombre sino por el idTanqueInstalacion de la URL.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ..avisos import Aviso
from .lineas import limpiar, lineas_de
from .valores import numero

_NUMERO = re.compile(r"\d[\d.]*(?:,\d+)?")


@dataclass
class FilaOD:
    modalidad: str
    origen: str
    origen_normalizado: str
    estado: str  # "ofrecida" | "no_ofrecida"
    precio: float | None
    texto_original: str


@dataclass
class OrigenDestino:
    id_tanque_instalacion: int
    destino: str | None
    unidad: str | None
    filas: list[FilaOD] = field(default_factory=list)
    notas: list[str] = field(default_factory=list)
    avisos: list[Aviso] = field(default_factory=list)


def normalizar_origen(nombre: str) -> str:
    """Mayúsculas y sin tildes; nada más. HUELVA y REFINERÍA HUELVA siguen siendo distintos."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", nombre) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", sin_tildes).strip().upper()


def parsear_origen_destino(html: str, id_ti: int) -> OrigenDestino:
    recurso = f"origen_destino/{id_ti}"
    od = OrigenDestino(id_ti, None, None)
    sopa = BeautifulSoup(html, "lxml")
    consumidos: set[str] = set()
    vistos: set[tuple[str, str]] = set()

    for titulo in sopa.find_all(class_="titulo_texto_detalle"):
        modalidad = limpiar(titulo.get_text(" "))
        consumidos.add(modalidad)
        contenedor = titulo.find_next_sibling()
        cajas = contenedor.find_all(class_="caja_datos") if contenedor else []
        if not cajas:
            od.avisos.append(Aviso("aviso", "modalidad_sin_filas", recurso, modalidad))
        for caja in cajas:
            t, v = caja.find(class_="caja_datos_titulo"), caja.find(class_="caja_datos_texto")
            origen = limpiar(t.get_text(" ")) if t else ""
            valor = limpiar(v.get_text(" ")) if v else ""
            consumidos |= {origen, valor}
            original = f"{modalidad} | {origen} | {valor}"
            if not origen or not _NUMERO.fullmatch(valor):
                od.avisos.append(Aviso("error", "bloque_desconocido", recurso, original))
                continue
            if (modalidad, origen) in vistos:
                od.avisos.append(Aviso("aviso", "origen_repetido", recurso, original))
                continue
            vistos.add((modalidad, origen))
            precio = numero(valor)
            # Un 0,00 significa «ruta no ofrecida», no un precio de cero euros.
            ofrecida = precio > 0
            od.filas.append(
                FilaOD(
                    modalidad=modalidad,
                    origen=origen,
                    origen_normalizado=normalizar_origen(origen),
                    estado="ofrecida" if ofrecida else "no_ofrecida",
                    precio=precio if ofrecida else None,
                    texto_original=original,
                )
            )

    # Destino y notas al pie, por texto: «DESTINO: …» y lo que queda hasta el pie.
    dentro = False
    for ln in lineas_de(html):
        t = ln.texto
        if t.startswith("DESTINO:"):
            od.destino = t.removeprefix("DESTINO:").strip() or None
            dentro = True
        elif t.startswith("©"):
            break
        elif dentro and t and t not in consumidos:
            od.notas.append(t)
    if od.destino is None:
        od.avisos.append(Aviso("error", "bloque_desconocido", recurso, "falta «DESTINO:»"))
    if not od.filas:
        od.avisos.append(Aviso("error", "origen_destino_vacio", recurso, od.destino or ""))
    if any("euros/m3" in n.casefold() for n in od.notas):
        od.unidad = "€/m3"
    elif od.filas:
        od.avisos.append(Aviso("aviso", "unidad_desconocida", recurso, "sin «Euros/m3» en notas"))
    return od
