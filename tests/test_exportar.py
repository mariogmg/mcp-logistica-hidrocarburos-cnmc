"""Exportación para la Release de datos: base, sha256, CSV, Parquet y diccionario."""

import csv
import gzip
import hashlib
import sqlite3
import zipfile

import pytest

from ayuda import RAIZ, crear_foto
from cnmc_logistica.bd import TABLAS, conectar
from cnmc_logistica.carga import cargar_foto, leer_config
from cnmc_logistica.config import Rutas
from cnmc_logistica.exportar import exportar

CFG = leer_config(Rutas(RAIZ))


@pytest.fixture(scope="module")
def exportacion(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("exportar")
    conn = conectar(tmp / "cnmc.db")
    cargar_foto(conn, crear_foto(tmp / "raw", "2026-11-11"), CFG)

    def subir(i, html):
        return html.replace("2,4577 &euro;", "2,6000 &euro;") if i == 17 else html

    cargar_foto(conn, crear_foto(tmp / "raw", "2026-12-11", cambiar=subir), CFG)
    conn.close()
    pytest.importorskip("pyarrow")
    return exportar(tmp / "cnmc.db", tmp / "salida"), tmp / "salida", tmp / "cnmc.db"


def test_base_y_sha256(exportacion):
    resumen, salida, original = exportacion
    assert resumen["foto"] == "2026-12-11"
    datos = (salida / "cnmc_logistica.db").read_bytes()
    assert datos == original.read_bytes()
    linea = (salida / "cnmc_logistica.db.sha256").read_text()
    assert linea == f"{hashlib.sha256(datos).hexdigest()}  cnmc_logistica.db\n"
    assert (
        sqlite3.connect(salida / "cnmc_logistica.db")
        .execute("PRAGMA integrity_check")
        .fetchone()[0]
        == "ok"
    )


def test_un_csv_y_un_parquet_por_tabla_con_las_filas_vigentes(exportacion):
    import pyarrow.parquet as pq

    resumen, salida, original = exportacion
    esperadas = {*TABLAS, "ejecuciones", "incidencias"}
    assert {p.name.removesuffix(".csv.gz") for p in (salida / "csv").iterdir()} == esperadas
    assert {p.stem for p in (salida / "parquet").iterdir()} == esperadas
    conn = sqlite3.connect(original)
    vigentes = conn.execute("SELECT COUNT(*) FROM precios_vigentes").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM precios").fetchone()[0]
    assert total == vigentes + 1  # el precio antiguo no se exporta: el historial está en la base
    with gzip.open(salida / "csv/precios.csv.gz", "rt", encoding="utf-8", newline="") as fh:
        filas = list(csv.DictReader(fh, delimiter=";"))
    assert len(filas) == vigentes == resumen["tablas"]["precios"]
    assert {"valor", "unidad", "categoria_servicio", "primera_vez_visto"} <= set(filas[0])
    tabla = pq.read_table(salida / "parquet/precios.parquet")
    assert tabla.num_rows == vigentes
    assert str(tabla.schema.field("valor").type) == "double"
    assert str(tabla.schema.field("id_tanque_instalacion").type) == "int64"
    valores = {v for v in tabla.column("valor").to_pylist() if v is not None}
    assert 2.6 in valores and 2.4577 in valores  # 2,4577 sigue vigente en otro detalle
    ejecuciones = pq.read_table(salida / "parquet/ejecuciones.parquet")
    assert ejecuciones.num_rows == 2  # las tablas de registro van enteras


def test_zip_por_formato(exportacion):
    _, salida, _ = exportacion
    for formato, sufijo in (("csv", ".csv.gz"), ("parquet", ".parquet")):
        with zipfile.ZipFile(salida / f"{formato}.zip") as z:
            nombres = z.namelist()
            assert len(nombres) == len(TABLAS) + 2
            assert all(n.startswith(f"{formato}/") and n.endswith(sufijo) for n in nombres)
            assert z.testzip() is None
    with zipfile.ZipFile(salida / "csv.zip") as z:
        primera = gzip.decompress(z.read("csv/detalles.csv.gz")).decode("utf-8").splitlines()[0]
    assert primera.startswith("id;")


def test_diccionario_describe_todas_las_tablas_y_columnas(exportacion):
    _, salida, original = exportacion
    texto = (salida / "DICCIONARIO.md").read_text(encoding="utf-8")
    assert "no afiliado a la CNMC" in texto and "## Unidades" in texto
    conn = sqlite3.connect(original)
    for tabla in [*TABLAS, "ejecuciones", "incidencias"]:
        assert f"## `{tabla}`" in texto
        for _, columna, *_resto in conn.execute(f"PRAGMA table_info({tabla})"):
            fila = next(
                ln
                for ln in texto.split(f"## `{tabla}`")[1].splitlines()
                if ln.startswith(f"| `{columna}` |")
            )
            assert len(fila.split("|")[2].strip()) > 5, f"{tabla}.{columna} sin descripción"


def test_exportar_sin_parquet_no_necesita_pyarrow(tmp_path, exportacion):
    _, _, original = exportacion
    r = exportar(original, tmp_path / "solo_csv", con_parquet=False)
    assert r["tablas"]["detalles"] == 4
    assert (tmp_path / "solo_csv/csv.zip").exists() and not (tmp_path / "solo_csv/parquet").exists()
