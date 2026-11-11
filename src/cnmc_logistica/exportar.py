"""Exportación de la base para publicarla como Release de datos.

Genera en una carpeta: la base SQLite y su sha256, un CSV y un Parquet por
tabla con el estado vigente, cuatro tablas planas listas para Excel, un Parquet por tabla con el historial completo
(todas las versiones de cada fila) y un diccionario de datos. Así quien solo
quiere los datos no necesita instalar nada del proyecto: abre el CSV en una
hoja de cálculo o el Parquet en pandas.
"""

from __future__ import annotations

import csv
import hashlib
import re
import shutil
import sqlite3
import zipfile
from pathlib import Path

from . import consultas
from .bd import TABLAS
from .config import BASE_PORTAL

# Tablas de registro: se exportan enteras (no tienen «vigentes»).
REGISTRO = ("ejecuciones", "incidencias")

DESCRIPCIONES = {
    "operadores": "Operadores (almacenistas) por su código estable en el buscador de la CNMC.",
    "operadores_nombres": "Nombre con el que el portal publica cada código de operador.",
    "instalaciones": "Instalaciones físicas (idTanque): tipo y comunidad autónoma.",
    "detalles": "Un producto dentro de una instalación (idTanqueInstalacion), con su titular.",
    "detalle_productos": "Productos de cada detalle, separados a partir de la cadena del portal.",
    "precios": "Precios publicados en la página de cada detalle; un valor por fila.",
    "condiciones": "Mermas y demás condiciones de cada detalle.",
    "servicios_incluidos": "Lista «Incluye:» de cada servicio principal.",
    "precios_origen_destino": "Precio de llevar producto de un origen a un detalle de destino, por modalidad.",
    "notas_origen_destino": "Notas al pie de cada página origen-destino.",
    "capacidades": "Capacidad de almacenamiento por compañía, instalación y producto (PDF de la CNMC).",
    "documentos": "PDF enlazados desde el portal: nombre, sha256, páginas y año.",
    "filas_documento": "Tablas de cada PDF tal como las detecta el extractor genérico.",
    "enlaces_documento": "Qué línea de precios de qué detalle remite a qué PDF.",
    "precios_pdf": "Tarifas de los PDF (concepto → precio), con página y posición de cada cifra.",
    "matrices_pdf": "Matrices origen × destino de los PDF: origen-destino, reconsignación y éster.",
    "bonificaciones": "Bonificaciones y compensaciones de los contratos a largo plazo.",
    "condiciones_pdf": "Cantidades con unidad dentro de las notas de los PDF (días, toneladas…).",
    "notas_pdf": "Notas explicativas de cada PDF (las marcadas [INFERIDO] son deducciones propias).",
    "cobertura_pdf": "Por PDF: cifras totales, guardadas, descartadas y sin cubrir.",
    "ejecuciones": "Registro de cada carga o reconstrucción: fecha, versión y nº de altas y cambios.",
    "incidencias": "Avisos y errores de cada ejecución (recurso, operador y texto).",
}
COMUNES = {
    "id": "Identificador interno de la fila.",
    "primera_vez_visto": "Fecha de la primera foto del portal en la que apareció esta fila (AAAA-MM-DD).",
    "ultima_vez_visto": "Fecha de la última foto en la que seguía igual. Si es la última foto, está vigente.",
    "id_tanque": "Identificador de la instalación en el portal de la CNMC (idTanque).",
    "id_tanque_instalacion": "Identificador del detalle en el portal (idTanqueInstalacion).",
    "categoria_servicio": "Categoría normalizada (config/categorias_servicio.yaml) para comparar servicios.",
    "tipo_precio": "numerico, regla (cómo se calcula), texto o pdf (remite a un documento).",
    "tipo_valor": "unico, max o min; nulo si no hay número.",
    "valor": "Importe o cantidad; nulo si no es numérico o la cifra es ilegible.",
    "precio": "Importe; nulo si la ruta no se ofrece o no aplica.",
    "unidad": "Unidad tal como se publica: €/m3, €/m3 y mes, €/Tm, %, días…",
    "fecha_precio": "Vigencia que publica la CNMC (AAAA-MM-DD); nula si no la indica.",
    "anio_tabla": "Año de la tabla de precios cuando la fuente solo da el año.",
    "texto_original": "Texto literal de la fuente del que sale la fila.",
    "documento": "Clave del PDF (su directorioHijo en el portal, o «capacidades»).",
    "metodo": "texto (capa de texto del PDF), ocr (página escaneada) o inferido.",
    "pagina": "Página del PDF donde está la cifra.",
    "x0": "Posición de la cifra en la página (puntos, borde izquierdo).",
    "top": "Posición de la cifra en la página (puntos, desde arriba).",
    "x1": "Posición de la cifra en la página (puntos, borde derecho).",
    "bottom": "Posición de la cifra en la página (puntos, borde inferior).",
    "estado": "ofrecida / no_ofrecida (la web muestra 0,00) / no_aplica.",
    "capacidad_miles_m3": "Capacidad en miles de metros cúbicos.",
    "operador": "Código del operador cuyo nombre coincide con el titular de la página.",
    "operador_buscador": "Código bajo el que el buscador de la CNMC lista el detalle.",
    "conexion": "red, por_oleoducto, no_conectada o sin_dato (subtítulo de la página).",
    "altas": "Filas nuevas con clave nueva en esa ejecución.",
    "cambios": "Filas nuevas por cambio de valor de una clave existente.",
    "desapariciones": "Claves que dejaron de verse en esa ejecución.",
    "errores": "Incidencias de nivel error.",
    "avisos": "Incidencias de nivel aviso.",
    "anio": "Año al que se refiere el documento.",
    "bloque": "bonificacion o compensacion.",
    "bytes": "Tamaño del PDF.",
    "clave": "Clave del PDF (directorioHijo en el portal, o «capacidades»).",
    "codigo": "Código estable del operador en el buscador de la CNMC.",
    "compania": "Compañía tal como aparece en el PDF de capacidades.",
    "comunidad": "Código de comunidad autónoma del portal (AND, CAN…).",
    "comunidad_nombre": "Nombre de la comunidad autónoma.",
    "concepto": "Concepto tarifado en el PDF.",
    "subconcepto": "Desglose del concepto (producto, medio…).",
    "condicion": "Condición o columna a la que corresponde el valor.",
    "contacto": "Dirección, teléfono y web del titular.",
    "destino": "Destino tal como lo nombra la fuente.",
    "origen": "Origen tal como lo nombra la fuente.",
    "origen_normalizado": "Origen en mayúsculas y sin tildes; no fusiona nombres distintos.",
    "discrepancia_operador": "1 si el titular de la página no es el operador bajo el que lo lista el buscador.",
    "ejecucion_id": "Ejecución a la que pertenece la incidencia.",
    "fecha": "Fecha de la foto de raw/ cargada.",
    "registrada_en": "Fecha y hora (UTC) de la ejecución.",
    "en_valores": "Cifras del PDF guardadas como valor.",
    "en_etiquetas": "Cifras que forman parte de una etiqueta guardada.",
    "en_notas": "Cifras dentro de una nota guardada.",
    "en_cabecera": "Cifras del título (año).",
    "descartadas": "Cifras descartadas con justificación (nº de página, totales verificados).",
    "ilegibles": "Cifras guardadas sin valor por no poder interpretarse.",
    "sin_cubrir": "Cifras que no están en la base ni descartadas (debe ser 0).",
    "etiqueta": "Nombre del servicio o de la condición tal como aparece en la página.",
    "subetiqueta": "Subapartado (producto, modalidad…).",
    "fila": "Nº de fila dentro de la tabla del PDF.",
    "tabla": "Nº de tabla dentro de la página.",
    "tablas": "Nº de tablas detectadas en el PDF.",
    "id_tanque_destino": "Instalación de destino, si se ha podido identificar.",
    "id_tanque_origen": "Instalación de origen, si se ha podido identificar.",
    "incluye_transporte": "1 si la lista «Incluye:» del servicio contiene transporte del producto.",
    "incluye_almacenamiento": "1 si incluye almacenamiento.",
    "incluye_expedicion": "1 si incluye expedición o carga.",
    "incluye_puesta_a_bordo": "1 si incluye puesta a bordo.",
    "instalacion": "Nombre de la instalación en la fuente.",
    "modalidad": "Modalidad de recepción.",
    "nivel": "error o aviso.",
    "nombre": "Nombre del operador publicado por el portal.",
    "nombre_archivo": "Nombre original del PDF.",
    "observacion": "Texto que acompaña a la unidad.",
    "ocurrencia": "1 salvo que la fuente repita la misma clave.",
    "orden": "Posición de la nota en el documento.",
    "paginas": "Nº de páginas.",
    "paginas_sin_texto": "Páginas sin capa de texto ni OCR.",
    "parser": "Parser que interpretó el PDF.",
    "por_tabla": "JSON con altas, cambios y desapariciones por tabla.",
    "producto": "Producto.",
    "producto_buscador": "Producto según el buscador de la CNMC.",
    "productos_texto": "Cadena de productos tal como la publica la página.",
    "recurso": "Recurso afectado, p. ej. detalle/17 o pdf/24.",
    "recursos": "Nº de ficheros de la foto.",
    "seccion": "principal u otros_servicios (o sección del PDF).",
    "servicio": "Servicio principal al que pertenece el elemento incluido.",
    "sha256": "Huella del fichero.",
    "subtitulo": "Subtítulo de la página.",
    "texto": "Texto no numérico (regla, condición o nota).",
    "tiene_origen_destino": "1 si el detalle tiene página origen-destino.",
    "tipo": "Tipo (de instalación, de matriz o de ejecución, según la tabla).",
    "titular": "Titular tal como aparece en la página.",
    "titulo": "Título de la página o del documento.",
    "tramo": "Tramo de la bonificación o compensación.",
    "version": "Versión del programa.",
    "vigencia": "Periodo de vigencia indicado en el PDF.",
}


PLANAS = {
    "precios_completos": "Todos los precios vigentes, los de las páginas (fuente = web) y los de "
    "las tarifas en PDF (fuente = pdf), una fila por precio y detalle.",
    "condiciones_completas": "Mermas, plazos y demás condiciones vigentes, de las páginas y de "
    "las notas de los PDF.",
    "origen_destino_completo": "Precio de llevar producto de cada origen a cada instalación de "
    "destino, por modalidad (páginas origen-destino del portal).",
    "capacidades_completas": "Capacidad de almacenamiento por compañía, instalación y producto.",
}
COLUMNAS_PLANAS = {
    "fuente": "web (página del portal) o pdf (tarifa en PDF enlazada desde la página).",
    "operador": "Nombre del operador (titular de la página).",
    "operador_codigo": "Código estable del operador en el buscador de la CNMC.",
    "instalacion": "Nombre de la instalación.",
    "tipo_instalacion": "almacenamiento o aeroportuaria.",
    "comunidad": "Comunidad autónoma.",
    "productos": "Productos del detalle, tal como los publica la página.",
    "seccion": "principal u otros_servicios.",
    "servicio": "Nombre del servicio o de la condición tal como aparece en la página.",
    "detalle_servicio": "Subapartado; en los precios de PDF, el concepto de la tarifa.",
    "categoria": "Categoría normalizada del servicio; solo son comparables los precios de la misma categoría.",
    "documento_pdf": "Clave del PDF del que sale el dato o al que remite la página (varias, separadas por |).",
    "pagina_pdf": "Página del PDF donde está la cifra.",
    "url_cnmc": "Página de la CNMC de la que sale el dato.",
    "url_pdf": "Descarga del PDF en el portal de la CNMC.",
    "foto": "Fecha de la descarga del portal a la que corresponde el fichero.",
    "destino": "Instalación de destino.",
    "id_tanque_instalacion": "Identificador del detalle en el portal (idTanqueInstalacion).",
    "ambito": "almacenamiento o aeroportuaria (tabla del PDF de capacidades).",
    "producto": "Producto; TOTAL es la suma de la instalación.",
}


def _url_detalle(id_ti: int) -> str:
    return f"{BASE_PORTAL}/detalle.htm?idTanqueInstalacion={id_ti}"


def _url_pdf(claves: str | None) -> str | None:
    """Enlace de descarga cuando el dato remite a un único PDF de tarifas."""
    if not claves or "," in claves:
        return None
    if claves == "capacidades":
        return f"{BASE_PORTAL}/downloadfile.htm?directorioPadre=carpdfpub"
    return f"{BASE_PORTAL}/downloadfile.htm?directorioPadre=pdfpub&directorioHijo={claves}"


def _contexto(d: dict) -> dict:
    return {
        "operador": d["operador_nombre"] or d["titular"],
        "operador_codigo": d["operador"],
        "instalacion": d["instalacion"],
        "tipo_instalacion": d["tipo"],
        "comunidad": d["comunidad_nombre"] or d["comunidad"],
        "productos": d["productos_texto"],
    }


def _ids(d: dict, foto: str) -> dict:
    return {
        "id_tanque": d["id_tanque"],
        "id_tanque_instalacion": d["id_tanque_instalacion"],
        "url_cnmc": _url_detalle(d["id_tanque_instalacion"]),
        "foto": foto,
    }


def tablas_planas(conn: sqlite3.Connection, foto: str) -> dict[str, list[dict]]:
    """Tablas listas para usar: lo vigente con los nombres ya cruzados, una fila por dato.

    Usan las mismas consultas que el MCP y la API, así que dicen lo mismo que ellos.
    """
    detalles = {d["id_tanque_instalacion"]: d for d in consultas.detalles_vigentes(conn)}
    titulares = {
        f["id_tanque_instalacion"]: f["titular"]
        for f in consultas.consultar(
            conn, "SELECT id_tanque_instalacion, titular FROM detalles_vigentes"
        )
    }
    for i, d in detalles.items():
        d["titular"] = titulares.get(i)

    precios = []
    for p in consultas.precios_con_contexto(conn, list(detalles.values())):
        d = detalles[p["id_tanque_instalacion"]]
        precios.append(
            {"fuente": "web" if p["fuente"] == "html" else "pdf"}
            | _contexto(d)
            | {
                "seccion": p["seccion"],
                "servicio": p["etiqueta"],
                "detalle_servicio": p["subetiqueta"],
                "categoria": p["categoria_servicio"],
                **{i: p[i] for i in consultas.INDICADORES},
                "tipo_precio": p["tipo_precio"],
                "tipo_valor": p["tipo_valor"],
                "valor": p["valor"],
                "unidad": p["unidad"],
                "fecha_precio": p["fecha_precio"],
                "anio_tabla": p["anio_tabla"],
                "observacion": p["observacion"],
                "texto": p["texto"],
                # «24,25» en Excel sería el número 24,25: las claves se separan con « | ».
                "documento_pdf": (p["documentos"] or "").replace(",", " | ") or None,
                "pagina_pdf": p["pagina_pdf"],
                "url_pdf": _url_pdf(p["documentos"]),
            }
            | _ids(d, foto)
        )

    condiciones = []
    for c in consultas.consultar(conn, "SELECT * FROM condiciones_vigentes ORDER BY id"):
        if (d := detalles.get(c["id_tanque_instalacion"])) is None:
            continue
        condiciones.append(
            {"fuente": "web"}
            | _contexto(d)
            | {
                "servicio": c["etiqueta"],
                "detalle_servicio": c["subetiqueta"],
                "tipo_valor": c["tipo_valor"],
                "valor": c["valor"],
                "unidad": c["unidad"],
                "texto": c["texto"],
                "documento_pdf": None,
                "pagina_pdf": None,
                "url_pdf": None,
            }
            | _ids(d, foto)
        )
    # Las condiciones de un PDF valen para todos los detalles que remiten a él.
    for c in consultas.consultar(
        conn,
        """SELECT DISTINCT e.id_tanque_instalacion, c.documento, c.texto, c.unidad, c.valor,
                  c.pagina, c.id
           FROM condiciones_pdf_vigentes c
           JOIN (SELECT DISTINCT id_tanque_instalacion, documento
                 FROM enlaces_documento_vigentes) e ON e.documento = c.documento
           ORDER BY e.id_tanque_instalacion, c.id""",
    ):
        if (d := detalles.get(c["id_tanque_instalacion"])) is None:
            continue
        condiciones.append(
            {"fuente": "pdf"}
            | _contexto(d)
            | {
                "servicio": "Condición del PDF",
                "detalle_servicio": None,
                "tipo_valor": "unico",
                "valor": c["valor"],
                "unidad": c["unidad"],
                "texto": c["texto"],
                "documento_pdf": c["documento"],
                "pagina_pdf": c["pagina"],
                "url_pdf": _url_pdf(c["documento"]),
            }
            | _ids(d, foto)
        )

    rutas = []
    for r in consultas.consultar(conn, "SELECT * FROM precios_origen_destino_vigentes ORDER BY id"):
        if (d := detalles.get(r["id_tanque_instalacion"])) is None:
            continue
        contexto = _contexto(d)
        rutas.append(
            {
                "operador": contexto["operador"],
                "operador_codigo": contexto["operador_codigo"],
                "origen": r["origen"],
                "destino": contexto["instalacion"],
                "tipo_instalacion": contexto["tipo_instalacion"],
                "comunidad": contexto["comunidad"],
                "productos": contexto["productos"],
                "modalidad": r["modalidad"],
                "estado": r["estado"],
                "precio": r["precio"],
                "unidad": r["unidad"],
                "anio_tabla": r["anio_tabla"],
                "id_tanque": d["id_tanque"],
                "id_tanque_instalacion": d["id_tanque_instalacion"],
                "url_cnmc": f"{BASE_PORTAL}/precio-producto.htm"
                f"?idTanqueInstalacion={d['id_tanque_instalacion']}",
                "foto": foto,
            }
        )

    capacidades = [
        f | {"url_cnmc": _url_pdf("capacidades"), "foto": foto}
        for f in consultas.consultar(
            conn,
            """SELECT c.compania, c.instalacion, c.ambito, i.tipo AS tipo_instalacion,
                      COALESCE(i.comunidad_nombre, i.comunidad) AS comunidad, c.producto,
                      c.capacidad_miles_m3, c.anio, c.id_tanque
               FROM capacidades_vigentes c
               LEFT JOIN instalaciones_vigentes i ON i.id_tanque = c.id_tanque
               ORDER BY c.id""",
        )
    ]
    return {
        "precios_completos": precios,
        "condiciones_completas": condiciones,
        "origen_destino_completo": rutas,
        "capacidades_completas": capacidades,
    }


def _comentarios_ddl(tabla: str) -> dict[str, str]:
    """Comentarios «-- …» escritos junto a cada columna en el esquema (bd.py)."""
    notas: dict[str, str] = {}
    for linea in TABLAS[tabla][2].splitlines():
        if "--" in linea:
            codigo, _, comentario = linea.partition("--")
            columnas = re.findall(r"(\w+) (?:TEXT|INTEGER|REAL)", codigo)
            if columnas:
                notas[columnas[-1]] = comentario.strip()
    return notas


def diccionario(conn: sqlite3.Connection, foto: str) -> str:
    partes = [
        f"# Diccionario de datos · foto {foto}\n",
        "Logística de productos petrolíferos publicada por la CNMC. Proyecto no oficial, no "
        "afiliado a la CNMC ni a ningún operador; datos públicos publicados por la CNMC.\n",
        "## Tablas planas (capa gold)\n",
        "Cuatro tablas con los nombres ya cruzados (operador, instalación, tipo, comunidad, "
        "producto…), una fila por dato y un enlace a la página de la CNMC de la que procede. "
        "Contienen las filas **vigentes** en la foto. Están en `csv.zip` y, con el mismo "
        "contenido, en `parquet.zip`.\n",
        "| Fichero | Contenido |\n|---|---|\n"
        + "\n".join(f"| `{n}.csv` | {d} |" for n, d in PLANAS.items())
        + "\n",
        "- En `precios_completos`, la comparación de precios requiere la misma `categoria` y "
        "la misma `unidad`: cada operador define sus servicios con sus propios términos. Los "
        "indicadores `incluye_*` "
        "dicen si el servicio principal incluye transporte, almacenamiento, expedición o puesta "
        "a bordo.\n"
        "- Un precio de PDF que vale para varios detalles aparece una vez por detalle.\n"
        "- `valor` vacío con `tipo_precio` = regla, texto o pdf: el precio no es una cifra. "
        "Está descrito en `texto` o en el PDF de `url_pdf`.\n"
        "- En `origen_destino_completo`, `estado` = no_ofrecida significa que el portal muestra "
        "0,00: la ruta no se ofrece; no es un precio.\n"
        "- En `capacidades_completas`, `producto` = TOTAL es la suma de la instalación y no "
        "debe sumarse con los productos.\n",
        "Columnas propias de estas tablas (el resto se explican más abajo, en la tabla "
        "normalizada correspondiente):\n",
        "| Columna | Significado |\n|---|---|\n"
        + "\n".join(f"| `{c}` | {d} |" for c, d in COLUMNAS_PLANAS.items())
        + "\n",
        "## Cómo leer los ficheros\n",
        "- `cnmc_logistica.db`: base SQLite con el modelo normalizado y todo el historial "
        "(capa silver). Cada tabla tiene una vista "
        "`<tabla>_vigentes` con lo visto en la última foto.\n"
        "- `csv/` y `parquet/`: las cuatro tablas planas y un fichero por tabla normalizada "
        "con las filas **vigentes** (las de registro, `ejecuciones` e `incidencias`, van enteras).\n"
        "- Los CSV usan el formato de Excel en español: UTF-8 con BOM, separador `;` y coma "
        "decimal. Lectura con pandas: `pd.read_csv(f, sep=';', decimal=',')`. Los Parquet "
        "llevan tipos de columna.\n"
        "- `historial/` (en `historial_parquet.zip`): un Parquet por tabla con **todas las "
        "versiones de cada fila**, vigentes o no. Lleva las mismas columnas más `vigente` "
        "(1 si la fila sigue en la última foto, 0 si es una versión anterior o algo que "
        "dejó de publicarse). El portal de la CNMC solo publica el estado actual.\n"
        "- Dos niveles: **instalación** (`id_tanque`) y **detalle**, un producto en una instalación "
        "(`id_tanque_instalacion`). Los precios pertenecen al detalle.\n"
        "- Historial: `primera_vez_visto` y `ultima_vez_visto` dicen entre qué fotos se vio cada "
        "fila. No se borra ni se modifica ninguna fila: un cambio es una fila nueva.\n",
        "## Cómo usar el historial\n",
        "Cada fila de `historial/<tabla>.parquet` es una versión, válida entre "
        "`primera_vez_visto` y `ultima_vez_visto` (ambas incluidas; son fechas de foto, y hay "
        "una foto al mes, así que un cambio se sitúa entre dos fotos consecutivas).\n\n"
        "- **Cambio de un precio**: la misma clave (detalle, sección, etiqueta…) aparece en "
        "dos filas; la antigua con `vigente = 0` y la nueva con `primera_vez_visto` posterior.\n"
        "- **Algo que dejó de publicarse**: su última fila tiene `vigente = 0` y ninguna "
        "posterior con la misma clave.\n"
        "- **Estado en una fecha F**: las filas con `primera_vez_visto <= F <= ultima_vez_visto`.\n"
        "- **Solo lo actual**: `vigente = 1`; es lo mismo que hay en `parquet/` y `csv/`.\n\n"
        "`ejecuciones` e `incidencias` no tienen versiones: están completas en `parquet/` y `csv/`. "
        "La foto de este fichero es la más reciente; las fotos cargadas están en `ejecuciones`.\n"
        "- Los precios de servicios distintos no son comparables. La comparación requiere la "
        "misma `categoria_servicio`, los mismos indicadores `incluye_*` y la misma `unidad`.\n",
        "## Unidades\n",
        "Precios en euros sin impuestos, en la unidad publicada (`€/m3`, `€/m3 y mes`, "
        "`€/m3 y día`, `€/Tm`, `€/operación`…). Mermas y descuentos en `%`. Capacidades en miles "
        "de m³. Fechas en formato ISO (AAAA-MM-DD).\n",
    ]
    for tabla in [*TABLAS, *REGISTRO]:
        columnas = [f["name"] for f in conn.execute(f"PRAGMA table_info({tabla})")]
        filas = conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
        notas = _comentarios_ddl(tabla) if tabla in TABLAS else {}
        partes.append(
            f"## `{tabla}`\n\n{DESCRIPCIONES.get(tabla, '')} ({filas} filas en la base)\n"
        )
        partes.append("| Columna | Significado |\n|---|---|")
        for c in columnas:
            partes.append(f"| `{c}` | {notas.get(c) or COMUNES.get(c, '')} |")
        partes.append("")
    return "\n".join(partes)


def exportar(ruta_bd: Path, salida: Path, con_parquet: bool = True) -> dict:
    """Escribe en `salida` todo lo que lleva una Release de datos y devuelve un resumen."""
    conn = sqlite3.connect(f"{ruta_bd.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    foto = conn.execute("SELECT MAX(fecha) FROM ejecuciones").fetchone()[0]
    salida.mkdir(parents=True, exist_ok=True)
    (salida / "csv").mkdir(exist_ok=True)

    copia = salida / "cnmc_logistica.db"
    shutil.copyfile(ruta_bd, copia)
    resumen = hashlib.sha256(copia.read_bytes()).hexdigest()
    (salida / "cnmc_logistica.db.sha256").write_text(f"{resumen}  cnmc_logistica.db\n")
    (salida / "DICCIONARIO.md").write_text(diccionario(conn, foto), encoding="utf-8")

    filas_por_tabla: dict[str, int] = {}
    for tabla in [*TABLAS, *REGISTRO]:
        origen = tabla if tabla in REGISTRO else f"{tabla}_vigentes"
        cursor = conn.execute(f"SELECT * FROM {origen} ORDER BY 1")
        columnas = [c[0] for c in cursor.description]
        filas = cursor.fetchall()
        filas_por_tabla[tabla] = len(filas)
        _escribir_csv(salida / "csv" / f"{tabla}.csv", columnas, filas)
        if con_parquet:
            _escribir_parquet(conn, tabla, columnas, filas, salida / "parquet")
            if tabla not in REGISTRO:
                todas = conn.execute(
                    f"SELECT *, ultima_vez_visto = ? AS vigente FROM {tabla} ORDER BY 1", (foto,)
                )
                columnas_h = [c[0] for c in todas.description]
                _escribir_parquet(conn, tabla, columnas_h, todas.fetchall(), salida / "historial")
    for nombre, registros in tablas_planas(conn, foto).items():
        columnas = list(registros[0])
        filas = [tuple(r[c] for c in columnas) for r in registros]
        filas_por_tabla[nombre] = len(filas)
        _escribir_csv(salida / "csv" / f"{nombre}.csv", columnas, filas)
        if con_parquet:
            _escribir_parquet(conn, None, columnas, filas, salida / "parquet", nombre)
    conn.close()
    # Un zip por formato, con un fichero por tabla dentro: cómodo de descargar de una Release.
    formatos = {"csv": "csv.zip"}
    if con_parquet:
        formatos |= {"parquet": "parquet.zip", "historial": "historial_parquet.zip"}
    for carpeta, nombre in formatos.items():
        with zipfile.ZipFile(salida / nombre, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted((salida / carpeta).iterdir()):
                z.write(f, f"{carpeta}/{f.name}")
    return {"foto": foto, "sha256": resumen, "tablas": filas_por_tabla}


def _escribir_csv(ruta: Path, columnas: list[str], filas: list) -> None:
    """CSV para Excel en español: UTF-8 con BOM, separador «;» y coma decimal."""
    with ruta.open("w", encoding="utf-8-sig", newline="") as fh:
        escritor = csv.writer(fh, delimiter=";")
        escritor.writerow(columnas)
        for fila in filas:
            escritor.writerow(
                [repr(v).replace(".", ",") if isinstance(v, float) else v for v in fila]
            )


def _escribir_parquet(
    conn, tabla: str | None, columnas: list[str], filas: list, carpeta: Path,
    nombre: str | None = None,
) -> None:  # fmt: skip
    import pyarrow as pa
    import pyarrow.parquet as pq

    carpeta.mkdir(exist_ok=True)
    datos = {c: [f[i] for f in filas] for i, c in enumerate(columnas)}
    if tabla is None:  # tabla plana: el tipo sale de los valores
        tipos = {}
        for c, valores in datos.items():
            clases = {type(v) for v in valores if v is not None}
            tipos[c] = "REAL" if float in clases else "INTEGER" if clases == {int} else "TEXT"
    else:
        tipos = {f["name"]: f["type"] for f in conn.execute(f"PRAGMA table_info({tabla})")} | {
            "vigente": "INTEGER"
        }
    a_arrow = {"INTEGER": pa.int64(), "REAL": pa.float64()}
    esquema = pa.schema([(c, a_arrow.get(tipos.get(c, "TEXT"), pa.string())) for c in columnas])
    pq.write_table(
        pa.table(datos, schema=esquema),
        carpeta / f"{nombre or tabla}.parquet",
        compression="zstd",
    )
