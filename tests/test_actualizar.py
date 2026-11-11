import httpx

from ayuda import FIXTURES
from cnmc_logistica import portal
from cnmc_logistica.actualizar import descargar_inventario
from cnmc_logistica.almacen import AlmacenCrudo
from cnmc_logistica.descarga import Descargador

INVENTARIO = {
    "instalaciones": [{"id_tanque": 9}],
    "detalles": [
        {"id_tanque_instalacion": 17, "id_tanque": 9, "operador": "CLH"},
        {"id_tanque_instalacion": 999, "id_tanque": 9, "operador": "CLH"},
    ],
}


def _descargador(tmp_path, pdf_roto: str):
    detalle_17 = (FIXTURES / "detalles/17.html").read_bytes()

    def manejador(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        if url == portal.detalle(17).url:
            return httpx.Response(200, content=detalle_17)
        if url == portal.detalle(999).url:
            return httpx.Response(200, text="<html><body>Página en mantenimiento</body></html>")
        if url == portal.pdf(int(pdf_roto)).url:
            return httpx.Response(404)
        return httpx.Response(200, content=b"%PDF-1.5" if "downloadfile" in url else b"<p>ok</p>")

    almacen = AlmacenCrudo(tmp_path / "raw", "2026-11-11")
    cliente = httpx.Client(transport=httpx.MockTransport(manejador))
    return Descargador(almacen, cliente=cliente, dormir=lambda _: None), almacen


def test_descarga_lo_que_enlaza_cada_detalle_y_sigue_ante_fallos(tmp_path):
    desc, almacen = _descargador(tmp_path, pdf_roto="26")
    avisos = descargar_inventario(desc, INVENTARIO)

    # Lo enlazado desde el detalle 17 se ha descubierto solo.
    assert almacen.buscar("ficha", "9") and almacen.buscar("detalle", "17")
    assert almacen.buscar("origen_destino", "17")
    assert {e.clave for e in almacen.de_tipo("pdf")} >= {"24", "25", "27", "capacidades"}
    # El PDF caído y la página irreconocible quedan anotados con su operador; nada aborta.
    assert almacen.buscar("pdf", "26") is None
    assert sorted((a.nivel, a.tipo, a.recurso, a.operador) for a in avisos) == [
        ("error", "bloque_desconocido", "detalle/999", "CLH"),
        ("error", "descarga", "pdf/26", "CLH"),
    ]
    # El crudo de la página irreconocible se conserva para poder estudiarla.
    assert almacen.buscar("detalle", "999") is not None
