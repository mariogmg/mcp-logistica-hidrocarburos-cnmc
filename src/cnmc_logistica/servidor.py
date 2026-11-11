"""Servidor MCP de solo lectura: una capa fina sobre `consultas.py`.

Cada herramienta abre una conexión de solo lectura, llama a la función de
consulta del mismo nombre y traduce sus errores a errores de herramienta. La
lógica (y la descripción de cada herramienta) vive en `consultas.py`, que
comparte con la API REST. Este módulo no importa nada del ETL.
"""

from __future__ import annotations

import hmac
import inspect
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__, consultas
from .consultas import AVISO_LEGAL, ErrorConsulta, _coincide, _plano  # noqa: F401 (reexportados)

INSTRUCCIONES = f"""Histórico de la logística de productos petrolíferos que publica la CNMC
(instalaciones de almacenamiento y aeroportuarias, precios, condiciones, rutas origen-destino
y capacidades). Solo lectura. {AVISO_LEGAL}

Cómo leer los datos:
- Hay dos niveles: instalación (id_tanque) y detalle = un producto en una instalación
  (id_tanque_instalacion). Los precios cuelgan del detalle.
- No compares precios de servicios distintos: usa `comparar_servicio`, que agrupa por
  categoria_servicio, indicadores de lo que incluye el servicio y unidad.
- En rutas origen-destino, estado «no_ofrecida» significa que la ruta no existe, no que sea gratis.
- Cada fila tiene primera_vez_visto / ultima_vez_visto; las herramientas devuelven lo vigente
  salvo `historial_precio`.
"""


def abrir_solo_lectura(ruta: Path):
    """Conexión de solo lectura a la base (los errores, como errores de herramienta)."""
    try:
        return consultas.abrir_solo_lectura(ruta)
    except ErrorConsulta as exc:
        raise ToolError(str(exc)) from exc


def _descripcion(funcion: Callable) -> str:
    return inspect.cleandoc(funcion.__doc__ or "")


def crear_servidor(ruta_bd: Path) -> MCPServer:
    servidor = MCPServer(
        "logistica-hidrocarburos-cnmc",
        title="Logística de hidrocarburos (CNMC)",
        instructions=INSTRUCCIONES,
        version=__version__,
    )

    def ejecutar(consulta: Callable, **parametros: Any) -> Any:
        """Una conexión por llamada: sustituir el fichero de la base no exige reiniciar."""
        conn = abrir_solo_lectura(ruta_bd)
        try:
            return consulta(conn, **parametros)
        except ErrorConsulta as exc:
            raise ToolError(str(exc)) from exc
        finally:
            conn.close()

    @servidor.tool(description=_descripcion(consultas.estado))
    def estado() -> dict[str, Any]:
        return ejecutar(consultas.estado)

    @servidor.tool(description=_descripcion(consultas.listar_operadores))
    def listar_operadores(solo_con_instalaciones: bool = True) -> dict[str, Any]:
        return ejecutar(consultas.listar_operadores, solo_con_instalaciones=solo_con_instalaciones)

    @servidor.tool(description=_descripcion(consultas.listar_instalaciones))
    def listar_instalaciones(
        operador: str | None = None,
        tipo: str | None = None,
        comunidad: str | None = None,
        producto: str | None = None,
        limite: int = 200,
    ) -> dict[str, Any]:
        return ejecutar(
            consultas.listar_instalaciones,
            operador=operador,
            tipo=tipo,
            comunidad=comunidad,
            producto=producto,
            limite=limite,
        )

    @servidor.tool(description=_descripcion(consultas.ver_instalacion))
    def ver_instalacion(id_tanque: int) -> dict[str, Any]:
        return ejecutar(consultas.ver_instalacion, id_tanque=id_tanque)

    @servidor.tool(description=_descripcion(consultas.buscar_precios))
    def buscar_precios(
        operador: str | None = None,
        categoria: str | None = None,
        producto: str | None = None,
        comunidad: str | None = None,
        tipo_instalacion: str | None = None,
        texto: str | None = None,
        solo_numericos: bool = False,
        detallar_aplicacion: bool = False,
        limite: int = 100,
    ) -> dict[str, Any]:
        return ejecutar(
            consultas.buscar_precios,
            operador=operador,
            categoria=categoria,
            producto=producto,
            comunidad=comunidad,
            tipo_instalacion=tipo_instalacion,
            texto=texto,
            solo_numericos=solo_numericos,
            detallar_aplicacion=detallar_aplicacion,
            limite=limite,
        )

    @servidor.tool(description=_descripcion(consultas.comparar_servicio))
    def comparar_servicio(
        categoria: str,
        producto: str | None = None,
        tipo_instalacion: str | None = None,
        comunidad: str | None = None,
        operador: str | None = None,
        detallar_aplicacion: bool = False,
    ) -> dict[str, Any]:
        return ejecutar(
            consultas.comparar_servicio,
            categoria=categoria,
            producto=producto,
            tipo_instalacion=tipo_instalacion,
            comunidad=comunidad,
            operador=operador,
            detallar_aplicacion=detallar_aplicacion,
        )

    @servidor.tool(description=_descripcion(consultas.historial_precio))
    def historial_precio(
        id_tanque_instalacion: int, etiqueta: str | None = None, subetiqueta: str | None = None
    ) -> dict[str, Any]:
        return ejecutar(
            consultas.historial_precio,
            id_tanque_instalacion=id_tanque_instalacion,
            etiqueta=etiqueta,
            subetiqueta=subetiqueta,
        )

    @servidor.tool(description=_descripcion(consultas.precios_origen_destino))
    def precios_origen_destino(
        origen: str | None = None,
        destino: str | None = None,
        modalidad: str | None = None,
        producto: str | None = None,
        incluir_no_ofrecidas: bool = False,
        limite: int = 200,
    ) -> dict[str, Any]:
        return ejecutar(
            consultas.precios_origen_destino,
            origen=origen,
            destino=destino,
            modalidad=modalidad,
            producto=producto,
            incluir_no_ofrecidas=incluir_no_ofrecidas,
            limite=limite,
        )

    @servidor.tool(description=_descripcion(consultas.capacidades))
    def capacidades(
        compania: str | None = None,
        instalacion: str | None = None,
        id_tanque: int | None = None,
        ambito: str | None = None,
        producto: str | None = None,
        solo_totales: bool = False,
        limite: int = 300,
    ) -> dict[str, Any]:
        return ejecutar(
            consultas.capacidades,
            compania=compania,
            instalacion=instalacion,
            id_tanque=id_tanque,
            ambito=ambito,
            producto=producto,
            solo_totales=solo_totales,
            limite=limite,
        )

    @servidor.tool(description=_descripcion(consultas.reconsignaciones))
    def reconsignaciones(
        origen: str | None = None,
        destino: str | None = None,
        tipo: str = "reconsignacion",
        limite: int = 200,
    ) -> dict[str, Any]:
        return ejecutar(
            consultas.reconsignaciones, origen=origen, destino=destino, tipo=tipo, limite=limite
        )

    @servidor.tool(description=_descripcion(consultas.bonificaciones))
    def bonificaciones(producto: str | None = None) -> dict[str, Any]:
        return ejecutar(consultas.bonificaciones, producto=producto)

    @servidor.tool(description=_descripcion(consultas.consulta_sql))
    def consulta_sql(sql: str, limite: int = 200) -> dict[str, Any]:
        return ejecutar(consultas.consulta_sql, sql=sql, limite=limite)

    return servidor


# --- arranque ------------------------------------------------------------------------


class _SoloConToken:
    """Middleware ASGI mínimo: exige «Authorization: Bearer <token>»."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self._esperado = f"Bearer {token}".encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            recibido = dict(scope.get("headers") or {}).get(b"authorization", b"")
            # Comparación en tiempo constante: no filtra el token por tiempos de respuesta.
            if not hmac.compare_digest(recibido, self._esperado):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"www-authenticate", b"Bearer"), (b"content-length", b"0")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


def app_http(ruta_bd: Path, host: str, puerto: int) -> Any:
    """Aplicación ASGI del transporte streamable-http, con sus protecciones.

    - CNMC_MCP_HOSTS_PERMITIDOS: lista separada por comas de cabeceras Host
      aceptadas (protección frente a DNS rebinding). Por defecto, solo local.
    - CNMC_MCP_TOKEN: si está definido, se exige como token Bearer.
    """
    permitidos = [
        h.strip() for h in os.environ.get("CNMC_MCP_HOSTS_PERMITIDOS", "").split(",") if h.strip()
    ] or [f"127.0.0.1:{puerto}", f"localhost:{puerto}"]
    seguridad = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=permitidos,
        allowed_origins=[f"{esquema}://{h}" for h in permitidos for esquema in ("http", "https")],
    )
    app = crear_servidor(ruta_bd).streamable_http_app(transport_security=seguridad, host=host)
    token = os.environ.get("CNMC_MCP_TOKEN", "").strip()
    return _SoloConToken(app, token) if token else app


def servir(ruta_bd: Path, transporte: str, host: str, puerto: int) -> None:
    if transporte == "stdio":
        crear_servidor(ruta_bd).run("stdio")
        return
    import uvicorn

    uvicorn.run(app_http(ruta_bd, host, puerto), host=host, port=puerto, log_level="info")
