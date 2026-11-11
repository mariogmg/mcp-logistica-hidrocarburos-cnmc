"""La capa de consultas: funciones puras sobre una conexión de solo lectura."""

import subprocess
import sys

import pytest

from ayuda import RAIZ, crear_foto
from cnmc_logistica import consultas
from cnmc_logistica.bd import conectar
from cnmc_logistica.carga import cargar_foto, leer_config
from cnmc_logistica.config import Rutas

ETL = ("descarga", "parsers", "inventario", "actualizar", "extraccion", "carga", "almacen",
       "releases", "cobertura", "categorias", "bd")  # fmt: skip


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("consultas")
    escritura = conectar(tmp / "cnmc.db")
    cargar_foto(escritura, crear_foto(tmp / "raw", "2026-11-11"), leer_config(Rutas(RAIZ)))
    escritura.close()
    lectura = consultas.abrir_solo_lectura(tmp / "cnmc.db")
    yield lectura
    lectura.close()


@pytest.mark.parametrize("modulo", ["consultas", "servidor"])
def test_la_capa_de_consultas_no_importa_nada_del_etl(modulo):
    codigo = (
        f"import sys, cnmc_logistica.{modulo};"
        f"etl = {ETL!r};"
        "malos = [m for m in sys.modules if m.startswith('cnmc_logistica.') and "
        "m.split('.')[1] in etl] + [m for m in ('bs4', 'pdfplumber', 'lxml', 'yaml') "
        "if m in sys.modules];"
        "print(malos)"
    )
    salida = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True)
    assert salida.stdout.strip() == "[]", salida.stdout + salida.stderr


def test_las_consultas_son_funciones_sobre_una_conexion(conn):
    r = consultas.listar_instalaciones(conn, tipo="aeroportuaria")
    assert [i["id_tanque"] for i in r["instalaciones"]] == [59]
    assert consultas.estado(conn)["ultima_foto"] == "2026-11-11"


def test_errores_propios_sin_depender_de_mcp_ni_de_http(conn, tmp_path):
    with pytest.raises(consultas.NoEncontrado, match="99999"):
        consultas.ver_instalacion(conn, id_tanque=99999)
    with pytest.raises(consultas.ErrorConsulta):
        consultas.consulta_sql(conn, sql="DELETE FROM precios")
    with pytest.raises(consultas.BaseNoDisponible):
        consultas.abrir_solo_lectura(tmp_path / "no_existe.db")
    assert issubclass(consultas.NoEncontrado, consultas.ErrorConsulta)
