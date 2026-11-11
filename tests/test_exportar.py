"""Exportación para la Release de datos: base, sha256, CSV, Parquet y diccionario."""

import csv
import hashlib
import sqlite3
import zipfile

import pytest

from ayuda import RAIZ, crear_foto
from cnmc_logistica.bd import TABLAS, conectar
from cnmc_logistica.carga import cargar_foto, leer_config
from cnmc_logistica.config import Rutas
from cnmc_logistica.exportar import PLANAS, exportar

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
    esperadas = {*TABLAS, "ejecuciones", "incidencias", *PLANAS}
    assert {p.name.removesuffix(".csv") for p in (salida / "csv").iterdir()} == esperadas
    assert {p.stem for p in (salida / "parquet").iterdir()} == esperadas
    conn = sqlite3.connect(original)
    vigentes = conn.execute("SELECT COUNT(*) FROM precios_vigentes").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM precios").fetchone()[0]
    assert total == vigentes + 1  # el precio antiguo no se exporta: el historial está en la base
    assert (salida / "csv/precios.csv").read_bytes().startswith(b"\xef\xbb\xbf")  # BOM: Excel
    assert ";2,4577;" in (salida / "csv/precios.csv").read_text(encoding="utf-8")  # coma decimal
    with (salida / "csv/precios.csv").open(encoding="utf-8-sig", newline="") as fh:
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


def test_historial_en_parquet_con_todas_las_versiones(exportacion):
    import pyarrow.parquet as pq

    _, salida, original = exportacion
    assert {p.stem for p in (salida / "historial").iterdir()} == set(TABLAS)  # sin las de registro
    conn = sqlite3.connect(original)
    total = conn.execute("SELECT COUNT(*) FROM precios").fetchone()[0]
    vigentes = conn.execute("SELECT COUNT(*) FROM precios_vigentes").fetchone()[0]
    tabla = pq.read_table(salida / "historial/precios.parquet")
    assert tabla.num_rows == total == vigentes + 1
    assert str(tabla.schema.field("vigente").type) == "int64"
    filas = tabla.to_pylist()
    assert sum(f["vigente"] for f in filas) == vigentes
    (antigua,) = [f for f in filas if not f["vigente"]]
    assert antigua["ultima_vez_visto"] < max(f["ultima_vez_visto"] for f in filas)
    # La versión antigua y la nueva de la misma clave están las dos.
    misma_clave = [
        f for f in filas
        if (f["id_tanque_instalacion"], f["etiqueta"]) == (antigua["id_tanque_instalacion"], antigua["etiqueta"])
    ]  # fmt: skip
    assert {f["vigente"] for f in misma_clave} == {0, 1}
    with zipfile.ZipFile(salida / "historial_parquet.zip") as z:
        assert sorted(z.namelist()) == sorted(f"historial/{t}.parquet" for t in TABLAS)
        assert z.testzip() is None
    assert "historial_parquet.zip" in (salida / "DICCIONARIO.md").read_text(encoding="utf-8")


def test_tablas_completas_con_nombres_cruzados_y_enlace(exportacion):
    import pyarrow.parquet as pq

    resumen, salida, original = exportacion
    with (salida / "csv/precios_completos.csv").open(encoding="utf-8-sig", newline="") as fh:
        precios = list(csv.DictReader(fh, delimiter=";"))
    conn = sqlite3.connect(original)
    web = conn.execute("SELECT COUNT(*) FROM precios_vigentes").fetchone()[0]
    assert len(precios) == resumen["tablas"]["precios_completos"]
    assert sum(p["fuente"] == "web" for p in precios) == web
    assert {p["fuente"] for p in precios} <= {"web", "pdf"}
    for p in precios:
        assert p["operador"] and p["instalacion"] and p["comunidad"] and p["categoria"]
        assert p["tipo_instalacion"] in ("almacenamiento", "aeroportuaria")
        assert p["url_cnmc"].endswith(
            f"detalle.htm?idTanqueInstalacion={p['id_tanque_instalacion']}"
        )
    tabla = pq.read_table(salida / "parquet/precios_completos.parquet")
    assert tabla.num_rows == len(precios)
    assert str(tabla.schema.field("valor").type) == "double"
    for nombre in PLANAS:
        with (salida / f"csv/{nombre}.csv").open(encoding="utf-8-sig", newline="") as fh:
            filas = list(csv.DictReader(fh, delimiter=";"))
        assert filas and all(f["url_cnmc"].startswith("https://") for f in filas), nombre
        assert pq.read_table(salida / f"parquet/{nombre}.parquet").num_rows == len(filas)
    diccionario = (salida / "DICCIONARIO.md").read_text(encoding="utf-8")
    assert diccionario.index("precios_completos.csv") < diccionario.index("## `operadores`")


def test_zip_por_formato(exportacion):
    _, salida, _ = exportacion
    for formato, sufijo in (("csv", ".csv"), ("parquet", ".parquet")):
        with zipfile.ZipFile(salida / f"{formato}.zip") as z:
            nombres = z.namelist()
            assert len(nombres) == len(TABLAS) + 2 + len(PLANAS)
            assert all(n.startswith(f"{formato}/") and n.endswith(sufijo) for n in nombres)
            assert z.testzip() is None
    with zipfile.ZipFile(salida / "csv.zip") as z:
        primera = z.read("csv/detalles.csv").decode("utf-8-sig").splitlines()[0]
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
    assert not (tmp_path / "solo_csv/historial").exists()
