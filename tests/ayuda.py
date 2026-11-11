"""Utilidades compartidas por los tests."""

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
RAIZ = Path(__file__).parent.parent


def leer(ruta: str) -> str:
    # Las fixtures son bytes reales del portal: UTF-8 aunque el <meta> diga ISO-8859-1.
    return (FIXTURES / ruta).read_text(encoding="utf-8")


# --- fotos de raw/ en miniatura, con páginas reales ---------------------------

MAPA_MINI = """
<a href='instalacion.htm?idTanque=9'><img src="imgs/tanques/5.png" title="EXOLUM - Puxeiros"/></a>
<a href='instalacion.htm?idTanque=17'><img src="imgs/tanques/2.png" title="TEPSA - Ciérvana"/></a>
<a href='instalacion.htm?idTanque=59'><img src="imgs/tanques/4_aer.png"
   title="EXOLUM / EXOLUM AVIATION - Barajas"/></a>
"""
# Como en el portal: el buscador lista los dos detalles de Barajas bajo CLHAV.
BUSCADOR_MINI = {"CLH": [17], "CLHAV": [123, 124], "TEPSA": [33]}
REDIRIGE = {9: 17, 17: 33}  # fichas de un solo producto: idTanque → detalle


def _resultados(comunidad: str, ids: list[int]) -> str:
    filas = "".join(
        f"<tr><td>{comunidad}</td><td>X</td><td>Producto</td>"
        f"<td><a href='detalle.htm?idTanqueInstalacion={i}'>ver</a></td></tr>"
        for i in ids
    )
    return f"<div>{len(ids)} filas encontradas.</div><table>{filas}</table>"


def crear_foto(
    dir_raw: Path,
    fecha: str,
    cambiar=None,
    sin_detalles: tuple[int, ...] = (),
    cambiar_od=None,
    pdfs_extra: tuple = (),
):
    """Foto con 3 instalaciones y 4 detalles reales (17, 33, 123 y 124).

    Incluye las páginas origen-destino de 17 y 123, los seis PDF a los que remiten
    esos detalles (24, 25, 26, 27, 32 y 34) y el PDF de capacidades.
    `cambiar(id_detalle, html) -> html` y `cambiar_od` permiten
    simular un cambio en el portal; `pdfs_extra` añade (bytes, recurso, nombre).
    """
    from cnmc_logistica import portal
    from cnmc_logistica.almacen import AlmacenCrudo
    from cnmc_logistica.config import BASE_PORTAL

    almacen = AlmacenCrudo(dir_raw, fecha)

    def guardar(recurso, html: str, url_final: str | None = None):
        almacen.guardar(
            recurso,
            html.encode(),
            url_final=url_final or recurso.url,
            estado_http=200,
            cabeceras={"content-type": "text/html;charset=UTF-8"},
        )

    guardar(portal.mapa("GAL"), MAPA_MINI)
    guardar(portal.formulario_buscador(), leer("buscador/formulario.html"))
    for codigo, ids in BUSCADOR_MINI.items():
        ids = [i for i in ids if i not in sin_detalles]
        guardar(portal.buscador_almacenista(codigo), _resultados("Galicia", ids))
    for id_tanque, id_ti in REDIRIGE.items():
        url = f"{BASE_PORTAL}/detalle.htm?idTanqueInstalacion={id_ti}"
        guardar(portal.ficha(id_tanque), "<p>redirigida</p>", url)
    ficha_59 = leer("fichas/59.html")
    for i in sin_detalles:
        ficha_59 = ficha_59.replace(f"detalle.htm?idTanqueInstalacion={i}'", "otra.htm'")
    guardar(portal.ficha(59), ficha_59)
    for id_ti in (17, 33, 123, 124):
        if id_ti in sin_detalles:
            continue
        html = leer(f"detalles/{id_ti}.html")
        guardar(portal.detalle(id_ti), cambiar(id_ti, html) if cambiar else html)
    for id_ti in (17, 123):  # los dos detalles con página origen-destino
        html = leer(f"origen_destino/{id_ti}.html")
        guardar(portal.origen_destino(id_ti), cambiar_od(id_ti, html) if cambiar_od else html)
    for clave, recurso, nombre in (
        ("24", portal.pdf(24), "EXOLUM 2026 precios SSLL GG.pdf"),
        ("25", portal.pdf(25), "EXOLUM 2026 precios_BONIF Y COMP CONTRATOS LP GG.pdf"),
        ("26", portal.pdf(26), "EXOLUM 2026 precios_ALMACENAMIENTO NO OPERATIVO GG.pdf"),
        ("27", portal.pdf(27), "EXOLUM 2026 precios_RECONSIGNACIÓN GG.pdf"),
        ("32", portal.pdf(32), "EXOLUM 2026 precios_SLB_JET_Aeroportuarias.pdf"),
        ("34", portal.pdf(34), "EXOLUM 2026 precios_Bonif e incumplim LP_JET.pdf"),
        ("capacidades", portal.pdf_capacidades(), None),
        *pdfs_extra,
    ):
        datos = clave if isinstance(clave, bytes) else (FIXTURES / f"pdf/{clave}.pdf").read_bytes()
        cabeceras = {"content-type": "application/octet-stream"}
        if nombre:
            # Las cabeceras HTTP van en latin-1, como las sirve el portal.
            nombre = nombre.encode("latin-1", "replace").decode("latin-1")
            cabeceras["content-disposition"] = f'attachment;filename="{nombre}"'
        almacen.guardar(recurso, datos, url_final=recurso.url, estado_http=200, cabeceras=cabeceras)
    return almacen
