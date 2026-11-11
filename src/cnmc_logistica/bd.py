"""Base de datos SQLite con historial (SCD tipo 2).

Idea central: una fila describe un hecho tal como se vio entre dos fechas.
- `primera_vez_visto` / `ultima_vez_visto`: fechas de la primera y la última
  foto en la que la fila apareció idéntica.
- Si el dato sigue igual, solo avanza `ultima_vez_visto`.
- Si cambia cualquier valor, la fila antigua se queda como está y se inserta
  una nueva. Nunca se borra ni se corrige nada.

Los triggers lo imponen en la propia base: rechazan todo DELETE y todo UPDATE
que toque una columna distinta de `ultima_vez_visto`. «Reprocesar» (p. ej. tras
cambiar un fichero de categorías) es reconstruir una base nueva desde raw/.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

VERSION_ESQUEMA = 3

# tabla → (columnas clave, columnas de valor, DDL de las columnas)
# La clave identifica «el mismo hecho» entre fotos; si cambia un valor con la
# misma clave es un cambio, si la clave es nueva es un alta.
TABLAS: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {
    "operadores": (("codigo",), (), "codigo TEXT NOT NULL"),
    "operadores_nombres": (
        ("codigo",),
        ("nombre",),
        "codigo TEXT NOT NULL, nombre TEXT NOT NULL",
    ),
    "instalaciones": (
        ("id_tanque",),
        ("tipo", "comunidad", "comunidad_nombre", "titulo"),
        """id_tanque INTEGER NOT NULL,
        tipo TEXT NOT NULL CHECK (tipo IN ('almacenamiento', 'aeroportuaria', 'desconocido')),
        comunidad TEXT NOT NULL, comunidad_nombre TEXT, titulo TEXT NOT NULL""",
    ),
    "detalles": (
        ("id_tanque_instalacion",),
        (
            "id_tanque", "operador", "operador_buscador", "discrepancia_operador", "titular",
            "titulo", "subtitulo", "conexion", "productos_texto", "producto_buscador",
            "contacto", "tiene_origen_destino",
        ),
        """id_tanque_instalacion INTEGER NOT NULL, id_tanque INTEGER NOT NULL,
        operador TEXT,            -- código cuyo nombre coincide con el titular de la página
        operador_buscador TEXT,   -- código bajo el que lo lista el buscador
        discrepancia_operador INTEGER NOT NULL CHECK (discrepancia_operador IN (0, 1)),
        titular TEXT NOT NULL, titulo TEXT NOT NULL, subtitulo TEXT,
        conexion TEXT NOT NULL
            CHECK (conexion IN ('red', 'por_oleoducto', 'no_conectada', 'sin_dato')),
        productos_texto TEXT NOT NULL, producto_buscador TEXT, contacto TEXT,
        tiene_origen_destino INTEGER NOT NULL CHECK (tiene_origen_destino IN (0, 1))""",
    ),
    "detalle_productos": (
        ("id_tanque_instalacion", "producto"),
        (),
        "id_tanque_instalacion INTEGER NOT NULL, producto TEXT NOT NULL",
    ),
    "precios": (
        ("id_tanque_instalacion", "seccion", "etiqueta", "subetiqueta", "ocurrencia", "tipo_valor"),
        (
            "categoria_servicio", "incluye_transporte", "incluye_almacenamiento",
            "incluye_expedicion", "incluye_puesta_a_bordo", "tipo_precio", "valor", "unidad",
            "fecha_precio", "anio_tabla", "observacion", "texto", "documentos", "texto_original",
        ),
        """id_tanque_instalacion INTEGER NOT NULL,
        seccion TEXT NOT NULL CHECK (seccion IN ('principal', 'otros_servicios')),
        etiqueta TEXT NOT NULL, subetiqueta TEXT,
        ocurrencia INTEGER NOT NULL,      -- 1 salvo que la página repita la misma etiqueta
        tipo_valor TEXT CHECK (tipo_valor IN ('unico', 'max', 'min')),  -- NULL si no es numérico
        categoria_servicio TEXT NOT NULL,
        -- Indicadores derivados de «Incluye:»; solo en servicios principales (NULL en el resto).
        incluye_transporte INTEGER, incluye_almacenamiento INTEGER,
        incluye_expedicion INTEGER, incluye_puesta_a_bordo INTEGER,
        tipo_precio TEXT NOT NULL CHECK (tipo_precio IN ('numerico', 'regla', 'texto', 'pdf')),
        valor REAL,                       -- NULL si tipo_precio no es 'numerico'
        unidad TEXT,
        fecha_precio TEXT,                -- vigencia que publica la CNMC; NULL si no hay
        anio_tabla INTEGER,               -- cuando la fuente solo da el año
        observacion TEXT, texto TEXT,
        documentos TEXT,                  -- claves de PDF separadas por comas
        texto_original TEXT NOT NULL,
        CHECK ((tipo_precio = 'numerico') = (valor IS NOT NULL))""",
    ),
    "condiciones": (
        ("id_tanque_instalacion", "etiqueta", "subetiqueta", "ocurrencia", "tipo_valor"),
        ("valor", "unidad", "texto", "texto_original"),
        """id_tanque_instalacion INTEGER NOT NULL, etiqueta TEXT NOT NULL, subetiqueta TEXT,
        ocurrencia INTEGER NOT NULL,
        tipo_valor TEXT CHECK (tipo_valor IN ('unico', 'max', 'min')),
        valor REAL, unidad TEXT, texto TEXT, texto_original TEXT NOT NULL""",
    ),
    "servicios_incluidos": (
        ("id_tanque_instalacion", "servicio", "texto", "ocurrencia"),
        (),
        """id_tanque_instalacion INTEGER NOT NULL, servicio TEXT NOT NULL, texto TEXT NOT NULL,
        ocurrencia INTEGER NOT NULL""",
    ),
    "precios_origen_destino": (
        ("id_tanque_instalacion", "modalidad", "origen"),
        ("destino", "origen_normalizado", "estado", "precio", "unidad", "anio_tabla",
         "texto_original"),
        """id_tanque_instalacion INTEGER NOT NULL,   -- el destino se identifica por id
        destino TEXT,                     -- nombre que muestra la página; solo informativo
        modalidad TEXT NOT NULL, origen TEXT NOT NULL, origen_normalizado TEXT NOT NULL,
        estado TEXT NOT NULL CHECK (estado IN ('ofrecida', 'no_ofrecida')),
        precio REAL,                      -- NULL si no_ofrecida: 0,00 no es un precio
        unidad TEXT, anio_tabla INTEGER, texto_original TEXT NOT NULL,
        CHECK ((estado = 'ofrecida') = (precio IS NOT NULL))""",
    ),
    "notas_origen_destino": (
        ("id_tanque_instalacion", "orden"),
        ("destino", "texto"),
        """id_tanque_instalacion INTEGER NOT NULL, orden INTEGER NOT NULL,
        destino TEXT, texto TEXT NOT NULL""",
    ),
    "capacidades": (
        ("ambito", "compania", "instalacion", "producto"),
        ("id_tanque", "capacidad_miles_m3", "anio", "documento"),
        """ambito TEXT NOT NULL,              -- 'almacenamiento' | 'aeroportuaria'
        compania TEXT NOT NULL, instalacion TEXT NOT NULL,
        producto TEXT NOT NULL,           -- 'TOTAL' para la suma de la instalación
        id_tanque INTEGER,                -- NULL si el nombre no está en las equivalencias
        capacidad_miles_m3 REAL,          -- NULL: el PDF lista el producto sin desglosar cifra
        anio INTEGER, documento TEXT NOT NULL""",
    ),
    "documentos": (
        ("clave",),
        ("nombre_archivo", "sha256", "bytes", "paginas", "paginas_sin_texto", "tablas", "anio",
         "titulo"),
        """clave TEXT NOT NULL, nombre_archivo TEXT, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
        paginas INTEGER, paginas_sin_texto INTEGER, tablas INTEGER, anio INTEGER, titulo TEXT""",
    ),
    "filas_documento": (
        ("documento", "pagina", "tabla", "fila"),
        ("celdas",),
        """documento TEXT NOT NULL, pagina INTEGER NOT NULL, tabla INTEGER NOT NULL,
        fila INTEGER NOT NULL, celdas TEXT NOT NULL  -- lista JSON de celdas""",
    ),
    # --- contenido estructurado de los PDF ---
    "enlaces_documento": (
        ("id_tanque_instalacion", "seccion", "etiqueta", "subetiqueta", "ocurrencia", "documento"),
        (),
        """id_tanque_instalacion INTEGER NOT NULL, seccion TEXT NOT NULL, etiqueta TEXT NOT NULL,
        subetiqueta TEXT, ocurrencia INTEGER NOT NULL,
        documento TEXT NOT NULL            -- PDF al que remite esa línea de precios""",
    ),
    "precios_pdf": (
        ("documento", "seccion", "concepto", "subconcepto", "ocurrencia"),
        ("categoria_servicio", "valor", "unidad", "anio_tabla", "vigencia", "condicion", "metodo",
         "pagina", "x0", "top", "x1", "bottom", "texto_original"),
        """documento TEXT NOT NULL, seccion TEXT, concepto TEXT NOT NULL, subconcepto TEXT,
        ocurrencia INTEGER NOT NULL, categoria_servicio TEXT NOT NULL,
        valor REAL,                       -- NULL si la cifra es ilegible (no se inventa)
        unidad TEXT, anio_tabla INTEGER, vigencia TEXT, condicion TEXT,
        metodo TEXT NOT NULL CHECK (metodo IN ('texto', 'ocr')),
        -- Dónde está la cifra en el PDF (puntos, origen arriba a la izquierda): para auditar.
        pagina INTEGER NOT NULL, x0 REAL NOT NULL, top REAL NOT NULL, x1 REAL NOT NULL,
        bottom REAL NOT NULL, texto_original TEXT NOT NULL""",
    ),
    "matrices_pdf": (
        ("documento", "tipo", "modalidad", "destino", "origen"),
        ("id_tanque_instalacion_destino", "id_tanque_destino", "id_tanque_origen", "estado",
         "precio", "unidad", "anio_tabla", "marca", "metodo", "pagina", "x0", "top", "x1",
         "bottom", "texto_original"),
        """documento TEXT NOT NULL,
        tipo TEXT NOT NULL
            CHECK (tipo IN ('origen_destino', 'reconsignacion', 'consignacion_ester')),
        modalidad TEXT, destino TEXT NOT NULL, origen TEXT NOT NULL,
        id_tanque_instalacion_destino INTEGER,  -- detalle cuyos precios HTML coinciden con la fila
        id_tanque_destino INTEGER, id_tanque_origen INTEGER,
        estado TEXT NOT NULL CHECK (estado IN ('ofrecida', 'no_aplica')),
        precio REAL, unidad TEXT, anio_tabla INTEGER,
        marca TEXT,                       -- llamada a nota de la columna, p. ej. «(2)»
        metodo TEXT NOT NULL CHECK (metodo IN ('texto', 'ocr')),
        pagina INTEGER NOT NULL, x0 REAL NOT NULL, top REAL NOT NULL, x1 REAL NOT NULL,
        bottom REAL NOT NULL, texto_original TEXT NOT NULL""",
    ),
    "bonificaciones": (
        ("documento", "bloque", "tramo", "condicion"),
        ("valor", "unidad", "anio", "metodo", "pagina", "x0", "top", "x1", "bottom",
         "texto_original"),
        """documento TEXT NOT NULL,
        bloque TEXT NOT NULL CHECK (bloque IN ('bonificacion', 'compensacion')),
        tramo TEXT NOT NULL, condicion TEXT NOT NULL, valor REAL, unidad TEXT NOT NULL,
        anio INTEGER, metodo TEXT NOT NULL CHECK (metodo IN ('texto', 'ocr')),
        pagina INTEGER NOT NULL, x0 REAL NOT NULL, top REAL NOT NULL, x1 REAL NOT NULL,
        bottom REAL NOT NULL, texto_original TEXT NOT NULL""",
    ),
    "condiciones_pdf": (
        ("documento", "texto", "unidad", "ocurrencia"),
        ("valor", "metodo", "pagina", "x0", "top", "x1", "bottom"),
        """documento TEXT NOT NULL,
        texto TEXT NOT NULL,              -- la nota completa de la que sale la cantidad
        unidad TEXT NOT NULL, ocurrencia INTEGER NOT NULL, valor REAL NOT NULL,
        metodo TEXT NOT NULL CHECK (metodo IN ('texto', 'ocr')),
        pagina INTEGER NOT NULL, x0 REAL NOT NULL, top REAL NOT NULL, x1 REAL NOT NULL,
        bottom REAL NOT NULL""",
    ),
    "notas_pdf": (
        ("documento", "pagina", "orden"),
        ("texto", "metodo"),
        """documento TEXT NOT NULL, pagina INTEGER NOT NULL, orden INTEGER NOT NULL,
        texto TEXT NOT NULL, metodo TEXT NOT NULL""",
    ),
    "cobertura_pdf": (
        ("documento",),
        ("parser", "cifras", "en_valores", "en_etiquetas", "en_notas", "en_cabecera",
         "descartadas", "ilegibles", "sin_cubrir"),
        """documento TEXT NOT NULL, parser TEXT NOT NULL,
        cifras INTEGER NOT NULL,          -- palabras con dígitos en el PDF
        en_valores INTEGER NOT NULL, en_etiquetas INTEGER NOT NULL, en_notas INTEGER NOT NULL,
        en_cabecera INTEGER NOT NULL, descartadas INTEGER NOT NULL, ilegibles INTEGER NOT NULL,
        sin_cubrir INTEGER NOT NULL""",
    ),
}  # fmt: skip

_REGISTRO = """
CREATE TABLE IF NOT EXISTS ejecuciones (
    id INTEGER PRIMARY KEY,
    fecha TEXT NOT NULL,              -- fecha de la foto de raw/ cargada
    registrada_en TEXT NOT NULL,
    version TEXT NOT NULL,
    tipo TEXT NOT NULL DEFAULT 'actualizacion'
        CHECK (tipo IN ('actualizacion', 'reconstruccion')),
    -- 'carga': registrada al ejecutarse; 'registro': recuperada de raw/FECHA/ejecuciones.jsonl
    origen TEXT NOT NULL DEFAULT 'carga' CHECK (origen IN ('carga', 'registro')),
    recursos INTEGER NOT NULL,        -- ficheros de la foto
    altas INTEGER NOT NULL, cambios INTEGER NOT NULL, desapariciones INTEGER NOT NULL,
    errores INTEGER NOT NULL, avisos INTEGER NOT NULL,
    por_tabla TEXT NOT NULL           -- JSON: {tabla: [altas, cambios, desapariciones]}
);
CREATE TABLE IF NOT EXISTS incidencias (
    id INTEGER PRIMARY KEY,
    ejecucion_id INTEGER NOT NULL REFERENCES ejecuciones(id),
    nivel TEXT NOT NULL CHECK (nivel IN ('error', 'aviso')),
    tipo TEXT NOT NULL,
    recurso TEXT NOT NULL,            -- p. ej. detalle/17
    operador TEXT,
    texto TEXT NOT NULL               -- p. ej. el texto del bloque desconocido
);
CREATE TABLE IF NOT EXISTS meta (clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
"""

# Vistas del estado vigente: lo visto en la última foto cargada.
_VISTAS = (
    "instalaciones", "detalles", "detalle_productos", "precios", "condiciones",
    "servicios_incluidos", "precios_origen_destino", "notas_origen_destino", "capacidades",
    "documentos", "filas_documento", "operadores_nombres", "enlaces_documento", "precios_pdf",
    "matrices_pdf", "bonificaciones", "notas_pdf", "cobertura_pdf", "condiciones_pdf",
)  # fmt: skip


def conectar(ruta: Path) -> sqlite3.Connection:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ruta)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def crear_esquema(conn: sqlite3.Connection) -> None:
    """Crea tablas, índices, triggers y vistas. Se puede llamar varias veces."""
    for tabla, (clave, _, ddl) in TABLAS.items():
        conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {tabla} (
                id INTEGER PRIMARY KEY,
                primera_vez_visto TEXT NOT NULL,
                ultima_vez_visto TEXT NOT NULL,
                {ddl}
                , CHECK (ultima_vez_visto >= primera_vez_visto)
            )"""
        )
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{tabla}_clave ON {tabla} ({', '.join(clave)})")
        conn.execute(f"CREATE INDEX IF NOT EXISTS ix_{tabla}_visto ON {tabla} (ultima_vez_visto)")
        fijas = [c for c in _columnas(conn, tabla) if c != "ultima_vez_visto"]
        cambia = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in fijas)
        conn.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS tr_{tabla}_sin_delete BEFORE DELETE ON {tabla}
            BEGIN SELECT RAISE(ABORT, 'historial inmutable: no se permite DELETE en {tabla}'); END;
            CREATE TRIGGER IF NOT EXISTS tr_{tabla}_solo_ultima_vez BEFORE UPDATE ON {tabla}
            WHEN {cambia} OR NEW.ultima_vez_visto < OLD.ultima_vez_visto
            BEGIN SELECT RAISE(ABORT,
                'historial inmutable: en {tabla} solo puede avanzar ultima_vez_visto'); END;
            """
        )
    conn.executescript(_REGISTRO)
    for tabla in ("ejecuciones", "incidencias"):
        conn.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS tr_{tabla}_sin_delete BEFORE DELETE ON {tabla}
            BEGIN SELECT RAISE(ABORT, 'registro inmutable: no se permite DELETE en {tabla}'); END;
            CREATE TRIGGER IF NOT EXISTS tr_{tabla}_sin_update BEFORE UPDATE ON {tabla}
            BEGIN SELECT RAISE(ABORT, 'registro inmutable: no se permite UPDATE en {tabla}'); END;
            """
        )
    for tabla in _VISTAS:
        conn.execute(
            f"""CREATE VIEW IF NOT EXISTS {tabla}_vigentes AS
                SELECT * FROM {tabla}
                WHERE ultima_vez_visto = (SELECT MAX(fecha) FROM ejecuciones)"""
        )
    conn.execute(
        """CREATE VIEW IF NOT EXISTS operadores_vigentes AS
           SELECT o.codigo, n.nombre, o.primera_vez_visto, o.ultima_vez_visto
           FROM operadores o
           LEFT JOIN operadores_nombres_vigentes n ON n.codigo = o.codigo
           WHERE o.ultima_vez_visto = (SELECT MAX(fecha) FROM ejecuciones)"""
    )
    conn.executescript(
        """
        -- Condiciones con cantidad de los PDF, vistas desde cada detalle que los enlaza.
        CREATE VIEW IF NOT EXISTS condiciones_pdf_por_detalle AS
            SELECT DISTINCT e.id_tanque_instalacion, c.documento, c.texto, c.valor, c.unidad,
                   c.pagina
            FROM enlaces_documento_vigentes e
            JOIN condiciones_pdf_vigentes c ON c.documento = e.documento;
        CREATE VIEW IF NOT EXISTS reconsignaciones AS
            SELECT * FROM matrices_pdf_vigentes WHERE tipo = 'reconsignacion';
        -- Precios de PDF vistos desde cada detalle que remite al documento.
        CREATE VIEW IF NOT EXISTS precios_pdf_por_detalle AS
            SELECT e.id_tanque_instalacion, e.seccion AS seccion_detalle, e.etiqueta,
                   e.subetiqueta, p.*
            FROM enlaces_documento_vigentes e
            JOIN precios_pdf_vigentes p ON p.documento = e.documento;
        -- Líneas «tipo_precio = pdf» cuyo documento no tiene ningún dato estructurado.
        CREATE VIEW IF NOT EXISTS lineas_pdf_sin_resolver AS
            SELECT e.* FROM enlaces_documento_vigentes e
            WHERE e.documento NOT IN (SELECT documento FROM precios_pdf_vigentes)
              AND e.documento NOT IN (SELECT documento FROM matrices_pdf_vigentes)
              AND e.documento NOT IN (SELECT documento FROM bonificaciones_vigentes);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta (clave, valor) VALUES ('version_esquema', ?)",
        (str(VERSION_ESQUEMA),),
    )


def _columnas(conn: sqlite3.Connection, tabla: str) -> list[str]:
    return [f["name"] for f in conn.execute(f"PRAGMA table_info({tabla})")]


@dataclass
class Cambios:
    altas: int = 0
    cambios: int = 0
    desapariciones: int = 0
    insertadas: int = 0  # filas nuevas (altas + cambios)
    ejemplos: list[str] = field(default_factory=list)


def aplicar_scd2(
    conn: sqlite3.Connection,
    tabla: str,
    filas: list[dict],
    fecha: str,
    fecha_previa: str | None,
) -> Cambios:
    """Vuelca en `tabla` lo visto en la foto `fecha`.

    Solo se comparan las filas «vivas»: las vistas en la foto anterior (o ya en
    esta, si se repite la carga). Un valor que reaparece tras una ausencia
    genera una fila nueva: así el hueco queda reflejado en el historial.
    """
    clave, valores, _ = TABLAS[tabla]
    columnas = clave + valores
    vivas = conn.execute(
        f"SELECT id, ultima_vez_visto, {', '.join(columnas)} FROM {tabla} "
        "WHERE ultima_vez_visto IN (?, ?)",
        (fecha, fecha_previa or fecha),
    ).fetchall()
    por_huella = {tuple(f[c] for c in columnas): (f["id"], f["ultima_vez_visto"]) for f in vivas}
    claves_previas = {tuple(f[c] for c in clave) for f in vivas}

    resultado = Cambios()
    claves_nuevas: set[tuple] = set()
    vistas: set[tuple] = set()
    for fila in filas:
        huella = tuple(fila.get(c) for c in columnas)
        if huella in vistas:
            continue  # fila repetida exacta en la misma foto
        vistas.add(huella)
        k = tuple(fila.get(c) for c in clave)
        claves_nuevas.add(k)
        if huella in por_huella:
            id_fila, ultima = por_huella[huella]
            if ultima != fecha:
                conn.execute(
                    f"UPDATE {tabla} SET ultima_vez_visto = ? WHERE id = ?", (fecha, id_fila)
                )
            continue
        marcas = ", ".join("?" * (len(columnas) + 2))
        conn.execute(
            f"INSERT INTO {tabla} ({', '.join(columnas)}, primera_vez_visto, ultima_vez_visto) "
            f"VALUES ({marcas})",
            (*huella, fecha, fecha),
        )
        resultado.insertadas += 1
        if k in claves_previas:
            resultado.cambios += 1
            if len(resultado.ejemplos) < 5:
                resultado.ejemplos.append(f"cambio {tabla} {k}")
        else:
            resultado.altas += 1
    desaparecidas = claves_previas - claves_nuevas
    resultado.desapariciones = len(desaparecidas)
    for k in sorted(desaparecidas, key=str)[: max(0, 5 - len(resultado.ejemplos))]:
        resultado.ejemplos.append(f"desaparece {tabla} {k}")
    return resultado
