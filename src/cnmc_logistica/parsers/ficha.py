"""Parser de instalacion.htm (ficha de una instalación física)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..portal import id_de_enlace
from .detalle import cabecera_y_cuerpo
from .lineas import lineas_de


@dataclass
class Ficha:
    id_tanque: int
    titulo: str
    subtitulo: str | None
    detalles: list[tuple[int, str]] = field(default_factory=list)  # (idTanqueInstalacion, producto)


def parsear_ficha(html: str, id_tanque: int) -> Ficha:
    """Lista los productos (enlaces a detalle) bajo la etiqueta «PRODUCTOS:».

    Una ficha sin productos es válida: hay instalaciones sin contratos vigentes.
    """
    cabecera, cuerpo = cabecera_y_cuerpo(lineas_de(html))
    ficha = Ficha(
        id_tanque=id_tanque,
        titulo=cabecera[0].texto if cabecera else "",
        subtitulo=cabecera[1].texto if len(cabecera) > 1 else None,
    )
    for ln in cuerpo:
        for texto, href in ln.enlaces:
            id_ti = id_de_enlace(href, "idTanqueInstalacion")
            if id_ti is not None and "detalle.htm" in href:
                ficha.detalles.append((id_ti, texto))
    return ficha
