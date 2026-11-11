"""Fase 5 y 5b: origen-destino (HTML fuente, PDF control), tablas de PDF y capacidades."""

import json

import pytest

from ayuda import FIXTURES, RAIZ, crear_foto, leer
from cnmc_logistica import portal
from cnmc_logistica.bd import conectar
from cnmc_logistica.carga import cargar_foto, leer_config
from cnmc_logistica.categorias import Equivalencias
from cnmc_logistica.config import Rutas
from cnmc_logistica.parsers.origen_destino import normalizar_origen, parsear_origen_destino
from cnmc_logistica.parsers.pdf import PdfIlegible, analizar_pdf, parsear_capacidades

CFG = leer_config(Rutas(RAIZ))
F1 = "2026-11-11"


def pdf(clave: str) -> bytes:
    return (FIXTURES / f"pdf/{clave}.pdf").read_bytes()


@pytest.fixture
def conn(tmp_path):
    conexion = conectar(tmp_path / "prueba.db")
    yield conexion
    conexion.close()


# --- parser origen-destino -----------------------------------------------------


def test_origen_destino_estructura():
    od = parsear_origen_destino(leer("origen_destino/17.html"), 17)
    assert od.destino == "Puxeiros (Pontevedra)" and od.unidad == "€/m3" and od.avisos == []
    assert 2 <= len({f.modalidad for f in od.filas}) <= 8
    assert 10 <= len(od.filas) <= 60
    assert {f.estado for f in od.filas} == {"ofrecida", "no_ofrecida"}
    assert 1 <= len(od.notas) <= 15 and any("Euros/m3" in n for n in od.notas)


def test_cero_es_ruta_no_ofrecida_no_precio_cero():
    od = parsear_origen_destino(leer("origen_destino/123.html"), 123)
    conectadas = {
        f.origen: (f.estado, f.precio)
        for f in od.filas
        if f.modalidad == "Recepción desde instalaciones conectadas a EXOLUM"
    }
    assert conectadas == {
        "HUELVA": ("no_ofrecida", None),
        "CARTAGENA": ("no_ofrecida", None),
        "BILBAO": ("no_ofrecida", None),
        "BARCELONA": ("ofrecida", 22.26),
        "TARRAGONA": ("ofrecida", 20.09),
    }
    assert all((f.precio is None) == (f.estado == "no_ofrecida") for f in od.filas)
    assert all(f.precio is None or f.precio > 0 for f in od.filas)


def test_origenes_distintos_no_se_fusionan():
    assert normalizar_origen("Refinería  Castellón") == "REFINERIA CASTELLON"
    assert normalizar_origen("HUELVA") != normalizar_origen("REFINERÍA HUELVA")
    od = parsear_origen_destino(leer("origen_destino/17.html"), 17)
    normalizados = {f.origen_normalizado for f in od.filas}
    assert {"HUELVA", "REFINERIA HUELVA"} <= normalizados
    # El mismo origen puede aparecer en varias modalidades con precio distinto.
    huelva = {f.modalidad: f.precio for f in od.filas if f.origen == "HUELVA"}
    assert len(huelva) == 2 and len(set(huelva.values())) == 2


def test_origen_destino_valor_ilegible_se_anota_y_sigue():
    html = leer("origen_destino/17.html").replace("22,29", "consultar", 1)
    od = parsear_origen_destino(html, 17)
    assert [(a.nivel, a.tipo) for a in od.avisos] == [("error", "bloque_desconocido")]
    assert "consultar" in od.avisos[0].texto and len(od.filas) >= 10


def test_pagina_sin_cajas_de_precios():
    od = parsear_origen_destino("<html><body><div>DESTINO: X</div></body></html>", 1)
    assert od.filas == [] and "origen_destino_vacio" in [a.tipo for a in od.avisos]


# --- PDF genérico -----------------------------------------------------------------


def test_pdf_metadatos_y_tablas():
    doc = analizar_pdf(pdf("24"), "EXOLUM 2026 precios SSLL GG.pdf")
    assert (doc.paginas, doc.paginas_sin_texto, doc.anio) == (3, 0, 2026)
    assert doc.titulo.startswith("PRECIOS AÑO 2026")
    assert 3 <= len(doc.tablas) <= 12
    pagina, _, filas = max(doc.tablas, key=lambda t: len(t[2]))
    assert 1 <= pagina <= 3 and len(filas) >= 8 and len(filas[0]) >= 4


def test_pdf_pequeno_de_otro_operador():
    doc = analizar_pdf(pdf("7"), "DISA precios 2026_GC.pdf")
    assert doc.paginas == 1 and doc.anio == 2026 and "DISA" in " ".join(doc.lineas)
    assert any("Gasolinas" in " ".join(fila) for _, _, filas in doc.tablas for fila in filas)


def test_anio_del_nombre_si_el_texto_no_lo_dice():
    doc = analizar_pdf(pdf("7"))
    assert doc.anio == 2026  # está en el encabezado
    from cnmc_logistica.parsers.pdf import anio_de

    assert anio_de("sin año", "EXOLUM AV 2027 BURGOS.pdf") == 2027
    assert anio_de("nada", None) is None


def test_pdf_ilegible():
    with pytest.raises(PdfIlegible):
        analizar_pdf(b"<html>Error 500</html>")


# --- capacidades ------------------------------------------------------------------


def test_capacidades_estructura():
    cap = parsear_capacidades(pdf("capacidades"))
    assert cap.anio == 2026 and cap.descuadres == []
    assert len(cap.sin_interpretar) <= 5
    instalaciones = [f for f in cap.filas if f.producto == "TOTAL"]
    assert 100 <= len(instalaciones) <= 180
    assert {f.ambito for f in cap.filas} == {"almacenamiento", "aeroportuaria"}
    assert 30 <= len({f.compania for f in cap.filas}) <= 60
    assert all(f.capacidad_miles_m3 is None or f.capacidad_miles_m3 >= 0 for f in cap.filas)


def test_capacidades_columnas_no_se_mezclan():
    cap = parsear_capacidades(pdf("capacidades"))

    def de(compania, instalacion):
        return {
            f.producto: f.capacidad_miles_m3
            for f in cap.filas
            if (f.compania, f.instalacion) == (compania, instalacion)
        }

    # Columna izquierda y derecha de la misma página, con sus productos debajo.
    assert de("EXOLUM CORPORATION", "Álava (Rivabellosa)") == {
        "TOTAL": 96.519,
        "Gasolinas": 20.238,
        "Gasóleos": 76.281,
    }
    huelva = de("EXOLUM CORPORATION", "Huelva")
    assert huelva["TOTAL"] == 557.863 and huelva["Queroseno"] == 95.87
    assert huelva["TOTAL"] == pytest.approx(sum(v for k, v in huelva.items() if k != "TOTAL"))
    # Punto de millares.
    assert de("EXOLUM CORPORATION", "Madrid (Torrejón)")["TOTAL"] == 1180.565
    # Productos listados sin cifra: se conservan con capacidad NULL.
    decal = de("DECAL", "Barcelona")
    assert decal["TOTAL"] == 495.0 and decal["Bioetanol"] is None
    # Lista plana de aeropuertos (sin negritas ni desglose).
    assert de("EXOLUM AVIATION", "Madrid (Barajas)") == {"TOTAL": 0.18}
    assert de("CMD AEROPUERTOS CANARIOS", "Fuerteventura")["Querosenos"] == 12.0


def test_equivalencias_curadas():
    eq = CFG.equivalencias
    assert eq.id_tanque("almacenamiento", "EXOLUM CORPORATION", "Álava (Rivabellosa)") == (23, True)
    assert eq.id_tanque("aeroportuaria", "EXOLUM AVIATION", "Madrid (Barajas)") == (59, True)
    # Revisada y sin ficha en el portal: None, pero no avisa.
    assert eq.id_tanque("almacenamiento", "OLIVIA PETROLEUM", "Cádiz") == (None, True)
    assert eq.id_tanque("almacenamiento", "NUEVA, S.A.", "Teruel") == (None, False)
    cap = parsear_capacidades(pdf("capacidades"))
    sin = [f for f in cap.filas if not eq.id_tanque(f.ambito, f.compania, f.instalacion)[1]]
    assert sin == []


# --- carga en la base ---------------------------------------------------------------


def test_origen_destino_en_la_base_con_anio_del_pdf(conn, tmp_path):
    r = cargar_foto(conn, crear_foto(tmp_path / "raw", F1), CFG)
    assert r.errores == 0
    assert "origen_destino_no_cuadra_con_pdf" not in {a.tipo for a in r.avisos}
    filas = conn.execute(
        "SELECT origen, estado, precio, unidad, anio_tabla FROM precios_origen_destino "
        "WHERE id_tanque_instalacion = 123 AND modalidad LIKE 'Recepción desde instalaciones%' "
        "AND origen IN ('HUELVA', 'BARCELONA') ORDER BY origen"
    ).fetchall()
    assert [tuple(f) for f in filas] == [
        ("BARCELONA", "ofrecida", 22.26, "€/m3", 2026),
        ("HUELVA", "no_ofrecida", None, None, 2026),
    ]
    assert conn.execute("SELECT COUNT(*) FROM notas_origen_destino").fetchone()[0] >= 2
    # El precio principal que remite al PDF hereda el año de la tabla.
    anio = conn.execute(
        "SELECT anio_tabla FROM precios WHERE id_tanque_instalacion = 123 AND tipo_precio = 'pdf'"
    ).fetchone()[0]
    assert anio == 2026


def test_control_cruzado_html_frente_a_pdf(conn, tmp_path):
    def alterar(id_ti, html):
        return html.replace("22,26", "99,99", 1) if id_ti == 123 else html

    r = cargar_foto(conn, crear_foto(tmp_path / "raw", F1, cambiar_od=alterar), CFG)
    avisos = [a for a in r.avisos if a.tipo == "origen_destino_no_cuadra_con_pdf"]
    assert [(a.nivel, a.recurso) for a in avisos] == [("aviso", "origen_destino/123")]
    # La fila del PDF de Barajas se queda sin detalle; la de Puxeiros sigue enlazada.
    enlazados = dict(
        conn.execute(
            "SELECT documento, COUNT(DISTINCT id_tanque_instalacion_destino) FROM matrices_pdf "
            "WHERE tipo = 'origen_destino' GROUP BY 1"
        )
    )
    assert enlazados == {"24": 1, "32": 0}
    # El HTML es la fuente: el dato se carga igualmente.
    assert (
        conn.execute("SELECT COUNT(*) FROM precios_origen_destino WHERE precio = 99.99").fetchone()[
            0
        ]
        == 1
    )


def test_documentos_y_filas_de_tablas(conn, tmp_path):
    cargar_foto(conn, crear_foto(tmp_path / "raw", F1), CFG)
    docs = {f["clave"]: f for f in conn.execute("SELECT * FROM documentos")}
    assert set(docs) == {"24", "25", "26", "27", "32", "34", "capacidades"}
    assert docs["24"]["nombre_archivo"] == "EXOLUM 2026 precios SSLL GG.pdf"
    assert docs["24"]["anio"] == 2026 and docs["24"]["paginas"] == 3 and docs["24"]["tablas"] >= 3
    assert len(docs["24"]["sha256"]) == 64
    fila = conn.execute(
        "SELECT celdas FROM filas_documento WHERE documento = '24' ORDER BY pagina, tabla, fila"
    ).fetchone()
    assert isinstance(json.loads(fila[0]), list)
    assert conn.execute("SELECT COUNT(*) FROM filas_documento").fetchone()[0] >= 20


def test_pdf_desconocido_o_roto_no_aborta(conn, tmp_path):
    roto = (b"<html>Servicio no disponible</html>", portal.pdf(999), None)
    r = cargar_foto(conn, crear_foto(tmp_path / "raw", F1, pdfs_extra=(roto,)), CFG)
    assert [(a.nivel, a.tipo, a.recurso) for a in r.avisos if a.es_error] == [
        ("error", "pdf_ilegible", "pdf/999")
    ]
    assert conn.execute("SELECT COUNT(*) FROM documentos").fetchone()[0] == 7
    assert (
        conn.execute("SELECT COUNT(*) FROM incidencias WHERE tipo = 'pdf_ilegible'").fetchone()[0]
        == 1
    )


def test_capacidades_en_la_base_y_aviso_si_falta_equivalencia(conn, tmp_path):
    import dataclasses

    sin_tabla = dataclasses.replace(CFG, equivalencias=Equivalencias({}))
    r = cargar_foto(conn, crear_foto(tmp_path / "raw", F1), sin_tabla)
    assert r.errores == 0
    assert sum(a.tipo == "capacidad_sin_equivalencia" for a in r.avisos) >= 100
    total, con_id = conn.execute(
        "SELECT COUNT(*), COUNT(id_tanque) FROM capacidades WHERE producto = 'TOTAL'"
    ).fetchone()
    assert total >= 100 and con_id == 0  # se cargan igualmente, con id_tanque NULL


def test_capacidades_con_id_tanque(conn, tmp_path):
    cargar_foto(conn, crear_foto(tmp_path / "raw", F1), CFG)
    fila = conn.execute(
        "SELECT id_tanque, capacidad_miles_m3, anio FROM capacidades "
        "WHERE compania = 'EXOLUM CORPORATION' AND instalacion = 'Vigo (Puxeiros)' "
        "AND producto = 'TOTAL'"
    ).fetchone()
    assert fila["id_tanque"] == 9 and fila["anio"] == 2026 and fila["capacidad_miles_m3"] > 0
