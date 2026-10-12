"""
Funciones puras y determinísticas de core.py que son genuinamente
compartidas (la vista entrega el mismo esquema de niveles/criterios/
brechas para cualquier versión de sesión) y que RI2 reutiliza: casos de
prueba CP02-CP11 de la ficha técnica F4 original (docs_nuevos/1. FICHA
TÉCNICA..., sección 18), más bandas de cobertura, desambiguación de IE y
el umbral de espera. Los casos que cubrían el orquestador/banco de
redacción propio de RI1 (ya no vive en este proyecto -- ver core.py,
sección RI2) se removieron de este archivo.
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


def test_primera_oracion_interpretacion_no_inventa_cifras():
    """La primera oración de la interpretación (sección 3) debe citar el
    conteo y % EXACTOS del agrupado predominante de la tabla de la
    sección 2 -- nunca un número distinto ni el total como si fuera el
    conteo del grupo (bug real observado: el LLM escribía "5 de 5" para
    un aspecto donde la tabla decía 80%, no 100%). Ahora la arma Python
    por completo, el LLM nunca la toca."""
    c_favorable = {
        "n": 5, "n_inicio": 1, "n_en_desarrollo": 0, "n_logrado": 3, "n_destacado": 1,
        "pct_logrado_destacado": 80, "pct_inicio_en_desarrollo": 20,
    }
    assert core._primera_oracion_interpretacion(c_favorable) == "En 4 de 5 sesiones (80%) se observa Logrado o Destacado."

    c_desfavorable = {
        "n": 5, "n_inicio": 1, "n_en_desarrollo": 2, "n_logrado": 2, "n_destacado": 0,
        "pct_logrado_destacado": 40, "pct_inicio_en_desarrollo": 60,
    }
    assert core._primera_oracion_interpretacion(c_desfavorable) == "En 3 de 5 sesiones (60%) se observa Inicio o En desarrollo."

    c_empate = {
        "n": 4, "n_inicio": 1, "n_en_desarrollo": 1, "n_logrado": 2, "n_destacado": 0,
        "pct_logrado_destacado": 50, "pct_inicio_en_desarrollo": 50,
    }
    assert core._primera_oracion_interpretacion(c_empate) == "En 2 de 4 sesiones (50%) se observa Logrado o Destacado."


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
        "_construir_prompt_secciones_narrativas_ri2."
    )
)
def test_cp12_no_causalidad_revision_manual():
    pass


def test_denominacion_visible_sin_codigo():
    """El documento prohíbe mostrar los códigos C1-C5/B1-B5 en texto
    visible (ficha técnica, secciones 8 y 11) -- _nombre_criterio y
    _nombre_brecha nunca deben anteponer el código."""
    assert core._nombre_criterio("C4") == "Mediación y evaluación formativa"
    assert not core._nombre_criterio("C4").startswith("C4")
    assert core._nombre_brecha("B1") == "Coherencia del diseño con la información del diagnóstico"


# =============================================================================
# _armar_clave_ie -- ugel es necesario para desambiguar IEs reales con el
# mismo nombre_ie + región (y distrito en NULL en ambos casos) pero ugel
# distinto, ej. dos "CESAR VALLEJO" en Apurímac (verificado contra datos
# reales de base_ebr_IA).
# =============================================================================

def test_armar_clave_ie_desambigua_por_ugel():
    clave_chincheros = core._armar_clave_ie("CESAR VALLEJO", "UGEL CHINCHEROS", "APURIMAC", None)
    clave_cotabambas = core._armar_clave_ie("CESAR VALLEJO", "UGEL COTABAMBAS", "APURIMAC", None)
    assert clave_chincheros != clave_cotabambas


def test_armar_clave_ie_tolera_ugel_y_distrito_none():
    assert core._armar_clave_ie("IE X", None, "LIMA", None) == "IE X||LIMA|"


# =============================================================================
# _clasificar_cobertura -- bandas de la Adenda F4-ADENDA-EMISION-RI-EBRJEC26
# v1.0, sección 4. Casos tomados directo de los CP13-CP18 de la propia
# adenda (sección 14, casos de prueba de aceptación).
# =============================================================================

def test_cp13_ie_grande_banda_b5_bajo_umbral():
    """N=96; 5 V1 válidas -> Banda B5; umbral 58; COB_BAJO_UMBRAL."""
    r = core._clasificar_cobertura(n_evidencias_validas=5, n_docentes_total=96)
    assert r == {"banda_aplicada": "B5", "umbral_requerido": 58, "estado_cobertura": "COB_BAJO_UMBRAL"}


def test_cp14_ie_banda_b3_cobertura_suficiente():
    """N=40; 28 V1 válidas -> Banda B3; umbral 28; COB_SUFICIENTE."""
    r = core._clasificar_cobertura(n_evidencias_validas=28, n_docentes_total=40)
    assert r == {"banda_aplicada": "B3", "umbral_requerido": 28, "estado_cobertura": "COB_SUFICIENTE"}


def test_cp15_ie_banda_b3_bajo_umbral_por_una_evidencia():
    """N=40; 27 V1 válidas -> COB_BAJO_UMBRAL por una sola evidencia de diferencia."""
    r = core._clasificar_cobertura(n_evidencias_validas=27, n_docentes_total=40)
    assert r["estado_cobertura"] == "COB_BAJO_UMBRAL"
    assert r["umbral_requerido"] == 28


def test_cp16_redondeo_umbral_siempre_hacia_arriba():
    """N=12; 10 V1 válidas -> Banda B1; umbral ceil(0.85*12)=10.2->11; COB_BAJO_UMBRAL."""
    r = core._clasificar_cobertura(n_evidencias_validas=10, n_docentes_total=12)
    assert r == {"banda_aplicada": "B1", "umbral_requerido": 11, "estado_cobertura": "COB_BAJO_UMBRAL"}


def test_cp17_confidencial_por_pocas_evidencias_aunque_n_sea_grande():
    """N=30; 4 V1 válidas -> COB_CONFIDENCIAL (n<5) aunque N>=5."""
    r = core._clasificar_cobertura(n_evidencias_validas=4, n_docentes_total=30)
    assert r["estado_cobertura"] == "COB_CONFIDENCIAL"


def test_cp18_banda_b0_siempre_confidencial_aunque_cobertura_sea_100():
    """N=4; 4 V1 válidas (100% de cobertura) -> Banda B0; COB_CONFIDENCIAL igual."""
    r = core._clasificar_cobertura(n_evidencias_validas=4, n_docentes_total=4)
    assert r == {"banda_aplicada": "B0", "umbral_requerido": None, "estado_cobertura": "COB_CONFIDENCIAL"}


# =============================================================================
# _debe_esperar_mas_docentes -- RE-03/RE-04: deja de esperar tanto en
# COB_SUFICIENTE como en COB_BAJO_UMBRAL (este último se emite con
# advertencia). Solo COB_CONFIDENCIAL sigue esperando (banda B0 espera
# hasta el 100%, ya que nunca sale de COB_CONFIDENCIAL por definición).
# =============================================================================

def test_debe_esperar_ie_grande_ya_no_espera_en_bajo_umbral():
    """IE de 96 docentes: 5 evaluados es COB_BAJO_UMBRAL (no alcanza el
    umbral de banda B5, 58) -- RE-04 exige emitir igual, con advertencia,
    no seguir esperando."""
    assert core._debe_esperar_mas_docentes(n_actual=5, n_total=96) is False


def test_debe_esperar_ie_grande_genera_al_llegar_al_umbral_de_su_banda():
    assert core._debe_esperar_mas_docentes(n_actual=58, n_total=96) is False


def test_debe_esperar_sigue_esperando_en_confidencial():
    """n=3 < 5 en una IE de 96 docentes -- COB_CONFIDENCIAL, todavía sin
    implementar (fuera de alcance de este cambio); sigue esperando."""
    assert core._debe_esperar_mas_docentes(n_actual=3, n_total=96) is True


def test_debe_esperar_banda_b0_espera_hasta_evaluar_a_todos():
    """IE de 3 docentes (banda B0): nunca sale de COB_CONFIDENCIAL -- se
    conserva el escape anterior, deja de esperar solo al llegar al 100%."""
    assert core._debe_esperar_mas_docentes(n_actual=2, n_total=3) is True
    assert core._debe_esperar_mas_docentes(n_actual=3, n_total=3) is False

