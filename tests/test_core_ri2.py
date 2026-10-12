"""
Motor de cálculo puro de RI2 (comparación V1->V2), Bloque 1.

Casos adaptados de los "Casos de prueba de aceptación" (sección 7) de la
Ficha de Verificación - Reglas de Clasificación Comparativa V1->V2 (F2),
en los que no dependen de la comparación semántica de contenido (esa
parte se simplificó a variación de niveles, ver nota en core.py sobre
"=== RI2 ===").
"""

import core
from tests.test_core_f4 import _fila


# =============================================================================
# _clasificar_tipo_cambio
# =============================================================================

def test_tipo_cambio_sin_cambio():
    assert core._clasificar_tipo_cambio("logrado", "logrado") == "sin_cambio"


def test_tipo_cambio_mejora_formal_dentro_del_mismo_grupo():
    # Inicio -> En desarrollo: sube, pero sigue en el grupo desfavorable.
    assert core._clasificar_tipo_cambio("inicio", "en_desarrollo") == "mejora_formal"
    # Logrado -> Destacado: sube, pero sigue en el grupo favorable.
    assert core._clasificar_tipo_cambio("logrado", "destacado") == "mejora_formal"


def test_tipo_cambio_mejora_real_cruza_de_grupo():
    assert core._clasificar_tipo_cambio("en_desarrollo", "logrado") == "mejora_real"
    assert core._clasificar_tipo_cambio("inicio", "destacado") == "mejora_real"


def test_tipo_cambio_retroceso():
    assert core._clasificar_tipo_cambio("destacado", "inicio") == "retroceso"
    assert core._clasificar_tipo_cambio("logrado", "en_desarrollo") == "retroceso"


# =============================================================================
# _clasificar_estado_evidencia
# =============================================================================

def test_estado_evidencia_sin_referencia_v1():
    """Ficha F2: 'si no existe un registro válido de V1... el AA no
    simula una comparación: registra estado_evidencia = sin_referencia_v1'."""
    filas_v1 = []
    filas_v2 = [_fila(1, 1, "logrado")]
    assert core._clasificar_estado_evidencia(filas_v1, filas_v2) == "sin_referencia_v1"


def test_estado_evidencia_no_valida_sin_v2():
    filas_v1 = [_fila(1, 1, "logrado")]
    filas_v2 = []
    assert core._clasificar_estado_evidencia(filas_v1, filas_v2) == "no_valida"


def test_estado_evidencia_no_valida_v2_rechazada():
    filas_v1 = [_fila(1, 1, "logrado")]
    filas_v2 = [_fila(1, 1, "logrado", status="validation_failed")]
    assert core._clasificar_estado_evidencia(filas_v1, filas_v2) == "no_valida"


def test_estado_evidencia_valida():
    filas_v1 = [_fila(1, 1, "logrado")]
    filas_v2 = [_fila(1, 1, "destacado")]
    assert core._clasificar_estado_evidencia(filas_v1, filas_v2) == "valida"


# =============================================================================
# _cruzar_comparaciones_por_criterio
# =============================================================================

def test_cruzar_comparaciones_solo_criterios_presentes_en_ambas():
    filas_v1 = [_fila(1, 1, "inicio"), _fila(1, 2, "logrado")]
    filas_v2 = [_fila(1, 1, "logrado"), _fila(1, 3, "logrado")]  # C2 falta en v2, C3 falta en v1
    resultado = core._cruzar_comparaciones_por_criterio(filas_v1, filas_v2)
    assert resultado == [
        {"criterio_id": "C1", "nivel_v1": "inicio", "nivel_v2": "logrado", "tipo_cambio": "mejora_real"},
    ]


# =============================================================================
# _calcular_brechas_comparativas -- Instrumento RI2 Parte D (BC1, BC3, BC4;
# BC2 omitida)
# =============================================================================

def test_bc1_activa_con_mejora_formal():
    comparacion = [{"tipo_cambio": "mejora_formal"}, {"tipo_cambio": "sin_cambio"}]
    assert core._calcular_brechas_comparativas(comparacion) == ["BC1"]


def test_bc3_activa_con_un_solo_retroceso():
    """Caso de prueba del documento F2: '1 criterio con tipo_cambio =
    retroceso -> brechas_comparativas_identificadas incluye BC3' (la
    escalación a caso prioritario con 2+ es un asunto de seguimiento de
    F2, no de la activación en sí, que ya ocurre con 1)."""
    comparacion = [{"tipo_cambio": "retroceso"}, {"tipo_cambio": "sin_cambio"}]
    assert core._calcular_brechas_comparativas(comparacion) == ["BC3"]


def test_bc3_activa_con_dos_o_mas_retrocesos():
    comparacion = [{"tipo_cambio": "retroceso"}, {"tipo_cambio": "retroceso"}]
    assert core._calcular_brechas_comparativas(comparacion) == ["BC3"]


def test_bc4_activa_con_patron_desigual():
    comparacion = [
        {"tipo_cambio": "mejora_real"}, {"tipo_cambio": "mejora_real"},
        {"tipo_cambio": "sin_cambio"}, {"tipo_cambio": "sin_cambio"},
    ]
    assert core._calcular_brechas_comparativas(comparacion) == ["BC4"]


def test_brechas_comparativas_maximo_2_con_prioridad():
    """BC1 y BC3 (Crítica/Estructural) priman sobre BC4 (De ajuste) --
    Parte E del Instrumento."""
    comparacion = [
        {"tipo_cambio": "mejora_formal"},  # BC1
        {"tipo_cambio": "retroceso"},      # BC3
        {"tipo_cambio": "mejora_real"}, {"tipo_cambio": "mejora_real"},
        {"tipo_cambio": "sin_cambio"}, {"tipo_cambio": "sin_cambio"},  # BC4 también calificaría
    ]
    assert core._calcular_brechas_comparativas(comparacion) == ["BC1", "BC3"]


def test_sin_brechas_comparativas_si_todo_mejora_real():
    comparacion = [{"tipo_cambio": "mejora_real"}] * 5
    assert core._calcular_brechas_comparativas(comparacion) == []


# =============================================================================
# _calcular_brechas_persistentes -- Instrumento RI2 Parte F
# =============================================================================

def test_brecha_persiste_si_no_hay_mejora_real():
    """Caso del documento F2: 'V1 con B1 activa (C1); V2 con C1 en
    tipo_cambio = mejora_formal -> brechas_v1_persistentes incluye B1'."""
    comparacion = [{"criterio_id": "C1", "tipo_cambio": "mejora_formal"}]
    assert core._calcular_brechas_persistentes(["B1"], comparacion) == ["B1"]


def test_brecha_no_persiste_si_hay_mejora_real():
    """Caso del documento F2: 'V1 con B2 activa (C3); V2 con C3 en
    tipo_cambio = mejora_real -> brechas_v1_persistentes NO incluye B2'."""
    comparacion = [{"criterio_id": "C3", "tipo_cambio": "mejora_real"}]
    assert core._calcular_brechas_persistentes(["B2"], comparacion) == []


# =============================================================================
# _calcular_avance_institucional -- Instrumento RI2 Parte D
# =============================================================================

def test_avance_institucional_en_puntos_porcentuales():
    comparaciones_validas = [
        [{"criterio_id": "C1", "nivel_v1": "inicio", "nivel_v2": "logrado"}],
        [{"criterio_id": "C1", "nivel_v1": "logrado", "nivel_v2": "logrado"}],
    ]
    resultado = core._calcular_avance_institucional(comparaciones_validas)
    assert resultado == [
        {"criterio_id": "C1", "n": 2, "pct_favorable_v1": 50, "pct_favorable_v2": 100, "avance_pct": 50},
    ]


# =============================================================================
# _calcular_logros_consolidados -- Instrumento RI2 Parte G
# =============================================================================

def test_logros_consolidados_vacio_si_nadie_cumple():
    """No se fuerza una fortaleza artificial (mismo principio que
    clasificar_necesidades_frecuentes en RI1)."""
    comparaciones_validas = [[{"criterio_id": "C1", "tipo_cambio": "sin_cambio", "nivel_v2": "logrado"}]]
    brechas_persistentes_por_docente = [[]]
    assert core._calcular_logros_consolidados(comparaciones_validas, brechas_persistentes_por_docente) == []


def test_logro_consolidado_con_doble_condicion():
    comparaciones_validas = [
        [{"criterio_id": "C1", "tipo_cambio": "mejora_real", "nivel_v2": "logrado"}],
        [{"criterio_id": "C1", "tipo_cambio": "mejora_real", "nivel_v2": "destacado"}],
        [{"criterio_id": "C1", "tipo_cambio": "sin_cambio", "nivel_v2": "logrado"}],
    ]
    brechas_persistentes_por_docente = [[], [], ["B1"]]
    resultado = core._calcular_logros_consolidados(comparaciones_validas, brechas_persistentes_por_docente)
    assert resultado == [{
        "criterio_id": "C1",
        "n_mejora_real": 2,
        "n_total": 3,
        "pct_mejora_real": 67,
        "practica_efectiva_transferible": True,
        "n_destacado": 1,
    }]


def test_logro_consolidado_no_transferible_sin_destacado():
    comparaciones_validas = [
        [{"criterio_id": "C1", "tipo_cambio": "mejora_real", "nivel_v2": "logrado"}],
        [{"criterio_id": "C1", "tipo_cambio": "mejora_real", "nivel_v2": "logrado"}],
    ]
    brechas_persistentes_por_docente = [[], []]
    resultado = core._calcular_logros_consolidados(comparaciones_validas, brechas_persistentes_por_docente)
    assert resultado[0]["practica_efectiva_transferible"] is False
    assert resultado[0]["n_destacado"] == 0


# =============================================================================
# _agregar_comparaciones_ie -- clasificación de la IE completa (sin DB)
# =============================================================================

def test_agregar_comparaciones_ie_clasifica_los_3_estados():
    # Docente 1: V1 y V2 completas (5 criterios, inicio -> logrado) -> valida.
    filas_v1 = [_fila(1, i, "inicio") for i in range(1, 6)]
    filas_v2 = [_fila(1, i, "logrado") for i in range(1, 6)]

    # Docente 2: V1 válida, sin ninguna fila V2 -> no_valida.
    filas_v1 += [_fila(2, 1, "logrado")]

    # Docente 3: sin V1, con V2 -> sin_referencia_v1.
    filas_v2 += [_fila(3, 1, "logrado")]

    resultado = core._agregar_comparaciones_ie(filas_v1, filas_v2)

    assert resultado["n_sin_referencia_v1"] == 1
    assert resultado["n_no_validas"] == 1
    assert len(resultado["comparaciones_validas"]) == 1
    assert len(resultado["comparaciones_validas"][0]) == 5
    assert all(c["tipo_cambio"] == "mejora_real" for c in resultado["comparaciones_validas"][0])
    assert resultado["brechas_persistentes_por_docente"] == [[]]
    assert resultado["naturaleza_cambios_por_docente"] == {1: []}
    assert resultado["persistentes_por_docente"] == {1: []}
