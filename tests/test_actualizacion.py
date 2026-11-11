"""Actualización automática de la base del usuario, con la red simulada."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ayuda import RAIZ, crear_foto
from cnmc_logistica import actualizacion
from cnmc_logistica.actualizacion import actualizar_si_procede, elegir_base, foto_de
from cnmc_logistica.bd import conectar
from cnmc_logistica.carga import cargar_foto, leer_config
from cnmc_logistica.config import Rutas

CFG = leer_config(Rutas(RAIZ))
F1, F2 = "2026-11-11", "2026-12-02"
AHORA = datetime(2026, 12, 3, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def bases(tmp_path_factory):
    """Bytes de dos bases: la de noviembre y la de diciembre."""
    tmp = tmp_path_factory.mktemp("bases")
    conn = conectar(tmp / "a.db")
    cargar_foto(conn, crear_foto(tmp / "raw", F1), CFG)
    conn.close()
    vieja = (tmp / "a.db").read_bytes()
    conn = conectar(tmp / "a.db")
    cargar_foto(conn, crear_foto(tmp / "raw", F2), CFG)
    conn.close()
    return vieja, (tmp / "a.db").read_bytes()


def github(releases: dict[str, bytes], pedidos: list | None = None, sha_falso: bool = False):
    """Cliente que simula la API de GitHub con una Release por etiqueta."""

    def manejador(req: httpx.Request) -> httpx.Response:
        if pedidos is not None:
            pedidos.append(str(req.url))
        if req.url.host == "api.github.com":
            return httpx.Response(200, json=[
                {"tag_name": etiqueta, "assets": [
                    {"name": n, "browser_download_url": f"https://d.example/{etiqueta}/{n}"}
                    for n in ("cnmc_logistica.db", "cnmc_logistica.db.sha256", "csv.zip")]}
                for etiqueta in [*releases, "raw-2026-11-11", "v0.3.0"]
            ])  # fmt: skip
        etiqueta, nombre = req.url.path.strip("/").split("/")
        if nombre.endswith(".sha256"):
            resumen = "0" * 64 if sha_falso else hashlib.sha256(releases[etiqueta]).hexdigest()
            return httpx.Response(200, text=f"{resumen}  cnmc_logistica.db\n")
        return httpx.Response(200, content=releases[etiqueta])

    return httpx.Client(transport=httpx.MockTransport(manejador))


def sin_red():
    def manejador(req):
        raise httpx.ConnectError("sin red")

    return httpx.Client(transport=httpx.MockTransport(manejador))


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    for variable in ("CNMC_SIN_ACTUALIZAR", "CNMC_BD", "CNMC_DIR_DATOS"):
        monkeypatch.delenv(variable, raising=False)


def test_primer_arranque_descarga_la_base(bases, tmp_path):
    vieja, nueva = bases
    ruta = tmp_path / "datos" / "cnmc_logistica.db"
    avisos = []
    r = actualizar_si_procede(ruta, cliente=github({f"datos-{F1}": vieja, f"datos-{F2}": nueva}),
                              ahora=AHORA, registrar=avisos.append)  # fmt: skip
    assert r == f"actualizada a la foto {F2}" and foto_de(ruta) == F2
    assert ruta.read_bytes() == nueva and any(F2 in a for a in avisos)
    assert json.loads((ruta.parent / "actualizacion.json").read_text())["release"] == f"datos-{F2}"
    assert [p.name for p in ruta.parent.iterdir() if p.name.startswith(".descarga")] == []


def test_actualiza_si_hay_una_release_mas_reciente_y_solo_una_vez_al_dia(bases, tmp_path):
    vieja, nueva = bases
    ruta = tmp_path / "cnmc_logistica.db"
    ruta.write_bytes(vieja)
    pedidos: list[str] = []
    cliente = github({f"datos-{F1}": vieja, f"datos-{F2}": nueva}, pedidos)
    assert (
        actualizar_si_procede(ruta, cliente=cliente, ahora=AHORA) == f"actualizada a la foto {F2}"
    )
    assert foto_de(ruta) == F2 and len(pedidos) == 3  # API + sha256 + base
    # Mismo día: ni una petición más.
    r = actualizar_si_procede(ruta, cliente=cliente, ahora=AHORA + timedelta(hours=5))
    assert r == "comprobada hace menos de un día" and len(pedidos) == 3
    # Al día siguiente se vuelve a mirar; no hay nada nuevo y no se descarga la base.
    r = actualizar_si_procede(ruta, cliente=cliente, ahora=AHORA + timedelta(days=1, minutes=1))
    assert r == f"al día (foto {F2})" and len(pedidos) == 4


def test_sin_red_sigue_con_la_base_que_hay(bases, tmp_path):
    ruta = tmp_path / "cnmc_logistica.db"
    ruta.write_bytes(bases[0])
    avisos = []
    r = actualizar_si_procede(ruta, cliente=sin_red(), ahora=AHORA, registrar=avisos.append)
    assert r == "error: ConnectError" and ruta.read_bytes() == bases[0]
    assert "Se sigue con lo que hay" in avisos[0]
    assert not (tmp_path / "actualizacion.json").exists()  # se reintentará en el próximo arranque
    # Sin base y sin red tampoco lanza: quien arranca verá que no hay base y lo dirá.
    assert actualizar_si_procede(
        tmp_path / "vacia" / "x.db", cliente=sin_red(), ahora=AHORA
    ).startswith("error")


def test_sha256_incorrecto_o_base_corrupta_no_sustituyen_nada(bases, tmp_path):
    vieja, nueva = bases
    ruta = tmp_path / "cnmc_logistica.db"
    ruta.write_bytes(vieja)
    falso = github({f"datos-{F2}": nueva}, sha_falso=True)
    assert actualizar_si_procede(ruta, cliente=falso, ahora=AHORA) == "error: ValueError"
    assert ruta.read_bytes() == vieja
    corrupta = github({f"datos-{F2}": b"esto no es una base de datos"})
    assert actualizar_si_procede(ruta, cliente=corrupta, ahora=AHORA).startswith("error")
    assert ruta.read_bytes() == vieja
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".descarga")] == []


def test_una_release_antigua_no_sustituye_a_una_base_mas_reciente(bases, tmp_path):
    vieja, nueva = bases
    ruta = tmp_path / "cnmc_logistica.db"
    ruta.write_bytes(nueva)
    r = actualizar_si_procede(ruta, cliente=github({f"datos-{F1}": vieja}), ahora=AHORA)
    assert r == f"al día (foto {F2})" and ruta.read_bytes() == nueva


def test_se_puede_desactivar(bases, tmp_path, monkeypatch):
    monkeypatch.setenv("CNMC_SIN_ACTUALIZAR", "1")
    pedidos: list[str] = []
    ruta = tmp_path / "cnmc_logistica.db"
    r = actualizar_si_procede(ruta, cliente=github({f"datos-{F2}": bases[1]}, pedidos), ahora=AHORA)
    assert r == "desactivada" and pedidos == [] and not ruta.exists()


def test_que_base_se_usa(tmp_path, monkeypatch):
    monkeypatch.setenv("CNMC_DIR_DATOS", str(tmp_path / "usuario"))
    # Fuera del repositorio: la carpeta de datos del usuario, gestionada.
    assert elegir_base(tmp_path / "otro") == (tmp_path / "usuario" / "cnmc_logistica.db", True)
    # Dentro del repositorio: su data/, que la actualización automática no toca.
    (tmp_path / "repo" / "data").mkdir(parents=True)
    (tmp_path / "repo" / "data" / "cnmc_logistica.db").write_bytes(b"x")
    assert elegir_base(tmp_path / "repo") == (tmp_path / "repo/data/cnmc_logistica.db", False)
    monkeypatch.setenv("CNMC_BD", str(tmp_path / "mia.db"))
    assert elegir_base(tmp_path / "repo") == (tmp_path / "mia.db", False)
    monkeypatch.delenv("CNMC_DIR_DATOS")
    assert actualizacion.dir_datos_usuario().name == "cnmc-logistica"
