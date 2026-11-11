"""Descargador educado.

- Una sola conexión y como mínimo 1 s entre peticiones (suelo no configurable).
- Peticiones sin estado: no se conservan cookies entre una y otra.
- robots.txt se consulta en cada ejecución antes de pedir nada más.
- Reintentos con espera exponencial solo ante fallos transitorios (red, 429,
  5xx). Un 404 no se reintenta: insistir no lo arregla y es ruido para el servidor.
- Todo lo que se baja pasa por el almacén de crudo (bytes + sha256 + manifiesto).
"""

from __future__ import annotations

import time
import urllib.robotparser
from collections.abc import Callable

import httpx

from .almacen import AlmacenCrudo, Entrada, Recurso
from .config import INTERVALO_MINIMO_S, ORIGEN_PORTAL, USER_AGENT

ESTADOS_REINTENTABLES = {429, 500, 502, 503, 504}
ESPERA_MAXIMA_S = 60.0


class ErrorDescarga(Exception):
    """El recurso no se pudo obtener. Quien llama lo registra y sigue."""


class RobotsProhibe(ErrorDescarga):
    """robots.txt no permite esta URL a nuestro User-Agent."""


class Descargador:
    def __init__(
        self,
        almacen: AlmacenCrudo,
        *,
        cliente: httpx.Client | None = None,
        intervalo: float = INTERVALO_MINIMO_S,
        intentos: int = 4,
        espera_base: float = 2.0,
        user_agent: str = USER_AGENT,
        dormir: Callable[[float], None] = time.sleep,
        reloj: Callable[[], float] = time.monotonic,
    ):
        if intervalo < INTERVALO_MINIMO_S:
            raise ValueError(f"el intervalo mínimo entre peticiones es {INTERVALO_MINIMO_S} s")
        self.almacen = almacen
        self.intervalo = intervalo
        self.intentos = intentos
        self.espera_base = espera_base
        self.user_agent = user_agent
        self._dormir = dormir
        self._reloj = reloj
        self._ultima: float | None = None
        self._robots: urllib.robotparser.RobotFileParser | None = None
        self._cliente = cliente or httpx.Client(timeout=60.0)
        self._cliente.headers["User-Agent"] = user_agent
        self._cliente.follow_redirects = True
        self.peticiones = 0

    def cerrar(self) -> None:
        self._cliente.close()

    def __enter__(self) -> Descargador:
        return self

    def __exit__(self, *_: object) -> None:
        self.cerrar()

    # --- red ---------------------------------------------------------------

    def _respetar_intervalo(self) -> None:
        if self._ultima is not None:
            falta = self.intervalo - (self._reloj() - self._ultima)
            if falta > 0:
                self._dormir(falta)
        self._ultima = self._reloj()

    def _pedir(self, url: str) -> httpx.Response:
        ultimo_error = ""
        for intento in range(1, self.intentos + 1):
            self._respetar_intervalo()
            self.peticiones += 1
            espera = self.espera_base * 2 ** (intento - 1)
            try:
                respuesta = self._cliente.get(url)
                # Sin estado: el buscador guarda la página actual en la sesión y
                # contaminaría la búsqueda siguiente. Cada petición va sin cookies.
                self._cliente.cookies.clear()
            except httpx.TransportError as exc:
                ultimo_error = f"{type(exc).__name__}: {exc}"
            else:
                if respuesta.status_code not in ESTADOS_REINTENTABLES:
                    return respuesta
                ultimo_error = f"HTTP {respuesta.status_code}"
                pedido = respuesta.headers.get("retry-after", "")
                if pedido.isdigit():
                    espera = max(espera, float(pedido))
            if intento < self.intentos:
                self._dormir(min(espera, ESPERA_MAXIMA_S))
        raise ErrorDescarga(f"{url}: agotados {self.intentos} intentos ({ultimo_error})")

    # --- robots ------------------------------------------------------------

    def comprobar_robots(self) -> Entrada:
        """Descarga robots.txt y lo guarda. Hay que llamarlo antes de `obtener`.

        Convención habitual: 401/403 → todo prohibido; resto de 4xx (el portal
        devuelve 404) → sin restricciones; 5xx o fallo de red → no se sigue,
        porque no sabemos qué permite el sitio.
        """
        recurso = Recurso("robots", "robots", f"{ORIGEN_PORTAL}/robots.txt")
        try:
            respuesta = self._pedir(recurso.url)
        except ErrorDescarga as exc:
            self.almacen.registrar_fallo(recurso, None, str(exc))
            raise ErrorDescarga(f"no se pudo consultar robots.txt: {exc}") from exc
        entrada = self.almacen.guardar(
            recurso,
            respuesta.content,
            url_final=str(respuesta.url),
            estado_http=respuesta.status_code,
            cabeceras=dict(respuesta.headers),
        )
        robots = urllib.robotparser.RobotFileParser()
        if respuesta.status_code == 200:
            robots.parse(respuesta.text.splitlines())
        elif respuesta.status_code in (401, 403):
            robots.disallow_all = True
        else:
            robots.allow_all = True
        self._robots = robots
        return entrada

    def permitido(self, url: str) -> bool:
        if self._robots is None:
            raise RuntimeError("llama a comprobar_robots() antes de descargar")
        return self._robots.can_fetch(self.user_agent, url)

    # --- recursos ----------------------------------------------------------

    def obtener(
        self, recurso: Recurso, *, forzar: bool = False, motivo: str | None = None
    ) -> Entrada:
        """Descarga un recurso al almacén, o devuelve el ya guardado en esta foto.

        No repetir lo ya descargado hace la ejecución reanudable y evita
        peticiones innecesarias si dos pasos necesitan la misma página.
        """
        if not forzar and (previa := self.almacen.buscar(recurso.tipo, recurso.clave)):
            return previa
        if not self.permitido(recurso.url):
            self.almacen.registrar_fallo(recurso, None, "prohibido por robots.txt")
            raise RobotsProhibe(f"{recurso.url}: prohibido por robots.txt")
        try:
            respuesta = self._pedir(recurso.url)
        except ErrorDescarga as exc:
            self.almacen.registrar_fallo(recurso, None, str(exc))
            raise
        if respuesta.status_code != 200:
            error = f"HTTP {respuesta.status_code}"
            self.almacen.registrar_fallo(recurso, respuesta.status_code, error)
            raise ErrorDescarga(f"{recurso.url}: {error}")
        return self.almacen.guardar(
            recurso,
            respuesta.content,
            url_final=str(respuesta.url),
            estado_http=respuesta.status_code,
            cabeceras=dict(respuesta.headers),
            motivo=motivo,
        )
