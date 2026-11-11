"""Parsers específicos de los PDF de precios, cobertura de cifras y OCR de respaldo."""

import dataclasses
import io
import shutil

import pytest

from ayuda import FIXTURES, RAIZ, crear_foto
from cnmc_logistica import portal
from cnmc_logistica.bd import conectar
from cnmc_logistica.carga import cargar_foto, leer_config
from cnmc_logistica.cobertura import Descartes, calcular
from cnmc_logistica.config import Rutas
from cnmc_logistica.parsers import pdf_lectura
from cnmc_logistica.parsers.pdf import parsear_capacidades
from cnmc_logistica.parsers.pdf_lectura import leer_paginas
from cnmc_logistica.parsers.pdf_precios import extraer_pdf, numero, unidad_de

CFG = leer_config(Rutas(RAIZ))
F1 = "2026-11-11"
TODOS = sorted(p.stem for p in (FIXTURES / "pdf").glob("*.pdf"))


def pdf(clave: str) -> bytes:
    return (FIXTURES / f"pdf/{clave}.pdf").read_bytes()


def valores(clave: str) -> dict[tuple, tuple]:
    return {
        (v.seccion, v.concepto, v.subconcepto): (v.valor, v.unidad)
        for v in extraer_pdf(pdf(clave)).valores
    }


def celda(ext, destino, origen, modalidad=None):
    return next(
        c for c in ext.celdas
        if (c.destino, c.origen) == (destino, origen)
        and (modalidad is None or modalidad in c.modalidad)
    )  # fmt: skip


@pytest.fixture
def conn(tmp_path):
    conexion = conectar(tmp_path / "prueba.db")
    yield conexion
    conexion.close()


# --- lectura ------------------------------------------------------------------------


def test_los_27_pdf_reales_estan_en_las_fixtures():
    assert len(TODOS) == 27 and "capacidades" in TODOS


def test_numeros_y_unidades():
    assert numero("1.180,565") == 1180.565 and numero("0,0681") == 0.0681
    assert numero("9.0526") == 9.0526  # cuatro decimales: no puede ser separador de millares
    # Sin coma ni forma reconocible no se interpreta: ni «01021» ni «1.000».
    assert numero("01021") is None and numero("1.000") is None and numero("19,2g3") is None
    assert unidad_de("(Euros/m3 y mes)") == "€/m3 y mes"
    assert unidad_de("0,1021 €/m3 por día que se exceda") == "€/m3 y día"
    assert unidad_de("€/m3 entregado") == "€/m3 entregado" and unidad_de("PRECIO") is None


def test_un_decimal_partido_por_un_espacio_se_recompone():
    # En el PDF 25 «0 ,5462» son dos palabras; debe leerse como una sola cifra.
    textos = [p.texto for pag in leer_paginas(pdf("25")) for ln in pag.lineas() for p in ln]
    assert "0,5462" in textos and "3,4137" in textos and ",5462" not in textos


def test_no_se_pasa_ocr_a_paginas_con_texto(monkeypatch):
    def prohibido(*_):
        raise AssertionError("no debe llamarse al OCR si la página tiene capa de texto")

    monkeypatch.setattr(pdf_lectura, "_palabras_ocr", prohibido)
    leer_paginas.cache_clear()
    for clave in ("7", "24", "capacidades"):
        assert {pag.metodo for pag in leer_paginas(pdf(clave))} == {"texto"}


# --- parsers por tipo de documento -----------------------------------------------------


def test_disa_precios_por_isla():
    assert valores("7") == {
        (None, "Servicios logísticos", "Gasolinas"): (19.293, "€/m3"),
        (None, "Servicios logísticos", "Gasóleos"): (21.244, "€/m3"),
        (None, "Almacenamiento adicional", None): (0.1021, "€/m3 y día"),
    }
    assert extraer_pdf(pdf("7")).anio == 2026


def test_una_cifra_ilegible_no_se_inventa():
    # El PDF de El Hierro trae «01021» (falta la coma): se guarda sin valor y se avisa.
    ext = extraer_pdf(pdf("12"))
    ilegible = next(v for v in ext.valores if v.concepto == "Almacenamiento adicional")
    assert ilegible.valor is None and "01021" in ilegible.texto_original
    assert [tipo for tipo, _ in ext.avisos] == ["numero_ilegible"]
    assert valores("12")[None, "Servicios logísticos", "Gasolinas"] == (252.951, "€/m3")


def test_terminales_canarios_logistica_basica_dos_columnas():
    v = valores("3")
    automocion = "Gasolinas y Gasóleos de Automoción"
    assert v[automocion, "RECEPCIÓN, MANIPULACIÓN Y ENTREGA", "Carga Camión cisterna"] == (
        6.003,
        "€/m3",
    )
    assert v[automocion, "ARRENDAMIENTO CAPACIDAD DE ALMACENAMIENTO", None] == (1.922, "€/m3 y mes")
    marina = "Gasóleos, Fuelóleos e IFOs para actividad de marina"
    assert v[marina, "RECEPCIÓN, MANIPULACIÓN Y SUMINISTRO", "Suministro mediante Tubería (1)"] == (
        3.915,
        "€/m3",
    )
    assert len(v) == 4


def test_terminales_canarios_logistica_primaria_con_rotulo_girado():
    ext = extraer_pdf(pdf("4"))
    assert {v.seccion for v in ext.valores} == {"Jet A-1"}  # texto girado 90° en el margen
    por_valor = {v.valor: v for v in ext.valores}
    assert por_valor[8.496].unidad == "€/m3" and "CARGA DE CC/CC" in por_valor[8.496].concepto
    assert "RECEPCIÓN Y DESCARGA" in por_valor[8.496].concepto
    assert por_valor[1.873].unidad == "€/m3 y mes"
    assert por_valor[1.873].concepto.startswith("ARRENDAMIENTO CAPACIDAD")
    assert "Las Palmas de Gran Canaria" in ext.notas[0].texto


def test_exolum_bunker_mezcla_y_biodiesel_integrado():
    assert valores("13") == {
        (None, "Recepción por buque y almacenamiento", None): (3.5995, None),
        ("Retirada (medios ajenos)", "Camión ajeno", None): (2.0128, None),
        ("Retirada (medios ajenos)", "Gabarra ajena", None): (1.7631, None),
    }
    mezcla = valores("21")
    assert {k[1]: v for k, v in mezcla.items()} == {
        "B10": (0.5133, "€/m3"),
        "B20": (0.6262, "€/m3"),
        "B30": (0.7391, "€/m3"),
    }
    barcelona = valores("36")
    seccion = "MEDIO DE RECEPCIÓN DEL BIODIÉSEL EN LA INSTALACIÓN DE BARCELONA"
    assert barcelona[seccion, "Camión Cisterna", None] == (14.1899, "€/m3 entregado")
    assert barcelona[seccion, "Transferencia", None] == (11.6071, "€/m3 entregado")
    for clave in ("37", "38", "39", "40"):
        assert 3 <= len(valores(clave)) <= 5


def test_exolum_ester_almacenamiento_y_descuentos():
    v = valores("22")
    assert v[None, "Almacenamiento de éster metílico", None] == (9.0526, "€/m3 y mes")
    descuentos = {k[2]: val for k, val in v.items() if k[1].startswith("Descuento")}
    assert descuentos == {
        "Capacidad mayor o igual que 1.000 m3": (10.0, "%"),
        "Capacidad mayor o igual que 2.000 m3": (20.0, "%"),
        "Capacidad mayor o igual que 3.000 m3": (30.0, "%"),
    }


def test_exolum_almacenamiento_no_operativo():
    ext = extraer_pdf(pdf("26"))
    por_concepto = {v.concepto: v for v in ext.valores}
    m2, m3 = por_concepto["Modalidad 2"], por_concepto["Modalidad 3"]
    assert (m2.valor, m2.unidad, m2.seccion) == (1.8883, "€/m3 y mes", "Almacenamiento")
    assert "Torrejón" in m2.condicion and "Córdoba" in m2.condicion and "Gasóleo B" in m2.condicion
    assert "indiferenciado" in m3.condicion and "Gasolina 98" in m3.condicion
    calidad = por_concepto["Mantenimiento de la calidad (Modalidad 2)"]
    assert (calidad.valor, calidad.unidad) == (0.155, "€/m3 y mes")


def test_exolum_aviation_avgas_burgos():
    ext = extraer_pdf(pdf("35"))
    assert ext.vigencia == "1-ABR-2026 / 31-MAR-2027"
    v = {(x.concepto.split(" — ")[1], x.subconcepto): (x.valor, x.unidad) for x in ext.valores}
    assert v == {
        ("FIJO", "Mediante punto fijo atendido"): (7.764, "€/operación"),
        ("VARIABLE", "Mediante punto fijo atendido"): (203.91, "€/m3"),
        ("FIJO", "Mediante unidad repostadora"): (27.176, "€/operación"),
        ("VARIABLE", "Mediante unidad repostadora"): (203.91, "€/m3"),
    }
    assert {x.seccion for x in ext.valores} == {"AVGAS 100LL"}


@pytest.mark.parametrize("clave", ["25", "34"])
def test_bonificaciones_y_compensaciones(clave):
    ext = extraer_pdf(pdf(clave))
    b = {(x.bloque, x.tramo, x.condicion): (x.valor, x.unidad) for x in ext.bonificaciones}
    assert b["bonificacion", "Por cada m3", "De cantidad garantizada"] == (0.0681, "€")
    assert b["bonificacion", "Por cada m3", "De exceso s/ c. garantizada"] == (0.5462, "€")
    assert b["bonificacion", "Por cada 1.000 m3 x km", "De exceso s/ c. garantizada"] == (
        3.4137,
        "€",
    )
    compensaciones = {k[1]: v for k, v in b.items() if k[0] == "compensacion" and v[1] == "€"}
    assert compensaciones == {
        "por cada m3 garantizado y no retirado": (1.9663, "€"),
        "por cada m3 x km garantizado y no realizado": (0.0136, "€"),
    }
    umbrales = sorted(v[0] for v in b.values() if v[1] == "%")
    assert umbrales == [95.0, 100.0, 120.0]
    assert len(ext.bonificaciones) == 9 and len(ext.notas) >= 5


def test_matriz_de_reconsignacion():
    ext = extraer_pdf(pdf("27"))
    assert ext.parser == "reconsignacion" and ext.anio == 2026 and ext.avisos == []
    assert 700 <= len(ext.celdas) <= 1100
    assert len({c.destino for c in ext.celdas}) == len({c.origen for c in ext.celdas})
    # Nombres de columna en dos líneas y páginas distintas.
    assert celda(ext, "Alicante", "ÁLAVA (Rivabellosa)").precio == 30.52
    assert celda(ext, "Álava (Rivabellosa)", "ALICANTE").precio == 29.40
    assert celda(ext, "Zaragoza", "MÁLAGA").precio == 21.91
    assert celda(ext, "Álava (Rivabellosa)", "MURCIA (Cartagena)").precio == 16.52
    assert {c.unidad for c in ext.celdas} == {"€/m3"} and {c.modalidad for c in ext.celdas} == {
        None
    }
    # El recargo de la nota al pie también es un dato.
    (recargo,) = [v for v in ext.valores if v.concepto.startswith("Suplemento")]
    assert recargo.valor == 0.4652 and "almacenamiento reducido" in recargo.subconcepto


def test_matriz_origen_destino_con_modalidades_por_pagina():
    ext = extraer_pdf(pdf("24"))
    assert ext.parser == "origen_destino" and ext.avisos == []
    assert len({c.modalidad for c in ext.celdas}) == 4
    puxeiros = "Pontevedra (Puxeiros)"
    assert celda(ext, puxeiros, "HUELVA", "CONECTADAS").precio == 22.29
    assert celda(ext, puxeiros, "BARCELONA", "CONECTADAS").marca == "(3)"
    assert celda(ext, puxeiros, "Refinería HUELVA", "NO DIRECTA").precio == 21.21
    assert celda(ext, "Álava (Rivabellosa)", "BILBAO", "BUQUE TANQUE").precio == 8.41
    # Dos modalidades en la misma página no se mezclan.
    conectadas = {c.origen for c in ext.celdas if "CONECTADAS" in c.modalidad}
    assert conectadas == {"BARCELONA", "BILBAO", "HUELVA", "CARTAGENA", "TARRAGONA"}
    suplementos = sorted({v.valor for v in ext.valores})
    assert suplementos == [0.1603, 0.4652, 0.4935, 4.82]


def test_matriz_con_dos_modalidades_en_la_misma_linea():
    ext = extraer_pdf(pdf("29"))
    assert ext.avisos == []
    assert celda(ext, "Ibiza", "IBIZA", "BUQUE TANQUE").precio == 18.35
    assert celda(ext, "Albuixech", "TARRAGONA", "CONECTADAS").precio == 21.82
    assert celda(ext, "Albuixech", "TARRAGONA", "BUQUE TANQUE").marca == "(2)"
    aeropuertos = extraer_pdf(pdf("32"))
    assert aeropuertos.avisos == []
    modalidades = {c.modalidad for c in aeropuertos.celdas}
    assert "RECEPCIÓN DESDE INSTALACIONES CONECTADAS A EXOLUM" in modalidades


def test_matriz_de_consignacion_de_ester_con_celdas_no_aplicables():
    ext = extraer_pdf(pdf("23"))
    assert ext.parser == "consignacion_ester"
    assert celda(ext, "Álava (Rivabellosa)", "VIZCAYA (El Calero)").precio == 7.24
    assert celda(ext, "Burgos", "HUELVA").precio == 21.71
    propia = celda(ext, "Huelva", "HUELVA")
    assert (propia.estado, propia.precio) == ("no_aplica", None)
    assert sum(c.estado == "no_aplica" for c in ext.celdas) == 3


def test_documento_desconocido_no_inventa_nada():
    assert extraer_pdf(pdf("capacidades")).parser == "desconocido"


def test_capacidades_total_general_sin_decimales_y_anotacion_suelta():
    cap = parsear_capacidades(pdf("capacidades"))
    assert cap.descuadres == []
    # «TOTAL 7.391» son 7.391 miles de m3: verificado contra la suma de instalaciones.
    assert sum(clase == "total_verificado" for clase in cap.usadas.values()) >= 40
    exolum = sum(
        f.capacidad_miles_m3 or 0
        for f in cap.filas
        if f.compania == "EXOLUM CORPORATION" and f.producto == "TOTAL"
    )
    assert round(exolum) == 7391
    # «15,632 antes»: un resto de edición del PDF. Se guarda como nota, no como capacidad.
    assert any("15,632 antes" in texto for _, texto in cap.notas)
    assert not any(f.producto == "antes" for f in cap.filas)
    assert all(f.palabra is not None for f in cap.filas if f.capacidad_miles_m3 is not None)


# --- cobertura ----------------------------------------------------------------------


@pytest.mark.parametrize("clave", TODOS)
def test_cobertura_completa_de_cada_pdf_real(clave):
    datos = pdf(clave)
    if clave == "capacidades":
        usadas, parser = parsear_capacidades(datos).usadas, "capacidades"
    else:
        ext = extraer_pdf(datos)
        usadas, parser = ext.usadas, ext.parser
    assert parser != "desconocido"
    cob = calcular(clave, leer_paginas(datos), usadas, CFG.descartes)
    assert cob.sin_cubrir == []
    assert cob.cifras >= 5 and cob.en_valores >= 2
    f = cob.fila()
    assert f["cifras"] == sum(v for k, v in f.items() if k not in ("cifras", "sin_cubrir"))


def test_sin_la_regla_de_descarte_el_numero_de_pagina_queda_sin_cubrir():
    datos = pdf("7")
    cob = calcular("7", leer_paginas(datos), extraer_pdf(datos).usadas, Descartes({}))
    assert len(cob.sin_cubrir) == 2 and all("Página 1 de 1" in s for s in cob.sin_cubrir)


def test_regla_de_descarte_limitada_a_un_documento():
    reglas = {"reglas": [{"motivo": "nº de página", "linea": "^Página", "documentos": [8]}]}
    datos = pdf("7")
    usadas = extraer_pdf(datos).usadas
    assert len(calcular("7", leer_paginas(datos), usadas, Descartes(reglas)).sin_cubrir) == 2
    assert calcular("8", leer_paginas(datos), usadas, Descartes(reglas)).sin_cubrir == []
    with pytest.raises(ValueError):
        Descartes({"reglas": [{"motivo": "sin patrón"}]})


def test_una_cifra_que_el_parser_no_usa_es_un_error_de_la_ejecucion(conn, tmp_path):
    sin_descartes = dataclasses.replace(CFG, descartes=Descartes({}))
    r = cargar_foto(conn, crear_foto(tmp_path / "raw", F1), sin_descartes)
    errores = [a for a in r.avisos if a.es_error]
    assert errores and {a.tipo for a in errores} == {"cifra_pdf_sin_cubrir"}
    assert all("Página" in a.texto for a in errores)
    registradas = conn.execute(
        "SELECT COUNT(*) FROM incidencias WHERE nivel = 'error' AND tipo = 'cifra_pdf_sin_cubrir'"
    ).fetchone()[0]
    assert registradas == len(errores)
    assert conn.execute("SELECT SUM(sin_cubrir) FROM cobertura_pdf").fetchone()[0] == len(errores)


# --- carga en la base -----------------------------------------------------------------


def test_los_pdf_quedan_estructurados_y_enlazados_a_sus_detalles(conn, tmp_path):
    r = cargar_foto(conn, crear_foto(tmp_path / "raw", F1), CFG)
    assert r.errores == 0
    assert conn.execute("SELECT COUNT(*) FROM lineas_pdf_sin_resolver").fetchone()[0] == 0
    assert conn.execute("SELECT SUM(sin_cubrir) FROM cobertura_pdf").fetchone()[0] == 0
    # El detalle 17 (Exolum Puxeiros) remite a cuatro PDF.
    enlaces = conn.execute(
        "SELECT etiqueta, documento FROM enlaces_documento WHERE id_tanque_instalacion = 17 "
        "ORDER BY documento"
    ).fetchall()
    assert [tuple(e) for e in enlaces] == [
        ("Servicios logísticos básicos", "24"),
        ("Servicios logísticos básicos", "25"),
        ("Almacenamiento no operativo", "26"),
        ("Reconsignaciones entre instalaciones", "27"),
    ]
    fila = conn.execute(
        "SELECT categoria_servicio, valor, unidad, anio_tabla, metodo, pagina, x0, top, "
        "texto_original FROM precios_pdf_por_detalle "
        "WHERE id_tanque_instalacion = 17 AND concepto = 'Modalidad 2'"
    ).fetchone()
    assert tuple(fila)[:6] == (
        "almacenamiento_no_operativo",
        1.8883,
        "€/m3 y mes",
        2026,
        "texto",
        1,
    )
    assert fila["x0"] > 0 and fila["top"] > 0 and "1,8883" in fila["texto_original"]
    categorias = dict(
        conn.execute("SELECT categoria_servicio, COUNT(*) FROM precios_pdf GROUP BY 1")
    )
    assert "sin_categoria" not in categorias and categorias["suplemento"] >= 4


def test_matrices_enlazadas_por_precios_y_por_nombre(conn, tmp_path):
    cargar_foto(conn, crear_foto(tmp_path / "raw", F1), CFG)
    # Origen-destino: la fila del PDF se une al detalle con los mismos precios en su página.
    od = conn.execute(
        "SELECT DISTINCT destino, id_tanque_instalacion_destino, id_tanque_destino "
        "FROM matrices_pdf WHERE tipo = 'origen_destino' AND id_tanque_destino IS NOT NULL "
        "ORDER BY 2"
    ).fetchall()
    assert [tuple(f) for f in od] == [
        ("Pontevedra (Puxeiros)", 17, 9),
        ("Barajas", 123, 59),
    ]
    # Reconsignación: mismas instalaciones, por nombre exacto.
    recon = conn.execute(
        "SELECT origen, destino, precio, id_tanque_origen, id_tanque_destino, pagina "
        "FROM reconsignaciones WHERE destino = 'Pontevedra (Puxeiros)' AND origen = 'HUELVA'"
    ).fetchone()
    assert (recon["id_tanque_destino"], recon["id_tanque_origen"]) == (9, None)
    assert recon["precio"] > 0 and recon["pagina"] == 2
    hacia_fuera = conn.execute(
        "SELECT COUNT(*) FROM reconsignaciones WHERE id_tanque_origen = 9"
    ).fetchone()[0]
    assert hacia_fuera >= 25  # la columna «PONTEVEDRA (Puxeiros)» también se identifica


def test_segunda_carga_con_pdf_no_crea_filas(conn, tmp_path):
    cargar_foto(conn, crear_foto(tmp_path / "raw", F1), CFG)
    tablas = ("precios_pdf", "matrices_pdf", "bonificaciones", "notas_pdf", "cobertura_pdf",
              "enlaces_documento")  # fmt: skip
    antes = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tablas}
    assert all(antes.values())
    r = cargar_foto(conn, crear_foto(tmp_path / "otra", F1), CFG)
    assert (r.altas, r.cambios, r.desapariciones) == (0, 0, 0)
    assert antes == {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tablas}


# --- OCR de respaldo ------------------------------------------------------------------

sin_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="Tesseract no está instalado"
)


def _pdf_solo_imagen(clave: str) -> bytes:
    """El mismo PDF convertido en una imagen por página: sin capa de texto."""
    import pdfplumber

    salida = io.BytesIO()
    with pdfplumber.open(io.BytesIO(pdf(clave))) as documento:
        imagen = documento.pages[0].to_image(resolution=200).original.convert("RGB")
    imagen.save(salida, "PDF", resolution=200)
    return salida.getvalue()


@sin_tesseract
def test_ocr_solo_en_paginas_sin_texto_y_marcado_como_tal():
    pytest.importorskip("pytesseract")
    datos = _pdf_solo_imagen("7")
    (pagina,) = leer_paginas(datos)
    assert pagina.metodo == "ocr" and len(pagina.palabras) > 10
    ext = extraer_pdf(datos)
    assert ext.parser == "disa"
    assert ext.valores and {v.palabra.metodo for v in ext.valores} == {"ocr"}
    leidos = {v.subconcepto: v.valor for v in ext.valores if v.subconcepto}
    # Lo que el OCR lea bien debe coincidir con el original; lo demás no aparece o va sin valor.
    for producto, esperado in (("Gasolinas", 19.293), ("Gasóleos", 21.244)):
        assert leidos.get(producto) in (esperado, None)
    assert esperado in leidos.values() or 19.293 in leidos.values()


@sin_tesseract
def test_ocr_en_la_carga_avisa_y_marca_el_metodo(conn, tmp_path):
    pytest.importorskip("pytesseract")
    escaneado = (_pdf_solo_imagen("7"), portal.pdf(999), "DISA precios 2026_GC.pdf")
    r = cargar_foto(conn, crear_foto(tmp_path / "raw", F1, pdfs_extra=(escaneado,)), CFG)
    avisos = [a for a in r.avisos if a.recurso == "pdf/999"]
    assert ("aviso", "valores_por_ocr") in {(a.nivel, a.tipo) for a in avisos}
    metodos = dict(conn.execute("SELECT metodo, COUNT(*) FROM precios_pdf GROUP BY 1"))
    assert metodos.get("ocr", 0) >= 1 and metodos["texto"] >= 5
    # Los PDF con capa de texto no pasan por OCR.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM precios_pdf WHERE metodo = 'ocr' AND documento != '999'"
        ).fetchone()[0]
        == 0
    )


def test_un_numero_que_el_ocr_lee_mal_no_se_guarda_y_queda_en_incidencias(
    conn, tmp_path, monkeypatch
):
    # Se simula un OCR que confunde un dígito («19,2g3»): no hace falta Tesseract.
    original = {p.clave: p for pag in leer_paginas(pdf("7")) for p in pag.palabras}

    def ocr_defectuoso(pagina, numero):
        return [
            dataclasses.replace(p, texto="19,2g3" if p.texto == "19,293" else p.texto, metodo="ocr")
            for p in original.values()
        ]

    monkeypatch.setattr(pdf_lectura, "_palabras_ocr", ocr_defectuoso)
    leer_paginas.cache_clear()
    escaneado = (_pdf_solo_imagen("7"), portal.pdf(999), None)
    r = cargar_foto(conn, crear_foto(tmp_path / "raw", F1, pdfs_extra=(escaneado,)), CFG)
    leer_paginas.cache_clear()
    errores = [a for a in r.avisos if a.es_error]
    assert [(a.tipo, a.recurso) for a in errores] == [("cifra_pdf_sin_cubrir", "pdf/999")]
    assert "19,2g3" in errores[0].texto
    guardados = [
        f[0] for f in conn.execute("SELECT valor FROM precios_pdf WHERE documento = '999'")
    ]
    assert 21.244 in guardados and not any(v and 19 <= v < 20 for v in guardados)


# --- condiciones con cantidad y registro de ejecuciones -----------------------------------


def test_las_notas_con_cantidad_pasan_a_condiciones():
    bunker = extraer_pdf(pdf("13"))
    assert [(c.valor, c.unidad, c.texto) for c in bunker.condiciones] == [
        (500.0, "toneladas", "(Facturación mínima 500 toneladas)")
    ]
    assert bunker.notas == []  # ya no es una nota: es una condición
    assert [(c.valor, c.unidad) for c in extraer_pdf(pdf("7")).condiciones] == [(15.0, "ºC")]
    ester = extraer_pdf(pdf("22"))
    assert [(c.valor, c.unidad) for c in ester.condiciones] == [(1000.0, "m3")]
    assert [n.texto for n in ester.notas] == ["precio anterior el siguiente descuento:"]
    jet = extraer_pdf(pdf("29"))
    assert sorted({(c.valor, c.unidad) for c in jet.condiciones}) == [
        (14.0, "días"),
        (15.0, "días"),
        (19.0, "días"),
    ]
    # Una nota solo explicativa sigue siendo nota; un precio o un umbral no se duplica.
    assert any("puertos alternativos" in n.texto for n in jet.notas)
    bonif = extraer_pdf(pdf("25"))
    assert bonif.condiciones == [] and len(bonif.bonificaciones) == 9
    assert [(c.valor, c.unidad) for c in extraer_pdf(pdf("27")).condiciones] == [(3.0, "días")]


def test_reconstruir_recupera_las_ejecuciones_reales_y_se_anota_a_si_misma(tmp_path):
    from cnmc_logistica.carga import leer_registro, reconstruir, registrar_ejecucion

    raiz = tmp_path / "proyecto"
    (raiz / "config").mkdir(parents=True)
    for fichero in (RAIZ / "config").glob("*.yaml"):
        (raiz / "config" / fichero.name).write_bytes(fichero.read_bytes())
    almacen = crear_foto(raiz / "raw", F1)
    conn = conectar(tmp_path / "original.db")
    for _ in range(2):  # dos actualizaciones reales el mismo día
        resumen = cargar_foto(conn, almacen, CFG)
        registrar_ejecucion(almacen, conn, resumen)
    conn.close()
    registro = leer_registro(almacen)
    assert [(r["altas"] > 0, r["fecha"]) for r in registro] == [(True, F1), (False, F1)]
    assert registro[0]["version"] and registro[0]["registrada_en"] and registro[0]["por_tabla"]
    assert {i["tipo"] for i in registro[0]["incidencias"]} == {a.tipo for a in resumen.avisos}

    reconstruir(Rutas(raiz), tmp_path / "nueva.db")
    nueva = conectar(tmp_path / "nueva.db")
    filas = nueva.execute(
        "SELECT tipo, origen, registrada_en, altas FROM ejecuciones ORDER BY id"
    ).fetchall()
    assert [(f["tipo"], f["origen"]) for f in filas] == [
        ("actualizacion", "registro"),
        ("actualizacion", "registro"),
        ("reconstruccion", "carga"),
    ]
    assert [f["registrada_en"] for f in filas[:2]] == [r["registrada_en"] for r in registro]
    assert filas[0]["altas"] == registro[0]["altas"] and filas[2]["altas"] > 0
    por_ejecucion = dict(nueva.execute("SELECT ejecucion_id, COUNT(*) FROM incidencias GROUP BY 1"))
    assert por_ejecucion.get(1, 0) == len(registro[0]["incidencias"])
    # Los datos son los mismos que en la base original.
    assert nueva.execute("SELECT COUNT(*) FROM precios").fetchone()[0] > 40
    nueva.close()
