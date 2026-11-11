"""API REST de solo lectura (FastAPI), sobre la misma capa de consultas que el MCP.

Decisiones:
- Solo GET. No hay SQL libre: cada ruta llama a una función de `consultas.py`
  con parámetros validados. El SQL libre queda en el MCP, con sus protecciones.
- Una conexión de solo lectura por petición: si se sustituye el fichero de la
  base, la siguiente petición ya lee el nuevo sin reiniciar nada.
- Paginación limit/offset con tope y el total en la respuesta.
- Los datos cambian una vez al mes: se sirven con Cache-Control público.
- No importa nada del ETL.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, consultas
from .consultas import AVISO_LEGAL, BaseNoDisponible, ErrorConsulta, NoEncontrado

LIMITE_POR_DEFECTO, LIMITE_MAXIMO = 100, 1000
CACHE = "public, max-age=86400"  # un día: la base se actualiza una vez al mes
T = TypeVar("T")

DESCRIPCION = f"""
Histórico de la **logística de productos petrolíferos** que publica la CNMC: instalaciones,
precios, condiciones, rutas origen-destino, tarifas en PDF y capacidades.

- Solo lectura. Todas las rutas son `GET` y devuelven JSON (algunas, también CSV con `?formato=csv`).
- Las listas se paginan con `limit` (máximo {LIMITE_MAXIMO}) y `offset`; `total` es el nº de filas sin paginar.
- Hay dos niveles: **instalación** (`id_tanque`) y **detalle**, un producto en una instalación
  (`id_tanque_instalacion`). Los precios cuelgan del detalle.
- `fuente` vale `html` (dato de la página web de la CNMC) o `pdf` (tarifa en un PDF enlazado).
- No compare precios de servicios distintos: use `/comparar`.

{AVISO_LEGAL}
"""


class Fila(BaseModel):
    """Base de los modelos: admite los campos adicionales que devuelva la consulta."""

    model_config = ConfigDict(extra="allow")


class Pagina(BaseModel, Generic[T]):
    total: int = Field(description="Filas que cumplen los filtros, sin paginar")
    limit: int
    offset: int
    datos: list[T]


class Operador(Fila):
    codigo: str = Field(description="Código estable del almacenista en el buscador de la CNMC")
    nombre: str | None
    instalaciones: int
    instalaciones_almacenamiento: int
    instalaciones_aeroportuarias: int
    detalles: int
    nombres_anteriores: list[str]
    model_config = ConfigDict(extra="allow", json_schema_extra={"example": {
        "codigo": "DECAL", "nombre": "DECAL ESPAÑA, S.A.U.", "instalaciones": 2,
        "instalaciones_almacenamiento": 2, "instalaciones_aeroportuarias": 0, "detalles": 9,
        "nombres_anteriores": []}})  # fmt: skip


class Instalacion(Fila):
    id_tanque: int
    instalacion: str
    tipo: Literal["almacenamiento", "aeroportuaria", "desconocido"]
    comunidad: str | None
    operadores: list[str]
    productos: list[str]
    detalles: list[int] = Field(description="id_tanque_instalacion de cada producto")
    model_config = ConfigDict(extra="allow", json_schema_extra={"example": {
        "id_tanque": 59, "instalacion": "EXOLUM / EXOLUM AVIATION - Barajas",
        "tipo": "aeroportuaria", "comunidad": "MADRID", "operadores": ["CLHAV", "CLH"],
        "productos": ["Querosenos"], "detalles": [124, 123]}})  # fmt: skip


class Precio(Fila):
    fuente: Literal["html", "pdf"] = Field(description="html = página web; pdf = tarifa en PDF")
    instalacion: str | None
    operador: str | None
    productos: str | None
    seccion: str
    etiqueta: str
    subetiqueta: str | None
    categoria_servicio: str
    tipo_precio: Literal["numerico", "regla", "texto", "pdf"]
    tipo_valor: Literal["unico", "max", "min"] | None
    valor: float | None
    unidad: str | None
    fecha_precio: str | None = Field(description="Vigencia que publica la CNMC (AAAA-MM-DD)")
    anio_tabla: int | None = Field(description="Año de la tabla cuando no hay fecha (PDF)")
    aplica_a: dict[str, Any] | None = Field(
        None, description="Solo en precios de PDF: a cuántos detalles e instalaciones aplica"
    )
    model_config = ConfigDict(extra="allow", json_schema_extra={"example": {
        "fuente": "html", "instalacion": "TEPSA - Ciérvana", "operador": "TEPSA",
        "productos": "Gasolinas y Gasóleos", "seccion": "principal",
        "etiqueta": "Almacenamiento y manipulación", "subetiqueta": None,
        "categoria_servicio": "almacenamiento_y_manipulacion", "tipo_precio": "numerico",
        "tipo_valor": "max", "valor": 4.12, "unidad": "€/m3 y mes",
        "fecha_precio": "2026-03-16", "anio_tabla": None, "aplica_a": None}})  # fmt: skip


class Ruta(Fila):
    origen: str
    destino: str | None
    instalacion_destino: str
    id_tanque_instalacion: int
    productos: str
    modalidad: str
    estado: Literal["ofrecida", "no_ofrecida"] = Field(
        description="no_ofrecida = la ruta no existe (la web muestra 0,00); no es un precio cero"
    )
    precio: float | None
    unidad: str | None
    anio_tabla: int | None
    model_config = ConfigDict(extra="allow", json_schema_extra={"example": {
        "origen": "HUELVA", "destino": "Alicante", "instalacion_destino": "EXOLUM - Alicante",
        "id_tanque_instalacion": 112, "productos": "Gasolinas y Gasóleos",
        "modalidad": "Recepción por buque tanque", "estado": "ofrecida", "precio": 20.48,
        "unidad": "€/m3", "anio_tabla": 2026}})  # fmt: skip


class CeldaMatriz(Fila):
    origen: str
    destino: str
    modalidad: str | None
    estado: str
    precio: float | None
    unidad: str | None
    anio_tabla: int | None
    documento: str
    pagina: int
    model_config = ConfigDict(extra="allow", json_schema_extra={"example": {
        "origen": "HUELVA", "destino": "Zaragoza", "modalidad": None, "estado": "ofrecida",
        "precio": 19.62, "unidad": "€/m3", "anio_tabla": 2026, "documento": "27", "pagina": 2}})  # fmt: skip


class Condicion(Fila):
    fuente: Literal["html", "pdf"]
    instalacion: str | None
    operador: str | None
    etiqueta: str
    subetiqueta: str | None
    tipo_valor: str | None
    valor: float | None
    unidad: str | None
    texto: str | None
    model_config = ConfigDict(extra="allow", json_schema_extra={"example": {
        "fuente": "html", "instalacion": "DECAL - Huelva", "operador": "DECAL",
        "etiqueta": "Mermas", "subetiqueta": "Gasóleos", "tipo_valor": "unico", "valor": 0.2,
        "unidad": "%", "texto": None}})  # fmt: skip


class Capacidad(Fila):
    ambito: str
    compania: str
    instalacion: str
    producto: str = Field(description="TOTAL = total de la instalación")
    id_tanque: int | None
    capacidad_miles_m3: float | None
    anio: int | None
    model_config = ConfigDict(extra="allow", json_schema_extra={"example": {
        "ambito": "almacenamiento", "compania": "EXOLUM CORPORATION", "instalacion": "Huelva",
        "producto": "Queroseno", "id_tanque": 87, "capacidad_miles_m3": 95.87, "anio": 2026}})  # fmt: skip


class Objeto(Fila):
    """Respuesta con estructura propia de la consulta (véase el ejemplo de cada ruta)."""


def _ejemplo(valor: dict) -> dict:
    return {200: {"content": {"application/json": {"example": valor}}}}


def _csv(filas: list[dict]) -> Response:
    columnas: list[str] = []
    for f in filas:
        columnas += [k for k in f if k not in columnas]
    salida = io.StringIO()
    escritor = csv.DictWriter(salida, fieldnames=columnas, delimiter=";")
    escritor.writeheader()
    for f in filas:
        escritor.writerow(
            {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
             for k, v in f.items()}
        )  # fmt: skip
    return Response(salida.getvalue(), media_type="text/csv; charset=utf-8")


def _pagina(filas: list[dict], limit: int, offset: int, formato: str = "json") -> Any:
    trozo = filas[offset : offset + limit]
    if formato == "csv":
        return _csv(trozo)
    return {"total": len(filas), "limit": limit, "offset": offset, "datos": trozo}


def crear_api(ruta_bd: Path) -> FastAPI:
    api = FastAPI(
        title="Logística de hidrocarburos (CNMC) · API",
        description=DESCRIPCION,
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        license_info={"name": "MIT (código). Datos: CNMC"},
    )
    api.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])

    @api.middleware("http")
    async def cache(request: Request, llamar):
        respuesta = await llamar(request)
        if request.method == "GET" and respuesta.status_code == 200:
            respuesta.headers.setdefault("Cache-Control", CACHE)
        return respuesta

    @api.exception_handler(ErrorConsulta)
    async def error_de_consulta(_: Request, exc: ErrorConsulta) -> JSONResponse:
        codigo = (
            404
            if isinstance(exc, NoEncontrado)
            else 503
            if isinstance(exc, BaseNoDisponible)
            else 400
        )
        return JSONResponse({"detail": str(exc)}, status_code=codigo)

    def conexion() -> Iterator[sqlite3.Connection]:
        conn = consultas.abrir_solo_lectura(ruta_bd)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Depends(conexion)
    Limit = Query(LIMITE_POR_DEFECTO, ge=1, le=LIMITE_MAXIMO, description="Filas por página")
    Offset = Query(0, ge=0, description="Filas que se saltan")
    Formato = Query("json", description="json (por defecto) o csv")
    v1 = APIRouter(prefix="/api/v1")

    @api.get("/salud", tags=["Servicio"], summary="Comprobación de vida", responses=_ejemplo(
        {"estado": "ok", "ultima_foto": "2026-11-11", "version": __version__}))  # fmt: skip
    def salud(conn: sqlite3.Connection = Conn) -> dict:
        ultima = consultas.consultar(conn, "SELECT MAX(fecha) AS f FROM ejecuciones")[0]["f"]
        return {"estado": "ok", "ultima_foto": ultima, "version": __version__}

    @v1.get("/estado", tags=["Servicio"], response_model=Objeto, summary="Estado de la base",
            responses=_ejemplo({"ultima_foto": "2026-11-11", "fotos": 1, "lineas_pdf_sin_resolver": 0,
                                "cobertura_pdf": {"documentos": 27, "cifras": 3237, "sin_cubrir": 0}}))  # fmt: skip
    def estado(conn: sqlite3.Connection = Conn) -> Any:
        """Última foto del portal, ejecuciones, nº de filas por tabla, incidencias y cobertura de los PDF."""
        return consultas.estado(conn)

    @v1.get("/operadores", tags=["Operadores"], response_model=Pagina[Operador])
    def operadores(
        solo_con_instalaciones: bool = Query(True, description="false incluye los almacenistas sin instalaciones publicadas"),
        limit: int = Limit, offset: int = Offset, conn: sqlite3.Connection = Conn,
    ) -> Any:  # fmt: skip
        """Operadores (almacenistas) con su nº de instalaciones por tipo. Una instalación con dos titulares cuenta para ambos."""
        return _pagina(
            consultas.listar_operadores(conn, solo_con_instalaciones)["operadores"], limit, offset
        )

    @v1.get("/operadores/{codigo}", tags=["Operadores"], response_model=Operador)
    def operador(codigo: str, conn: sqlite3.Connection = Conn) -> Any:
        """Un operador por su código (p. ej. `CLH`, `DECAL`), con la lista de sus instalaciones."""
        return consultas.operador(conn, codigo)

    @v1.get("/instalaciones", tags=["Instalaciones"], response_model=Pagina[Instalacion])
    def instalaciones(
        tipo: Literal["almacenamiento", "aeroportuaria"] | None = None,
        comunidad: str | None = Query(None, description="Código (AND, CAN…) o nombre"),
        operador: str | None = Query(None, description="Código o parte del nombre"),
        producto: str | None = Query(None, description="Parte del nombre; sin tildes ni mayúsculas"),
        formato: Literal["json", "csv"] = Formato,
        limit: int = Limit, offset: int = Offset, conn: sqlite3.Connection = Conn,
    ) -> Any:  # fmt: skip
        """Instalaciones vigentes, con filtros opcionales."""
        r = consultas.listar_instalaciones(conn, operador, tipo, comunidad, producto, limite=None)
        return _pagina(r["instalaciones"], limit, offset, formato)

    @v1.get("/instalaciones/{id_tanque}", tags=["Instalaciones"], response_model=Objeto,
            responses=_ejemplo({"instalacion": {"id_tanque": 9, "tipo": "almacenamiento", "titulo": "EXOLUM - Puxeiros"},
                                "detalles": [{"id_tanque_instalacion": 17, "operador": "CLH", "precios": []}],
                                "capacidades": []}))  # fmt: skip
    def instalacion(id_tanque: int, conn: sqlite3.Connection = Conn) -> Any:
        """Ficha completa: detalles, precios (web y PDF), condiciones y capacidad."""
        return consultas.ver_instalacion(conn, id_tanque)

    @v1.get("/detalles/{id_tanque_instalacion}", tags=["Instalaciones"], response_model=Objeto,
            responses=_ejemplo({"id_tanque_instalacion": 17, "id_tanque": 9, "operador": "CLH",
                                "productos_texto": "Gasolinas y Gasóleos", "precios": [], "condiciones": []}))  # fmt: skip
    def detalle(id_tanque_instalacion: int, conn: sqlite3.Connection = Conn) -> Any:
        """Un detalle (un producto en una instalación) con sus precios, condiciones y PDF enlazados."""
        return consultas.detalle(conn, id_tanque_instalacion)

    @v1.get("/precios", tags=["Precios"], response_model=Pagina[Precio])
    def precios(
        categoria: str | None = Query(None, description="categoria_servicio exacta, p. ej. almacenamiento"),
        operador: str | None = None, producto: str | None = None, comunidad: str | None = None,
        tipo_precio: Literal["numerico", "regla", "texto", "pdf"] | None = None,
        fuente: Literal["web", "pdf"] | None = Query(None, description="web = página de la CNMC (en la respuesta, `html`)"),
        historico: bool = Query(False, description="true: todas las versiones, no solo las vigentes"),
        formato: Literal["json", "csv"] = Formato,
        limit: int = Limit, offset: int = Offset, conn: sqlite3.Connection = Conn,
    ) -> Any:  # fmt: skip
        """Precios vigentes (o todo el historial). Los de PDF comunes a varios detalles salen una vez, con `aplica_a`."""
        filas = consultas.precios(
            conn, categoria, operador, producto, comunidad, None, tipo_precio, fuente, historico
        )
        return _pagina(filas, limit, offset, formato)

    @v1.get("/precios/historial", tags=["Precios"], response_model=Objeto, responses=_ejemplo(
        {"detalle": {"titulo": "EXOLUM - Puxeiros"}, "ultima_foto": "2026-11-11",
         "versiones": [{"etiqueta": "Transferencias", "valor": 2.4577, "primera_vez_visto": "2026-11-11",
                        "ultima_vez_visto": "2026-11-11", "vigente": True}]}))  # fmt: skip
    def historial(
        id_tanque_instalacion: int = Query(description="Detalle del que se quiere el historial"),
        etiqueta: str | None = None, subetiqueta: str | None = None, conn: sqlite3.Connection = Conn,
    ) -> Any:  # fmt: skip
        """Todas las versiones de los precios de un detalle, con las fechas entre las que se vio cada una."""
        return consultas.historial_precio(conn, id_tanque_instalacion, etiqueta, subetiqueta)

    @v1.get("/comparar", tags=["Precios"], response_model=Objeto, responses=_ejemplo(
        {"categoria": "transferencias", "avisos": ["Hay 2 grupos no comparables entre sí…"],
         "grupos": [{"unidad": "€/operación", "indicadores": {}, "resumen": {"precios": 91, "minimo": 2.4577}, "filas": []}],
         "sin_precio_numerico": []}))  # fmt: skip
    def comparar(
        categoria: str = Query(description="categoria_servicio que se compara"),
        producto: str | None = None, tipo_instalacion: Literal["almacenamiento", "aeroportuaria"] | None = None,
        comunidad: str | None = None, operador: str | None = None, conn: sqlite3.Connection = Conn,
    ) -> Any:  # fmt: skip
        """Compara una categoría entre instalaciones y operadores. Solo son comparables los precios de un mismo grupo (misma categoría, mismos indicadores y misma unidad); si hay varios grupos, `avisos` lo dice."""
        return consultas.comparar_servicio(
            conn, categoria, producto, tipo_instalacion, comunidad, operador
        )

    @v1.get("/origen-destino", tags=["Rutas y matrices"], response_model=Pagina[Ruta])
    def origen_destino(
        destino: str | None = None, origen: str | None = None, modalidad: str | None = None,
        estado: Literal["ofrecida", "no_ofrecida"] | None = Query(None, description="Sin indicar: todas"),
        formato: Literal["json", "csv"] = Formato,
        limit: int = Limit, offset: int = Offset, conn: sqlite3.Connection = Conn,
    ) -> Any:  # fmt: skip
        """Precio de llevar producto desde un origen hasta una instalación, por modalidad de recepción."""
        r = consultas.precios_origen_destino(conn, origen, destino, modalidad, None, True, None)
        filas = [f for f in r["rutas"] if estado is None or f["estado"] == estado]
        return _pagina(filas, limit, offset, formato)

    @v1.get("/reconsignaciones", tags=["Rutas y matrices"], response_model=Pagina[CeldaMatriz])
    def reconsignaciones(
        origen: str | None = None, destino: str | None = None,
        tipo: Literal["reconsignacion", "consignacion_ester", "origen_destino"] = "reconsignacion",
        limit: int = Limit, offset: int = Offset, conn: sqlite3.Connection = Conn,
    ) -> Any:  # fmt: skip
        """Matrices de los PDF de Exolum: reconsignación entre instalaciones (por defecto), consignación de éster u origen-destino."""
        return _pagina(
            consultas.reconsignaciones(conn, origen, destino, tipo, None)["filas"], limit, offset
        )

    @v1.get("/bonificaciones", tags=["Rutas y matrices"], response_model=Objeto, responses=_ejemplo(
        {"fuente": "pdf", "documentos": [{"documento": "25", "anio": 2026,
         "aplica_a": {"productos": ["Gasolinas y Gasóleos"], "detalles": 33},
         "filas": [{"bloque": "bonificacion", "tramo": "Por cada m3", "condicion": "De cantidad garantizada",
                    "valor": 0.0681, "unidad": "€"}], "notas": []}]}))  # fmt: skip
    def bonificaciones(producto: str | None = None, conn: sqlite3.Connection = Conn) -> Any:
        """Bonificaciones y compensaciones de los contratos a largo plazo."""
        return consultas.bonificaciones(conn, producto)

    @v1.get("/condiciones", tags=["Precios"], response_model=Pagina[Condicion])
    def condiciones(
        operador: str | None = None, comunidad: str | None = None, producto: str | None = None,
        etiqueta: str | None = Query(None, description="Parte de la etiqueta, p. ej. mermas"),
        fuente: Literal["web", "pdf"] | None = None,
        limit: int = Limit, offset: int = Offset, conn: sqlite3.Connection = Conn,
    ) -> Any:  # fmt: skip
        """Condiciones vigentes: mermas y demás condiciones de las páginas, y cantidades de las notas de los PDF."""
        return _pagina(
            consultas.condiciones(conn, operador, comunidad, producto, etiqueta, fuente),
            limit,
            offset,
        )

    @v1.get("/capacidades", tags=["Instalaciones"], response_model=Pagina[Capacidad])
    def capacidades(
        operador: str | None = Query(None, description="Parte del nombre de la compañía en el PDF"),
        instalacion: str | None = None, producto: str | None = None,
        formato: Literal["json", "csv"] = Formato,
        limit: int = Limit, offset: int = Offset, conn: sqlite3.Connection = Conn,
    ) -> Any:  # fmt: skip
        """Capacidad de almacenamiento en miles de m³ por compañía, instalación y producto."""
        r = consultas.capacidades(conn, operador, instalacion, None, None, producto, False, None)
        return _pagina(r["capacidades"], limit, offset, formato)

    api.include_router(v1)
    return api
