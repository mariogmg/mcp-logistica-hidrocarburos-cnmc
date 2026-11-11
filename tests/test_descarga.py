import hashlib
import json

import httpx
import pytest

from cnmc_logistica.almacen import AlmacenCrudo, Recurso, decodificar
from cnmc_logistica.descarga import Descargador, ErrorDescarga, RobotsProhibe

BASE = "https://hidrocarburos.cnmc.es/hidrocarburos"


class Reloj:
    """Reloj falso: dormir avanza el tiempo, así los tests no esperan."""

    def __init__(self):
        self.t = 0.0
        self.siestas: list[float] = []

    def ahora(self) -> float:
        return self.t

    def dormir(self, s: float) -> None:
        self.siestas.append(s)
        self.t += s


def crear(tmp_path, manejador, **kw):
    reloj = Reloj()
    cliente = httpx.Client(transport=httpx.MockTransport(manejador))
    almacen = AlmacenCrudo(tmp_path / "raw", "2026-11-11")
    d = Descargador(almacen, cliente=cliente, dormir=reloj.dormir, reloj=reloj.ahora, **kw)
    return d, almacen, reloj


def portal(robots=(404, ""), paginas=None, vistos=None):
    paginas = paginas or {}

    def manejador(req: httpx.Request) -> httpx.Response:
        if vistos is not None:
            vistos.append(req)
        if req.url.path == "/robots.txt":
            return httpx.Response(robots[0], text=robots[1])
        r = paginas.get(str(req.url))
        if r is None:
            return httpx.Response(404, text="no")
        return r(req) if callable(r) else r

    return manejador


def test_guarda_crudo_sha256_y_manifiesto(tmp_path):
    cuerpo = "Logística de Productos Petrolíferos".encode()
    url = f"{BASE}/detalle.htm?idTanqueInstalacion=17"
    pag = httpx.Response(
        200,
        content=cuerpo,
        headers={"content-type": "text/html;charset=UTF-8", "set-cookie": "JSESSIONID=x"},
    )
    d, almacen, _ = crear(tmp_path, portal(paginas={url: pag}))
    d.comprobar_robots()
    e = d.obtener(Recurso("detalle", "17", url))

    assert (almacen.dir / "detalles/17.html").read_bytes() == cuerpo
    assert e.sha256 == hashlib.sha256(cuerpo).hexdigest()
    assert e.bytes == len(cuerpo) and e.estado_http == 200
    assert "set-cookie" not in e.cabeceras
    lineas = [json.loads(x) for x in almacen.manifiesto.read_text("utf-8").splitlines()]
    assert [x["tipo"] for x in lineas] == ["robots", "detalle"]
    assert lineas[0]["estado_http"] == 404
    assert almacen.leer_texto(e) == (cuerpo.decode(), None)


def test_user_agent_en_todas_las_peticiones(tmp_path):
    vistos = []
    url = f"{BASE}/a.htm"
    d, _, _ = crear(tmp_path, portal(paginas={url: httpx.Response(200, text="x")}, vistos=vistos))
    d.comprobar_robots()
    d.obtener(Recurso("portada", "portada", url))
    assert {r.headers["user-agent"] for r in vistos} == {d.user_agent}
    assert d.user_agent.startswith("mcp-logistica-hidrocarburos-cnmc/")


def test_al_menos_un_segundo_entre_peticiones(tmp_path):
    urls = {f"{BASE}/p{i}.htm": httpx.Response(200, text="x") for i in range(3)}
    d, _, reloj = crear(tmp_path, portal(paginas=urls))
    d.comprobar_robots()
    for i, u in enumerate(urls):
        d.obtener(Recurso("ficha", str(i), u))
    assert reloj.siestas == [1.0, 1.0, 1.0]  # 4 peticiones, 3 esperas


def test_intervalo_inferior_a_un_segundo_no_se_admite(tmp_path):
    with pytest.raises(ValueError):
        crear(tmp_path, portal(), intervalo=0.2)


def test_sigue_redirecciones_y_guarda_url_final(tmp_path):
    origen = f"{BASE}/instalacion.htm?idTanque=9"
    destino = f"{BASE}/detalle.htm?idTanqueInstalacion=17"
    paginas = {
        origen: httpx.Response(302, headers={"location": destino}),
        destino: httpx.Response(200, text="detalle"),
    }
    d, _, _ = crear(tmp_path, portal(paginas=paginas))
    d.comprobar_robots()
    e = d.obtener(Recurso("ficha", "9", origen))
    assert e.url == origen and e.url_final == destino


def test_reintenta_con_espera_creciente(tmp_path):
    url = f"{BASE}/x.htm"
    respuestas = iter([httpx.Response(503), httpx.Response(502), httpx.Response(200, text="ok")])
    d, _, reloj = crear(tmp_path, portal(paginas={url: lambda _: next(respuestas)}))
    d.comprobar_robots()
    assert d.obtener(Recurso("ficha", "1", url)).ok
    esperas_reintento = [s for s in reloj.siestas if s > 1.0]
    assert esperas_reintento == [2.0, 4.0]


def test_error_de_red_agota_intentos_y_queda_registrado(tmp_path):
    url = f"{BASE}/x.htm"

    def cae(_):
        raise httpx.ConnectError("sin red")

    d, almacen, _ = crear(tmp_path, portal(paginas={url: cae}), intentos=3)
    d.comprobar_robots()
    antes = d.peticiones
    with pytest.raises(ErrorDescarga):
        d.obtener(Recurso("ficha", "1", url))
    assert d.peticiones - antes == 3
    fallo = almacen.entradas()[-1]
    assert not fallo.ok and "ConnectError" in fallo.error
    assert almacen.buscar("ficha", "1") is None


def test_404_no_se_reintenta(tmp_path):
    d, almacen, _ = crear(tmp_path, portal())
    d.comprobar_robots()
    antes = d.peticiones
    with pytest.raises(ErrorDescarga, match="HTTP 404"):
        d.obtener(Recurso("ficha", "1", f"{BASE}/no.htm"))
    assert d.peticiones - antes == 1
    assert almacen.entradas()[-1].estado_http == 404


def test_respeta_retry_after(tmp_path):
    url = f"{BASE}/x.htm"
    respuestas = iter([httpx.Response(429, headers={"retry-after": "30"}), httpx.Response(200)])
    d, _, reloj = crear(tmp_path, portal(paginas={url: lambda _: next(respuestas)}))
    d.comprobar_robots()
    d.obtener(Recurso("ficha", "1", url))
    assert 30.0 in reloj.siestas


def test_robots_404_no_restringe(tmp_path):
    d, _, _ = crear(tmp_path, portal(robots=(404, "")))
    d.comprobar_robots()
    assert d.permitido(f"{BASE}/detalle.htm?idTanqueInstalacion=1")


def test_robots_disallow_se_respeta(tmp_path):
    vistos = []
    robots = "User-agent: *\nDisallow: /hidrocarburos/detalle.htm\n"
    d, almacen, _ = crear(tmp_path, portal(robots=(200, robots), vistos=vistos))
    d.comprobar_robots()
    with pytest.raises(RobotsProhibe):
        d.obtener(Recurso("detalle", "1", f"{BASE}/detalle.htm?idTanqueInstalacion=1"))
    assert len(vistos) == 1  # solo robots.txt: la URL prohibida ni se pide
    assert "robots" in almacen.entradas()[-1].error


def test_robots_403_prohibe_todo(tmp_path):
    d, _, _ = crear(tmp_path, portal(robots=(403, "")))
    d.comprobar_robots()
    assert not d.permitido(f"{BASE}/buscador.htm")


def test_robots_caido_detiene_la_ejecucion(tmp_path):
    d, _, _ = crear(tmp_path, portal(robots=(503, "")), intentos=2)
    with pytest.raises(ErrorDescarga, match="robots"):
        d.comprobar_robots()


def test_sin_consultar_robots_no_se_descarga(tmp_path):
    d, _, _ = crear(tmp_path, portal())
    with pytest.raises(RuntimeError):
        d.obtener(Recurso("ficha", "1", f"{BASE}/x.htm"))


def test_no_repite_lo_ya_descargado_en_la_misma_foto(tmp_path):
    url = f"{BASE}/x.htm"
    vistos = []
    d, _, _ = crear(tmp_path, portal(paginas={url: httpx.Response(200, text="a")}, vistos=vistos))
    d.comprobar_robots()
    a = d.obtener(Recurso("ficha", "1", url))
    b = d.obtener(Recurso("ficha", "1", url))
    assert a == b and len(vistos) == 2
    d.obtener(Recurso("ficha", "1", url), forzar=True)
    assert len(vistos) == 3


def test_nombre_de_archivo_del_pdf(tmp_path):
    url = f"{BASE}/downloadfile.htm?directorioPadre=pdfpub&directorioHijo=24"
    cab = {
        "content-type": "application/octet-stream",
        "content-disposition": 'attachment;filename="EXOLUM 2026 precios SSLL GG.pdf"',
    }
    pag = httpx.Response(200, content=b"%PDF-1.5", headers=cab)
    d, almacen, _ = crear(tmp_path, portal(paginas={url: pag}))
    d.comprobar_robots()
    e = d.obtener(Recurso("pdf", "24", url))
    assert e.nombre_archivo == "EXOLUM 2026 precios SSLL GG.pdf"
    assert e.ruta == "pdf/24.pdf" and almacen.leer_bytes(e) == b"%PDF-1.5"


def test_sha256_alterado_se_detecta(tmp_path):
    url = f"{BASE}/x.htm"
    d, almacen, _ = crear(tmp_path, portal(paginas={url: httpx.Response(200, text="a")}))
    d.comprobar_robots()
    e = d.obtener(Recurso("ficha", "1", url))
    (almacen.dir / e.ruta).write_text("manipulado")
    with pytest.raises(OSError, match="sha256"):
        almacen.leer_bytes(e)


def test_clave_no_puede_escapar_del_directorio():
    with pytest.raises(ValueError):
        _ = Recurso("ficha", "../../etc", "u").ruta


@pytest.mark.parametrize(
    ("datos", "ctype", "texto", "cod", "hay_aviso"),
    [
        ("Logística".encode(), "text/html;charset=UTF-8", "Logística", "utf-8", False),
        # El caso del portal: sin cabecera fiable, UTF-8 estricto.
        ("Logística".encode(), None, "Logística", "utf-8", False),
        # La cabecera dice latin-1 y cuadra: se respeta.
        (
            "Logística".encode("iso-8859-1"),
            "text/html; charset=ISO-8859-1",
            "Logística",
            "iso-8859-1",
            False,
        ),
        # Sin cabecera y no es UTF-8: último recurso, con aviso.
        ("Logística".encode("iso-8859-1"), "text/html", "Logística", "iso-8859-1", True),
        # La cabecera miente (dice ascii): cae a UTF-8 y avisa.
        ("Logística".encode(), "text/html; charset=ascii", "Logística", "utf-8", True),
    ],
)
def test_decodificar(datos, ctype, texto, cod, hay_aviso):
    t, c, aviso = decodificar(datos, ctype)
    assert (t, c, aviso is not None) == (texto, cod, hay_aviso)


def test_fechas_disponibles(tmp_path):
    raw = tmp_path / "raw"
    for f in ("2026-12-01", "2026-11-11"):
        a = AlmacenCrudo(raw, f)
        a.guardar(
            Recurso("portada", "portada", "u"), b"x", url_final="u", estado_http=200, cabeceras={}
        )
    (raw / "basura").mkdir()
    assert AlmacenCrudo.fechas(raw) == ["2026-11-11", "2026-12-01"]


def test_no_conserva_cookies_entre_peticiones(tmp_path):
    # El buscador del portal guarda la página en la sesión: con cookies, la
    # búsqueda de un almacenista heredaba la página de la anterior.
    vistos = []
    urls = {
        f"{BASE}/a.htm": httpx.Response(200, text="a", headers={"set-cookie": "JSESSIONID=abc"}),
        f"{BASE}/b.htm": httpx.Response(200, text="b"),
    }
    d, _, _ = crear(tmp_path, portal(paginas=urls, vistos=vistos))
    d.comprobar_robots()
    for i, u in enumerate(urls):
        d.obtener(Recurso("ficha", str(i), u))
    assert all("cookie" not in r.headers for r in vistos)
