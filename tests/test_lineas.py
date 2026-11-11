from cnmc_logistica.parsers.lineas import limpiar, lineas_de

# Así escribe el portal los subapartados: el primer hijo va DENTRO del <li>
# padre y los siguientes FUERA, todos sin <ul>. Solo el margen los distingue.
ANIDADA = """
<div>CONDICIONES</div>
<div><ul>
  <li>Mermas:
      <li style="margin-left:20px;">Gas&oacute;leos:&nbsp;0,045 %</li>
  </li>
      <li style="margin-left:20px;">Gasolinas:&nbsp;0,06 %</li>
  <li>Programaci&oacute;n:&nbsp;Planes anuales</li>
</ul></div>
"""


def test_limpiar_colapsa_espacios_y_nbsp():
    assert limpiar("  Precio\xa0(01/01/2026):\n\t 2,4106  ") == "Precio (01/01/2026): 2,4106"


def test_lista_anidada_sin_ul():
    lineas = lineas_de(ANIDADA)
    assert [(ln.texto, ln.en_li, ln.sangrada) for ln in lineas] == [
        ("CONDICIONES", False, False),
        ("Mermas:", True, False),
        ("Gasóleos: 0,045 %", True, True),
        ("Gasolinas: 0,06 %", True, True),
        ("Programación: Planes anuales", True, False),
    ]


def test_br_separa_lineas_y_se_conservan_los_enlaces():
    html = (
        '<div>» <span>Servicios</span><br/><a href="/x?directorioHijo=24">Tabla</a><br/>'
        "Precio max. (01/01/2026): 4,12 &euro;/m3</div>"
    )
    lineas = lineas_de(html)
    assert [ln.texto for ln in lineas] == [
        "» Servicios",
        "Tabla",
        "Precio max. (01/01/2026): 4,12 €/m3",
    ]
    assert lineas[1].enlaces == [("Tabla", "/x?directorioHijo=24")]


def test_ignora_scripts_y_comentarios():
    html = "<div>uno<script>var x = 1;</script><!-- oculto -->dos</div>"
    assert [ln.texto for ln in lineas_de(html)] == ["unodos"]
