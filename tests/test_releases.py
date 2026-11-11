"""descargar-releases: varias Releases simuladas → raw/ listo para reconstruir."""

import hashlib
import io
import json
import tarfile

import httpx
import pytest

from ayuda import RAIZ, crear_foto
from cnmc_logistica.bd import conectar
from cnmc_logistica.carga import cargar_foto, leer_config, reconstruir, registrar_ejecucion
from cnmc_logistica.config import Rutas
from cnmc_logistica.releases import ErrorReleases, descargar_releases

CFG = leer_config(Rutas(RAIZ))
REPO = "alguien/proyecto"
F1, F2 = "2026-11-11", "2026-12-02"


def tarball(carpeta, fecha: str) -> bytes:
    salida = io.BytesIO()
    with tarfile.open(fileobj=salida, mode="w:gz") as tar:
        # Como el tarball real subido desde macOS: con ficheros de metadatos «._*».
        basura = tarfile.TarInfo(f"._{fecha}")
        basura.size = 4
        tar.addfile(basura, io.BytesIO(b"Mac "))
        tar.add(carpeta, arcname=fecha)
    return salida.getvalue()


def linea(registrada_en: str, altas: int) -> str:
    return json.dumps(
        {"fecha": registrada_en[:10], "registrada_en": registrada_en, "version": "0.1.0",
         "recursos": 1, "altas": altas, "cambios": 0, "desapariciones": 0, "errores": 0,
         "avisos": 0, "por_tabla": {}, "incidencias": []}
    )  # fmt: skip


@pytest.fixture
def publicado(tmp_path):
    """Lo que habría en GitHub: dos fechas; la primera con tres descargas y un .jsonl suelto."""
    origen = tmp_path / "origen"
    conn = conectar(tmp_path / "origen.db")
    ficheros: dict[str, bytes] = {}

    # Foto 1, descarga principal: publicada antes de que existiera el registro (sin .jsonl).
    foto1 = crear_foto(origen / "a", F1)
    ficheros[f"raw-{F1}.tar.gz"] = tarball(foto1.dir, F1)
    # Sus dos primeras ejecuciones se recuperaron después en un .jsonl suelto.
    sueltas = [linea(f"{F1}T12:12:41+00:00", 8038), linea(f"{F1}T12:23:54+00:00", 0)]
    ficheros[f"raw-{F1}.ejecuciones.jsonl"] = ("\n".join(sueltas) + "\n").encode()
    # Otra descarga del mismo día, sin registro, y una tercera que ya lo lleva dentro.
    ficheros[f"raw-{F1}.ejecucion-1.tar.gz"] = tarball(crear_foto(origen / "b", F1).dir, F1)
    tercera = crear_foto(origen / "c", F1)
    registrar_ejecucion(tercera, conn, cargar_foto(conn, tercera, CFG))
    ficheros[f"raw-{F1}.ejecucion-2.tar.gz"] = tarball(tercera.dir, F1)
    # Foto 2: el caso normal, un tarball con su registro dentro.
    foto2 = crear_foto(origen / "d", F2)
    registrar_ejecucion(foto2, conn, cargar_foto(conn, foto2, CFG))
    ficheros[f"raw-{F2}.tar.gz"] = tarball(foto2.dir, F2)
    conn.close()
    for nombre in [n for n in ficheros if n.endswith(".tar.gz")]:
        ficheros[f"{nombre}.sha256"] = (
            f"{hashlib.sha256(ficheros[nombre]).hexdigest()}  {nombre}\n".encode()
        )
    return ficheros


def cliente_de(ficheros: dict[str, bytes], pedidos: list | None = None) -> httpx.Client:
    def activos(fecha):
        return [
            {"name": n, "browser_download_url": f"https://descargas.example/{n}"}
            for n in ficheros
            if n.startswith(f"raw-{fecha}")
        ]

    releases = [
        {"tag_name": f"raw-{F2}", "assets": activos(F2)},
        {"tag_name": "v0.2.0", "assets": [{"name": "otra.zip", "browser_download_url": "x"}]},
        {"tag_name": f"raw-{F1}", "assets": activos(F1)},
    ]

    def manejador(req: httpx.Request) -> httpx.Response:
        if pedidos is not None:
            pedidos.append(str(req.url))
        if req.url.host == "api.github.com":
            assert req.url.path == f"/repos/{REPO}/releases"
            return httpx.Response(200, json=releases)
        nombre = req.url.path.rsplit("/", 1)[-1]
        return (
            httpx.Response(200, content=ficheros[nombre])
            if nombre in ficheros
            else httpx.Response(404)
        )

    return httpx.Client(transport=httpx.MockTransport(manejador))


def test_descarga_todo_y_une_los_registros_sin_duplicar(publicado, tmp_path):
    raw = tmp_path / "proyecto" / "raw"
    r = descargar_releases(raw, REPO, cliente_de(publicado))
    assert r.fotos_nuevas == [F1, F2] and r.descargas_adicionales == [f"{F1}/1", f"{F1}/2"]
    # Añadidas al registro de la foto 1: 2 sueltas + 1 de la tercera descarga. El de la
    # foto 2 ya venía dentro de su tarball principal.
    assert r.lineas_de_registro == 3
    assert len((raw / F2 / "ejecuciones.jsonl").read_text().splitlines()) == 1
    # La foto principal queda en raw/FECHA y las otras descargas, aparte y completas.
    assert (raw / F1 / "manifiesto.jsonl").exists() and (raw / F1 / "detalles/17.html").exists()
    assert (raw / F1 / "_ejecuciones/1/manifiesto.jsonl").exists()
    assert (raw / F1 / "_ejecuciones/2/ejecuciones.jsonl").exists()
    registro = [json.loads(x) for x in (raw / F1 / "ejecuciones.jsonl").read_text().splitlines()]
    assert len(registro) == 3 and {x["altas"] for x in registro} >= {8038, 0}

    # Repetir la orden no baja las fotos otra vez ni duplica líneas.
    pedidos: list[str] = []
    r2 = descargar_releases(raw, REPO, cliente_de(publicado, pedidos))
    assert r2.fotos_nuevas == [] and r2.fotos_ya_presentes == [F1, F2]
    assert r2.lineas_de_registro == 0 and r2.descargas_adicionales == []
    assert not any(u.endswith(".tar.gz") for u in pedidos)
    assert len((raw / F1 / "ejecuciones.jsonl").read_text().splitlines()) == 3


def test_tras_descargar_y_reconstruir_se_recuperan_todas_las_ejecuciones(publicado, tmp_path):
    raiz = tmp_path / "proyecto"
    (raiz / "config").mkdir(parents=True)
    for fichero in (RAIZ / "config").glob("*.yaml"):
        (raiz / "config" / fichero.name).write_bytes(fichero.read_bytes())
    descargar_releases(raiz / "raw", REPO, cliente_de(publicado))
    reconstruir(Rutas(raiz), tmp_path / "nueva.db")
    conn = conectar(tmp_path / "nueva.db")
    filas = conn.execute(
        "SELECT fecha, tipo, origen, registrada_en FROM ejecuciones ORDER BY id"
    ).fetchall()
    resumen = [(f["fecha"], f["tipo"], f["origen"]) for f in filas]
    assert resumen == [
        (F1, "actualizacion", "registro"),
        (F1, "actualizacion", "registro"),
        (F1, "actualizacion", "registro"),
        (F1, "reconstruccion", "carga"),
        (F2, "actualizacion", "registro"),
        (F2, "reconstruccion", "carga"),
    ]
    reales = [f["registrada_en"] for f in filas if f["origen"] == "registro"]
    assert reales == sorted(reales)  # en orden cronológico aunque llegaran de tres ficheros
    assert conn.execute("SELECT COUNT(*) FROM detalles").fetchone()[0] == 4
    conn.close()


def test_sha256_incorrecto_o_tarball_malicioso_se_rechazan(publicado, tmp_path):
    alterado = dict(publicado)
    alterado[f"raw-{F2}.tar.gz.sha256"] = b"0" * 64 + b"  x\n"
    with pytest.raises(ErrorReleases, match="sha256"):
        descargar_releases(tmp_path / "a", REPO, cliente_de(alterado))

    malo = io.BytesIO()
    with tarfile.open(fileobj=malo, mode="w:gz") as tar:
        info = tarfile.TarInfo("../fuera.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    trampa = {f"raw-{F1}.tar.gz": malo.getvalue()}
    with pytest.raises(ErrorReleases, match="ruta inesperada"):
        descargar_releases(tmp_path / "b", REPO, cliente_de(trampa))
    assert not (tmp_path / "fuera.txt").exists()


def test_la_anotacion_inferida_del_bunker_queda_marcada(tmp_path):
    from ayuda import FIXTURES
    from cnmc_logistica import portal

    bunker = (
        (FIXTURES / "pdf/13.pdf").read_bytes(),
        portal.pdf(13),
        "EXOLUM 2026 precios_BUNKER.pdf",
    )
    conn = conectar(tmp_path / "b.db")
    cargar_foto(conn, crear_foto(tmp_path / "raw", F1, pdfs_extra=(bunker,)), CFG)
    notas = conn.execute(
        "SELECT pagina, metodo, texto FROM notas_pdf WHERE documento = '13'"
    ).fetchall()
    assert [(n["pagina"], n["metodo"]) for n in notas] == [(0, "inferido")]
    assert notas[0]["texto"].startswith("[INFERIDO]") and "€/Tm" in notas[0]["texto"]
    unidades = {f[0] for f in conn.execute("SELECT unidad FROM precios_pdf WHERE documento = '13'")}
    assert unidades == {None}  # la unidad sigue nula: la inferencia no rellena datos
    conn.close()
