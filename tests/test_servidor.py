"""Servidor MCP probado con el cliente en memoria del SDK (sin red ni procesos)."""

import asyncio
import json
import subprocess
import sys

import pytest
from mcp import Client

from ayuda import RAIZ, crear_foto
from cnmc_logistica.bd import conectar
from cnmc_logistica.carga import cargar_foto, leer_config
from cnmc_logistica.config import Rutas
from cnmc_logistica.servidor import AVISO_LEGAL, _SoloConToken, app_http, crear_servidor

CFG = leer_config(Rutas(RAIZ))
F1, F2 = "2026-11-11", "2026-12-11"
HERRAMIENTAS = {
    "estado", "listar_operadores", "listar_instalaciones", "ver_instalacion", "buscar_precios",
    "comparar_servicio", "historial_precio", "precios_origen_destino", "capacidades",
    "consulta_sql",
}  # fmt: skip


def subir_precio(id_ti, html):
    if id_ti != 17:
        return html
    return html.replace("2,4577 &euro;/operaci&oacute;n", "2,6000 &euro;/operaci&oacute;n")


@pytest.fixture(scope="module")
def ruta_bd(tmp_path_factory):
    """Base con dos fotos: en la segunda sube un precio de Exolum."""
    tmp = tmp_path_factory.mktemp("servidor")
    conn = conectar(tmp / "cnmc.db")
    cargar_foto(conn, crear_foto(tmp / "raw", F1), CFG)
    cargar_foto(conn, crear_foto(tmp / "raw", F2, cambiar=subir_precio), CFG)
    conn.close()
    return tmp / "cnmc.db"


def llamar(ruta_bd, herramienta, **argumentos):
    """Llama a una herramienta por MCP y devuelve (resultado, es_error)."""

    async def _llamar():
        async with Client(crear_servidor(ruta_bd)) as cliente:
            r = await cliente.call_tool(herramienta, argumentos)
            texto = r.content[0].text if r.content else ""
            return (texto if r.is_error else json.loads(texto)), r.is_error

    return asyncio.run(_llamar())


def ok(ruta_bd, herramienta, **argumentos):
    resultado, es_error = llamar(ruta_bd, herramienta, **argumentos)
    assert not es_error, resultado
    return resultado


def test_expone_las_diez_herramientas_documentadas(ruta_bd):
    async def listar():
        async with Client(crear_servidor(ruta_bd)) as cliente:
            return (await cliente.list_tools()).tools, cliente.instructions

    herramientas, instrucciones = asyncio.run(listar())
    assert {h.name for h in herramientas} == HERRAMIENTAS
    assert all(h.description and len(h.description) > 40 for h in herramientas)
    assert "no afiliado a la CNMC" in instrucciones


def test_estado(ruta_bd):
    e = ok(ruta_bd, "estado")
    assert (e["primera_foto"], e["ultima_foto"], e["fotos"]) == (F1, F2, 2)
    assert e["aviso_legal"] == AVISO_LEGAL
    assert e["tablas"]["detalles"]["vigentes"] == 4
    assert "valor" in e["tablas"]["precios"]["columnas"]
    assert e["ultimas_ejecuciones"][0]["cambios"] >= 1


def test_listar_operadores(ruta_bd):
    r = ok(ruta_bd, "listar_operadores")
    por_codigo = {o["codigo"]: o for o in r["operadores"]}
    assert set(por_codigo) == {"CLH", "CLHAV", "TEPSA"}
    # Barajas cuenta para los dos titulares; Exolum Corporation tiene además Puxeiros.
    assert por_codigo["CLH"]["instalaciones_almacenamiento"] == 1
    assert por_codigo["CLH"]["instalaciones_aeroportuarias"] == 1
    assert por_codigo["CLHAV"]["instalaciones"] == 1 and por_codigo["CLHAV"]["detalles"] == 1
    assert r["instalaciones_totales_por_tipo"] == {"aeroportuaria": 1, "almacenamiento": 2}
    todos = ok(ruta_bd, "listar_operadores", solo_con_instalaciones=False)
    assert 30 <= todos["total_operadores"] <= 80


@pytest.mark.parametrize(
    ("filtros", "esperadas"),
    [
        ({}, {9, 17, 59}),
        ({"tipo": "aeroportuaria"}, {59}),
        ({"operador": "CLHAV"}, {59}),
        ({"operador": "tepsa"}, {17}),
        ({"producto": "queroseno"}, {59}),  # sin tilde ni plural exacto
        ({"producto": "gasoleos"}, {9, 17}),
        ({"comunidad": "GAL", "tipo": "almacenamiento"}, {9, 17}),
        ({"comunidad": "Madrid"}, set()),
    ],
)
def test_listar_instalaciones_con_filtros(ruta_bd, filtros, esperadas):
    r = ok(ruta_bd, "listar_instalaciones", **filtros)
    assert {i["id_tanque"] for i in r["instalaciones"]} == esperadas
    assert r["total"] == len(esperadas)


def test_ver_instalacion_con_dos_titulares(ruta_bd):
    r = ok(ruta_bd, "ver_instalacion", id_tanque=59)
    assert r["instalacion"]["tipo"] == "aeroportuaria"
    assert {d["operador"] for d in r["detalles"]} == {"CLH", "CLHAV"}
    corporation = next(d for d in r["detalles"] if d["operador"] == "CLH")
    assert corporation["discrepancia_operador"] == 1 and corporation["operador_buscador"] == "CLHAV"
    assert corporation["precios"] and corporation["condiciones"]
    assert corporation["rutas_origen_destino"] >= 10
    assert r["capacidades"][0]["capacidad_miles_m3"] == 0.18


def test_ver_instalacion_inexistente_devuelve_error_claro(ruta_bd):
    mensaje, es_error = llamar(ruta_bd, "ver_instalacion", id_tanque=99999)
    assert es_error and "99999" in mensaje


def test_buscar_precios(ruta_bd):
    r = ok(ruta_bd, "buscar_precios", operador="TEPSA", solo_numericos=True)
    assert r["total"] >= 10
    assert {p["operador"] for p in r["precios"]} == {"TEPSA"}
    assert all(p["valor"] is not None and p["tipo_precio"] == "numerico" for p in r["precios"])
    r = ok(ruta_bd, "buscar_precios", texto="transferencias")
    assert {p["categoria_servicio"] for p in r["precios"]} == {"transferencias"}
    assert ok(ruta_bd, "buscar_precios", limite=3)["precios"].__len__() == 3


def test_comparar_servicio_separa_grupos_no_comparables(ruta_bd):
    r = ok(ruta_bd, "comparar_servicio", categoria="transferencias")
    # Exolum cobra por operación y TEPSA por m3: dos grupos, con aviso.
    unidades = {g["unidad"] for g in r["grupos"]}
    assert unidades == {"€/operación", "€/m3"}
    assert any("no comparables" in a for a in r["avisos"])
    for g in r["grupos"]:
        valores = [f["valor"] for f in g["filas"]]
        assert g["resumen"]["minimo"] == min(valores) and g["resumen"]["maximo"] == max(valores)
        assert valores == sorted(valores)
    por_operacion = next(g for g in r["grupos"] if g["unidad"] == "€/operación")
    assert {f["valor"] for f in por_operacion["filas"]} == {2.6, 2.4577}  # vigentes de 17 y 123


def test_comparar_servicio_usa_indicadores_y_lista_los_no_numericos(ruta_bd):
    r = ok(ruta_bd, "comparar_servicio", categoria="logistica_aeroportuaria")
    assert r["grupos"] == []
    assert [s["tipo_precio"] for s in r["sin_precio_numerico"]] == ["pdf"]
    alm = ok(ruta_bd, "comparar_servicio", categoria="almacenamiento_y_manipulacion")
    (grupo,) = alm["grupos"]
    assert set(grupo["indicadores"]) == {
        "incluye_transporte", "incluye_almacenamiento", "incluye_expedicion",
        "incluye_puesta_a_bordo",
    }  # fmt: skip
    assert {f["tipo_valor"] for f in grupo["filas"]} == {"max", "min"}
    assert any("rango" in a for a in alm["avisos"])


def test_comparar_servicio_categoria_inexistente_sugiere_las_validas(ruta_bd):
    mensaje, es_error = llamar(ruta_bd, "comparar_servicio", categoria="teletransporte")
    assert es_error and "transferencias" in mensaje


def test_historial_precio_muestra_todas_las_versiones(ruta_bd):
    r = ok(ruta_bd, "historial_precio", id_tanque_instalacion=17, etiqueta="transferencias")
    assert [(v["valor"], v["primera_vez_visto"], v["ultima_vez_visto"], v["vigente"])
            for v in r["versiones"]] == [(2.4577, F1, F1, False), (2.6, F2, F2, True)]  # fmt: skip
    assert r["detalle"]["titular"] == "EXOLUM CORPORATION, S.A."


def test_precios_origen_destino(ruta_bd):
    r = ok(ruta_bd, "precios_origen_destino", origen="huelva", destino="puxeiros")
    assert r["total"] >= 2
    assert {f["origen"] for f in r["rutas"]} == {"HUELVA", "REFINERÍA HUELVA"}
    assert all(f["estado"] == "ofrecida" and f["precio"] > 0 for f in r["rutas"])
    assert all(f["anio_tabla"] == 2026 and f["unidad"] == "€/m3" for f in r["rutas"])
    assert r["notas"]
    # Barajas no recibe desde HUELVA por instalaciones conectadas: solo aparece si se pide.
    filtros = {"origen": "huelva", "destino": "barajas", "modalidad": "conectadas"}
    assert ok(ruta_bd, "precios_origen_destino", **filtros)["total"] == 0
    con_todas = ok(ruta_bd, "precios_origen_destino", incluir_no_ofrecidas=True, **filtros)
    assert [(f["estado"], f["precio"]) for f in con_todas["rutas"]] == [("no_ofrecida", None)]


def test_capacidades(ruta_bd):
    r = ok(ruta_bd, "capacidades", id_tanque=9)
    assert r["unidad"] == "miles de m3"
    assert {f["producto"] for f in r["capacidades"]} >= {"TOTAL", "Gasolinas", "Gasóleos"}
    totales = ok(ruta_bd, "capacidades", compania="exolum corporation", solo_totales=True)
    assert 20 <= totales["total"] <= 60 and totales["suma_totales_miles_m3"] > 1000


# --- consulta_sql: solo lectura ----------------------------------------------------


def test_consulta_sql_select(ruta_bd):
    r = ok(ruta_bd, "consulta_sql", sql="SELECT tipo, COUNT(*) AS n FROM instalaciones GROUP BY 1")
    assert r["columnas"] == ["tipo", "n"]
    assert sorted(map(tuple, r["filas"])) == [("aeroportuaria", 1), ("almacenamiento", 2)]
    r = ok(ruta_bd, "consulta_sql", sql="WITH t AS (SELECT plano('Gasóleos') AS p) SELECT p FROM t")
    assert r["filas"] == [["gasoleos"]]


def test_consulta_sql_limite_de_filas(ruta_bd):
    r = ok(ruta_bd, "consulta_sql", sql="SELECT id FROM precios", limite=5)
    assert len(r["filas"]) == 5 and r["truncado"] is True
    r = ok(ruta_bd, "consulta_sql", sql="SELECT id FROM precios", limite=10_000_000)
    assert r["limite"] == 500 and len(r["filas"]) <= 500


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM precios",
        "UPDATE precios SET valor = 0",
        "INSERT INTO meta VALUES ('x', 'y')",
        "DROP TABLE precios",
        "CREATE TABLE t (x)",
        "PRAGMA query_only = OFF",
        "ATTACH DATABASE ':memory:' AS otra",
        "SELECT 1; DELETE FROM precios",
        "UPDATE precios SET ultima_vez_visto = '2099-01-01'",
        "VACUUM",
    ],
)
def test_consulta_sql_rechaza_todo_lo_que_no_sea_un_select(ruta_bd, sql):
    antes = ok(ruta_bd, "consulta_sql", sql="SELECT COUNT(*), MAX(ultima_vez_visto) FROM precios")
    mensaje, es_error = llamar(ruta_bd, "consulta_sql", sql=sql)
    assert es_error, mensaje
    assert (
        ok(ruta_bd, "consulta_sql", sql="SELECT COUNT(*), MAX(ultima_vez_visto) FROM precios")
        == antes
    )


def test_consulta_sql_interrumpe_consultas_desbocadas(ruta_bd):
    infinita = (
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT COUNT(*) FROM n"
    )
    mensaje, es_error = llamar(ruta_bd, "consulta_sql", sql=infinita)
    assert es_error and "interrumpida" in mensaje


def test_la_conexion_del_servidor_no_puede_escribir(ruta_bd):
    import sqlite3

    from cnmc_logistica.servidor import abrir_solo_lectura

    conn = abrir_solo_lectura(ruta_bd)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("UPDATE precios SET ultima_vez_visto = '2099-01-01'")
    conn.close()


# --- separación ETL / servidor y transporte HTTP --------------------------------------


def test_el_servidor_no_carga_codigo_de_scraping():
    codigo = (
        "import sys, cnmc_logistica.servidor;"
        "malos = [m for m in sys.modules if m.startswith('cnmc_logistica.') and "
        "m.split('.')[1] in ('descarga', 'parsers', 'inventario', 'actualizar', 'extraccion', "
        "'carga', 'almacen')] + [m for m in ('bs4', 'pdfplumber', 'lxml') if m in sys.modules];"
        "print(malos)"
    )
    salida = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True)
    assert salida.stdout.strip() == "[]", salida.stdout + salida.stderr


def _peticion(app, cabeceras):
    """Hace una petición HTTP mínima a una app ASGI y devuelve el código de estado."""
    enviados = []

    async def recibir():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def enviar(mensaje):
        enviados.append(mensaje)

    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": cabeceras}
    asyncio.run(app(scope, recibir, enviar))
    return enviados[0]["status"]


def test_token_bearer_opcional():
    async def interior(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    protegida = _SoloConToken(interior, "secreto")
    assert _peticion(protegida, []) == 401
    assert _peticion(protegida, [(b"authorization", b"Bearer otro")]) == 401
    assert _peticion(protegida, [(b"authorization", b"secreto")]) == 401
    assert _peticion(protegida, [(b"authorization", b"Bearer secreto")]) == 200


def test_app_http_exige_token_solo_si_esta_configurado(ruta_bd, monkeypatch):
    monkeypatch.delenv("CNMC_MCP_TOKEN", raising=False)
    assert not isinstance(app_http(ruta_bd, "127.0.0.1", 8000), _SoloConToken)
    monkeypatch.setenv("CNMC_MCP_TOKEN", "secreto")
    monkeypatch.setenv("CNMC_MCP_HOSTS_PERMITIDOS", "mcp.ejemplo.org, localhost:8000")
    assert isinstance(app_http(ruta_bd, "0.0.0.0", 8000), _SoloConToken)
