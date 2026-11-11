"""Historial SCD2: altas, cambios, desapariciones, triggers e idempotencia."""

import dataclasses
import sqlite3

import pytest

from ayuda import RAIZ, crear_foto
from cnmc_logistica.bd import TABLAS, conectar, crear_esquema
from cnmc_logistica.carga import (
    FotoFueraDeOrden,
    FotoYaCargada,
    cargar_foto,
    leer_config,
    reconstruir,
)
from cnmc_logistica.categorias import Categorizador
from cnmc_logistica.config import Rutas

F1, F2, F3 = "2026-11-11", "2026-12-11", "2027-01-11"
CFG = leer_config(Rutas(RAIZ))


@pytest.fixture
def conn(tmp_path):
    conexion = conectar(tmp_path / "prueba.db")
    yield conexion
    conexion.close()


def cargar(conn, tmp_path, fecha, **kw):
    return cargar_foto(conn, crear_foto(tmp_path / "raw", fecha, **kw), CFG)


def filas(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLAS}


def subir_precio(id_ti, html):
    # Transferencias de Exolum pasa de 2,4577 a 2,6000 con nueva fecha de vigencia.
    if id_ti != 17:
        return html
    nuevo = html.replace("2,4577 &euro;/operaci&oacute;n", "2,6000 &euro;/operaci&oacute;n")
    assert nuevo != html
    return nuevo


# --- primera carga -------------------------------------------------------------


def test_primera_carga_todo_son_altas(conn, tmp_path):
    r = cargar(conn, tmp_path, F1)
    assert r.cambios == 0 and r.desapariciones == 0 and r.errores == 0
    n = filas(conn)
    assert n["instalaciones"] == 3 and n["detalles"] == 4
    assert 30 <= n["operadores"] <= 80
    assert 40 <= n["precios"] <= 200 and 15 <= n["condiciones"] <= 120
    assert r.altas == sum(n.values())
    fechas = conn.execute(
        "SELECT DISTINCT primera_vez_visto, ultima_vez_visto FROM precios"
    ).fetchall()
    assert [tuple(f) for f in fechas] == [(F1, F1)]


def test_operador_por_titular_y_discrepancia_con_el_buscador(conn, tmp_path):
    cargar(conn, tmp_path, F1)
    det = {
        f["id_tanque_instalacion"]: tuple(f)[1:]
        for f in conn.execute(
            "SELECT id_tanque_instalacion, operador, operador_buscador, discrepancia_operador "
            "FROM detalles"
        )
    }
    assert det[17] == ("CLH", "CLH", 0)
    assert det[33] == ("TEPSA", "TEPSA", 0)
    # Barajas: la página dice Exolum Corporation; el buscador lo lista bajo Exolum Aviation.
    assert det[123] == ("CLH", "CLHAV", 1)
    assert det[124] == ("CLHAV", "CLHAV", 0)


def test_precios_tipo_valor_y_tipo_precio(conn, tmp_path):
    cargar(conn, tmp_path, F1)
    tepsa = conn.execute(
        "SELECT tipo_valor, valor, unidad, fecha_precio, categoria_servicio FROM precios "
        "WHERE id_tanque_instalacion = 33 AND seccion = 'principal' ORDER BY tipo_valor"
    ).fetchall()
    assert [tuple(f) for f in tepsa] == [
        ("max", 4.12, "€/m3 y mes", "2026-03-16", "almacenamiento_y_manipulacion"),
        ("min", 2.12, "€/m3 y mes", "2026-01-01", "almacenamiento_y_manipulacion"),
    ]
    sin_numero = conn.execute(
        "SELECT COUNT(*) FROM precios WHERE tipo_precio != 'numerico' "
        "AND (valor IS NOT NULL OR tipo_valor IS NOT NULL OR texto_original = '')"
    ).fetchone()[0]
    assert sin_numero == 0
    tipos = {f[0] for f in conn.execute("SELECT DISTINCT tipo_precio FROM precios")}
    assert tipos == {"numerico", "regla", "texto", "pdf"}


def test_categoria_segun_tipo_de_instalacion_e_indicadores(conn, tmp_path):
    cargar(conn, tmp_path, F1)
    consulta = (
        "SELECT categoria_servicio, incluye_transporte, incluye_almacenamiento, "
        "incluye_expedicion, incluye_puesta_a_bordo FROM precios "
        "WHERE id_tanque_instalacion = ? AND seccion = 'principal'"
    )
    # Exolum en terminal: logística básica con transporte, almacenamiento y expedición.
    assert tuple(conn.execute(consulta, (17,)).fetchone()) == ("logistica_basica", 1, 1, 1, 0)
    # Exolum hacia Barajas: misma familia de etiqueta, pero en aeropuerto y sin expedición.
    assert tuple(conn.execute(consulta, (123,)).fetchone()) == (
        "logistica_aeroportuaria", 1, 1, 0, 0,
    )  # fmt: skip
    # Los indicadores solo existen en servicios principales.
    otros = conn.execute(
        "SELECT COUNT(*) FROM precios WHERE seccion = 'otros_servicios' "
        "AND incluye_transporte IS NOT NULL"
    ).fetchone()[0]
    assert otros == 0


def test_productos_normalizados_y_cadena_original(conn, tmp_path):
    cargar(conn, tmp_path, F1)
    assert (
        conn.execute(
            "SELECT productos_texto FROM detalles WHERE id_tanque_instalacion = 17"
        ).fetchone()[0]
        == "Gasolinas y Gasóleos"
    )
    productos = conn.execute(
        "SELECT producto FROM detalle_productos WHERE id_tanque_instalacion = 17 ORDER BY 1"
    ).fetchall()
    assert [p[0] for p in productos] == ["Gasolinas", "Gasóleos"]


# --- idempotencia e historial ----------------------------------------------------


def test_segunda_carga_inmediata_no_crea_filas(conn, tmp_path):
    cargar(conn, tmp_path, F1)
    antes = filas(conn)
    almacen = crear_foto(tmp_path / "otra", F1)
    r = cargar_foto(conn, almacen, CFG)
    assert filas(conn) == antes
    assert (r.altas, r.cambios, r.desapariciones) == (0, 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM ejecuciones").fetchone()[0] == 2


def test_foto_posterior_sin_cambios_solo_avanza_ultima_vez_visto(conn, tmp_path):
    cargar(conn, tmp_path, F1)
    antes = filas(conn)
    r = cargar(conn, tmp_path, F2)
    assert filas(conn) == antes and (r.altas, r.cambios, r.desapariciones) == (0, 0, 0)
    for tabla in ("detalles", "precios", "condiciones", "operadores_nombres"):
        fechas = conn.execute(
            f"SELECT DISTINCT primera_vez_visto, ultima_vez_visto FROM {tabla}"
        ).fetchall()
        assert [tuple(f) for f in fechas] == [(F1, F2)]


def test_un_cambio_de_precio_inserta_fila_nueva_y_conserva_la_antigua(conn, tmp_path):
    cargar(conn, tmp_path, F1)
    antes = filas(conn)
    r = cargar(conn, tmp_path, F2, cambiar=subir_precio)

    assert filas(conn)["precios"] == antes["precios"] + 1
    assert r.por_tabla["precios"] == (0, 1, 0) and r.cambios == 1
    historia = conn.execute(
        "SELECT valor, primera_vez_visto, ultima_vez_visto FROM precios "
        "WHERE id_tanque_instalacion = 17 AND etiqueta = 'Transferencias' ORDER BY id"
    ).fetchall()
    assert [tuple(f) for f in historia] == [(2.4577, F1, F1), (2.6, F2, F2)]
    vigente = conn.execute(
        "SELECT valor FROM precios_vigentes "
        "WHERE id_tanque_instalacion = 17 AND etiqueta = 'Transferencias'"
    ).fetchall()
    assert [f[0] for f in vigente] == [2.6]


def test_valor_que_vuelve_tras_un_cambio_es_una_fila_nueva(conn, tmp_path):
    cargar(conn, tmp_path, F1)
    cargar(conn, tmp_path, F2, cambiar=subir_precio)
    cargar(conn, tmp_path, F3)
    historia = conn.execute(
        "SELECT valor, primera_vez_visto, ultima_vez_visto FROM precios "
        "WHERE id_tanque_instalacion = 17 AND etiqueta = 'Transferencias' ORDER BY id"
    ).fetchall()
    assert [tuple(f) for f in historia] == [(2.4577, F1, F1), (2.6, F2, F2), (2.4577, F3, F3)]


def test_desaparicion_de_un_detalle(conn, tmp_path):
    cargar(conn, tmp_path, F1)
    antes = filas(conn)
    r = cargar(conn, tmp_path, F2, sin_detalles=(124,))
    assert filas(conn) == antes  # nada se borra
    assert r.por_tabla["detalles"] == (0, 0, 1) and r.desapariciones > 1
    fila = conn.execute(
        "SELECT ultima_vez_visto FROM detalles WHERE id_tanque_instalacion = 124"
    ).fetchone()
    assert fila[0] == F1
    vigentes = {f[0] for f in conn.execute("SELECT id_tanque_instalacion FROM detalles_vigentes")}
    assert vigentes == {17, 33, 123}


def test_cambio_de_nombre_de_un_operador_queda_en_el_historial(conn, tmp_path):
    cargar(conn, tmp_path, F1)
    almacen = crear_foto(tmp_path / "raw", F2)
    from cnmc_logistica import portal

    formulario = almacen.leer_texto(almacen.buscar("buscador", "formulario"))[0]
    renombrado = formulario.replace(">VISTA ENERGIA, S.L.<", ">VISTA ENERGIA RENOVADA, S.L.<")
    assert renombrado != formulario
    almacen.guardar(
        portal.formulario_buscador(), renombrado.encode(), url_final="u", estado_http=200,
        cabeceras={"content-type": "text/html;charset=UTF-8"},
    )  # fmt: skip
    cargar_foto(conn, almacen, CFG)
    nombres = conn.execute(
        "SELECT nombre, primera_vez_visto, ultima_vez_visto FROM operadores_nombres "
        "WHERE codigo = 'VISTA' ORDER BY id"
    ).fetchall()
    assert [tuple(f) for f in nombres] == [
        ("VISTA ENERGIA, S.L.", F1, F1),
        ("VISTA ENERGIA RENOVADA, S.L.", F2, F2),
    ]
    assert conn.execute("SELECT COUNT(*) FROM operadores WHERE codigo = 'VISTA'").fetchone()[0] == 1


def test_misma_foto_con_otro_contenido_se_rechaza(conn, tmp_path):
    cargar(conn, tmp_path, F1)
    antes = filas(conn)
    almacen = crear_foto(tmp_path / "otra", F1, cambiar=subir_precio)
    with pytest.raises(FotoYaCargada):
        cargar_foto(conn, almacen, CFG)
    assert filas(conn) == antes  # la transacción se deshace entera
    assert conn.execute("SELECT COUNT(*) FROM ejecuciones").fetchone()[0] == 1


def test_fotos_en_orden_cronologico(conn, tmp_path):
    cargar(conn, tmp_path, F2)
    with pytest.raises(FotoFueraDeOrden):
        cargar(conn, tmp_path, F1)


# --- triggers --------------------------------------------------------------------


def test_todas_las_tablas_tienen_sus_triggers(conn):
    crear_esquema(conn)
    triggers = {f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    for tabla in TABLAS:
        assert {f"tr_{tabla}_sin_delete", f"tr_{tabla}_solo_ultima_vez"} <= triggers
    for tabla in ("ejecuciones", "incidencias"):
        assert {f"tr_{tabla}_sin_delete", f"tr_{tabla}_sin_update"} <= triggers


def test_delete_rechazado(conn, tmp_path):
    cargar(conn, tmp_path, F1, cambiar=lambda i, h: h.replace("OTROS SERVICIOS", "X", 1))
    con_filas = [t for t, n in filas(conn).items() if n] + ["ejecuciones", "incidencias"]
    assert len(con_filas) >= 10
    for tabla in con_filas:
        with pytest.raises(sqlite3.IntegrityError, match="inmutable"):
            conn.execute(f"DELETE FROM {tabla}")
        with pytest.raises(sqlite3.IntegrityError, match="inmutable"):
            conn.execute(f"DELETE FROM {tabla} WHERE id = 1")


def test_update_solo_puede_avanzar_ultima_vez_visto(conn, tmp_path):
    cargar(conn, tmp_path, F1)
    for sentencia in (
        "UPDATE precios SET valor = 0 WHERE valor IS NOT NULL",
        "UPDATE precios SET primera_vez_visto = '2020-01-01'",
        "UPDATE detalles SET operador = 'OTRO'",
        "UPDATE condiciones SET texto_original = 'x'",
        "UPDATE precios SET ultima_vez_visto = ultima_vez_visto, texto = 'x'",
        "UPDATE ejecuciones SET errores = 0",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="inmutable"):
            conn.execute(sentencia)
    conn.execute("UPDATE precios SET ultima_vez_visto = ?", (F2,))  # permitido
    with pytest.raises(sqlite3.IntegrityError):  # pero no hacia atrás
        conn.execute("UPDATE precios SET ultima_vez_visto = ?", (F1,))


# --- incidencias y reconstrucción ---------------------------------------------------


def test_etiqueta_sin_categoria_se_carga_y_queda_en_incidencias(conn, tmp_path):
    almacen = crear_foto(tmp_path / "raw", F1)
    solo_una = Categorizador(
        {"categorias": {"transferencias": "x"}, "etiquetas": {"Transferencias": "transferencias"}}
    )
    r = cargar_foto(conn, almacen, dataclasses.replace(CFG, categorias=solo_una))
    assert r.errores == 0  # es un aviso: no pone el workflow en rojo
    cats = dict(conn.execute("SELECT categoria_servicio, COUNT(*) FROM precios GROUP BY 1"))
    assert set(cats) == {"transferencias", "sin_categoria"}
    inc = conn.execute(
        "SELECT recurso, operador, texto FROM incidencias WHERE tipo = 'sin_categoria' "
        "AND recurso = 'detalle/33' AND texto = 'Limpieza de tanques'"
    ).fetchall()
    assert [tuple(f) for f in inc] == [("detalle/33", "TEPSA", "Limpieza de tanques")]


def test_bloque_desconocido_queda_registrado_con_id_operador_y_texto(conn, tmp_path):
    def romper(id_ti, html):
        if id_ti != 33:
            return html
        return html.replace("OTROS SERVICIOS", "TARIFAS ESPECIALES", 1)

    r = cargar(conn, tmp_path, F1, cambiar=romper)
    inc = conn.execute(
        "SELECT nivel, recurso, operador, texto FROM incidencias WHERE tipo = 'bloque_desconocido'"
    ).fetchall()
    assert inc and all(f["recurso"] == "detalle/33" and f["operador"] == "TEPSA" for f in inc)
    assert any("TARIFAS ESPECIALES" in f["texto"] for f in inc)
    # El resto de la página y de la foto se ha cargado igualmente.
    assert conn.execute("SELECT COUNT(*) FROM detalles").fetchone()[0] == 4
    assert r.ejecucion_id > 0


def test_reconstruir_da_lo_mismo_que_la_carga_incremental(tmp_path):
    raiz = tmp_path / "proyecto"
    (raiz / "config").mkdir(parents=True)
    for fichero in (RAIZ / "config").glob("*.yaml"):
        (raiz / "config" / fichero.name).write_bytes(fichero.read_bytes())
    crear_foto(raiz / "raw", F1)
    crear_foto(raiz / "raw", F2, cambiar=subir_precio)

    incremental = conectar(tmp_path / "incremental.db")
    for fecha in (F1, F2):
        from cnmc_logistica.almacen import AlmacenCrudo

        cargar_foto(incremental, AlmacenCrudo(raiz / "raw", fecha), CFG)

    resumenes = reconstruir(Rutas(raiz), tmp_path / "nueva.db")
    assert [r.fecha for r in resumenes] == [F1, F2]
    nueva = conectar(tmp_path / "nueva.db")

    def volcado(c):
        return {t: c.execute(f"SELECT * FROM {t} ORDER BY id").fetchall() for t in TABLAS}

    assert {t: [tuple(f) for f in v] for t, v in volcado(nueva).items()} == {
        t: [tuple(f) for f in v] for t, v in volcado(incremental).items()
    }
    with pytest.raises(FileExistsError):
        reconstruir(Rutas(raiz), tmp_path / "nueva.db")
