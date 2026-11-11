"""Servidor MCP de solo lectura.

Separación estricta: este módulo no importa nada del ETL (ni descargador ni
parsers). Solo abre la base SQLite en modo lectura y responde consultas.

Capas de protección de `consulta_sql`, de fuera adentro:
1. La conexión se abre con `mode=ro` (el fichero no se puede escribir).
2. `PRAGMA query_only = ON`.
3. Un *authorizer* de SQLite que solo autoriza SELECT, lecturas de columnas y
   funciones; deniega PRAGMA, ATTACH y cualquier escritura.
4. Límite de filas y de trabajo (se interrumpe una consulta desbocada).
"""

from __future__ import annotations

import hmac
import os
import re
import sqlite3
import statistics
import unicodedata
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from . import __version__

AVISO_LEGAL = (
    "Proyecto no oficial, no afiliado a la CNMC ni a ningún operador. "
    "Datos públicos publicados por la CNMC; consulte la fuente original antes de tomar decisiones."
)
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
LIMITE_MAXIMO = 500
PASOS_MAXIMOS = 20_000_000  # instrucciones de la máquina virtual de SQLite por consulta
INDICADORES = (
    "incluye_transporte", "incluye_almacenamiento", "incluye_expedicion", "incluye_puesta_a_bordo",
)  # fmt: skip

_ACCIONES_PERMITIDAS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def _plano(texto: Any) -> str:
    """Minúsculas y sin tildes, para buscar «queroseno» y encontrar «Querosenos»."""
    sin = "".join(
        c for c in unicodedata.normalize("NFD", str(texto or "")) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", sin).strip().casefold()


def _contiene(campo: Any, buscado: str | None) -> bool:
    return not buscado or _plano(buscado) in _plano(campo)


def _coincide(buscado: str | None, codigo: Any, *nombres: Any) -> bool:
    """Filtro por código o por nombre.

    Un texto corto (hasta 4 letras) solo puede ser un código y debe coincidir
    entero: así «CAN» es Canarias y no también Cantabria. Un texto más largo
    coincide con el código exacto o con parte del nombre.
    """
    if not buscado:
        return True
    b = _plano(buscado)
    if b == _plano(codigo) or any(b == _plano(n) for n in nombres):
        return True
    return len(b) > 4 and any(b in _plano(n) for n in nombres)


def _autorizador(accion: int, *_: Any) -> int:
    return sqlite3.SQLITE_OK if accion in _ACCIONES_PERMITIDAS else sqlite3.SQLITE_DENY


def abrir_solo_lectura(ruta: Path) -> sqlite3.Connection:
    if not ruta.exists():
        raise ToolError(f"No existe la base de datos {ruta}. Ejecuta antes «actualizar».")
    conn = sqlite3.connect(f"{ruta.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.create_function("plano", 1, _plano, deterministic=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _limite(limite: int) -> int:
    return max(1, min(int(limite), LIMITE_MAXIMO))


def crear_servidor(ruta_bd: Path) -> MCPServer:
    servidor = MCPServer(
        "logistica-hidrocarburos-cnmc",
        title="Logística de hidrocarburos (CNMC)",
        instructions=INSTRUCCIONES,
        version=__version__,
    )

    def consultar(sql: str, parametros: tuple | dict = ()) -> list[dict]:
        conn = abrir_solo_lectura(ruta_bd)
        try:
            return [dict(f) for f in conn.execute(sql, parametros)]
        finally:
            conn.close()

    def operadores() -> dict[str, str]:
        return {f["codigo"]: f["nombre"] for f in consultar("SELECT * FROM operadores_vigentes")}

    def detalles_vigentes() -> list[dict]:
        """Detalles vigentes con su instalación, operador y productos."""
        nombres = operadores()
        productos: dict[int, list[str]] = {}
        for f in consultar("SELECT * FROM detalle_productos_vigentes ORDER BY id"):
            productos.setdefault(f["id_tanque_instalacion"], []).append(f["producto"])
        filas = consultar(
            """SELECT d.id_tanque_instalacion, d.id_tanque, d.operador, d.operador_buscador,
                      d.discrepancia_operador, d.titular, d.conexion, d.productos_texto,
                      d.tiene_origen_destino, i.titulo AS instalacion, i.tipo, i.comunidad,
                      i.comunidad_nombre
               FROM detalles_vigentes d
               JOIN instalaciones_vigentes i ON i.id_tanque = d.id_tanque"""
        )
        for f in filas:
            f["operador_nombre"] = nombres.get(f["operador"])
            f["productos"] = productos.get(f["id_tanque_instalacion"], [])
        return filas

    def filtrar_detalles(
        operador: str | None = None,
        tipo: str | None = None,
        comunidad: str | None = None,
        producto: str | None = None,
    ) -> list[dict]:
        salida = []
        for d in detalles_vigentes():
            if not _coincide(operador, d["operador"], d["operador_nombre"], d["titular"]):
                continue
            if tipo and _plano(tipo) != _plano(d["tipo"]):
                continue
            if not _coincide(comunidad, d["comunidad"], d["comunidad_nombre"]):
                continue
            if producto and not _contiene(d["productos_texto"], producto):
                continue
            salida.append(d)
        return salida

    # --- herramientas -------------------------------------------------------------

    @servidor.tool()
    def estado() -> dict[str, Any]:
        """Estado de la base: última foto del portal, volumen de datos, incidencias y tablas."""
        ejecuciones = consultar(
            "SELECT id, fecha, registrada_en, altas, cambios, desapariciones, errores, avisos "
            "FROM ejecuciones ORDER BY id DESC LIMIT 5"
        )
        if not ejecuciones:
            raise ToolError("La base no tiene ninguna carga todavía.")
        ultima = ejecuciones[0]
        fotos = consultar(
            "SELECT COUNT(DISTINCT fecha) AS n, MIN(fecha) AS primera FROM ejecuciones"
        )
        tablas = {}
        for t in consultar("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
            nombre = t["name"]
            columnas = [c["name"] for c in consultar(f"PRAGMA table_info({nombre})")]
            total = consultar(f"SELECT COUNT(*) AS n FROM {nombre}")[0]["n"]
            tablas[nombre] = {"filas": total, "columnas": columnas}
            if "ultima_vez_visto" in columnas:
                tablas[nombre]["vigentes"] = consultar(
                    f"SELECT COUNT(*) AS n FROM {nombre} WHERE ultima_vez_visto = ?",
                    (ultima["fecha"],),
                )[0]["n"]
        return {
            "ultima_foto": ultima["fecha"],
            "primera_foto": fotos[0]["primera"],
            "fotos": fotos[0]["n"],
            "ultimas_ejecuciones": ejecuciones,
            "incidencias_ultima_ejecucion": consultar(
                "SELECT nivel, tipo, COUNT(*) AS n FROM incidencias WHERE ejecucion_id = ? "
                "GROUP BY nivel, tipo ORDER BY nivel, n DESC",
                (ultima["id"],),
            ),
            "cobertura_pdf": consultar(
                "SELECT COUNT(*) AS documentos, SUM(cifras) AS cifras, "
                "SUM(en_valores) AS en_valores, SUM(en_etiquetas + en_notas + en_cabecera) "
                "AS en_etiquetas_y_notas, SUM(descartadas) AS descartadas, "
                "SUM(ilegibles) AS ilegibles, SUM(sin_cubrir) AS sin_cubrir "
                "FROM cobertura_pdf_vigentes"
            )[0],
            "lineas_pdf_sin_resolver": consultar(
                "SELECT COUNT(*) AS n FROM lineas_pdf_sin_resolver"
            )[0]["n"],
            "tablas": tablas,
            "vistas": "Cada tabla con historial tiene una vista <tabla>_vigentes.",
            "version": __version__,
            "aviso_legal": AVISO_LEGAL,
        }

    @servidor.tool()
    def listar_operadores(solo_con_instalaciones: bool = True) -> dict[str, Any]:
        """Operadores (almacenistas) con su nº de instalaciones y detalles, por tipo.

        Una instalación con dos titulares (p. ej. Exolum Corporation y Exolum Aviation
        en un aeropuerto) cuenta para los dos. `nombres_anteriores` son los nombres
        con que el portal ha publicado antes ese mismo código.
        """
        nombres = operadores()
        historico: dict[str, list[str]] = {}
        for f in consultar("SELECT codigo, nombre FROM operadores_nombres ORDER BY id"):
            if f["nombre"] != nombres.get(f["codigo"]):
                historico.setdefault(f["codigo"], []).append(f["nombre"])
        por_operador: dict[str, dict] = {}
        for d in detalles_vigentes():
            o = por_operador.setdefault(
                d["operador"], {"detalles": 0, "almacenamiento": set(), "aeroportuaria": set()}
            )
            o["detalles"] += 1
            o.setdefault(d["tipo"], set()).add(d["id_tanque"])
        salida = []
        for codigo, nombre in nombres.items():
            o = por_operador.get(codigo)
            if o is None and solo_con_instalaciones:
                continue
            alm = len(o["almacenamiento"]) if o else 0
            aer = len(o["aeroportuaria"]) if o else 0
            salida.append(
                {
                    "codigo": codigo,
                    "nombre": nombre,
                    "instalaciones": alm + aer,
                    "instalaciones_almacenamiento": alm,
                    "instalaciones_aeroportuarias": aer,
                    "detalles": o["detalles"] if o else 0,
                    "nombres_anteriores": historico.get(codigo, []),
                }
            )
        salida.sort(key=lambda o: (-o["instalaciones"], o["codigo"]))
        total = consultar(
            "SELECT tipo, COUNT(*) AS n FROM instalaciones_vigentes GROUP BY tipo ORDER BY tipo"
        )
        return {
            "operadores": salida,
            "total_operadores": len(salida),
            "instalaciones_totales_por_tipo": {t["tipo"]: t["n"] for t in total},
            "nota": "Las instalaciones compartidas por dos titulares cuentan para ambos.",
        }

    @servidor.tool()
    def listar_instalaciones(
        operador: str | None = None,
        tipo: str | None = None,
        comunidad: str | None = None,
        producto: str | None = None,
        limite: int = 200,
    ) -> dict[str, Any]:
        """Instalaciones vigentes, con filtros opcionales.

        operador: código (CLH, DECAL…) o parte del nombre. tipo: «almacenamiento» o
        «aeroportuaria». comunidad: código (AND, CAN…) o nombre. producto: parte del
        nombre («queroseno», «gasóleo»…); no distingue mayúsculas ni tildes.
        """
        agrupadas: dict[int, dict] = {}
        for d in filtrar_detalles(operador, tipo, comunidad, producto):
            inst = agrupadas.setdefault(
                d["id_tanque"],
                {
                    "id_tanque": d["id_tanque"],
                    "instalacion": d["instalacion"],
                    "tipo": d["tipo"],
                    "comunidad": d["comunidad_nombre"] or d["comunidad"],
                    "operadores": [],
                    "productos": [],
                    "detalles": [],
                },
            )
            if d["operador"] not in inst["operadores"]:
                inst["operadores"].append(d["operador"])
            inst["productos"] += [p for p in d["productos"] if p not in inst["productos"]]
            inst["detalles"].append(d["id_tanque_instalacion"])
        if not any((operador, producto)):
            # Sin filtros que dependan del detalle se incluyen las instalaciones sin productos.
            for i in consultar("SELECT * FROM instalaciones_vigentes"):
                if i["id_tanque"] in agrupadas:
                    continue
                if tipo and _plano(tipo) != _plano(i["tipo"]):
                    continue
                if not _coincide(comunidad, i["comunidad"], i["comunidad_nombre"]):
                    continue
                agrupadas[i["id_tanque"]] = {
                    "id_tanque": i["id_tanque"],
                    "instalacion": i["titulo"],
                    "tipo": i["tipo"],
                    "comunidad": i["comunidad_nombre"] or i["comunidad"],
                    "operadores": [],
                    "productos": [],
                    "detalles": [],
                }
        filas = sorted(agrupadas.values(), key=lambda i: i["id_tanque"])
        return {"total": len(filas), "instalaciones": filas[: _limite(limite)]}

    @servidor.tool()
    def ver_instalacion(id_tanque: int) -> dict[str, Any]:
        """Ficha completa de una instalación: detalles, precios, condiciones y capacidad.

        De cada detalle se dan los precios de su página (`precios`), los de las tarifas
        en PDF a las que remite (`precios_pdf`, con documento y página) y la lista de
        esos PDF (`documentos_pdf`). Las matrices se consultan con `reconsignaciones`
        y `precios_origen_destino`; las bonificaciones, con `bonificaciones`.
        """
        inst = consultar("SELECT * FROM instalaciones_vigentes WHERE id_tanque = ?", (id_tanque,))
        if not inst:
            raise ToolError(f"No hay ninguna instalación vigente con id_tanque = {id_tanque}.")
        detalles = [d for d in detalles_vigentes() if d["id_tanque"] == id_tanque]
        for d in detalles:
            id_ti = (d["id_tanque_instalacion"],)
            d["precios"] = consultar(
                """SELECT seccion, etiqueta, subetiqueta, categoria_servicio, tipo_precio,
                          tipo_valor, valor, unidad, fecha_precio, anio_tabla, observacion, texto,
                          documentos, primera_vez_visto
                   FROM precios_vigentes WHERE id_tanque_instalacion = ? ORDER BY id""",
                id_ti,
            )
            d["condiciones"] = consultar(
                """SELECT etiqueta, subetiqueta, tipo_valor, valor, unidad, texto
                   FROM condiciones_vigentes WHERE id_tanque_instalacion = ? ORDER BY id""",
                id_ti,
            )
            d["servicios_incluidos"] = consultar(
                "SELECT servicio, texto FROM servicios_incluidos_vigentes "
                "WHERE id_tanque_instalacion = ? ORDER BY id",
                id_ti,
            )
            d["precios_pdf"] = [
                {k: p[k] for k in ("documento", "pagina", "etiqueta", "seccion", "concepto",
                                   "subconcepto", "categoria_servicio", "valor", "unidad",
                                   "anio_tabla", "vigencia", "condicion", "metodo")}
                for p in consultar(
                    "SELECT * FROM precios_pdf_por_detalle WHERE id_tanque_instalacion = ? "
                    "ORDER BY documento, id",
                    id_ti,
                )
            ]  # fmt: skip
            d["documentos_pdf"] = consultar(
                """SELECT DISTINCT e.documento, e.etiqueta, doc.nombre_archivo, doc.anio,
                          (SELECT COUNT(*) FROM matrices_pdf_vigentes m
                            WHERE m.documento = e.documento) AS celdas_de_matriz,
                          (SELECT COUNT(*) FROM bonificaciones_vigentes b
                            WHERE b.documento = e.documento) AS bonificaciones
                   FROM enlaces_documento_vigentes e
                   LEFT JOIN documentos_vigentes doc ON doc.clave = e.documento
                   WHERE e.id_tanque_instalacion = ? ORDER BY e.documento""",
                id_ti,
            )
            d["rutas_origen_destino"] = consultar(
                "SELECT COUNT(*) AS n FROM precios_origen_destino_vigentes "
                "WHERE id_tanque_instalacion = ? AND estado = 'ofrecida'",
                id_ti,
            )[0]["n"]
        return {
            "instalacion": inst[0],
            "detalles": detalles,
            "capacidades": consultar(
                "SELECT compania, instalacion, producto, capacidad_miles_m3, anio "
                "FROM capacidades_vigentes WHERE id_tanque = ? ORDER BY id",
                (id_tanque,),
            ),
        }

    def contenido_de_documentos() -> dict[str, dict[str, int]]:
        """Para cada PDF, cuántas filas estructuradas tiene en cada tabla."""
        contenido: dict[str, dict[str, int]] = {}
        for tabla in ("precios_pdf", "matrices_pdf", "bonificaciones"):
            for f in consultar(f"SELECT documento, COUNT(*) AS n FROM {tabla}_vigentes GROUP BY 1"):
                contenido.setdefault(f["documento"], {})[tabla] = f["n"]
        return contenido

    def precios_con_contexto(detalles: list[dict]) -> list[dict]:
        """Precios vigentes de esos detalles: los de la página (fuente «html») y los de
        las tarifas en PDF a las que remite cada detalle (fuente «pdf»)."""
        por_id = {d["id_tanque_instalacion"]: d for d in detalles}
        contenido = contenido_de_documentos()
        salida = []
        lineas: dict[tuple, dict] = {}
        for p in consultar("SELECT * FROM precios_vigentes ORDER BY id"):
            d = por_id.get(p["id_tanque_instalacion"])
            if d is None:
                continue
            lineas[p["id_tanque_instalacion"], p["seccion"], p["etiqueta"], p["subetiqueta"]] = p
            donde = None
            if p["tipo_precio"] == "pdf":
                tablas = sorted(
                    {t for c in p["documentos"].split(",") for t in contenido.get(c, {})}
                )
                donde = "Datos del PDF en: " + ", ".join(tablas) if tablas else "PDF sin resolver"
            salida.append(
                {
                    "fuente": "html",
                    "pagina_pdf": None,
                    "resuelto_en": donde,
                    "id_tanque_instalacion": p["id_tanque_instalacion"],
                    "id_tanque": d["id_tanque"],
                    "instalacion": d["instalacion"],
                    "tipo_instalacion": d["tipo"],
                    "comunidad": d["comunidad_nombre"] or d["comunidad"],
                    "operador": d["operador"],
                    "operador_nombre": d["operador_nombre"],
                    "productos": d["productos_texto"],
                    **{
                        k: p[k]
                        for k in (
                            "seccion",
                            "etiqueta",
                            "subetiqueta",
                            "categoria_servicio",
                            *INDICADORES,
                            "tipo_precio",
                            "tipo_valor",
                            "valor",
                            "unidad",
                            "fecha_precio",
                            "anio_tabla",
                            "observacion",
                            "texto",
                            "documentos",
                        )
                    },
                }
            )
        contexto = (
            "id_tanque_instalacion", "id_tanque", "instalacion", "tipo_instalacion", "comunidad",
            "operador", "operador_nombre", "productos",
        )  # fmt: skip
        base = {f["id_tanque_instalacion"]: f for f in salida}
        for p in consultar("SELECT * FROM precios_pdf_por_detalle ORDER BY id"):
            if p["id_tanque_instalacion"] not in por_id or p["id_tanque_instalacion"] not in base:
                continue
            linea = lineas.get(
                (p["id_tanque_instalacion"], p["seccion_detalle"], p["etiqueta"], p["subetiqueta"])
            )
            # Los indicadores de «Incluye:» solo valen si el precio es del mismo servicio.
            misma = linea is not None and linea["categoria_servicio"] == p["categoria_servicio"]
            concepto = " · ".join(x for x in (p["seccion"], p["concepto"], p["subconcepto"]) if x)
            salida.append(
                {
                    "fuente": "pdf",
                    "pagina_pdf": p["pagina"],
                    "resuelto_en": None,
                    **{k: base[p["id_tanque_instalacion"]][k] for k in contexto},
                    "seccion": p["seccion_detalle"],
                    "etiqueta": p["etiqueta"],
                    "subetiqueta": concepto,
                    "categoria_servicio": p["categoria_servicio"],
                    **{i: (linea[i] if misma else None) for i in INDICADORES},
                    "tipo_precio": "numerico" if p["valor"] is not None else "texto",
                    "tipo_valor": "unico" if p["valor"] is not None else None,
                    "valor": p["valor"],
                    "unidad": p["unidad"],
                    "fecha_precio": None,
                    "anio_tabla": p["anio_tabla"],
                    "observacion": p["condicion"],
                    "texto": None if p["valor"] is not None else p["texto_original"],
                    "documentos": p["documento"],
                }
            )
        return salida

    @servidor.tool()
    def buscar_precios(
        operador: str | None = None,
        categoria: str | None = None,
        producto: str | None = None,
        comunidad: str | None = None,
        tipo_instalacion: str | None = None,
        texto: str | None = None,
        solo_numericos: bool = False,
        limite: int = 100,
    ) -> dict[str, Any]:
        """Precios vigentes con filtros. No agrupa: para comparar usa `comparar_servicio`.

        Cada precio dice su `fuente`: «html» (la página del detalle) o «pdf» (una tarifa
        en PDF a la que remite el detalle; `documentos` es el PDF y `pagina_pdf` la
        página). Una línea «html» con tipo_precio «pdf» es solo el puntero al documento;
        `resuelto_en` dice en qué tablas están sus datos.

        categoria: categoria_servicio exacta (p. ej. «almacenamiento»). texto: parte de
        la etiqueta o subetiqueta del servicio. tipo_precio puede ser numerico, regla
        (p. ej. «según contrato»), texto o pdf (remite a un documento; valor nulo).
        """
        filas = []
        for p in precios_con_contexto(
            filtrar_detalles(operador, tipo_instalacion, comunidad, producto)
        ):
            if categoria and _plano(categoria) != _plano(p["categoria_servicio"]):
                continue
            if texto and not _contiene(f"{p['etiqueta']} {p['subetiqueta'] or ''}", texto):
                continue
            if solo_numericos and p["tipo_precio"] != "numerico":
                continue
            filas.append(p)
        categorias = sorted({p["categoria_servicio"] for p in filas})
        return {"total": len(filas), "categorias": categorias, "precios": filas[: _limite(limite)]}

    @servidor.tool()
    def comparar_servicio(
        categoria: str,
        producto: str | None = None,
        tipo_instalacion: str | None = None,
        comunidad: str | None = None,
        operador: str | None = None,
    ) -> dict[str, Any]:
        """Compara el precio de una categoría de servicio entre instalaciones y operadores.

        Solo son comparables los precios de un mismo grupo: misma categoría, mismos
        indicadores de lo que incluye el servicio (transporte, almacenamiento,
        expedición, puesta a bordo) y misma unidad. Si salen varios grupos, la
        respuesta lo avisa: no mezcles sus cifras. Incluye los precios de las tarifas
        en PDF (fuente «pdf», con su documento y página; solo tienen año, no fecha).
        Los servicios sin precio numérico se listan aparte con `resuelto_en`.
        """
        candidatos = [
            p
            for p in precios_con_contexto(
                filtrar_detalles(operador, tipo_instalacion, comunidad, producto)
            )
            if _plano(p["categoria_servicio"]) == _plano(categoria)
        ]
        if not candidatos:
            existentes = consultar(
                "SELECT DISTINCT categoria_servicio AS c FROM precios_vigentes ORDER BY 1"
            )
            raise ToolError(
                f"Sin precios vigentes para la categoría «{categoria}» con esos filtros. "
                f"Categorías disponibles: {', '.join(e['c'] for e in existentes)}"
            )
        grupos: dict[tuple, dict] = {}
        sin_numero = []
        for p in candidatos:
            if p["tipo_precio"] != "numerico":
                sin_numero.append(
                    {k: p[k] for k in ("instalacion", "operador", "productos", "etiqueta",
                                       "subetiqueta", "tipo_precio", "texto", "documentos",
                                       "resuelto_en")}
                )  # fmt: skip
                continue
            clave = (*(p[i] for i in INDICADORES), p["unidad"])
            g = grupos.setdefault(
                clave,
                {"indicadores": {i: p[i] for i in INDICADORES}, "unidad": p["unidad"], "filas": []},
            )
            g["filas"].append(
                {k: p[k] for k in ("id_tanque_instalacion", "instalacion", "tipo_instalacion",
                                   "comunidad", "operador", "productos", "etiqueta",
                                   "subetiqueta", "tipo_valor", "valor", "fecha_precio",
                                   "anio_tabla", "observacion", "fuente", "documentos",
                                   "pagina_pdf")}
            )  # fmt: skip
        salida = []
        for g in grupos.values():
            valores = [f["valor"] for f in g["filas"]]
            g["resumen"] = {
                "precios": len(valores),
                "servicios": len({(f["id_tanque_instalacion"], f["etiqueta"], f["subetiqueta"])
                                  for f in g["filas"]}),
                "operadores": sorted({f["operador"] for f in g["filas"] if f["operador"]}),
                "minimo": min(valores),
                "mediana": round(statistics.median(valores), 4),
                "maximo": max(valores),
            }  # fmt: skip
            g["filas"].sort(key=lambda f: f["valor"])
            salida.append(g)
        salida.sort(key=lambda g: -g["resumen"]["precios"])
        avisos = []
        if len(salida) > 1:
            avisos.append(
                f"Hay {len(salida)} grupos no comparables entre sí (difieren en lo que incluye el "
                "servicio o en la unidad). Compara solo dentro de cada grupo."
            )
        if any(f["tipo_valor"] != "unico" for g in salida for f in g["filas"]):
            avisos.append(
                "Algunos operadores publican un rango (tipo_valor max/min), no un precio único."
            )
        return {
            "categoria": categoria,
            "grupos": salida,
            "sin_precio_numerico": sin_numero,
            "avisos": avisos,
        }

    @servidor.tool()
    def historial_precio(
        id_tanque_instalacion: int, etiqueta: str | None = None, subetiqueta: str | None = None
    ) -> dict[str, Any]:
        """Todas las versiones de los precios de un detalle, vigentes o no.

        Cada fila dice entre qué fotos se vio (primera_vez_visto … ultima_vez_visto) y
        la fecha de vigencia que publica la CNMC (fecha_precio). `vigente` indica si
        sigue en la última foto. etiqueta/subetiqueta: parte del nombre del servicio.
        """
        ultima = consultar("SELECT MAX(fecha) AS f FROM ejecuciones")[0]["f"]
        filas = [
            {**f, "vigente": f["ultima_vez_visto"] == ultima}
            for f in consultar(
                """SELECT seccion, etiqueta, subetiqueta, categoria_servicio, tipo_precio,
                          tipo_valor, valor, unidad, fecha_precio, anio_tabla, texto,
                          primera_vez_visto, ultima_vez_visto
                   FROM precios WHERE id_tanque_instalacion = ?
                   ORDER BY seccion, etiqueta, subetiqueta, tipo_valor, primera_vez_visto""",
                (id_tanque_instalacion,),
            )
            if _contiene(f["etiqueta"], etiqueta) and _contiene(f["subetiqueta"], subetiqueta)
        ]
        if not filas:
            raise ToolError(
                f"Sin precios para id_tanque_instalacion = {id_tanque_instalacion} con ese filtro."
            )
        detalle = consultar(
            "SELECT titulo, titular, productos_texto FROM detalles "
            "WHERE id_tanque_instalacion = ? ORDER BY id DESC LIMIT 1",
            (id_tanque_instalacion,),
        )
        return {
            "detalle": detalle[0] if detalle else None,
            "ultima_foto": ultima,
            "versiones": filas,
        }

    @servidor.tool()
    def precios_origen_destino(
        origen: str | None = None,
        destino: str | None = None,
        modalidad: str | None = None,
        producto: str | None = None,
        incluir_no_ofrecidas: bool = False,
        limite: int = 200,
    ) -> dict[str, Any]:
        """Precio de llevar producto desde un origen hasta una instalación de destino.

        origen: «Huelva» encuentra HUELVA y REFINERÍA HUELVA, que son orígenes distintos
        (cada fila dice cuál). destino: nombre de la instalación o del destino. El mismo
        par origen-destino tiene un precio por modalidad de recepción y por producto.
        Las rutas «no_ofrecida» no existen (no son gratis); por defecto se omiten.
        """
        detalles = {d["id_tanque_instalacion"]: d for d in detalles_vigentes()}
        filas = []
        for f in consultar("SELECT * FROM precios_origen_destino_vigentes ORDER BY id"):
            d = detalles.get(f["id_tanque_instalacion"])
            if d is None or (f["estado"] != "ofrecida" and not incluir_no_ofrecidas):
                continue
            if not _contiene(f["origen"], origen) or not _contiene(f["modalidad"], modalidad):
                continue
            if not _contiene(f"{f['destino']} {d['instalacion']}", destino):
                continue
            if not _contiene(d["productos_texto"], producto):
                continue
            filas.append(
                {
                    "origen": f["origen"],
                    "destino": f["destino"],
                    "instalacion_destino": d["instalacion"],
                    "id_tanque_instalacion": f["id_tanque_instalacion"],
                    "productos": d["productos_texto"],
                    "modalidad": f["modalidad"],
                    "estado": f["estado"],
                    "precio": f["precio"],
                    "unidad": f["unidad"],
                    "anio_tabla": f["anio_tabla"],
                    "operador": d["operador"],
                }
            )
        filas.sort(key=lambda f: (f["destino"] or "", f["productos"], f["origen"], f["modalidad"]))
        ids = sorted({f["id_tanque_instalacion"] for f in filas})[:20]
        notas = (
            consultar(
                f"""SELECT id_tanque_instalacion, texto FROM notas_origen_destino_vigentes
                    WHERE id_tanque_instalacion IN ({",".join("?" * len(ids))}) ORDER BY id""",
                tuple(ids),
            )
            if ids
            else []
        )
        return {
            "total": len(filas),
            "rutas": filas[: _limite(limite)],
            "notas": notas[:60],
            "aviso": "anio_tabla es el año de la tabla; la página no publica fecha exacta.",
        }

    @servidor.tool()
    def capacidades(
        compania: str | None = None,
        instalacion: str | None = None,
        id_tanque: int | None = None,
        ambito: str | None = None,
        producto: str | None = None,
        solo_totales: bool = False,
        limite: int = 300,
    ) -> dict[str, Any]:
        """Capacidad de almacenamiento (miles de m3) por compañía, instalación y producto.

        Fuente: PDF «Características de las instalaciones de almacenamiento» de la CNMC.
        producto = «TOTAL» es el total de la instalación. id_tanque es nulo cuando la
        instalación del PDF no tiene ficha en el portal. ambito: «almacenamiento» o
        «aeroportuaria». Una cifra nula significa que el PDF lista el producto sin desglose.
        """
        filas = [
            f
            for f in consultar(
                "SELECT ambito, compania, instalacion, producto, id_tanque, capacidad_miles_m3, "
                "anio FROM capacidades_vigentes ORDER BY id"
            )
            if _contiene(f["compania"], compania)
            and _contiene(f["instalacion"], instalacion)
            and (id_tanque is None or f["id_tanque"] == id_tanque)
            and (not ambito or _plano(ambito) == _plano(f["ambito"]))
            and (f["producto"] == "TOTAL" if solo_totales else _contiene(f["producto"], producto))
        ]
        total = sum(f["capacidad_miles_m3"] or 0 for f in filas if f["producto"] == "TOTAL")
        return {
            "total": len(filas),
            "suma_totales_miles_m3": round(total, 3),
            "unidad": "miles de m3",
            "capacidades": filas[: _limite(limite)],
        }

    @servidor.tool()
    def reconsignaciones(
        origen: str | None = None,
        destino: str | None = None,
        tipo: str = "reconsignacion",
        limite: int = 200,
    ) -> dict[str, Any]:
        """Precios de las matrices de los PDF de Exolum entre dos instalaciones.

        tipo «reconsignacion» (por defecto): cambiar la instalación de entrega de
        gasolinas y gasóleos, de la instalación ORIGEN a la de DESTINO. tipo
        «consignacion_ester»: consignación del éster metílico para mezcla en línea.
        tipo «origen_destino»: las tablas origen-destino tal como vienen en el PDF
        (para el día a día usa `precios_origen_destino`, que parte de la página web).
        origen/destino: parte del nombre («Huelva», «Rivabellosa»). Cada precio
        indica el PDF, la página y el año de la tabla. Estado «no_aplica» = «(*)».
        """
        filas = [
            {k: f[k] for k in ("origen", "destino", "modalidad", "estado", "precio", "unidad",
                               "anio_tabla", "marca", "id_tanque_origen", "id_tanque_destino",
                               "documento", "pagina", "metodo")}
            for f in consultar(
                "SELECT * FROM matrices_pdf_vigentes WHERE tipo = ? ORDER BY id", (tipo,)
            )
            if _contiene(f["origen"], origen) and _contiene(f["destino"], destino)
        ]  # fmt: skip
        if not filas:
            raise ToolError(
                f"Sin filas de tipo «{tipo}» para origen «{origen}» y destino «{destino}». "
                "Tipos: reconsignacion, consignacion_ester, origen_destino."
            )
        documentos = sorted({f["documento"] for f in filas})
        marcas = ",".join("?" * len(documentos))
        return {
            "total": len(filas),
            "filas": filas[: _limite(limite)],
            "notas": consultar(
                f"SELECT DISTINCT documento, texto FROM notas_pdf_vigentes "
                f"WHERE documento IN ({marcas}) ORDER BY documento, pagina, orden",
                tuple(documentos),
            ),
            "fuente": "pdf",
        }

    @servidor.tool()
    def bonificaciones(producto: str | None = None) -> dict[str, Any]:
        """Bonificaciones y compensaciones de los contratos a largo plazo (PDF de Exolum).

        Por cumplir (bonificación) o incumplir (compensación) las cantidades
        garantizadas. Hay un documento por familia de productos; `aplica_a` dice a qué
        productos e instalaciones corresponde. producto: filtra por parte del nombre
        («queroseno», «gasolinas»). Incluye los umbrales en % y las notas del documento.
        """
        aplica: dict[str, dict] = {}
        for f in consultar(
            """SELECT e.documento, d.productos_texto, COUNT(DISTINCT d.id_tanque_instalacion) AS n
               FROM enlaces_documento_vigentes e
               JOIN detalles_vigentes d USING (id_tanque_instalacion)
               WHERE e.documento IN (SELECT documento FROM bonificaciones_vigentes)
               GROUP BY 1, 2"""
        ):
            a = aplica.setdefault(f["documento"], {"productos": [], "detalles": 0})
            a["productos"].append(f["productos_texto"])
            a["detalles"] += f["n"]
        salida = []
        for doc in consultar(
            "SELECT clave, nombre_archivo, anio FROM documentos_vigentes "
            "WHERE clave IN (SELECT documento FROM bonificaciones_vigentes) ORDER BY clave"
        ):
            a = aplica.get(doc["clave"], {"productos": [], "detalles": 0})
            if producto and not any(_contiene(p, producto) for p in a["productos"]):
                continue
            salida.append(
                {
                    "documento": doc["clave"],
                    "nombre_archivo": doc["nombre_archivo"],
                    "anio": doc["anio"],
                    "aplica_a": a,
                    "filas": consultar(
                        "SELECT bloque, tramo, condicion, valor, unidad, pagina, metodo "
                        "FROM bonificaciones_vigentes WHERE documento = ? ORDER BY id",
                        (doc["clave"],),
                    ),
                    "notas": [
                        n["texto"]
                        for n in consultar(
                            "SELECT texto FROM notas_pdf_vigentes WHERE documento = ? "
                            "ORDER BY pagina, orden",
                            (doc["clave"],),
                        )
                    ],
                }
            )
        if not salida:
            raise ToolError(f"Sin bonificaciones para el producto «{producto}».")
        return {"documentos": salida, "fuente": "pdf"}

    @servidor.tool()
    def consulta_sql(sql: str, limite: int = 200) -> dict[str, Any]:
        """Ejecuta una única sentencia SELECT (o WITH … SELECT) sobre la base, en solo lectura.

        Tablas principales: instalaciones, detalles, detalle_productos, operadores,
        operadores_nombres, precios, condiciones, servicios_incluidos,
        precios_origen_destino, notas_origen_destino, capacidades, documentos,
        filas_documento, enlaces_documento, precios_pdf, matrices_pdf, bonificaciones,
        notas_pdf, cobertura_pdf, ejecuciones, incidencias. Vistas útiles:
        precios_pdf_por_detalle, reconsignaciones, lineas_pdf_sin_resolver. Cada tabla
        con historial tiene una vista <tabla>_vigentes con lo visto en la última foto.
        La función plano(texto) quita tildes y mayúsculas. Usa `estado` para ver las
        columnas.
        """
        tope = _limite(limite)
        conn = abrir_solo_lectura(ruta_bd)
        pasos = 0

        def vigilar() -> int:
            nonlocal pasos
            pasos += 1
            return 1 if pasos * 10_000 > PASOS_MAXIMOS else 0  # ≠ 0 interrumpe la consulta

        try:
            conn.set_authorizer(_autorizador)
            conn.set_progress_handler(vigilar, 10_000)
            cursor = conn.execute(sql)
            if cursor.description is None:
                raise ToolError("Solo se admiten sentencias SELECT.")
            columnas = [c[0] for c in cursor.description]
            filas = cursor.fetchmany(tope + 1)
        except sqlite3.Warning as exc:  # más de una sentencia
            raise ToolError(f"Solo se admite una sentencia por consulta: {exc}") from exc
        except sqlite3.Error as exc:
            mensaje = str(exc)
            if "not authorized" in mensaje or "prohibited" in mensaje:
                mensaje = f"operación no permitida, solo SELECT ({mensaje})"
            elif "interrupted" in mensaje:
                mensaje = "consulta interrumpida por exceder el límite de trabajo"
            raise ToolError(f"Error SQL: {mensaje}") from exc
        finally:
            conn.close()
        return {
            "columnas": columnas,
            "filas": [list(f) for f in filas[:tope]],
            "truncado": len(filas) > tope,
            "limite": tope,
        }

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
