# Ejemplos de configuración

## Claude Desktop (transporte stdio)

Copiar el bloque de `claude_desktop_config.json` en el fichero de configuración de
Claude Desktop y sustituir `/RUTA/A/...` por la ruta del repositorio clonado:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Reiniciar la aplicación. El cliente MCP arranca el servidor bajo demanda. El servidor solo lee
`data/cnmc_logistica.db` y no hace peticiones al portal de la CNMC.

## Servidor HTTP (MCP y API REST)

```bash
# Local, sin token:
uv run cnmc-logistica servir --transporte http --puerto 8000

# Expuesto en red: Host admitidos y token Bearer para /mcp.
export CNMC_MCP_HOSTS_PERMITIDOS="mcp.midominio.example"
export CNMC_MCP_TOKEN="$(openssl rand -hex 32)"
uv run cnmc-logistica servir --transporte http --host 0.0.0.0 --puerto 8000
```

El MCP se sirve en `http://HOST:PUERTO/mcp`, la API REST en `/api/v1` y su documentación en
`/docs`. El token y la lista de Host solo afectan a `/mcp`. El token se pasa por variable de
entorno y no se guarda en el repositorio.
