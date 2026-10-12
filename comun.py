"""
comun.py

Constantes y catálogos genuinamente compartidos entre RI1 y RI2 -- las
mismas reglas, escritas una sola vez, tal como las repiten (casi palabra
por palabra) las fichas técnicas de ambos reportes. No contiene lógica de
negocio ni cálculo: eso sigue siendo propio de cada reporte en core.py,
por decisión explícita de no forzar un motor genérico único.
"""

from __future__ import annotations

# =============================================================================
# Criterios pedagógicos (C1-C5) -- idénticos en RI1 y RI2 (Ficha RI2,
# "Parte A -- Criterios reutilizados de V1": "Los 5 criterios se reutilizan
# literalmente de la Rúbrica analítica de Evidencia 1").
# =============================================================================

CRITERIOS: dict[str, dict[str, str]] = {
    "C1": {
        "nombre": "Coherencia entre propósito y desafíos",
        "que_evalua": (
            "Que la sesión responda a los desafíos identificados en el "
            "diagnóstico previo (MPE/ENLA/evidencias de aula), no a un "
            "formato genérico."
        ),
    },
    "C2": {
        "nombre": "Situación significativa",
        "que_evalua": (
            "Que la situación de aprendizaje esté orientada al desarrollo "
            "de competencias y pensamiento crítico, no solo a completar "
            "actividades."
        ),
    },
    "C3": {
        "nombre": "Demanda cognitiva e involucramiento activo",
        "que_evalua": (
            "Que las actividades propuestas exijan análisis, toma de "
            "decisiones o argumentación, no solo participación."
        ),
    },
    "C4": {
        "nombre": "Mediación y evaluación formativa",
        "que_evalua": (
            "Que las estrategias de mediación y los criterios de "
            "evaluación formativa sean coherentes entre sí y con el "
            "propósito."
        ),
    },
    "C5": {
        "nombre": "Recursos y espacios",
        "que_evalua": (
            "Que los recursos y espacios educativos elegidos respondan a "
            "una decisión pedagógica, no a disponibilidad."
        ),
    },
}

# Denominación visible según la ficha técnica F4, sección 8. Los IDs
# B1-B5 se conservan solo en la capa de datos (campo "id" interno) -- el
# reporte visible al directivo nunca debe mostrar el código. RI2 reutiliza
# estas mismas denominaciones para "brechas que persisten desde V1"
# (Instrumento RI2, Parte F.4: "Traducir los identificadores a las
# denominaciones visibles validadas en el Reporte Institucional 1").
BRECHAS: dict[str, dict[str, str]] = {
    "B1": {
        "nombre": "Coherencia del diseño con la información del diagnóstico",
        "tipo": "Crítica",
        "refiere_a": (
            "la sesión no siempre se articula con la información del diagnóstico "
            "previo de los estudiantes (MPE/ENLA/evidencias de aula)."
        ),
    },
    "B2": {
        "nombre": "Participación de los estudiantes y nivel de exigencia de las actividades",
        "tipo": "Crítica",
        "refiere_a": (
            "las actividades ofrecen oportunidades limitadas para que los "
            "estudiantes analicen, decidan, argumenten o produzcan respuestas propias."
        ),
    },
    "B3": {
        "nombre": "Situaciones significativas",
        "tipo": "Estructural",
        "refiere_a": (
            "la situación propuesta no siempre presenta un contexto o desafío "
            "suficientemente significativo para movilizar el aprendizaje."
        ),
    },
    "B4": {
        "nombre": "Mediación y evaluación formativa",
        "tipo": "Estructural",
        "refiere_a": (
            "no siempre está prevista una relación clara entre las acciones de "
            "mediación, la evidencia que se recoge y las decisiones para "
            "acompañar o ajustar el aprendizaje."
        ),
    },
    "B5": {
        "nombre": "Uso pedagógico de recursos y espacios",
        "tipo": "De ajuste",
        "refiere_a": (
            "los recursos o espacios seleccionados no siempre tienen una función "
            "pedagógica claramente vinculada con el propósito."
        ),
    },
}

# Mapeo brecha -> criterio asociado, tal como lo codifica el CASE WHEN de
# la vista vw_processing_brechas_ebr (criterion_index=1->B1, 3->B2, 2->B3,
# 4->B4, 5->B5). RI1 lo usa implícitamente vía SQL; RI2 lo necesita en
# Python para "Parte F" (brechas que persisten de V1 a V2).
BRECHA_A_CRITERIO: dict[str, int] = {"B1": 1, "B2": 3, "B3": 2, "B4": 4, "B5": 5}

# =============================================================================
# Umbrales compartidos
# =============================================================================

# Mínimo de sesiones/comparaciones válidas para no usar lenguaje
# referencial/prudente (ficha técnica RI1, secciones 5 y 14; Ficha RI2,
# "Cuando una práctica efectiva se sustente en menos de cinco casos...").
UMBRAL_MUESTRA_INSUFICIENTE = 5

# Umbral de frecuencia institucional, reutilizado en dos reglas distintas
# que ambos documentos fijan en 30%: en RI1, inclusión en "Aspectos que se
# requieren analizar con el colegiado" (Especificaciones Funcionales §9 y
# §16 paso 6); en RI2, condición de "logro consolidado" (Instrumento RI2,
# Parte G). Antes se llamaba UMBRAL_NECESIDAD_FRECUENTE_PCT en core.py --
# renombrado acá porque deja de ser exclusivo de "necesidades".
UMBRAL_FRECUENCIA_PCT = 30

# =============================================================================
# Esquema de niveles de la rúbrica (4 niveles, 2 agrupados de lectura) --
# usado tal cual por RI1 (hoy de forma implícita, por campo) y de forma
# explícita por RI2 para clasificar tipo_cambio entre V1 y V2.
# =============================================================================

NIVELES_RUBRICA = ("inicio", "en_desarrollo", "logrado", "destacado")
NIVELES_ORDEN = {nivel: indice for indice, nivel in enumerate(NIVELES_RUBRICA)}
GRUPO_FAVORABLE = {"logrado", "destacado"}
