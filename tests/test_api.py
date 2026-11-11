"""API REST y aplicación única (MCP + API), sobre la base de las fixtures."""

import asyncio
import csv
import io
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from ayuda import RAIZ, crear_foto
from cnmc_logistica.api import LIMITE_MAXIMO, crear_api
from cnmc_logistica.bd import conectar
from cnmc_logistica.carga import cargar_foto, leer_config
from cnmc_logistica.config import Rutas
from cnmc_logistica.servidor import app_http, crear_servidor

CFG = leer_config(Rutas(RAIZ))
F1, F2 = "2026-11-11", "2026-12-11"
V1 = "/api/v1"


def subir_precio(id_ti, html):
    if id_ti != 17:
        return html
    return html.replace("2,4577 &euro;/operaci&oacute;n", "2,6000 &euro;/operaci&oacute;n")


@pytest.fixture(scope="module")
def ruta_bd(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("api")
    conn = conectar(tmp / "cnmc.db")
    cargar_foto(conn, crear_foto(tmp / "raw", F1), CFG)
    cargar_foto(conn, crear_foto(tmp / "raw", F2, cambiar=subir_precio), CFG)
    conn.close()
    return tmp / "cnmc.db"


@pytest.fixture(scope="module")
def api(ruta_bd):
    return TestClient(crear_api(ruta_bd))


def mcp(ruta_bd, herramienta, **argumentos):
    async def llamar():
        async with Client(crear_servidor(ruta_bd)) as cliente:
            r = await cliente.call_tool(herramienta, argumentos)
            assert not r.is_error, r.content[0].text
            return json.loads(r.content[0].text)

    return asyncio.run(llamar())


# --- rutas ------------------------------------------------------------------------------


def test_salud_y_estado(api):
    r = api.get("/salud")
    assert r.status_code == 200 and r.json()["estado"] == "ok" and r.json()["ultima_foto"] == F2
    e = api.get(f"{V1}/estado").json()
    assert e["ultima_foto"] == F2 and e["cobertura_pdf"]["sin_cubrir"] == 0
    assert e["tablas"]["detalles"]["vigentes"] == 4 and e["ultimas_ejecuciones"]


def test_operadores(api):
    r = api.get(f"{V1}/operadores").json()
    assert r["total"] == 3 and {o["codigo"] for o in r["datos"]} == {"CLH", "CLHAV", "TEPSA"}
    todos = api.get(f"{V1}/operadores", params={"solo_con_instalaciones": False, "limit": 5}).json()
    assert todos["total"] > 30 and len(todos["datos"]) == 5 and todos["limit"] == 5
    uno = api.get(f"{V1}/operadores/clh").json()
    assert uno["codigo"] == "CLH" and {i["id_tanque"] for i in uno["lista_instalaciones"]} == {
        9,
        59,
    }
    assert api.get(f"{V1}/operadores/NOEXISTE").status_code == 404


def test_instalaciones_y_detalles(api):
    r = api.get(f"{V1}/instalaciones", params={"tipo": "aeroportuaria"}).json()
    assert [i["id_tanque"] for i in r["datos"]] == [59]
    assert api.get(f"{V1}/instalaciones", params={"producto": "gasoleos"}).json()["total"] == 2
    ficha = api.get(f"{V1}/instalaciones/59").json()
    assert {d["operador"] for d in ficha["detalles"]} == {"CLH", "CLHAV"}
    d = api.get(f"{V1}/detalles/17").json()
    assert d["operador"] == "CLH" and d["precios"] and d["precios_pdf"] and d["documentos_pdf"]
    assert d["instalacion_ficha"]["id_tanque"] == 9
    assert api.get(f"{V1}/instalaciones/99999").status_code == 404
    assert api.get(f"{V1}/detalles/99999").status_code == 404


def test_precios_filtros_fuente_e_historico(api):
    todos = api.get(f"{V1}/precios", params={"limit": LIMITE_MAXIMO}).json()
    assert todos["total"] == len(todos["datos"]) > 50
    assert {p["fuente"] for p in todos["datos"]} == {"html", "pdf"}
    pdf = api.get(
        f"{V1}/precios", params={"fuente": "pdf", "categoria": "almacenamiento_no_operativo"}
    ).json()
    assert {p["valor"] for p in pdf["datos"]} == {1.8883}
    assert all(p["aplica_a"]["detalles"] == 1 for p in pdf["datos"])
    web = api.get(
        f"{V1}/precios", params={"fuente": "web", "operador": "TEPSA", "tipo_precio": "regla"}
    ).json()
    assert web["total"] >= 1 and {p["tipo_precio"] for p in web["datos"]} == {"regla"}
    # Vigentes por defecto; con historico=true aparece también la versión anterior.
    filtro = {"operador": "CLH", "categoria": "transferencias", "fuente": "web"}
    vigentes = api.get(f"{V1}/precios", params=filtro).json()["datos"]
    assert sorted(p["valor"] for p in vigentes) == [2.4577, 2.6]  # detalles 123 y 17
    historia = api.get(f"{V1}/precios", params={**filtro, "historico": True}).json()["datos"]
    del_17 = sorted(
        (p["valor"], p["vigente"]) for p in historia if p["id_tanque_instalacion"] == 17
    )
    assert del_17 == [(2.4577, False), (2.6, True)]
    antiguo = next(p for p in historia if not p["vigente"])
    assert (antiguo["primera_vez_visto"], antiguo["ultima_vez_visto"]) == (F1, F1)


def test_historial_y_comparar(api):
    h = api.get(
        f"{V1}/precios/historial",
        params={"id_tanque_instalacion": 17, "etiqueta": "transferencias"},
    ).json()
    assert [(v["valor"], v["vigente"]) for v in h["versiones"]] == [(2.4577, False), (2.6, True)]
    c = api.get(f"{V1}/comparar", params={"categoria": "transferencias"}).json()
    assert {g["unidad"] for g in c["grupos"]} == {"€/operación", "€/m3"}
    assert any("no comparables" in a for a in c["avisos"])
    assert api.get(f"{V1}/comparar", params={"categoria": "teletransporte"}).status_code == 404
    assert api.get(f"{V1}/comparar").status_code == 422  # falta el parámetro obligatorio


def test_origen_destino_reconsignaciones_bonificaciones(api):
    od = api.get(f"{V1}/origen-destino", params={"origen": "huelva", "destino": "barajas"}).json()
    assert {f["estado"] for f in od["datos"]} == {"ofrecida", "no_ofrecida"}
    no = api.get(
        f"{V1}/origen-destino", params={"destino": "barajas", "estado": "no_ofrecida"}
    ).json()
    assert no["total"] >= 5 and all(f["precio"] is None for f in no["datos"])
    r = api.get(f"{V1}/reconsignaciones", params={"origen": "huelva", "destino": "puxeiros"}).json()
    assert r["total"] == 1 and r["datos"][0]["documento"] == "27" and r["datos"][0]["precio"] > 0
    assert api.get(f"{V1}/reconsignaciones", params={"origen": "Marte"}).status_code == 404
    b = api.get(f"{V1}/bonificaciones", params={"producto": "queroseno"}).json()
    assert [d["documento"] for d in b["documentos"]] == ["34"]


def test_condiciones_de_web_y_de_pdf_con_su_fuente(api):
    mermas = api.get(f"{V1}/condiciones", params={"etiqueta": "mermas", "operador": "TEPSA"}).json()
    assert mermas["total"] == 2 and {c["fuente"] for c in mermas["datos"]} == {"html"}
    assert {(c["subetiqueta"], c["valor"], c["unidad"]) for c in mermas["datos"]} == {
        ("Gasóleos", 0.5, "%"),
        ("Gasolinas", 0.5, "%"),
    }
    pdf = api.get(f"{V1}/condiciones", params={"fuente": "pdf"}).json()
    assert pdf["total"] >= 2 and {c["fuente"] for c in pdf["datos"]} == {"pdf"}
    assert (3.0, "días") in {(c["valor"], c["unidad"]) for c in pdf["datos"]}
    assert all(c["aplica_a"]["detalles"] >= 1 and c["documento"] for c in pdf["datos"])


def test_capacidades(api):
    r = api.get(
        f"{V1}/capacidades", params={"operador": "exolum aviation", "instalacion": "barajas"}
    ).json()
    assert [(c["producto"], c["capacidad_miles_m3"], c["id_tanque"]) for c in r["datos"]] == [
        ("TOTAL", 0.18, 59)
    ]


# --- requisitos transversales ---------------------------------------------------------------


def test_paginacion_con_total_y_tope(api):
    total = api.get(f"{V1}/capacidades").json()["total"]
    p1 = api.get(f"{V1}/capacidades", params={"limit": 7}).json()
    p2 = api.get(f"{V1}/capacidades", params={"limit": 7, "offset": 7}).json()
    assert p1["total"] == p2["total"] == total > 300 and len(p1["datos"]) == len(p2["datos"]) == 7
    assert p1["datos"] != p2["datos"] and (p2["limit"], p2["offset"]) == (7, 7)
    assert api.get(f"{V1}/capacidades", params={"offset": total}).json()["datos"] == []
    for malo in ({"limit": LIMITE_MAXIMO + 1}, {"limit": 0}, {"offset": -1}, {"limit": "muchos"}):
        assert api.get(f"{V1}/capacidades", params=malo).status_code == 422


def test_parametros_invalidos_dan_422(api):
    assert api.get(f"{V1}/instalaciones", params={"tipo": "submarina"}).status_code == 422
    assert api.get(f"{V1}/precios", params={"fuente": "fax"}).status_code == 422
    assert api.get(f"{V1}/instalaciones/abc").status_code == 422
    assert api.get(f"{V1}/precios/historial").status_code == 422


@pytest.mark.parametrize("ruta", ["precios", "instalaciones", "origen-destino", "capacidades"])
def test_formato_csv(api, ruta):
    r = api.get(f"{V1}/{ruta}", params={"formato": "csv", "limit": 5})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    filas = list(csv.DictReader(io.StringIO(r.text), delimiter=";"))
    en_json = api.get(f"{V1}/{ruta}", params={"limit": 5}).json()["datos"]
    assert 1 <= len(filas) == len(en_json) <= 5
    # Mismas columnas que el JSON (salvo las que el modelo añade vacías).
    assert set(filas[0]) >= {k for k, v in en_json[0].items() if v is not None}


def test_solo_lectura_cache_y_cors(api):
    r = api.get(f"{V1}/operadores", headers={"Origin": "https://ejemplo.org"})
    assert r.headers["cache-control"] == "public, max-age=86400"
    assert r.headers["access-control-allow-origin"] == "*"
    for metodo in ("post", "put", "delete", "patch"):
        assert getattr(api, metodo)(f"{V1}/operadores").status_code == 405
    previo = api.options(
        f"{V1}/operadores",
        headers={"Origin": "https://ejemplo.org", "Access-Control-Request-Method": "POST"},
    )
    assert previo.status_code == 400  # CORS solo admite GET
    assert "cache-control" not in api.get(f"{V1}/operadores/NOEXISTE").headers


def test_no_hay_sql_libre_en_la_api(api):
    rutas = api.get("/openapi.json").json()["paths"]
    assert not any("sql" in r.lower() for r in rutas)
    assert all(set(metodos) == {"get"} for metodos in rutas.values())
    assert api.get(f"{V1}/consulta_sql", params={"sql": "SELECT 1"}).status_code == 404


def test_openapi_documentado_en_espanol_con_ejemplos(api):
    esquema = api.get("/openapi.json").json()
    esperadas = {
        "/operadores", "/operadores/{codigo}", "/instalaciones", "/instalaciones/{id_tanque}",
        "/detalles/{id_tanque_instalacion}", "/precios", "/precios/historial", "/comparar",
        "/origen-destino", "/reconsignaciones", "/bonificaciones", "/condiciones", "/capacidades",
        "/estado",
    }  # fmt: skip
    assert {V1 + r for r in esperadas} <= set(esquema["paths"])
    texto = json.dumps(esquema, ensure_ascii=False)
    for ruta, metodos in esquema["paths"].items():
        op = metodos["get"]
        assert len(op.get("description", "") + op.get("summary", "")) > 15, ruta
        respuesta = op["responses"]["200"]["content"]["application/json"]
        con_ejemplo = "example" in respuesta or "example" in json.dumps(respuesta.get("schema", {}))
        referido = [
            n for n in esquema["components"]["schemas"] if f"schemas/{n}" in json.dumps(respuesta)
        ]
        assert con_ejemplo or any(
            "example" in json.dumps(esquema["components"]["schemas"][n])
            or any(
                "example" in json.dumps(esquema["components"]["schemas"].get(m, {}))
                for m in esquema["components"]["schemas"]
                if f"schemas/{m}" in json.dumps(esquema["components"]["schemas"][n])
            )
            for n in referido
        ), ruta
    assert "almacenistas" in texto and "no afiliado a la CNMC" in texto
    assert api.get("/docs").status_code == 200


# --- coherencia MCP ↔ REST ------------------------------------------------------------------


def test_la_misma_consulta_por_mcp_y_por_rest_da_los_mismos_datos(ruta_bd, api):
    assert (
        mcp(ruta_bd, "listar_operadores")["operadores"]
        == api.get(f"{V1}/operadores").json()["datos"]
    )
    assert (
        mcp(ruta_bd, "listar_instalaciones", tipo="almacenamiento")["instalaciones"]
        == api.get(f"{V1}/instalaciones", params={"tipo": "almacenamiento"}).json()["datos"]
    )
    assert mcp(ruta_bd, "ver_instalacion", id_tanque=9) == api.get(f"{V1}/instalaciones/9").json()
    por_mcp = mcp(ruta_bd, "buscar_precios", operador="CLH", categoria="suplemento", limite=500)
    por_rest = api.get(
        f"{V1}/precios", params={"operador": "CLH", "categoria": "suplemento"}
    ).json()
    assert por_mcp["precios"] == por_rest["datos"] and por_mcp["total"] == por_rest["total"]
    assert (
        mcp(ruta_bd, "comparar_servicio", categoria="transferencias")
        == api.get(f"{V1}/comparar", params={"categoria": "transferencias"}).json()
    )
    rutas_mcp = mcp(ruta_bd, "precios_origen_destino", origen="huelva", destino="puxeiros")
    rutas_rest = api.get(
        f"{V1}/origen-destino",
        params={"origen": "huelva", "destino": "puxeiros", "estado": "ofrecida"},
    ).json()
    assert rutas_mcp["rutas"] == rutas_rest["datos"]
    assert mcp(ruta_bd, "bonificaciones") == api.get(f"{V1}/bonificaciones").json()
    assert (
        mcp(ruta_bd, "reconsignaciones", destino="Zaragoza", limite=500)["filas"]
        == api.get(f"{V1}/reconsignaciones", params={"destino": "Zaragoza"}).json()["datos"]
    )


# --- una sola aplicación ---------------------------------------------------------------------


def test_app_unica_monta_mcp_api_docs_y_salud(ruta_bd, monkeypatch):
    monkeypatch.delenv("CNMC_MCP_TOKEN", raising=False)
    monkeypatch.setenv("CNMC_MCP_HOSTS_PERMITIDOS", "testserver")
    with TestClient(app_http(ruta_bd, "127.0.0.1", 8000)) as cliente:
        assert cliente.get("/salud").json()["estado"] == "ok"
        assert cliente.get(f"{V1}/operadores").json()["total"] == 3
        assert cliente.get("/docs").status_code == 200
        inicio = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "prueba", "version": "0"}},
        }  # fmt: skip
        cabeceras = {"Accept": "application/json, text/event-stream"}
        r = cliente.post("/mcp", json=inicio, headers=cabeceras)
        assert r.status_code == 200 and "logistica-hidrocarburos-cnmc" in r.text


def test_el_token_protege_solo_el_mcp(ruta_bd, monkeypatch):
    monkeypatch.setenv("CNMC_MCP_TOKEN", "secreto")
    monkeypatch.setenv("CNMC_MCP_HOSTS_PERMITIDOS", "testserver")
    with TestClient(app_http(ruta_bd, "127.0.0.1", 8000)) as cliente:
        assert cliente.post("/mcp", json={}).status_code == 401
        assert cliente.get(f"{V1}/estado").status_code == 200  # la API es pública
        assert cliente.get("/salud").status_code == 200
        con_token = cliente.post("/mcp", json={}, headers={"Authorization": "Bearer secreto"})
        assert con_token.status_code != 401


def test_sustituir_la_base_no_exige_reiniciar(ruta_bd, tmp_path):
    import os
    import shutil

    viva = tmp_path / "viva.db"
    shutil.copy(ruta_bd, viva)
    cliente = TestClient(crear_api(viva))
    assert cliente.get("/salud").json()["ultima_foto"] == F2
    # Llega una base nueva: se escribe aparte y se cambia de golpe (rename atómico).
    nueva = conectar(tmp_path / "nueva.tmp")
    cargar_foto(nueva, crear_foto(tmp_path / "raw", F1), CFG)
    nueva.close()
    os.replace(tmp_path / "nueva.tmp", viva)
    assert cliente.get("/salud").json()["ultima_foto"] == F1  # sin reiniciar la aplicación
    viva.unlink()
    assert cliente.get("/salud").status_code == 503


def test_la_api_no_importa_nada_del_etl():
    etl = ("descarga", "parsers", "inventario", "actualizar", "extraccion", "carga", "almacen",
           "releases", "cobertura", "categorias", "bd")  # fmt: skip
    codigo = (
        f"import sys, cnmc_logistica.api, cnmc_logistica.servidor; etl = {etl!r};"
        "malos = [m for m in sys.modules if m.startswith('cnmc_logistica.') and "
        "m.split('.')[1] in etl] + [m for m in ('bs4', 'pdfplumber', 'lxml', 'yaml', 'httpx_no') "
        "if m in sys.modules]; print(malos)"
    )
    salida = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True)
    assert salida.stdout.strip() == "[]", salida.stdout + salida.stderr
