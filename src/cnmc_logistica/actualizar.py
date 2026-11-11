"""Descarga de todo el inventario: fichas, detalles, origen-destino y PDFs."""

from __future__ import annotations

from . import portal
from .avisos import Aviso
from .descarga import Descargador, ErrorDescarga
from .parsers.detalle import PaginaNoReconocida, parsear_detalle


def descargar_inventario(desc: Descargador, inventario: dict) -> list[Aviso]:
    """Baja a raw/ todo lo que cuelga del inventario.

    Los enlaces a origen-destino y a PDFs se descubren leyendo cada detalle:
    no hay lista fija de documentos, así un PDF nuevo entra solo. Un fallo en
    un recurso se anota y no impide bajar los demás.
    """
    avisos: list[Aviso] = []
    almacen = desc.almacen
    desc.comprobar_robots()

    def bajar(recurso, operador=None):
        try:
            return desc.obtener(recurso)
        except ErrorDescarga as exc:
            ref = f"{recurso.tipo}/{recurso.clave}"
            avisos.append(Aviso("error", "descarga", ref, str(exc), operador=operador))
            return None

    for inst in inventario["instalaciones"]:
        bajar(portal.ficha(inst["id_tanque"]))

    claves_pdf: dict[str, str | None] = {}
    for det in inventario["detalles"]:
        id_ti, operador = det["id_tanque_instalacion"], det.get("operador")
        entrada = bajar(portal.detalle(id_ti), operador)
        if entrada is None:
            continue
        try:
            detalle = parsear_detalle(almacen.leer_texto(entrada)[0], id_ti)
        except PaginaNoReconocida as exc:
            avisos.append(
                Aviso("error", "bloque_desconocido", f"detalle/{id_ti}", str(exc), operador)
            )
            continue
        if detalle.tiene_origen_destino:
            bajar(portal.origen_destino(id_ti), operador)
        for enlace in detalle.pdfs:
            claves_pdf.setdefault(enlace.clave, operador)

    for clave, operador in sorted(claves_pdf.items()):
        if clave.isdigit():
            bajar(portal.pdf(int(clave)), operador)
        else:
            avisos.append(Aviso("error", "pdf_sin_clave", f"pdf/{clave[:40]}", clave, operador))
    bajar(portal.pdf_capacidades())
    return avisos
