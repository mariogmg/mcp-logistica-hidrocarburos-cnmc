from pathlib import Path

import pytest

from ayuda import FIXTURES, RAIZ, leer
from cnmc_logistica.categorias import Categorizador
from cnmc_logistica.parsers.detalle import (
    PaginaNoReconocida,
    conexion_de,
    parsear_detalle,
    partir_etiqueta,
    partir_productos,
)

IDS = sorted(int(p.stem) for p in (FIXTURES / "detalles").glob("*.html"))


def detalle(id_ti: int):
    return parsear_detalle(leer(f"detalles/{id_ti}.html"), id_ti)


def precio(d, etiqueta, subetiqueta=None):
    return next(p for p in d.precios if p.etiqueta == etiqueta and p.subetiqueta == subetiqueta)


def condiciones(d, etiqueta):
    return [c for c in d.condiciones if c.etiqueta == etiqueta]


# --- estructura común a todos los operadores ---------------------------------


def test_fixtures_de_al_menos_cinco_operadores():
    assert len({detalle(i).titular for i in IDS}) >= 5


@pytest.mark.parametrize("id_ti", IDS)
def test_estructura_minima(id_ti):
    d = detalle(id_ti)
    assert d.titulo and d.titular and d.productos
    assert d.conexion in {"red", "por_oleoducto", "no_conectada", "sin_dato"}
    assert any(p.seccion == "principal" for p in d.precios)
    assert 1 <= len(d.condiciones) <= 60
    assert 1 <= len(d.incluidos) <= 40
    for p in d.precios:
        assert p.tipo_precio in {"numerico", "regla", "texto", "pdf"}
        assert p.texto_original
        assert (p.tipo_precio == "numerico") == bool(p.valores)
        for v in p.valores:
            assert v.tipo_valor in {"unico", "max", "min"} and v.valor >= 0 and v.unidad
    for c in d.condiciones:
        assert c.texto_original
        assert (c.valor is None) == (c.tipo_valor is None)


@pytest.mark.parametrize("id_ti", IDS)
def test_sin_bloques_desconocidos_en_paginas_reales(id_ti):
    assert detalle(id_ti).avisos == []


@pytest.mark.parametrize("id_ti", IDS)
def test_todas_las_etiquetas_tienen_categoria(id_ti):
    cat = Categorizador.de_fichero(RAIZ / "config" / "categorias_servicio.yaml")
    sin = [p.etiqueta for p in detalle(id_ti).precios if not cat.categoria(p.etiqueta)[1]]
    assert sin == []


# --- Exolum Corporation: listas anidadas, PDFs y origen-destino ---------------


def test_exolum_cabecera_y_conexion():
    d = detalle(17)
    assert d.titulo == "EXOLUM - Puxeiros"
    assert d.titular == "EXOLUM CORPORATION, S.A."
    assert (d.subtitulo, d.conexion) == ("Instalación conectada por oleoducto", "por_oleoducto")
    assert d.productos == ["Gasolinas", "Gasóleos"]
    assert d.tiene_origen_destino


def test_exolum_servicio_principal_remite_a_pdfs():
    p = precio(detalle(17), "Servicios logísticos básicos")
    assert (p.seccion, p.tipo_precio, p.valores) == ("principal", "pdf", [])
    assert p.pdfs == ["24", "25"]


def test_exolum_incluidos():
    textos = [s.texto for s in detalle(17).incluidos]
    assert "Transporte" in textos and "Expedición por camión cisterna" in textos


def test_lista_anidada_mermas_por_producto():
    mermas = condiciones(detalle(17), "Mermas")
    assert [(c.subetiqueta, c.tipo_valor, c.valor, c.unidad) for c in mermas] == [
        ("Gasóleos", "unico", 0.045, "%"),
        ("Gasolinas", "unico", 0.06, "%"),
    ]
    # El padre «Mermas:» no genera fila propia y el siguiente apartado no es hijo suyo.
    assert all(c.subetiqueta is None for c in condiciones(detalle(17), "Programación"))


def test_lista_anidada_precios_por_subapartado():
    d = detalle(17)
    c = precio(d, "Colorante y trazador fiscal", "Gasóleo C")
    assert [(v.valor, v.fecha, v.unidad) for v in c.valores] == [(2.4106, "2026-01-01", "€/m3")]
    b = precio(d, "Colorante y trazador fiscal", "Gasóleo B")
    assert b.valores[0].valor == 1.0351
    padre = "Almacenamiento operativo adicional - Posibilidad de contratación"
    subs = {p.subetiqueta for p in d.precios if p.etiqueta == padre}
    assert subs == {"Diario (gnas y gos)", "Anual (gnas y gos)", "Mensual (gnas y gos)"}
    diario = precio(d, padre, "Diario (gnas y gos)")
    assert diario.valores[0].unidad == "€/m3 y día"
    assert diario.valores[0].observacion.startswith("exceso diario")


def test_padre_con_pdf_y_subapartados_de_texto():
    d = detalle(17)
    padre = precio(d, "Almacenamiento no operativo")
    assert (padre.tipo_precio, padre.pdfs) == ("pdf", ["26"])
    hijos = [p for p in d.precios if p.etiqueta == "Almacenamiento no operativo" and p.subetiqueta]
    assert {h.subetiqueta for h in hijos} == {"Modalidad 2", "Modalidad 3"}
    assert all(h.tipo_precio == "texto" for h in hijos)


def test_precio_simple_de_otros_servicios():
    p = precio(detalle(17), "Transferencias")
    assert p.tipo_precio == "numerico"
    assert (p.valores[0].valor, p.valores[0].unidad) == (2.4577, "€/operación")


# --- otros operadores: cada uno redacta a su manera ---------------------------


def test_exolum_aviation_precio_en_la_cabecera_y_merma_condicionada():
    d = detalle(20)
    p = precio(d, "Servicio almacenamiento")
    assert [(v.tipo_valor, v.valor, v.fecha, v.unidad) for v in p.valores] == [
        ("unico", 41.235, "2026-04-01", "€/m3")
    ]
    (m,) = condiciones(d, "Mermas")
    assert m.valor == 0.1 and m.texto.startswith("si no se dispone")
    sin_precio = precio(d, "Servicio de puesta a bordo")
    assert sin_precio.tipo_precio == "texto" and sin_precio.valores == []


def test_slca_merma_como_texto():
    d = detalle(45)
    assert precio(d, "Servicios aeroportuarios").valores[0].valor == 48.64
    (m,) = condiciones(d, "Mermas")
    assert (m.valor, m.texto) == (None, "Según legislación vigente")


def test_tepsa_maximo_y_minimo_con_fechas_distintas():
    d = detalle(33)
    p = precio(d, "Almacenamiento y manipulación")
    assert [(v.tipo_valor, v.valor, v.fecha) for v in p.valores] == [
        ("max", 4.12, "2026-03-16"),
        ("min", 2.12, "2026-01-01"),
    ]
    c = precio(d, "Almacenamiento complementario")
    assert [(v.tipo_valor, v.valor) for v in c.valores] == [("max", 5.15), ("min", 2.64)]
    assert precio(d, "Limpieza de tanques").tipo_precio == "regla"
    assert precio(d, "Trazado", "Gasóleo B").valores[0].tipo_valor == "max"


def test_disa_sin_subtitulo_y_padre_con_texto_e_hijos():
    d = detalle(214)
    assert (d.subtitulo, d.conexion) == (None, "sin_dato")
    assert d.titular == "DISA Grupo"
    assert precio(d, "Servicios logísticos").pdfs == ["12"]
    etiqueta = "Gestión aprovisionamiento desde refinería Tenerife"
    assert precio(d, etiqueta).texto == "Posibilidad de contratación"
    assert precio(d, etiqueta, "Gasóleos").valores[0].valor == 0.391


def test_decal_unidad_semanal_y_precio_descrito_como_regla():
    d = detalle(79)
    c = precio(d, "Almacenamiento complementario")
    assert {v.unidad for v in c.valores} == {"€/m3 y semana"}
    limpieza = precio(d, "Limpieza de tanques")
    assert limpieza.tipo_precio == "regla" and "coste real" in limpieza.texto


def test_cmd_sin_bloque_de_otros_servicios():
    d = detalle(225)
    assert all(p.seccion == "principal" for p in d.precios)
    (m,) = condiciones(d, "Mermas")
    assert (m.subetiqueta, m.valor) == ("Jet A-1", 0.1)


def test_terminales_canarios_merma_con_base_de_calculo():
    (m,) = condiciones(detalle(295), "Mermas")
    assert m.valor == 0.5 and m.texto == "sobre existencia media trimestral"


def test_barajas_dos_titulares_en_la_misma_instalacion():
    a, b = detalle(123), detalle(124)
    assert a.titulo == b.titulo
    assert {a.titular, b.titular} == {"EXOLUM AVIATION, S.A.", "EXOLUM CORPORATION, S.A."}


# --- robustez -----------------------------------------------------------------


def test_bloque_desconocido_se_anota_y_no_aborta():
    html = leer("detalles/17.html").replace(
        '<div class="titulo_verde">\n\t    \t\t\tCONDICIONES',
        '<div class="titulo_verde">TARIFAS ESPECIALES</div><div>Texto nuevo</div>'
        '<div class="titulo_verde">\n\t    \t\t\tCONDICIONES',
    )
    assert "TARIFAS ESPECIALES" in html
    d = parsear_detalle(html, 17)
    tipos = [(a.tipo, a.recurso, a.operador) for a in d.avisos]
    assert ("bloque_desconocido", "detalle/17", "EXOLUM CORPORATION, S.A.") in tipos
    assert any("TARIFAS ESPECIALES" in a.texto for a in d.avisos)
    assert condiciones(d, "Mermas")  # lo que viene después se sigue leyendo


def test_unidad_desconocida_se_anota():
    html = leer("detalles/17.html").replace("2,4577 &euro;/operaci&oacute;n", "2,4577 puntos")
    d = parsear_detalle(html, 17)
    assert [a.tipo for a in d.avisos] == ["unidad_desconocida"]
    assert precio(d, "Transferencias").valores[0].unidad == "puntos"


def test_subtitulo_desconocido_queda_sin_dato_con_aviso():
    html = leer("detalles/17.html").replace(
        "Instalaci&oacute;n conectada por oleoducto", "Instalaci&oacute;n flotante"
    )
    d = parsear_detalle(html, 17)
    assert d.conexion == "sin_dato"
    assert [a.tipo for a in d.avisos] == ["conexion_desconocida"]


def test_pagina_que_no_es_un_detalle():
    with pytest.raises(PaginaNoReconocida):
        parsear_detalle("<html><body><h1>404 Not Found</h1></body></html>", 1)


def test_funciones_auxiliares():
    assert conexion_de(
        "Instalación conectada a la red de oleoductos. No admite consignaciones"
    ) == (
        "red",
        True,
    )
    assert partir_productos("Gasolinas, Gasóleos, Querosenos y Biodiésel") == [
        "Gasolinas",
        "Gasóleos",
        "Querosenos",
        "Biodiésel",
    ]
    assert partir_productos("Gasolinas de aviación") == ["Gasolinas de aviación"]
    assert partir_etiqueta("Mermas:") == ("Mermas", "")
    assert partir_etiqueta("Riesgo producto: A cargo del almacenista") == (
        "Riesgo producto",
        "A cargo del almacenista",
    )


def test_fixtures_son_copia_fiel_del_crudo():
    crudo = RAIZ / "raw" / "2026-11-11" / "detalles"
    if not crudo.exists():
        pytest.skip("raw/ no está disponible (no se versiona)")
    for i in IDS:
        assert (
            Path(crudo / f"{i}.html").read_bytes() == (FIXTURES / f"detalles/{i}.html").read_bytes()
        )


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Precio según contrato", True),
        ("Precios vigentes hasta aprobación tarifas", True),
        ("Según contrato (€/operación)", True),
        ("Se repercutirá el coste real incrementado en un 3%", True),
        ("Revisión anual, máximo IPC", True),
        ("Posibilidad de contratación", False),
        ("Servicio adicional no incluido en precio", False),
        ("Almacenamiento en instalaciones identificadas", False),
    ],
)
def test_regla_frente_a_texto(texto, esperado):
    from cnmc_logistica.parsers.detalle import es_regla

    assert es_regla(texto) is esperado
