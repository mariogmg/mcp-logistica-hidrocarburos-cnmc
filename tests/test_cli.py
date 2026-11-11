import pytest

from cnmc_logistica import __version__
from cnmc_logistica.cli import ORDENES, SALIDA_OK, crear_parser, main
from cnmc_logistica.config import USER_AGENT


def test_ordenes_previstas():
    assert set(ORDENES) == {"descubrir", "actualizar", "reconstruir", "servir", "info"}


def test_user_agent_sin_datos_personales():
    assert USER_AGENT.startswith(f"mcp-logistica-hidrocarburos-cnmc/{__version__} (+https://")
    assert "@" not in USER_AGENT


def test_info(tmp_path, capsys):
    assert main(["--raiz", str(tmp_path), "info"]) == SALIDA_OK
    assert __version__ in capsys.readouterr().out


def test_orden_obligatoria():
    with pytest.raises(SystemExit):
        crear_parser().parse_args([])
