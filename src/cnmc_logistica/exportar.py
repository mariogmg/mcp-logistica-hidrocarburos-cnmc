"""Exportación de la base para publicarla como Release de datos.

Genera en una carpeta: la base SQLite y su sha256, un CSV y un Parquet por
tabla (el estado vigente; el historial completo está en la base) y un
diccionario de datos. Así quien solo quiere los datos no necesita instalar
nada del proyecto: abre el CSV en una hoja de cálculo o el Parquet en pandas.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import re
import shutil
import sqlite3
import zipfile
from pathlib import Path

from .bd import TABLAS

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
        "## Cómo leer los ficheros\n",
        "- `cnmc_logistica.db`: base SQLite con todo el historial. Cada tabla tiene una vista "
        "`<tabla>_vigentes` con lo visto en la última foto.\n"
        "- `csv/` y `parquet/`: un fichero por tabla con las filas **vigentes** (las de registro, "
        "`ejecuciones` e `incidencias`, van enteras). CSV en UTF-8, separador `;`, comprimido con gzip.\n"
        "- Dos niveles: **instalación** (`id_tanque`) y **detalle**, un producto en una instalación "
        "(`id_tanque_instalacion`). Los precios cuelgan del detalle.\n"
        "- Historial: `primera_vez_visto` y `ultima_vez_visto` dicen entre qué fotos se vio cada "
        "fila. Nunca se borra ni se corrige nada: un cambio es una fila nueva.\n"
        "- No compare precios de servicios distintos: use `categoria_servicio`, los indicadores "
        "`incluye_*` y la `unidad`.\n",
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
        with gzip.open(
            salida / "csv" / f"{tabla}.csv.gz", "wt", encoding="utf-8", newline=""
        ) as fh:
            escritor = csv.writer(fh, delimiter=";")
            escritor.writerow(columnas)
            escritor.writerows(filas)
        if con_parquet:
            import pyarrow as pa
            import pyarrow.parquet as pq

            (salida / "parquet").mkdir(exist_ok=True)
            tipos = {f["name"]: f["type"] for f in conn.execute(f"PRAGMA table_info({tabla})")}
            a_arrow = {"INTEGER": pa.int64(), "REAL": pa.float64()}
            esquema = pa.schema(
                [(c, a_arrow.get(tipos.get(c, "TEXT"), pa.string())) for c in columnas]
            )
            datos = {c: [f[i] for f in filas] for i, c in enumerate(columnas)}
            pq.write_table(
                pa.table(datos, schema=esquema), salida / "parquet" / f"{tabla}.parquet",
                compression="zstd",
            )  # fmt: skip
    conn.close()
    # Un zip por formato, con un fichero por tabla dentro: cómodo de descargar de una Release.
    for formato in ("csv", "parquet") if con_parquet else ("csv",):
        with zipfile.ZipFile(salida / f"{formato}.zip", "w", zipfile.ZIP_STORED) as z:
            for f in sorted((salida / formato).iterdir()):
                z.write(f, f"{formato}/{f.name}")
    return {"foto": foto, "sha256": resumen, "tablas": filas_por_tabla}
