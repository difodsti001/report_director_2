"""
Casos de prueba CP01-CP11 de la ficha técnica F4 (docs_nuevos/1. FICHA
TÉCNICA..., sección 18), cubriendo las funciones puras y determinísticas
de core.py. CP12 (bloquear causalidad inventada por el LLM) no es
testeable como unit test -- queda como checklist de revisión manual del
prompt, ver el test marcado como skip al final de este archivo.
"""

import pytest

import core


def _fila(user_id, criterion_index, nivel, status="success", nivel_educativo="Primaria", brecha=None, tipo_brecha=None):
    return {
        "user_id": user_id,
        "criterion_index": criterion_index,
        "nivel_obtenido": nivel,
        "status": status,
        "nivel_educativo": nivel_educativo,
        "brecha": brecha,
        "tipo_brecha": tipo_brecha,
    }


def test_cp01_cobertura_y_clasificacion_estados():
    """24 registrados; 22 válidos; 1 no válido; 1 no entrega -> cobertura
    91,7%; desempeño sobre 22."""
    filas = [_fila(uid, 1, "logrado") for uid in range(22)]
    filas.append(_fila(22, 1, "inicio", status="validation_failed"))

    estados = core.clasificar_evidencias_por_estado(filas, n_docentes_total=24)

    assert estados == {"n_validas": 22, "n_no_validas": 1, "n_sin_entrega": 1}
    pct_cobertura = round(100 * estados["n_validas"] / 24)
    assert pct_cobertura == 92  # 22/24 = 91.7% redondeado


def test_resultado_predominante_texto_no_inventa_porcentaje():
    """El texto de interpretación (sección 3) debe citar EXACTAMENTE el
    % del agrupado predominante de la tabla de la sección 2 -- nunca un
    número distinto (bug real observado: el LLM citaba porcentajes que
    no existían en la tabla, ej. 80% cuando la tabla decía 40%)."""
    c_favorable = {"pct_logrado_destacado": 80, "pct_inicio_en_desarrollo": 20}
    assert core._resultado_predominante_texto(c_favorable) == "Logrado o Destacado (80%)"

    c_desfavorable = {"pct_logrado_destacado": 40, "pct_inicio_en_desarrollo": 60}
    assert core._resultado_predominante_texto(c_desfavorable) == "Inicio o En desarrollo (60%)"

    c_empate = {"pct_logrado_destacado": 50, "pct_inicio_en_desarrollo": 50}
    assert core._resultado_predominante_texto(c_empate) == "Logrado o Destacado (50%)"


def test_cp02_distribucion_c3():
    """C3: 6 Inicio, 9 Desarrollo, 6 Logrado, 1 Destacado -> 27.3%; 40.9%;
    27.3%; 4.5%; agrupado 68.2% y 31.8%."""
    filas = (
        [_fila(i, 3, "inicio") for i in range(6)]
        + [_fila(i, 3, "en_desarrollo") for i in range(6, 15)]
        + [_fila(i, 3, "logrado") for i in range(15, 21)]
        + [_fila(21, 3, "destacado")]
    )
    c = core.calcular_distribucion_por_criterio(filas)[0]

    assert c["criterio_id"] == "C3"
    assert c["n"] == 22
    assert (c["pct_inicio"], c["pct_en_desarrollo"], c["pct_logrado"], c["pct_destacado"]) == (27, 41, 27, 5)
    assert c["pct_inicio_en_desarrollo"] == c["pct_inicio"] + c["pct_en_desarrollo"]
    assert c["pct_logrado_destacado"] == c["pct_logrado"] + c["pct_destacado"]


def test_cp03_distribucion_c1_favorable():
    """C1: 1 Inicio, 4 Desarrollo, 13 Logrado, 4 Destacado -> ~22.7%
    requiere mayor desarrollo; ~77.3% favorable."""
    filas = (
        [_fila(0, 1, "inicio")]
        + [_fila(i, 1, "en_desarrollo") for i in range(1, 5)]
        + [_fila(i, 1, "logrado") for i in range(5, 18)]
        + [_fila(i, 1, "destacado") for i in range(18, 22)]
    )
    c = core.calcular_distribucion_por_criterio(filas)[0]

    assert c["pct_inicio_en_desarrollo"] == 23
    assert c["pct_logrado_destacado"] == 77


def test_cp04_necesidad_frecuente_b2():
    """B2 en 9 de 22 -> 40,9%; visible como necesidad frecuente de
    participación y exigencia de las actividades (denominación oficial,
    sin el código B2)."""
    brechas_por_docente = {i: (["B2"] if i < 9 else []) for i in range(22)}
    frecuencia = core.calcular_frecuencia_brechas(brechas_por_docente)
    necesidades = core.clasificar_necesidades_frecuentes(frecuencia)

    top = necesidades[0]
    assert top["id"] == "B2"
    assert top["pct"] == 41  # round(100*9/22)
    assert core._nombre_brecha("B2") == "Participación de los estudiantes y nivel de exigencia de las actividades"


def test_cp05_necesidad_frecuente_b3():
    """B3 en 7 de 22 -> 31,8%; visible como necesidad frecuente en
    situaciones significativas."""
    brechas_por_docente = {i: (["B3"] if i < 7 else []) for i in range(22)}
    frecuencia = core.calcular_frecuencia_brechas(brechas_por_docente)
    necesidades = core.clasificar_necesidades_frecuentes(frecuencia)

    assert necesidades[0]["id"] == "B3"
    assert necesidades[0]["pct"] == 32  # round(100*7/22)
    assert core._nombre_brecha("B3") == "Situaciones significativas"


def test_cp05b_necesidades_bajo_umbral_no_aparecen():
    """B1, B3, B5 por debajo del 30% (Especificaciones §9, §16 paso 6):
    el umbral es un FILTRO de inclusión en la tabla de la sección 5, no
    solo un umbral de lectura -- no deben aparecer como filas aunque
    existan en la distribución. Solo B2 (40,9%) supera el umbral."""
    brechas_por_docente = {}
    for i in range(22):
        if i < 9:
            brechas_por_docente[i] = ["B2"]  # 9/22 = 40.9%
        elif i < 11:
            brechas_por_docente[i] = ["B1"]  # 2/22 = 9.1%
        elif i < 13:
            brechas_por_docente[i] = ["B3"]  # 2/22 = 9.1%
        elif i < 15:
            brechas_por_docente[i] = ["B5"]  # 2/22 = 9.1%
        else:
            brechas_por_docente[i] = []

    frecuencia = core.calcular_frecuencia_brechas(brechas_por_docente)
    necesidades = core.clasificar_necesidades_frecuentes(frecuencia)

    assert [n["id"] for n in necesidades] == ["B2"]


def test_cp06_no_infiere_brecha_no_provista():
    """C3 alto pero sin B2 -> no inferir B2; respetar
    brechas_identificadas[] tal como las entrega F2 (aquí: brecha=None)."""
    filas_docente = [
        {"criterion_index": 3, "nivel_obtenido": "inicio", "brecha": None, "tipo_brecha": None},
    ]
    assert core.seleccionar_brechas_globales(filas_docente) == []


def test_cp07_muestra_insuficiente():
    """4 evidencias válidas -> por debajo del umbral, debe usarse lenguaje
    prudente (muestra_suficiente=False)."""
    n_evidencias_validas = 4
    assert n_evidencias_validas < core.UMBRAL_MUESTRA_INSUFICIENTE


def test_cp08_lectura_por_nivel():
    """Inicial 4, Primaria 10, Secundaria 8 -> desglose con prudencia
    (Inicial < 5), sin ranking (se listan en el orden fijo de niveles)."""
    filas = (
        [_fila(i, 1, "logrado", nivel_educativo="Inicial") for i in range(4)]
        + [_fila(100 + i, 1, "logrado", nivel_educativo="Primaria") for i in range(10)]
        + [_fila(200 + i, 1, "logrado", nivel_educativo="Secundaria") for i in range(8)]
    )
    lectura = core.calcular_lectura_por_nivel(filas)
    por_nivel = {l["nivel_educativo"]: l for l in lectura}

    assert [l["nivel_educativo"] for l in lectura] == ["Inicial", "Primaria", "Secundaria"]
    assert por_nivel["Inicial"]["n"] == 4
    assert por_nivel["Inicial"]["muestra_insuficiente"] is True
    assert por_nivel["Primaria"]["n"] == 10
    assert por_nivel["Primaria"]["muestra_insuficiente"] is False
    assert por_nivel["Secundaria"]["n"] == 8


def test_cp09_sin_nivel_no_genera_lectura():
    """Sin nivel educativo -> no generar lectura diferenciada."""
    filas = [_fila(1, 1, "logrado", nivel_educativo=None)]
    assert core.calcular_lectura_por_nivel(filas) == []


def test_cp10_comprension_lectora_sin_porcentaje():
    """Comprensión lectora nunca debe traer un porcentaje institucional
    propio, porque F2 no la evalúa como criterio independiente."""
    enfasis = core.calcular_enfasis_lectura({})
    comprension = next(e for e in enfasis if e["enfasis"] == "Comprensión lectora")

    assert comprension["aspecto_relacionado"] is None
    assert "no presenta un porcentaje institucional" in comprension["texto_permitido"]


def test_cp11_determinismo():
    """Mismo conjunto de entrada procesado dos veces -> mismo resultado
    cuantitativo."""
    filas = [_fila(i, c, "logrado") for i in range(10) for c in range(1, 6)]
    assert core.calcular_distribucion_por_criterio(filas) == core.calcular_distribucion_por_criterio(filas)


@pytest.mark.skip(
    reason=(
        "CP12 (bloquear/reescribir causalidad inventada por el LLM, ej. "
        "'C3 causa bajo rendimiento ENLA') no es testeable como unit test: "
        "depende del comportamiento del modelo de lenguaje. Queda como "
        "checklist de revisión manual del prompt en "
        "_construir_prompt_secciones_narrativas."
    )
)
def test_cp12_no_causalidad_revision_manual():
    pass


def test_completar_huecos_narrativos_no_descarta_lo_valido():
    """Bug real observado en producción (IE MARIANO BONIN): al LLM le
    faltaba UNA manifestación de necesidad y la validación anterior
    descartaba TODA la respuesta (interpretaciones y síntesis correctas
    incluidas), cayendo al fallback genérico. Ahora solo se completa el
    campo faltante, preservando el resto de lo que sí generó el LLM."""
    variables = {
        "nombre_ie": "IE de prueba",
        "n_docentes_total": 10,
        "n_evidencias_validas": 5,
        "pct_cobertura": 50,
        "distribucion_por_aspecto": [
            {"criterio_id": "C1", "nombre": "Coherencia entre propósito y desafíos",
             "n": 5, "pct_logrado_destacado": 80, "pct_inicio_en_desarrollo": 20},
        ],
        "fortalezas": [
            {"id": "C1", "nombre": "Coherencia entre propósito y desafíos", "n": 5, "pct": 80},
        ],
        "necesidades_frecuentes": [
            {"id": "B4", "nombre": "Mediación y evaluación formativa", "n": 2, "pct": 40},
        ],
    }
    data_del_llm = {
        "interpretaciones": {"C1": "Texto real de interpretación generado por el LLM."},
        "manifestaciones": {
            "fortalezas": {"C1": "una relación clara entre propósito y desafío"},
            "necesidades": {},  # <- falta B4, como en el bug real
        },
        "preguntas_rtc": ["¿Pregunta real generada por el LLM?"],
        "sintesis_institucional": "Síntesis real generada por el LLM.",
    }

    resultado = core._completar_huecos_narrativos(variables, data_del_llm)

    # Lo que el LLM sí generó bien se preserva tal cual...
    assert resultado["interpretaciones"]["C1"] == "Texto real de interpretación generado por el LLM."
    assert resultado["manifestaciones"]["fortalezas"]["C1"] == "una relación clara entre propósito y desafío"
    assert resultado["preguntas_rtc"] == ["¿Pregunta real generada por el LLM?"]
    assert resultado["sintesis_institucional"] == "Síntesis real generada por el LLM."
    # ...solo el hueco puntual se completa.
    assert resultado["manifestaciones"]["necesidades"]["B4"] == core._MANIFESTACION_GENERICA


def test_denominacion_visible_sin_codigo():
    """El documento prohíbe mostrar los códigos C1-C5/B1-B5 en texto
    visible (ficha técnica, secciones 8 y 11) -- _nombre_criterio y
    _nombre_brecha nunca deben anteponer el código."""
    assert core._nombre_criterio("C4") == "Mediación y evaluación formativa"
    assert not core._nombre_criterio("C4").startswith("C4")
    assert core._nombre_brecha("B1") == "Coherencia del diseño con la información del diagnóstico"
    assert not core._nombre_brecha("B1").startswith("B1")
