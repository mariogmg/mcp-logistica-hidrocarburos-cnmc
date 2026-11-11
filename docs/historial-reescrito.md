# Historial reescrito el 11 de noviembre de 2026

Ese día se reescribió el historial publicado de este repositorio con `git filter-repo`.

**Motivo.** Dos cambios, y solo esos dos:

1. Quitar de los mensajes de commit las líneas de coautoría añadidas por un asistente de IA.
2. Unificar el autor de los dos commits automáticos del workflow mensual, que figuraban como
   `github-actions[bot]`, con la identidad del autor del proyecto.

**Qué no cambió.** El contenido de cada commit (su árbol de ficheros) es idéntico, y también
las fechas y el orden. Solo cambian los mensajes, el autor de esos dos commits y, como
consecuencia, todos los identificadores (hashes).

**Para qué sirve esta tabla.** Algunas referencias anteriores usan hashes antiguos; por ejemplo,
el registro de ejecuciones de la foto del 11 de noviembre (`raw-2026-11-11.ejecuciones.jsonl`)
menciona el commit `47e5fbe`. Con esta correspondencia siguen siendo trazables.

La etiqueta `raw-2026-11-11` apuntaba a `520208d` y apunta ahora a su equivalente de la tabla.
Los 24 commits publicados, del más antiguo al más reciente:

| Hash antiguo | Hash nuevo | Commit |
|---|---|---|
| `835b1fb` | `a19a324` | Fase 1: esqueleto del proyecto |
| `e56941e` | `4d51eb7` | Fase 2: descargador educado y almacén de crudo |
| `05ddbe0` | `1a368c7` | Fase 3: descubrimiento y parsers de fichas y detalles |
| `3899940` | `d6d3aad` | Correcciones tras la revisión de la fase 3 |
| `fce3868` | `24c0f37` | Fase 4: base de datos con historial SCD2, triggers y reconstruir |
| `d864542` | `a40034c` | Fases 5 y 5b: origen-destino, tablas de PDF y capacidades |
| `6d2c45e` | `d740156` | Fase 6: servidor MCP de solo lectura |
| `6fdaa15` | `c2bb11c` | Fase 7: CI, actualización mensual, Docker y ejemplo para Claude Desktop |
| `758df43` | `7396737` | Fase 8: README, base de datos e inventario de la primera foto |
| `520208d` | `cbe6156` | Revisión previa a la publicación |
| `16fb29a` | `c24b45a` | CI: no ejecutar en las etiquetas de las Releases de crudo |
| `47e5fbe` | `265337f` | Datos: foto del portal de 2026-11-11 |
| `ac488b9` | `df5e26f` | PDF leídos por completo: parsers específicos, cobertura de cifras y OCR de respaldo |
| `a88045e` | `60da836` | Precios de PDF agrupados, registro de ejecuciones en raw/ y condiciones con cantidad |
| `6e17b3a` | `b7a4c12` | Datos: foto del portal de 2026-11-11 |
| `f72f4aa` | `db5347b` | Fase 1: reconstrucción completa desde las Releases |
| `c6213e7` | `59bfc34` | Fase 2: capa de consultas compartida |
| `1396a4b` | `4cc2635` | Fases 3 y 4: API REST con FastAPI y aplicación única (MCP + API) |
| `5536627` | `ecd4389` | Fase 5: preparación del despliegue |
| `c3161b3` | `360db2b` | Añadir .env.example (plantilla sin secretos) |
| `19ba397` | `82a2228` | CI: límite de tiempo al instalar Tesseract |
| `09faf5a` | `b66027a` | CI: Tesseract opcional y con tope de 4 minutos |
| `e693cb0` | `b263d41` | CI: avisar de forma visible cuando falta Tesseract |
| `2e9b64e` | `6c6a590` | CI: corregir la sintaxis del paso de aviso |
