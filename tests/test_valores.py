import pytest

from cnmc_logistica.parsers.valores import (
    extraer_porcentajes,
    extraer_precios,
    fecha_iso,
    numero,
    partir_unidad,
)


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [("2,4106", 2.4106), ("0,00", 0.0), ("1.180,565", 1180.565), ("96", 96.0)],
)
def test_numero_con_coma_decimal(texto, esperado):
    assert numero(texto) == pytest.approx(esperado)


def test_fecha_iso():
    assert fecha_iso("01/04/2026") == "2026-04-01"
    assert fecha_iso("1/4/2026") == "2026-04-01"


def test_precio_unico_con_fecha():
    valores, resto, desconocidas = extraer_precios("Precio (01/01/2026): 2,4106 €/m3")
    assert [(v.tipo_valor, v.valor, v.fecha, v.unidad) for v in valores] == [
        ("unico", 2.4106, "2026-01-01", "€/m3")
    ]
    assert resto == "" and desconocidas == []


def test_precio_con_observacion_entre_parentesis():
    texto = "Precio (01/01/2026): 0,2029 €/m3 y día (exceso diario sobre el nivel máximo)"
    (v,), _, _ = extraer_precios(texto)
    assert (v.unidad, v.observacion) == ("€/m3 y día", "exceso diario sobre el nivel máximo")


def test_maximo_y_minimo_en_cabecera_cada_uno_con_su_fecha():
    texto = "Precio max. (16/03/2026): 4,12 €/m3 y mes Precio min. (01/01/2026): 2,12 €/m3 y mes"
    valores, _, _ = extraer_precios(texto)
    assert [(v.tipo_valor, v.valor, v.fecha) for v in valores] == [
        ("max", 4.12, "2026-03-16"),
        ("min", 2.12, "2026-01-01"),
    ]


def test_maximo_y_minimo_en_linea_con_el_orden_cambiado():
    texto = "Precio (16/03/2026): Max. 5,15 €/m3 Precio (01/01/2026): Min. 2,64 €/m3"
    valores, _, _ = extraer_precios(texto)
    assert [(v.tipo_valor, v.valor, v.fecha, v.unidad) for v in valores] == [
        ("max", 5.15, "2026-03-16", "€/m3"),
        ("min", 2.64, "2026-01-01", "€/m3"),
    ]


def test_precio_sin_fecha():
    (v,), _, _ = extraer_precios("Precio: 3,5 €/Tm")
    assert v.fecha is None and v.valor == 3.5


def test_regla_no_es_un_precio_numerico():
    valores, resto, _ = extraer_precios("Precio según contrato")
    assert valores == [] and resto == "Precio según contrato"


def test_unidad_mas_larga_gana_y_el_resto_es_observacion():
    assert partir_unidad("€/m3 y mes") == ("€/m3 y mes", None, True)
    assert partir_unidad("€/m3 Gasolina BOB") == ("€/m3", "Gasolina BOB", True)
    assert partir_unidad("€/m3 suministrado") == ("€/m3 suministrado", None, True)


def test_unidad_desconocida_se_conserva_y_se_avisa():
    valores, _, desconocidas = extraer_precios("Precio (01/01/2026): 3,00 $/barril")
    assert valores[0].unidad == "$/barril" and desconocidas == ["$/barril"]


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("0,045 %", [("unico", 0.045, None)]),
        ("0,1% (si no se dispone de contrato)", [("unico", 0.1, "si no se dispone de contrato")]),
        (
            "0,50 % sobre existencia media trimestral",
            [("unico", 0.5, "sobre existencia media trimestral")],
        ),
        ("Max. 0,30 % Min. 0,20 %", [("max", 0.3, None), ("min", 0.2, None)]),
        ("Según legislación vigente", [(None, None, "Según legislación vigente")]),
        ("Revisión anual según % IPC", [(None, None, "Revisión anual según % IPC")]),
        ("+/- 5%", [(None, None, "+/- 5%")]),
        ("", [(None, None, None)]),
    ],
)
def test_porcentajes(texto, esperado):
    assert extraer_porcentajes(texto) == esperado
