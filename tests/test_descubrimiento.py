"""Mapas, fichas, buscador, categorías e inventario con control cruzado."""

from ayuda import RAIZ, leer
from cnmc_logistica import portal
from cnmc_logistica.almacen import AlmacenCrudo, Recurso
from cnmc_logistica.categorias import SIN_CATEGORIA, Categorizador, Indicadores
from cnmc_logistica.config import BASE_PORTAL
from cnmc_logistica.inventario import construir_inventario
from cnmc_logistica.parsers.buscador import parsear_formulario, parsear_resultados
from cnmc_logistica.parsers.ficha import parsear_ficha
from cnmc_logistica.parsers.mapa import comunidades_de_portada, parsear_mapa, tipo_de_icono

# --- portada y mapas -----------------------------------------------------------


def test_portada_enlaza_las_comunidades():
    codigos = comunidades_de_portada(leer("portada.html"))
    assert 15 <= len(codigos) <= 25
    assert {"MAD", "CAN", "AND"} <= set(codigos)
    assert len(codigos) == len(set(codigos))


def test_tipo_segun_icono():
    assert tipo_de_icono("imgs/tanques/4_aer.png") == "aeroportuaria"
    assert tipo_de_icono("imgs/tanques/18.png") == "almacenamiento"
    assert tipo_de_icono("imgs/tanques/oleoducto.gif") == "desconocido"


def test_mapa_con_los_dos_tipos_de_instalacion():
    for comunidad in ("MAD", "CAN"):
        instalaciones = parsear_mapa(leer(f"mapas/{comunidad}.html"))
        assert 3 <= len(instalaciones) <= 40
        assert {i.tipo for i in instalaciones} == {"almacenamiento", "aeroportuaria"}
        assert all(i.id_tanque > 0 and i.titulo for i in instalaciones)
        assert len({i.id_tanque for i in instalaciones}) == len(instalaciones)


def test_mapa_barajas_es_aeroportuaria():
    barajas = next(i for i in parsear_mapa(leer("mapas/MAD.html")) if i.id_tanque == 59)
    assert barajas.tipo == "aeroportuaria" and "Barajas" in barajas.titulo


# --- fichas --------------------------------------------------------------------


def test_ficha_con_varios_productos():
    ficha = parsear_ficha(leer("fichas/1.html"), 1)
    assert ficha.titulo == "FORESTAL DEL ATLANTICO"
    assert sorted(ficha.detalles) == [(381, "HVO"), (382, "Fuelóleos")]


def test_ficha_sin_productos_es_valida():
    ficha = parsear_ficha(leer("fichas/16.html"), 16)
    assert ficha.detalles == [] and "CHANE" in ficha.titulo


def test_ficha_de_barajas_tiene_dos_detalles():
    assert sorted(i for i, _ in parsear_ficha(leer("fichas/59.html"), 59).detalles) == [123, 124]


# --- buscador ------------------------------------------------------------------


def test_formulario_lista_almacenistas_y_comunidades():
    f = parsear_formulario(leer("buscador/formulario.html"))
    assert 30 <= len(f.almacenistas) <= 80
    assert f.almacenistas["CLH"] == "EXOLUM CORPORATION, S.A."
    assert f.almacenistas["DECAL"] == "DECAL ESPAÑA, S.A.U."  # sin los espacios de relleno
    assert "0" not in f.almacenistas
    assert 15 <= len(f.comunidades) <= 25 and f.comunidades["MAD"] == "MADRID"


def test_resultados_en_una_sola_pagina():
    pag = parsear_resultados(leer("buscador/almacenista_DECAL_p1.html"))
    assert pag.total == len(pag.filas) and pag.paginas == 1
    assert {f.comunidad for f in pag.filas} == {"Cataluña", "Andalucía"}
    assert all(f.id_tanque_instalacion > 0 and f.producto for f in pag.filas)


def test_resultados_paginados():
    pag = parsear_resultados(leer("buscador/almacenista_CLHAV_p1.html"))
    assert len(pag.filas) == 20 and pag.total > 20
    assert pag.paginas == -(-pag.total // 20)  # techo
    assert pag.parametro_pagina.startswith("d-") and pag.parametro_pagina.endswith("-p")


def test_url_de_pagina_usa_el_parametro_leido():
    r = portal.buscador_almacenista("DBA BILBAO PORT", 2, "d-49653-p")
    assert r.url.endswith("almacenista=DBA%20BILBAO%20PORT&d-49653-p=2")
    assert r.clave == "almacenista_DBA_BILBAO_PORT_p2"


# --- categorías ----------------------------------------------------------------


def test_categorias_del_fichero_real():
    cat = Categorizador.de_fichero(RAIZ / "config" / "categorias_servicio.yaml")
    assert cat.categoria("Servicios aeroportuarios") == ("servicios_aeroportuarios", True)
    assert cat.categoria("servicio  ALMACENAMIENTO") == ("almacenamiento", True)
    # No se confunden: son categorías distintas aunque ambas hablen de almacenar.
    assert cat.categoria("Almacenamiento y manipulación")[0] != cat.categoria("Almacenamiento")[0]


def test_etiqueta_desconocida_queda_sin_categoria():
    cat = Categorizador({"categorias": {"a": "x"}, "etiquetas": {"Uno": "a"}})
    assert cat.categoria("Servicio inventado") == (SIN_CATEGORIA, False)


def test_el_tipo_de_instalacion_entra_en_la_categoria():
    cat = Categorizador.de_fichero(RAIZ / "config" / "categorias_servicio.yaml")
    assert cat.categoria("Servicios logísticos", "almacenamiento")[0] == "logistica_basica"
    assert cat.categoria("Servicios logísticos", "aeroportuaria")[0] == "logistica_aeroportuaria"
    # Las etiquetas específicas no cambian con el tipo.
    assert cat.categoria("Transferencias", "aeroportuaria")[0] == "transferencias"


def test_indicadores_a_partir_de_incluye():
    ind = Indicadores.de_fichero(RAIZ / "config" / "indicadores_incluye.yaml")
    flags, desconocidos = ind.de_servicio(
        ["Aceptación y recepción de productos", "Transporte", "Expedición por camión cisterna"]
    )
    assert flags == {
        "incluye_transporte": 1,
        "incluye_almacenamiento": 0,
        "incluye_expedicion": 1,
        "incluye_puesta_a_bordo": 0,
    }
    assert desconocidos == []
    # Transportar el aditivo no es transportar el producto.
    assert (
        ind.de_servicio(["Transporte y entrega del aditivo antiestático"])[0]["incluye_transporte"]
        == 0
    )


def test_texto_de_incluye_desconocido_se_devuelve_para_avisar():
    ind = Indicadores.de_fichero(RAIZ / "config" / "indicadores_incluye.yaml")
    flags, desconocidos = ind.de_servicio(["Teletransporte"])
    assert sum(flags.values()) == 0 and desconocidos == ["Teletransporte"]


# --- inventario y control cruzado ----------------------------------------------

MAPA = """<a href='instalacion.htm?idTanque=1'><img src="imgs/tanques/3.png" title="OP - Uno"/></a>
<a href='instalacion.htm?idTanque=2'><img src="imgs/tanques/4_aer.png" title="AV - Dos"/></a>"""
FICHA_1 = """<div><label>Logística</label><label>Buscar:</label></div>
<div>OP - Uno</div><div>Instalación no conectada a la red de oleoductos</div>
<div>PRODUCTOS:</div><div><a href='detalle.htm?idTanqueInstalacion=10'>Gasóleos</a><br/>
<a href='detalle.htm?idTanqueInstalacion=11'>HVO</a><br/></div>"""
FORMULARIO = """<select name="comunidad"><option value="0">-</option>
<option value="MAD">MADRID</option></select>
<select name="almacenista"><option value="0">-</option><option value="OP">OPERADOR, S.A.</option>
<option value="AV">AVIACIÓN, S.L.</option>
<option value="NADA">SIN INSTALACIONES</option></select>"""


def _resultados(ids: list[int]) -> str:
    filas = "".join(
        f"<tr><td>Madrid</td><td>X</td><td>Producto {i}</td>"
        f"<td><a href='detalle.htm?idTanqueInstalacion={i}'>ver</a></td></tr>"
        for i in ids
    )
    return (
        f"<div>{len(ids)} filas encontradas, mostrando todas las filas.</div><table>{filas}</table>"
    )


def _foto(tmp_path, ids_op: list[int], ids_av: list[int]) -> AlmacenCrudo:
    almacen = AlmacenCrudo(tmp_path / "raw", "2026-11-11")

    def guardar(recurso: Recurso, html: str, url_final: str | None = None):
        almacen.guardar(
            recurso,
            html.encode(),
            url_final=url_final or recurso.url,
            estado_http=200,
            cabeceras={"content-type": "text/html;charset=UTF-8"},
        )

    guardar(portal.mapa("MAD"), MAPA)
    guardar(portal.ficha(1), FICHA_1)
    # La instalación 2 tiene un solo producto: su ficha redirige al detalle 30.
    guardar(portal.ficha(2), "<p>detalle</p>", f"{BASE_PORTAL}/detalle.htm?idTanqueInstalacion=30")
    guardar(portal.formulario_buscador(), FORMULARIO)
    guardar(portal.buscador_almacenista("OP"), _resultados(ids_op))
    guardar(portal.buscador_almacenista("AV"), _resultados(ids_av))
    guardar(portal.buscador_almacenista("NADA"), "<div>No se han encontrado filas.</div>")
    return almacen


def test_inventario_cuadra(tmp_path):
    inv, avisos = construir_inventario(_foto(tmp_path, [10, 11], [30]))
    assert avisos == []
    assert [
        (i["id_tanque"], i["tipo"], i["comunidad"], i["detalles"]) for i in inv["instalaciones"]
    ] == [
        (1, "almacenamiento", "MAD", [10, 11]),
        (2, "aeroportuaria", "MAD", [30]),
    ]
    assert {d["id_tanque_instalacion"]: d["operador"] for d in inv["detalles"]} == {
        10: "OP",
        11: "OP",
        30: "AV",
    }
    assert {o["codigo"]: o["detalles"] for o in inv["operadores"]} == {"AV": 1, "NADA": 0, "OP": 2}
    assert inv["control_cruzado"]["solo_fichas"] == inv["control_cruzado"]["solo_buscador"] == []


def test_control_cruzado_detecta_diferencias(tmp_path):
    inv, avisos = construir_inventario(_foto(tmp_path, [10], [30, 99]))
    assert inv["control_cruzado"]["solo_fichas"] == [11]
    assert inv["control_cruzado"]["solo_buscador"] == [99]
    errores = {(a.tipo, a.recurso) for a in avisos if a.es_error}
    assert errores == {("control_cruzado", "detalle/11"), ("control_cruzado", "detalle/99")}


def test_buscador_incompleto_es_error(tmp_path):
    almacen = _foto(tmp_path, [10, 11], [30])
    cortada = _resultados([10, 11]).replace("2 filas encontradas", "40 filas encontradas")
    almacen.guardar(
        portal.buscador_almacenista("OP"),
        cortada.encode(),
        url_final="u",
        estado_http=200,
        cabeceras={},
    )
    _, avisos = construir_inventario(almacen)
    assert [(a.tipo, a.operador) for a in avisos if a.es_error] == [("buscador_incompleto", "OP")]


def test_suministro_depende_del_tipo_de_instalacion():
    ind = Indicadores.de_fichero(RAIZ / "config" / "indicadores_incluye.yaml")
    for texto in ("Suministro", "Entrega o suministro"):
        en_aeropuerto, _ = ind.de_servicio([texto], "aeroportuaria")
        en_terminal, desconocidos = ind.de_servicio([texto], "almacenamiento")
        assert (en_aeropuerto["incluye_puesta_a_bordo"], en_aeropuerto["incluye_expedicion"]) == (
            1,
            0,
        )
        assert (en_terminal["incluye_puesta_a_bordo"], en_terminal["incluye_expedicion"]) == (0, 1)
        assert desconocidos == []


def test_los_indicadores_salen_solo_de_la_lista_incluye():
    """Vigo (Exolum Aviation): «Carga en unidad repostadora o uso red de hidrante» y
    «Servicio de puesta a bordo» están en OTROS SERVICIOS como «Servicio adicional no
    incluido en precio». No están en «Incluye:» y no deben activar ningún indicador."""
    from cnmc_logistica.parsers.detalle import parsear_detalle

    d = parsear_detalle(leer("detalles/20.html"), 20)
    no_incluidos = {p.etiqueta for p in d.precios if p.texto and "no incluido" in p.texto}
    assert no_incluidos == {
        "Carga en unidad repostadora o uso red de hidrante",
        "Servicio de puesta a bordo",
    }
    incluye = [s.texto for s in d.incluidos if s.servicio == "Servicio almacenamiento"]
    assert not any("repostadora" in t or "bordo" in t for t in incluye)
    ind = Indicadores.de_fichero(RAIZ / "config" / "indicadores_incluye.yaml")
    flags, _ = ind.de_servicio(incluye, "aeroportuaria")
    assert flags == {
        "incluye_transporte": 0,
        "incluye_almacenamiento": 1,
        "incluye_expedicion": 0,
        "incluye_puesta_a_bordo": 0,
    }
