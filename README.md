# mcp-logistica-hidrocarburos-cnmc

[![CI](https://github.com/mariogmg/mcp-logistica-hidrocarburos-cnmc/actions/workflows/ci.yml/badge.svg)](https://github.com/mariogmg/mcp-logistica-hidrocarburos-cnmc/actions/workflows/ci.yml)

Histórico de la **logística de productos petrolíferos** que publica la CNMC, consultable en
lenguaje natural a través de un servidor [MCP](https://modelcontextprotocol.io) de solo lectura.

> **Aviso legal.** Proyecto no oficial, no afiliado a la CNMC ni a ningún operador. Los datos
> son públicos y los publica la CNMC en
> [hidrocarburos.cnmc.es](https://hidrocarburos.cnmc.es/hidrocarburos/instalaciones_nacionales.htm).
> Este repositorio los copia y los estructura tal cual, sin garantía de exactitud ni de
> actualización. Para cualquier decisión, consulte la fuente original.

## Qué es

La CNMC publica las condiciones de acceso a las instalaciones de almacenamiento y transporte de
productos petrolíferos de todos los operadores: precios, mermas, servicios incluidos, rutas
origen-destino y capacidades. El portal solo muestra el estado actual y cada operador redacta sus
servicios a su manera.

Este proyecto:

1. **Descarga** una vez al mes todo el portal (unas 600 páginas y PDF), con educación: una
   petición por segundo como máximo, `robots.txt` en cada ejecución y un User-Agent identificable.
2. **Guarda el crudo** tal cual (`raw/AAAA-MM-DD/`, con sha256 y manifiesto) y **nunca borra
   nada**: cada cambio de un precio o de una condición se conserva como una versión nueva.
3. **Normaliza** lo justo para poder comparar: categoría de servicio, tipo de precio, unidad,
   qué incluye cada servicio.
4. **Lo expone** mediante un servidor MCP que solo lee una base SQLite. El servidor nunca hace
   scraping.

Con la foto del 11/11/2026: 115 instalaciones (71 de almacenamiento y 44 aeroportuarias),
258 detalles (un producto en una instalación), 31 operadores con instalaciones, 1.579 precios,
2.163 rutas origen-destino y 27 PDF.

## Cómo funciona

```mermaid
flowchart LR
    portal[("hidrocarburos.cnmc.es")]
    subgraph ETL["ETL (mensual, GitHub Actions)"]
        descubrir["descubrir<br/>mapas + buscador"]
        actualizar["actualizar<br/>fichas, detalles,<br/>origen-destino, PDF"]
        raw[("raw/AAAA-MM-DD/<br/>bytes + sha256 + manifiesto")]
        parsers["parsers<br/>(guiados por etiquetas de texto)"]
    end
    bd[("SQLite con historial<br/>data/cnmc_logistica.db")]
    mcp["servidor MCP<br/>solo lectura"]
    cliente["Claude u otro<br/>cliente MCP"]

    portal --> descubrir --> actualizar --> raw --> parsers --> bd
    raw -. "reconstruir (sin red)" .-> bd
    bd --> mcp --> cliente
    config[/"config/*.yaml<br/>correspondencias revisables"/] --> parsers
```

- **ETL y servidor están separados.** El servidor no importa ni una línea del descargador o de
  los parsers (hay un test que lo comprueba).
- **El crudo es la fuente de verdad.** La base se puede regenerar entera desde `raw/` con
  `reconstruir`; por eso un fallo de interpretación nunca pierde información.
- **Nada aborta por contenido inesperado.** Un bloque o un PDF que no se entiende se anota en la
  tabla `incidencias` (recurso, operador y texto), se conserva su crudo y se sigue. La orden
  termina con código distinto de cero si hubo errores, para que el workflow se marque en rojo.

## Instalación

Requiere [uv](https://docs.astral.sh/uv/) (instala Python 3.12 por su cuenta).

```bash
git clone https://github.com/mariogmg/mcp-logistica-hidrocarburos-cnmc
cd mcp-logistica-hidrocarburos-cnmc
uv sync
uv run cnmc-logistica info
```

El repositorio incluye la base de datos ya cargada (`data/cnmc_logistica.db`), así que el servidor
funciona sin descargar nada.

## Uso

### Consultar los datos desde Claude Desktop

Añade a la configuración de Claude Desktop el bloque de
[ejemplos/claude_desktop_config.json](ejemplos/claude_desktop_config.json) (cambiando la ruta) y
reinicia. Más detalles, incluido el transporte HTTP, en [ejemplos/LEEME.md](ejemplos/LEEME.md).

### Órdenes de la CLI

| Orden | Qué hace | ¿Usa la red? |
|---|---|---|
| `descubrir` | Mapas + buscador → `config/inventario.json`, con control cruzado | Sí |
| `actualizar` | Descarga todo el inventario a `raw/` y carga la foto en la base | Sí |
| `reconstruir` | Crea una base nueva a partir de todas las fotos de `raw/` | No |
| `servir` | Arranca el servidor MCP (`--transporte stdio` o `streamable-http`) | No |
| `info` | Versión, fotos disponibles y filas de cada tabla | No |

```bash
uv run cnmc-logistica descubrir      # ~1 min si ya hay foto del día; ~4 min desde cero
uv run cnmc-logistica actualizar     # ~10 min desde cero (1 petición por segundo)
uv run cnmc-logistica reconstruir --salida /tmp/nueva.db
uv run cnmc-logistica servir --transporte streamable-http --puerto 8000
```

Variables de entorno del servidor HTTP: `CNMC_MCP_TOKEN` (si se define, se exige como token
Bearer) y `CNMC_MCP_HOSTS_PERMITIDOS` (cabeceras `Host` admitidas, separadas por comas; por
defecto solo local). `CNMC_BD` cambia la ruta de la base.

### Docker

```bash
docker build -t cnmc-logistica .
docker run --rm -p 8000:8000 cnmc-logistica      # MCP en http://localhost:8000/mcp
```

## Herramientas MCP

| Herramienta | Para qué |
|---|---|
| `estado` | Última foto, volumen de datos, incidencias y esquema de tablas |
| `listar_operadores` | Operadores con su nº de instalaciones por tipo y nombres anteriores |
| `listar_instalaciones` | Filtros por operador, tipo, comunidad y producto |
| `ver_instalacion` | Ficha completa: detalles, precios, condiciones y capacidad |
| `buscar_precios` | Precios vigentes por operador, categoría, producto, comunidad o texto |
| `comparar_servicio` | Compara una categoría entre instalaciones y operadores, solo entre comparables |
| `historial_precio` | Todas las versiones de los precios de un detalle |
| `precios_origen_destino` | Coste de llevar producto de un origen a una instalación |
| `capacidades` | Capacidad (miles de m³) por compañía, instalación y producto |
| `consulta_sql` | Un `SELECT` libre, con cuatro capas de protección de solo lectura |

### Ejemplos de preguntas

- ¿Cuántos operadores hay y cuántas instalaciones tiene cada uno?
- ¿Qué cuota de instalaciones tiene Exolum por tipo?
- Compara el precio de almacenamiento de queroseno en aeropuertos, separando «almacenamiento» de
  «servicios aeroportuarios».
- ¿Qué operadores ofrecen almacenamiento en Canarias y a qué precio?
- ¿Qué mermas aplican los distintos operadores al gasóleo?
- ¿Cuánto cuesta llevar producto desde Huelva hasta Cartagena?
- ¿Ha cambiado el precio de las transferencias de Exolum en Puxeiros?

## Modelo de datos

Dos niveles de identificador, los mismos que usa el portal: **instalación** (`id_tanque`) y
**detalle** (`id_tanque_instalacion`, un producto dentro de una instalación). Los precios cuelgan
del detalle, y cada detalle tiene su propio titular.

| Tabla | Contenido |
|---|---|
| `operadores`, `operadores_nombres` | Código estable del buscador e historial de sus nombres |
| `instalaciones` | Tipo (almacenamiento / aeroportuaria) y comunidad |
| `detalles`, `detalle_productos` | Titular, conexión a oleoducto y productos normalizados |
| `precios` | Un valor por fila, con `categoria_servicio`, `tipo_precio`, `tipo_valor` y unidad |
| `condiciones` | Mermas y demás condiciones (valor numérico si lo hay, y texto) |
| `servicios_incluidos` | Lista «Incluye:» de cada servicio principal |
| `precios_origen_destino`, `notas_origen_destino` | Rutas (`ofrecida` / `no_ofrecida`) y notas al pie |
| `capacidades` | PDF global de capacidades, enlazado a `id_tanque` |
| `documentos`, `filas_documento` | Metadatos y tablas genéricas de cada PDF |
| `ejecuciones`, `incidencias` | Registro de cada carga: altas, cambios, desapariciones y avisos |

### Historial (SCD tipo 2)

Cada fila lleva `primera_vez_visto` y `ultima_vez_visto` (fechas de foto). Si el dato sigue igual
solo avanza `ultima_vez_visto`; si cambia, la fila antigua se queda y se inserta otra. Unos
*triggers* rechazan cualquier `DELETE` y cualquier `UPDATE` que no sea avanzar `ultima_vez_visto`.
Las vistas `<tabla>_vigentes` muestran lo visto en la última foto. `fecha_precio` es la vigencia
que publica la CNMC (puede faltar) y `anio_tabla` el año cuando la fuente solo da el año.

### Cómo se evita comparar servicios distintos

- `tipo_precio`: `numerico`, `regla` («según contrato», «coste real + 3 %»…), `texto` o `pdf`
  (remite a un documento). Si no es numérico, `valor` es nulo y se guarda el texto original.
- `tipo_valor`: `unico`, `max` o `min`, cada uno con su fecha.
- `categoria_servicio`: sale de [config/categorias_servicio.yaml](config/categorias_servicio.yaml)
  (etiqueta + tipo de instalación → categoría). No hay expresiones regulares que adivinen: lo que
  no está en el fichero queda como `sin_categoria` y genera un aviso.
- Indicadores `incluye_transporte`, `incluye_almacenamiento`, `incluye_expedicion` e
  `incluye_puesta_a_bordo`, derivados de la lista «Incluye:» mediante
  [config/indicadores_incluye.yaml](config/indicadores_incluye.yaml).
- `comparar_servicio` solo agrupa precios con la misma categoría, los mismos indicadores y la
  misma unidad, y avisa cuando hay más de un grupo.

## Decisiones de diseño

| Decisión | Motivo |
|---|---|
| Crudo inmutable + base reconstruible | Un fallo de un parser se arregla sin perder ni volver a descargar nada |
| Parsers guiados por etiquetas de texto («PRODUCTOS:», «Incluye:»…) | El texto visible cambia menos que las clases CSS |
| Correspondencias en YAML revisable | Las decisiones de negocio se leen y se discuten sin leer código |
| Operador = código del buscador | Los nombres cambian (Cepsa → Moeve); el código no |
| Control cruzado fichas ↔ buscador y HTML ↔ PDF | Detecta descargas incompletas; ya cazó un fallo real de paginación |
| Peticiones sin cookies | El buscador guarda la página en la sesión y contaminaba la búsqueda siguiente |
| Decodificación cabecera → UTF-8 → ISO-8859-1 | El portal declara ISO-8859-1 en el `<meta>` pero sirve UTF-8 |
| `0,00` = ruta no ofrecida | En origen-destino un cero no es un precio; se guarda `NULL` + estado |
| SQLite en el repositorio | Clonar basta para consultar; el histórico viaja con el código |

## Actualización automática y reconstrucción

- **CI** en cada push: `ruff` y `pytest` (los tests usan páginas reales guardadas como fixtures y
  no tocan la red).
- **Actualización mensual** (día 2): ejecuta `descubrir` y `actualizar`, hace commit de la base
  y del inventario, y sube `raw/AAAA-MM-DD` como `.tar.gz` a una
  [Release](https://github.com/mariogmg/mcp-logistica-hidrocarburos-cnmc/releases). Si hubo
  errores, el trabajo queda en rojo después de conservar todo lo descargado.

Para reconstruir la base desde cero se descargan los crudos de las Releases:

```bash
gh release list --json tagName --jq '.[].tagName | select(startswith("raw-"))' |
  while read -r etiqueta; do gh release download "$etiqueta" --pattern '*.tar.gz' --dir /tmp/raw; done
mkdir -p raw && for f in /tmp/raw/*.tar.gz; do tar -xzf "$f" -C raw; done
uv run cnmc-logistica reconstruir --reemplazar
```

## Limitaciones conocidas

- El histórico empieza el 11/11/2026; no hay datos anteriores.
- La provincia no se publica de forma estructurada y no se guarda en esta versión.
- El portal no fecha las rutas origen-destino: solo se conoce el año de la tabla (del PDF).
- Los productos se separan por comas y por « y », sin fusionar variantes («Gasóleo marino» y
  «Gasóleos marinos» son dos valores).
- Las tablas de los PDF se guardan de forma genérica (`filas_documento`); solo el PDF de
  capacidades tiene un parser específico.
- El OCR (Tesseract, `uv sync --extra ocr`) solo se usa en páginas sin capa de texto; hasta hoy
  ningún PDF lo ha necesitado.

## Desarrollo

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

## Licencia

Código bajo licencia [MIT](LICENSE). Los datos son de la CNMC y de los operadores que los
declaran.
