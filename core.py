"""
core.py

Lógica de negocio del asistente de reporte institucional.

Dos bases de datos, dos pools separados:
- pool_datos:  base con public."base_ebr_IA" y public.vw_processing_brechas_ebr
- pool_cache:  base con f4_reportes_directivo y f4_consultas_directivo
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import structlog
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from llm_client import generar_texto_narrativo
from comun import (
    CRITERIOS,
    BRECHAS,
    BRECHA_A_CRITERIO,
    UMBRAL_MUESTRA_INSUFICIENTE,
    UMBRAL_FRECUENCIA_PCT as UMBRAL_NECESIDAD_FRECUENTE_PCT,
    NIVELES_RUBRICA,
    NIVELES_ORDEN,
    GRUPO_FAVORABLE,
)


# =============================================================================
# Logging estructurado
# =============================================================================

def configurar_logging(nivel: str = "INFO") -> None:
    """Llamar UNA VEZ al arrancar la aplicación, antes de loguear nada."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, nivel.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, nivel.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger(__name__)


# =============================================================================
# Excepciones de negocio
# =============================================================================

class DirectivoNoEncontradoError(Exception):
    """El userid no resuelve a ninguna IE en base_ebr_IA."""


class SinDatosDisponiblesError(Exception):
    """No hay filas en vw_processing_brechas_ebr para ese cod_modular,
    o ningún curso de docentes tiene cmids configurados todavía."""


# =============================================================================
# Configuración de conexión
# =============================================================================

_KEEPALIVE_PARAMS = "keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=3"


def _con_keepalives(dsn: str) -> str:
    separador = "&" if "?" in dsn else "?"
    return f"{dsn}{separador}{_KEEPALIVE_PARAMS}"


DSN_DATOS = _con_keepalives(os.environ["DSN_DATOS"])
DSN_CACHE = _con_keepalives(os.environ["DSN_CACHE"])

_MAX_IDLE = int(os.environ.get("POOL_MAX_IDLE_SECONDS", "300"))

_POOL_KWARGS = dict(
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row},
    open=False,
    max_idle=_MAX_IDLE,
    check=AsyncConnectionPool.check_connection,
)

pool_datos = AsyncConnectionPool(conninfo=DSN_DATOS, **_POOL_KWARGS)
pool_cache = AsyncConnectionPool(conninfo=DSN_CACHE, **_POOL_KWARGS)


async def abrir_pools() -> None:
    """Llamar en el evento startup de FastAPI."""
    await pool_datos.open()
    await pool_cache.open()
    await _asegurar_schema_cache()


async def cerrar_pools() -> None:
    """Llamar en el evento shutdown de FastAPI."""
    await pool_datos.close()
    await pool_cache.close()


async def _asegurar_schema_cache() -> None:
    """Crea las tablas de caché si no existen, y migra las ya existentes.
    tipo_reporte se conserva (aunque este servicio solo emite RI2) por si
    en el futuro este mismo servicio también aloja RI3 -- decisión
    explícita del usuario, no sobra. La columna es VARCHAR nullable, sin
    DEFAULT en la base -- el valor "RI2" nunca lo pone un DEFAULT de SQL,
    siempre lo fija explícitamente cada función en Python (ver parámetro
    tipo_reporte de obtener_reporte_guardado/guardar_estado_reporte/
    registrar_consulta)."""
    ddl = """
        CREATE TABLE IF NOT EXISTS f4_reportes_directivo (
            id SERIAL PRIMARY KEY,
            cod_modular VARCHAR NOT NULL,
            tipo_reporte VARCHAR,
            estado VARCHAR NOT NULL DEFAULT 'generado',
            n_docentes_evaluados INTEGER NOT NULL,
            reporte_json JSONB,
            fecha_generacion TIMESTAMP NOT NULL DEFAULT now(),
            fecha_actualizacion TIMESTAMP NOT NULL DEFAULT now(),
            CONSTRAINT chk_estado CHECK (estado IN ('generado', 'muestra_insuficiente', 'sin_datos')),
            CONSTRAINT chk_reporte_json_si_generado
                CHECK (estado <> 'generado' OR reporte_json IS NOT NULL),
            CONSTRAINT uq_cod_modular_tipo_reporte UNIQUE (cod_modular, tipo_reporte)
        );

        CREATE TABLE IF NOT EXISTS f4_consultas_directivo (
            id SERIAL PRIMARY KEY,
            cod_modular VARCHAR NOT NULL,
            tipo_reporte VARCHAR,
            userid_directivo INTEGER NOT NULL,
            courseid INTEGER NOT NULL,
            cmid INTEGER NOT NULL,
            id_reporte_directivo INTEGER NOT NULL REFERENCES f4_reportes_directivo(id),
            fecha_consulta TIMESTAMP NOT NULL DEFAULT now(),
            CONSTRAINT uq_cod_modular_userid_tipo UNIQUE (cod_modular, userid_directivo, tipo_reporte)
        );

        CREATE INDEX IF NOT EXISTS idx_consultas_cod_modular ON f4_consultas_directivo (cod_modular);
        CREATE INDEX IF NOT EXISTS idx_consultas_userid ON f4_consultas_directivo (userid_directivo);
        CREATE INDEX IF NOT EXISTS idx_consultas_reporte ON f4_consultas_directivo (id_reporte_directivo);

        -- Migración idempotente para bases ya existentes: agrega la
        -- columna si falta y reemplaza el UNIQUE de un solo componente por
        -- el compuesto con tipo_reporte.
        ALTER TABLE f4_reportes_directivo ADD COLUMN IF NOT EXISTS tipo_reporte VARCHAR;
        ALTER TABLE f4_consultas_directivo ADD COLUMN IF NOT EXISTS tipo_reporte VARCHAR;
        ALTER TABLE f4_reportes_directivo DROP CONSTRAINT IF EXISTS f4_reportes_directivo_cod_modular_key;
        ALTER TABLE f4_consultas_directivo DROP CONSTRAINT IF EXISTS uq_cod_modular_userid;

        -- Postgres reporta "ya existe" para un constraint UNIQUE con
        -- duplicate_table (42P07, por el índice que lo respalda) y no con
        -- duplicate_object (42710) -- se capturan ambos códigos.
        DO $$ BEGIN
            ALTER TABLE f4_reportes_directivo
                ADD CONSTRAINT uq_cod_modular_tipo_reporte UNIQUE (cod_modular, tipo_reporte);
        EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL;
        END $$;

        DO $$ BEGIN
            ALTER TABLE f4_consultas_directivo
                ADD CONSTRAINT uq_cod_modular_userid_tipo UNIQUE (cod_modular, userid_directivo, tipo_reporte);
        EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL;
        END $$;
    """
    async with pool_cache.connection() as conn:
        await conn.execute(ddl)
        await conn.commit()


# =============================================================================
# Configuración de negocio
# =============================================================================
COLUMNAS_FILTRO_IE = ("nombre_ie", "ugel", '"región"', "distrito")
_SEPARADOR_CLAVE_IE = "|"


def _armar_clave_ie(nombre_ie: str, ugel: str, region: str, distrito: str) -> str:
    """Arma la clave compuesta que identifica una IE en el caché, a
    partir de los 4 valores ya resueltos desde la consulta SQL.

    ugel es necesario para desambiguar: existen pares reales de IEs con
    el mismo nombre_ie + región y distrito en NULL en ambos casos, pero
    ugel distinto (ej. dos "CESAR VALLEJO" en Apurímac, una en UGEL
    Chincheros y otra en UGEL Cotabambas) -- sin ugel, el sistema las
    trataría como una sola institución y mezclaría su evidencia."""
    return _SEPARADOR_CLAVE_IE.join([nombre_ie or "", ugel or "", region or "", distrito or ""])


# =============================================================================
# Referencia de criterios y brechas (CRITERIOS/BRECHAS viven en comun.py --
# compartidos con RI2, ver import al inicio del archivo)
# =============================================================================


def _nombre_criterio(criterio_id: str) -> str:
    """'C1' -> 'Coherencia entre propósito y desafíos'. Nunca antepone el
    código: el documento prohíbe mostrar C1-C5/B1-B5 en texto visible al
    directivo. Si el id no está en el diccionario (por ejemplo un criterio
    nuevo aún no documentado), retorna el id tal cual, sin romper el flujo."""
    info = CRITERIOS.get(criterio_id)
    return info["nombre"] if info else criterio_id


def _nombre_brecha(brecha_id: str) -> str:
    """'B4' -> 'Mediación y evaluación formativa' (denominación visible,
    sin el código -- ver _nombre_criterio)."""
    info = BRECHAS.get(brecha_id)
    return info["nombre"] if info else brecha_id

CARGOS_DOCENTE = ("DOCENTE", "JERARQUICO")

# Prioridad de las brechas (crítica>estructural>de ajuste) y de niveles (inicio>en_desarrollo)
PRIORIDAD_TIPO = {"Crítica": 3, "Estructural": 2, "De ajuste": 1}
PRIORIDAD_NIVEL = {"inicio": 2, "en_desarrollo": 1}
NIVELES_EDUCATIVOS = ("Inicial", "Primaria", "Secundaria")

# UMBRAL_MUESTRA_INSUFICIENTE y UMBRAL_NECESIDAD_FRECUENTE_PCT viven en
# comun.py (el segundo, renombrado a UMBRAL_FRECUENCIA_PCT, se reutiliza
# en RI2 -- ver import al inicio del archivo).
# Adenda F4-ADENDA-EMISION-RI-EBRJEC26 v1.0, sección 4 -- reemplaza al
# umbral fijo que se usaba antes (UMBRAL_COBERTURA_MINIMA_PCT = 35%): el
# piso absoluto (5) por sí solo es insuficiente en IEs grandes (5 de 100
# docentes es 5% de cobertura, poco representativo institucionalmente).
# La adenda establece bandas escalonadas -- el % exigido baja a medida
# que crece la IE (fundamento estadístico: margen ±10pp al 95% de
# confianza, con corrección por población finita), aunque el número
# absoluto de evidencias requeridas sigue subiendo.
# (N mínimo, N máximo inclusive o None=sin techo, código de banda, % exigido)
BANDAS_COBERTURA = (
    (5, 20, "B1", 85),
    (21, 35, "B2", 80),
    (36, 50, "B3", 70),
    (51, 70, "B4", 65),
    (71, None, "B5", 60),
)


def _clasificar_cobertura(n_evidencias_validas: int, n_docentes_total: int) -> dict:
    """Clasifica la cobertura de una IE según las bandas de la adenda
    (sección 4), retornando {"banda_aplicada", "umbral_requerido",
    "estado_cobertura"}.

    Si n_docentes_total < 5 (banda "B0"), no hay umbral numérico posible
    -- la adenda establece que ese caso siempre es COB_CONFIDENCIAL, aun
    con 100% de cobertura (RE-05, CP18): con tan pocos docentes, mostrar
    cualquier cifra agregada arriesga identificar el resultado de una
    persona puntual.

    Para el resto, umbral_requerido = máx(UMBRAL_MUESTRA_INSUFICIENTE;
    ⌈% de la banda × n_docentes_total⌉) (RE-02, redondeo siempre hacia
    arriba). estado_cobertura es COB_CONFIDENCIAL si no llega ni al piso
    absoluto de 5, COB_BAJO_UMBRAL si llega a 5 pero no al umbral de la
    banda, o COB_SUFICIENTE si alcanza el umbral (RE-03/04/05)."""
    if n_docentes_total < 5:
        return {
            "banda_aplicada": "B0",
            "umbral_requerido": None,
            "estado_cobertura": "COB_CONFIDENCIAL",
        }

    for n_min, n_max, codigo, pct in BANDAS_COBERTURA:
        if n_min <= n_docentes_total and (n_max is None or n_docentes_total <= n_max):
            banda_aplicada = codigo
            umbral_requerido = max(
                UMBRAL_MUESTRA_INSUFICIENTE,
                math.ceil(pct / 100 * n_docentes_total),
            )
            break

    if n_evidencias_validas < UMBRAL_MUESTRA_INSUFICIENTE:
        estado_cobertura = "COB_CONFIDENCIAL"
    elif n_evidencias_validas < umbral_requerido:
        estado_cobertura = "COB_BAJO_UMBRAL"
    else:
        estado_cobertura = "COB_SUFICIENTE"

    return {
        "banda_aplicada": banda_aplicada,
        "umbral_requerido": umbral_requerido,
        "estado_cobertura": estado_cobertura,
    }

# Cmids de docentes por curso, para cada versión de la Sesión de
# aprendizaje que RI2 compara. V1 es la línea base (Evidencia 1, ya
# evaluada); V2 es la versión a comparar (Evidencia 3). Mismo curso,
# cmid distinto -- son dos actividades separadas de Moodle dentro del
# mismo curso. Sin gestor en base de datos -- decisión explícita: este
# servicio es puro RI2 de EBR, los cmids viven hardcodeados acá.
CMIDS_V1_POR_CURSO: dict[int, list[int] | None] = {
    2918: [206189],
    2926: [207613],
    2931: [207631],
    2934: [207649],
    2937: [207667],
    2940: [207685],
    2943: [207703],
    2946: [207721],
    2949: [207739],
    2952: [207757],
    2955: [207775],
    2958: [207793],
    2961: [207811],
    2964: [207829],
    2967: [207847],
    2970: [207865],
    2973: [207883],
    2976: [207901],
    2979: [207919],
    2982: [207937],
    2985: [207955],
    2988: [207973],
    2991: [207991],
    2994: [208010],
}

# Pendiente de completar: 2918-2967 (15 de 24) están confirmados contra
# datos reales. 2970-2994 quedan en None hasta confirmarse -- mejor
# esperar a tener el cmid real que arriesgar una IE con un cmid de V2
# incorrecto y silenciosamente sin datos.
CMIDS_V2_POR_CURSO: dict[int, list[int] | None] = {
    2918: [206728],
    2926: [216633],
    2931: [216644],
    2934: [216654],
    2937: [216664],
    2940: [216674],
    2943: [216684],
    2946: [216694],
    2949: [216704],
    2952: [216714],
    2955: [216724],
    2958: [216734],
    2961: [216744],
    2964: [216754],
    2967: [216764],
    2970: None,
    2973: None,
    2976: None,
    2979: None,
    2982: None,
    2985: None,
    2988: None,
    2991: None,
    2994: None,
}


def _cmids_docentes_configurados(version_sesion: str) -> list[int]:
    """Aplana todos los cmids de cursos que YA tienen configuración para
    la versión de sesión dada ('V1' o 'V2')."""
    fuente = CMIDS_V1_POR_CURSO if version_sesion == "V1" else CMIDS_V2_POR_CURSO
    return [
        cmid
        for cmids in fuente.values()
        if cmids is not None
        for cmid in cmids
    ]


# =============================================================================
# Acceso a datos
# =============================================================================

async def resolver_cod_modular(userid: int) -> dict:
    """
    Busca la IE del directivo por userid, trayendo las 4 columnas del
    filtro compuesto (nombre_ie, ugel, región, distrito). Retorna
    {"cod_modular": <clave compuesta>, "nombre_ie": ..., "ugel": ...,
    "region": ..., "distrito": ...} -- el campo "cod_modular" en el
    diccionario de retorno se mantiene por compatibilidad con el resto
    del pipeline (caché, logs, registrar_consulta), aunque ya no es la
    columna cod_modular de la base sino la clave compuesta armada con
    _armar_clave_ie.
    """
    query = """
        SELECT nombre_ie, ugel, "región" AS region, distrito
        FROM public."base_ebr_IA"
        WHERE userid = %(userid)s
        LIMIT 1
    """
    async with pool_datos.connection() as conn:
        row = await (await conn.execute(query, {"userid": userid})).fetchone()

    if row is None:
        log.warning("directivo_no_encontrado", userid=userid)
        raise DirectivoNoEncontradoError(f"userid {userid} no tiene IE asociada")

    clave_ie = _armar_clave_ie(row["nombre_ie"], row["ugel"], row["region"], row["distrito"])

    log.info(
        "cod_modular_resuelto",
        userid=userid,
        cod_modular=clave_ie,
        nombre_ie=row["nombre_ie"],
        ugel=row["ugel"],
        region=row["region"],
        distrito=row["distrito"],
    )
    return {
        "cod_modular": clave_ie,
        "nombre_ie": row["nombre_ie"],
        "ugel": row["ugel"],
        "region": row["region"],
        "distrito": row["distrito"],
    }


async def contar_docentes_total(nombre_ie: str, ugel: str, region: str, distrito: str, nivel_educativo: Optional[str] = None) -> int:
    """Cuenta docentes+jerárquicos en base_ebr_IA para esta IE, filtrando
    por las 4 columnas del filtro compuesto (nombre_ie, ugel, región,
    distrito) por separado -- no concatenadas -- para que Postgres pueda
    usar índices existentes sobre esas columnas. Si se pasa
    nivel_educativo, filtra además por ese nivel (Secundaria/Primaria/
    Inicial); si es None, cuenta los 3 niveles juntos."""
    query = """
        SELECT COUNT(*) AS total
        FROM public."base_ebr_IA"
        WHERE nombre_ie = %(nombre_ie)s
          AND ugel IS NOT DISTINCT FROM %(ugel)s
          AND "región" = %(region)s
          AND distrito IS NOT DISTINCT FROM %(distrito)s
          AND UPPER(cargo) = ANY(%(cargos)s)
          AND (%(nivel_educativo)s::text IS NULL OR nivel_educativo = %(nivel_educativo)s::text)
    """
    async with pool_datos.connection() as conn:
        row = await (await conn.execute(
            query,
            {
                "nombre_ie": nombre_ie,
                "ugel": ugel,
                "region": region,
                "distrito": distrito,
                "cargos": list(CARGOS_DOCENTE),
                "nivel_educativo": nivel_educativo,
            },
        )).fetchone()

    return row["total"] if row else 0


async def obtener_filas_vista(
    nombre_ie: str, ugel: str, region: str, distrito: str,
    nivel_educativo: Optional[str] = None, version_sesion: str = "V1",
) -> list[dict]:
    """Trae todas las filas de la vista, sin filtrar por status -- incluye
    'success' (válida), 'validation_failed' (no válida) y 'failed' (sin
    evidencia real, se trata como sin entrega). El llamador filtra según lo
    que necesite (ver filas_validas). Filtro compuesto por las 4 columnas,
    igual que en base_ebr_IA. Si se pasa nivel_educativo, filtra además
    por ese nivel; si es None, trae los 3 niveles juntos. version_sesion
    ('V1'/'V2') determina qué cmids se usan -- V1 y V2 son actividades
    distintas del mismo curso."""
    cmids_validos = _cmids_docentes_configurados(version_sesion)

    if not cmids_validos:
        log.error("sin_cmids_configurados")
        raise SinDatosDisponiblesError(
            "Ningún curso de docentes tiene cmids válidos configurados todavía"
        )

    query = """
        SELECT user_id, criterion_index, nivel_obtenido, brecha, tipo_brecha, status,
               nivel_educativo, retroalimentacion
        FROM public.vw_processing_brechas_ebr
        WHERE nombre_ie = %(nombre_ie)s
          AND ugel IS NOT DISTINCT FROM %(ugel)s
          AND "región" = %(region)s
          AND distrito IS NOT DISTINCT FROM %(distrito)s
          AND cmid = ANY(%(cmids_validos)s)
          AND (%(nivel_educativo)s::text IS NULL OR nivel_educativo = %(nivel_educativo)s::text)
        ORDER BY user_id, criterion_index
    """
    async with pool_datos.connection() as conn:
        rows = await (await conn.execute(
            query,
            {
                "nombre_ie": nombre_ie,
                "ugel": ugel,
                "region": region,
                "distrito": distrito,
                "cmids_validos": cmids_validos,
                "nivel_educativo": nivel_educativo,
            },
        )).fetchall()

    _normalizar_nivel_obtenido(rows)

    log.info(
        "filas_vista_obtenidas",
        nombre_ie=nombre_ie, ugel=ugel, region=region, distrito=distrito,
        nivel_educativo=nivel_educativo, n_filas=len(rows),
    )
    return rows


# La vista en producción todavía devuelve "en_proceso" como valor de
# nivel_obtenido, aunque el resto del pipeline (F4, ficha técnica) usa la
# denominación de 4 niveles con "en_desarrollo". Se normaliza acá, en el
# único punto donde se leen las filas crudas, para que el resto del
# código (distribución, prioridad de brechas) solo vea el nombre
# canónico.
_ALIAS_NIVEL_OBTENIDO = {"en_proceso": "en_desarrollo"}


def _normalizar_nivel_obtenido(rows: list[dict]) -> None:
    for row in rows:
        row["nivel_obtenido"] = _ALIAS_NIVEL_OBTENIDO.get(row["nivel_obtenido"], row["nivel_obtenido"])


async def contar_docentes_evaluados(
    nombre_ie: str, ugel: str, region: str, distrito: str,
    nivel_educativo: Optional[str] = None, version_sesion: str = "V1",
) -> int:
    """
    Cuenta cuántos docentes DISTINTOS tienen evidencia 'success' completa
    (todas sus filas con criterion_index no nulo) para esta IE, sin traer
    las filas completas. Más liviana que obtener_filas_vista +
    agrupar_por_docente -- usada por el endpoint de consulta (que debe
    ser rápido) para decidir la acción sin pagar el costo de traer todo
    el detalle por criterio. Si nivel_educativo es None, cuenta los 3
    niveles juntos (usado para el mínimo de activación de
    UMBRAL_MUESTRA_INSUFICIENTE sobre la IE completa). version_sesion
    ('V1'/'V2') determina qué cmids se usan.

    Un docente con alguna fila 'success' sin criterion_index (carga de F2
    todavía en curso) NO cuenta como evaluado -- misma regla que
    _excluir_docentes_con_registro_incompleto usa al generar el reporte,
    para que el conteo que decide "hay novedades, regenerar" y el
    contenido real del reporte nunca queden desalineados.
    """
    cmids_validos = _cmids_docentes_configurados(version_sesion)
    if not cmids_validos:
        return 0

    query = """
        WITH filas AS (
            SELECT user_id, criterion_index, nivel_obtenido, nivel_educativo
            FROM public.vw_processing_brechas_ebr
            WHERE nombre_ie = %(nombre_ie)s
              AND ugel IS NOT DISTINCT FROM %(ugel)s
              AND "región" = %(region)s
              AND distrito IS NOT DISTINCT FROM %(distrito)s
              AND cmid = ANY(%(cmids_validos)s)
              AND status = 'success'
              AND (%(nivel_educativo)s::text IS NULL OR nivel_educativo = %(nivel_educativo)s::text)
        )
        SELECT COUNT(DISTINCT user_id) AS total
        FROM filas
        WHERE user_id NOT IN (
            SELECT user_id FROM filas
            WHERE criterion_index IS NULL OR nivel_obtenido IS NULL OR nivel_educativo IS NULL
        )
    """
    async with pool_datos.connection() as conn:
        row = await (await conn.execute(
            query,
            {
                "nombre_ie": nombre_ie,
                "ugel": ugel,
                "region": region,
                "distrito": distrito,
                "cmids_validos": cmids_validos,
                "nivel_educativo": nivel_educativo,
            },
        )).fetchone()

    return row["total"] if row else 0


# =============================================================================
# Reglas de negocio
# =============================================================================

def agrupar_por_docente(filas: list[dict]) -> dict[int, list[dict]]:
    """Agrupa las filas crudas de la vista por user_id (docente)."""
    agrupado: dict[int, list[dict]] = {}
    for fila in filas:
        agrupado.setdefault(fila["user_id"], []).append(fila)
    return agrupado


def filas_validas(filas: list[dict]) -> list[dict]:
    """Filtra solo las filas con evidencia válida (status='success').
    Punto de uso obligatorio antes de cualquier cálculo de desempeño
    (distribución por aspecto, brechas, fortalezas): esos cálculos nunca
    deben incluir evidencia no válida o sin entrega real."""
    return [f for f in filas if f["status"] == "success"]


_COLUMNAS_REQUERIDAS_SI_SUCCESS = ("criterion_index", "nivel_obtenido", "nivel_educativo")


def _excluir_docentes_con_registro_incompleto(filas: list[dict]) -> list[dict]:
    """Si un docente tiene alguna fila 'success' con alguna columna clave
    en NULL (criterion_index, nivel_obtenido o nivel_educativo), su carga
    en la vista todavía está en curso (F2 parece insertar la fila del
    docente antes de completarla) -- se excluye al docente COMPLETO de
    este cálculo, no solo la fila incompleta. Contar solo sus otros
    aspectos ya cargados produciría un 'n de N' distinto entre secciones
    para el mismo docente (o, en el caso de nivel_educativo, haría que
    esa fila desaparezca silenciosamente de la sección de lectura por
    nivel sin que el resto del reporte se entere). En el siguiente
    refresco de caché, cuando la carga termine, vuelve a contarse con
    normalidad."""
    por_docente = agrupar_por_docente(filas)
    incompletos = {
        user_id
        for user_id, filas_docente in por_docente.items()
        if any(
            f["status"] == "success" and f.get(col) is None
            for f in filas_docente
            for col in _COLUMNAS_REQUERIDAS_SI_SUCCESS
        )
    }
    if incompletos:
        log.warning("docentes_con_registro_incompleto_excluidos", user_ids=sorted(incompletos))
    return [f for f in filas if f["user_id"] not in incompletos]


# Tope de retroalimentaciones que se envían al prompt -- evita un prompt
# desmedido en IEs con muchos docentes; una muestra representativa basta
# para que el LLM identifique temas recurrentes.
MAX_RETROALIMENTACIONES_EN_PROMPT = 40


def _extraer_retroalimentaciones(filas_por_docente: dict[int, list[dict]]) -> list[str]:
    """
    Extrae el texto de retroalimentación de F2, una vez por docente (la
    columna 'retroalimentacion' viene repetida en las 5 filas -- una por
    criterio -- de cada docente). Se pasa como insumo agregado y sin
    identificar al docente al prompt que redacta las preguntas
    movilizadoras de la RTC 2. Se capa a MAX_RETROALIMENTACIONES_EN_PROMPT
    para no desmedir el prompt en IEs con muchos docentes.
    """
    vistas = []
    for filas_docente in filas_por_docente.values():
        texto = (filas_docente[0].get("retroalimentacion") or "").strip()
        if texto:
            vistas.append(texto)
    return vistas[:MAX_RETROALIMENTACIONES_EN_PROMPT]


def seleccionar_brechas_globales(filas_docente: list[dict]) -> list[str]:
    """
    5 reglas de prioridad para reducir las brechas de un docente a máximo 2:
      1. Crítica > Estructural > De ajuste.
      2. Dentro del mismo tipo, Inicio > En proceso.
      3. Empate total -> gana el criterion_index más bajo.
      4. Máximo 2 brechas.
      5. Si ninguna es Crítica ni Estructural, no se selecciona ninguna
         (no se fuerza una de ajuste solo para completar el cupo).
    """
    candidatas = [f for f in filas_docente if f.get("brecha") is not None]
    if not candidatas:
        return []

    hay_critica_o_estructural = any(
        f["tipo_brecha"] in ("Crítica", "Estructural") for f in candidatas
    )
    if not hay_critica_o_estructural:
        return []

    candidatas_ordenadas = sorted(
        candidatas,
        key=lambda f: (
            -PRIORIDAD_TIPO.get(f["tipo_brecha"], 0),
            -PRIORIDAD_NIVEL.get(f["nivel_obtenido"], 0),
            f["criterion_index"] if f["criterion_index"] is not None else 999,
        ),
    )
    return [f["brecha"] for f in candidatas_ordenadas[:2]]


def calcular_distribucion_por_criterio(filas: list[dict]) -> list[dict]:
    """Distribución de las filas VÁLIDAS por aspecto (criterion_index), en
    los 4 niveles de la rúbrica (inicio, en_desarrollo, logrado, destacado)
    más los dos agrupados de lectura del documento: "Inicio + En
    desarrollo" (requiere fortalecimiento) y "Logrado + Destacado"
    (desempeño favorable). El llamador es responsable de pasar solo filas
    válidas (ver filas_validas)."""
    por_criterio: dict[int, dict[str, int]] = {}

    for fila in filas:
        c = fila["criterion_index"]
        if c is None:
            # Fila 'success' pero sin criterion_index -- registro
            # incompleto en la vista de origen. Se descarta (no se puede
            # atribuir a ningún aspecto) en vez de romper el ordenamiento
            # o distorsionar los conteos.
            log.warning("fila_sin_criterion_index_descartada", user_id=fila.get("user_id"))
            continue
        nivel = fila["nivel_obtenido"]
        conteos = por_criterio.setdefault(
            c, {"inicio": 0, "en_desarrollo": 0, "logrado": 0, "destacado": 0}
        )
        if nivel in conteos:
            conteos[nivel] += 1

    resultado = []
    for criterio_id, conteos in sorted(por_criterio.items()):
        total = sum(conteos.values())
        if total == 0:
            continue
        pct_inicio = round(100 * conteos["inicio"] / total)
        pct_en_desarrollo = round(100 * conteos["en_desarrollo"] / total)
        pct_logrado = round(100 * conteos["logrado"] / total)
        pct_destacado = round(100 * conteos["destacado"] / total)
        resultado.append({
            "criterio_id": f"C{criterio_id}",
            "n": total,
            # Conteos crudos (no derivados de porcentajes redondeados) --
            # necesarios para poder citar "[n] de [N]" con el número
            # EXACTO de sesiones en el agrupado predominante, sin que el
            # LLM tenga que reconstruirlo a partir del %.
            "n_inicio": conteos["inicio"],
            "n_en_desarrollo": conteos["en_desarrollo"],
            "n_logrado": conteos["logrado"],
            "n_destacado": conteos["destacado"],
            "pct_inicio": pct_inicio,
            "pct_en_desarrollo": pct_en_desarrollo,
            "pct_logrado": pct_logrado,
            "pct_destacado": pct_destacado,
            "pct_inicio_en_desarrollo": pct_inicio + pct_en_desarrollo,
            "pct_logrado_destacado": pct_logrado + pct_destacado,
        })
    return resultado


def _primera_oracion_interpretacion(c: dict) -> str:
    """
    Arma en Python -- con el conteo EXACTO, no derivado de un porcentaje
    redondeado -- la primera oración de la interpretación de un aspecto
    (sección 3): "En [n] de [N] sesiones ([pct]%) se observa [resultado]."
    El LLM NUNCA escribe esta oración (ni sus números): solo redacta la
    explicación pedagógica y la fuente que van después. Así se elimina de
    raíz el riesgo de que el modelo invente o desalinee una cifra frente
    a la tabla de la sección 2 -- no hay instrucción de prompt que pueda
    garantizar esto de forma confiable, solo no dejarlo en sus manos.
    """
    n_total = c["n"]
    if c["pct_logrado_destacado"] >= c["pct_inicio_en_desarrollo"]:
        n_grupo = c["n_logrado"] + c["n_destacado"]
        pct_grupo = c["pct_logrado_destacado"]
        resultado = "Logrado o Destacado"
    else:
        n_grupo = c["n_inicio"] + c["n_en_desarrollo"]
        pct_grupo = c["pct_inicio_en_desarrollo"]
        resultado = "Inicio o En desarrollo"
    return f"En {n_grupo} de {n_total} sesiones ({pct_grupo}%) se observa {resultado}."


def calcular_frecuencia_brechas(brechas_por_docente: dict[int, list[str]]) -> dict[str, dict]:
    n_docentes = len(brechas_por_docente)
    if n_docentes == 0:
        return {}

    conteos: dict[str, int] = {}
    for lista_brechas in brechas_por_docente.values():
        for brecha_id in lista_brechas:
            conteos[brecha_id] = conteos.get(brecha_id, 0) + 1

    return {
        brecha_id: {"conteo": conteo, "pct": round(100 * conteo / n_docentes)}
        for brecha_id, conteo in conteos.items()
    }




def calcular_enfasis_lectura(distribucion_por_id: dict[str, dict]) -> list[dict]:
    """Énfasis del programa (ficha técnica, sección 7): se leen a partir
    de los aspectos ya evaluados por F2, sin crear indicadores que F2 no
    genera. Comprensión lectora no tiene indicador independiente en la
    rúbrica de la Sesión V1 -- su texto es fijo, tal como exige el
    documento (no se genera un % institucional para ese énfasis)."""
    enfasis = []
    c3 = distribucion_por_id.get("C3")

    if c3:
        favorable = c3["pct_logrado_destacado"] >= c3["pct_inicio_en_desarrollo"]
        texto_involucramiento = (
            f"Las actividades previstas ofrecen oportunidades favorables para participar, "
            f"analizar, decidir y producir: {c3['pct_logrado_destacado']}% de las sesiones "
            "revisadas se ubica en Logrado o Destacado en demanda cognitiva e involucramiento "
            "activo."
            if favorable else
            f"En {c3['pct_inicio_en_desarrollo']}% de las sesiones revisadas, la demanda "
            "cognitiva y el involucramiento activo de los estudiantes todavía requiere mayor "
            "fortalecimiento."
        )
        enfasis.append({
            "enfasis": "Involucramiento activo",
            "aspecto_relacionado": "C3",
            "texto_permitido": texto_involucramiento,
        })
        enfasis.append({
            "enfasis": "Pensamiento crítico",
            "aspecto_relacionado": "C2/C3",
            "texto_permitido": (
                "La planificación de una parte de las sesiones revisadas puede ofrecer mayores "
                "oportunidades para analizar, interpretar, tomar decisiones, argumentar y "
                "elaborar respuestas propias."
            ),
        })

    enfasis.append({
        "enfasis": "Comprensión lectora",
        "aspecto_relacionado": None,
        "texto_permitido": (
            "Este reporte no presenta un porcentaje institucional de comprensión lectora "
            "porque la rúbrica de la Sesión V1 no la evalúa como un criterio independiente. "
            "Su análisis debe complementarse con evidencias específicas de aprendizaje de los "
            "estudiantes."
        ),
    })
    enfasis.append({
        # Denominación oficial de la Ficha Técnica / Especificaciones §7
        # ("Resolución de problemas"). Producción venía usando
        # "Aprendizaje basado en situaciones y problemas" -- corregido
        # por indicación directa del usuario.
        "enfasis": "Resolución de problemas",
        "aspecto_relacionado": "C2/C3",
        "texto_permitido": (
            "Las situaciones significativas y la demanda cognitiva son aspectos relevantes "
            "para analizar cómo se están generando oportunidades de aprendizaje profundo; no "
            "se genera un porcentaje independiente para este énfasis."
        ),
    })

    return enfasis


def calcular_lectura_por_nivel(filas_validas_ie: list[dict]) -> list[dict]:
    """Sección 7 del reporte -- lectura diferenciada por nivel educativo,
    a partir de las filas VÁLIDAS ya traídas para la IE completa (con
    nivel_educativo incluido, ver obtener_filas_vista). Solo incluye los
    niveles presentes, en el orden fijo NIVELES_EDUCATIVOS. No establece
    ranking entre niveles (ficha técnica, sección 13)."""
    por_nivel: dict[str, list[dict]] = {}
    for fila in filas_validas_ie:
        por_nivel.setdefault(fila["nivel_educativo"], []).append(fila)

    lectura = []
    for nivel in NIVELES_EDUCATIVOS:
        filas_nivel = por_nivel.get(nivel)
        if not filas_nivel:
            continue
        n = len(agrupar_por_docente(filas_nivel))
        lectura.append({
            "nivel_educativo": nivel,
            "n": n,
            "muestra_insuficiente": n < UMBRAL_MUESTRA_INSUFICIENTE,
            "distribucion_por_aspecto": _enriquecer_distribucion_con_nombre(
                calcular_distribucion_por_criterio(filas_nivel)
            ),
        })
    return lectura


FUENTES_CONTRASTE_POR_ASPECTO = {
    "C1": "MPE y otras evidencias de planificación",
    "C2": "MPE, ENLA y evidencias de aprendizaje de los estudiantes",
    "C3": "MPE, ENLA y evidencias de aprendizaje de los estudiantes",
    "C4": "MPE, instrumentos de evaluación y producciones de los estudiantes",
    "C5": "MPE y evidencias de aula",
}

PREGUNTAS_CONTRASTE_POR_ASPECTO = {
    "C1": "¿Qué decisiones de planificación pueden mantenerse y compartirse para fortalecer otros aspectos?",
    "C2": "¿Qué relación encontramos entre las oportunidades que brindan las experiencias de aprendizaje y lo que muestran los estudiantes en sus aprendizajes?",
    "C3": "¿Lo previsto en las sesiones se corresponde con lo que ocurre durante la interacción en el aula?",
    "C4": "¿Cómo se recoge y utiliza la información sobre el aprendizaje para ajustar la enseñanza?",
    "C5": "¿Los recursos y espacios previstos se aprovechan efectivamente durante la sesión?",
}


def calcular_aspectos_contraste(distribucion: list[dict], fortalezas: list[dict]) -> list[dict]:
    """Sección 8 -- 'Lo que conviene contrastar con otras evidencias'
    (banco de redacción, punto 12, plantilla 'Contraste'). Se arma en
    Python, sin LLM: toma los aspectos con mayor % en Inicio+En desarrollo
    (necesitan fortalecimiento) y la principal fortaleza, y les asocia la
    fuente y pregunta de contraste correspondientes."""
    aspectos = []

    ordenados = sorted(distribucion, key=lambda c: -c["pct_inicio_en_desarrollo"])
    for c in ordenados[:2]:
        if c["pct_inicio_en_desarrollo"] <= 0:
            continue
        cid = c["criterio_id"]
        aspectos.append({
            "hallazgo": (
                f"{_nombre_criterio(cid)}: {c['pct_inicio_en_desarrollo']}% de las sesiones "
                "revisadas requiere mayor fortalecimiento."
            ),
            "fuente_sugerida": FUENTES_CONTRASTE_POR_ASPECTO.get(cid, "MPE y ENLA"),
            "pregunta": PREGUNTAS_CONTRASTE_POR_ASPECTO.get(
                cid, "¿Qué conviene revisar para comprender mejor esta situación?"
            ),
        })

    if fortalezas:
        top = max(fortalezas, key=lambda f: f["pct"])
        cid = top["id"]
        aspectos.append({
            "hallazgo": f"{_nombre_criterio(cid)} aparece como una fortaleza ({top['pct']}% en Logrado o Destacado).",
            "fuente_sugerida": FUENTES_CONTRASTE_POR_ASPECTO.get(cid, "MPE"),
            "pregunta": PREGUNTAS_CONTRASTE_POR_ASPECTO.get(
                cid, "¿Qué decisiones de planificación conviene preservar?"
            ),
        })

    return aspectos


# Preguntas movilizadoras para la RTC. El camino normal es que el LLM las
# genere a partir de las retroalimentaciones reales de los docentes (ver
# _construir_prompt_secciones_narrativas_ri2 / campo "retroalimentacion"
# de la vista) -- esta lista fija es solo el fallback si el LLM falla dos
# veces seguidas (ver _secciones_narrativas_fallback_ri2).
PREGUNTAS_RTC_FIJAS = (
    "¿Qué información de este reporte se confirma cuando la contrastamos con MPE, ENLA y las "
    "evidencias de aprendizaje de nuestros estudiantes?",
    "¿Qué oportunidades estamos ofreciendo actualmente a los estudiantes para comprender, "
    "analizar, argumentar, tomar decisiones y producir respuestas propias?",
    "¿Qué diferencias encontramos entre lo que se planifica en las sesiones y lo que "
    "observamos en la práctica real de aula?",
    "¿Qué decisiones de planificación que ya están funcionando favorablemente podemos "
    "aprovechar para fortalecer otros aspectos?",
    "¿Qué información adicional necesitamos revisar antes de definir la necesidad que la "
    "institución debe priorizar?",
)


# =============================================================================
# Redacción del texto narrativo (LLM)
# =============================================================================

def _enriquecer_distribucion_con_nombre(distribucion: list[dict]) -> list[dict]:
    return [{**c, "nombre": _nombre_criterio(c["criterio_id"])} for c in distribucion]


def _enriquecer_necesidades_con_nombre(necesidades: list[dict]) -> list[dict]:
    return [{**n, "nombre": _nombre_brecha(n["id"])} for n in necesidades]


# =============================================================================
# Caché
# =============================================================================

async def obtener_reporte_guardado(cod_modular: str, tipo_reporte: str = "RI2") -> Optional[dict]:
    """Trae la fila de f4_reportes_directivo para esta IE y tipo de
    reporte, sin importar el estado (generado, muestra_insuficiente,
    sin_datos) -- o None si esta combinación nunca tuvo ni un intento de
    generación. tipo_reporte se conserva por si en el futuro este mismo
    servicio también aloja RI3; hoy el único valor real es 'RI2'."""
    query = """
        SELECT id, cod_modular, tipo_reporte, estado, n_docentes_evaluados, reporte_json,
               fecha_generacion, fecha_actualizacion
        FROM f4_reportes_directivo
        WHERE cod_modular = %(cod_modular)s AND tipo_reporte = %(tipo_reporte)s
    """
    async with pool_cache.connection() as conn:
        return await (await conn.execute(query, {"cod_modular": cod_modular, "tipo_reporte": tipo_reporte})).fetchone()


async def guardar_estado_reporte(
    cod_modular: str,
    estado: str,
    n_docentes_evaluados: int,
    reporte_json: Optional[dict] = None,
    tipo_reporte: str = "RI2",
) -> int:
    """
    Upsert de la fila de f4_reportes_directivo para esta IE y tipo de
    reporte. estado es uno de 'generado', 'muestra_insuficiente',
    'sin_datos'. reporte_json solo se pasa (y solo debe pasarse) cuando
    estado='generado'.

    Como hay una sola fila por (cod_modular, tipo_reporte), esto cubre
    tanto el primer intento (exitoso o no) como los siguientes -- INSERT
    si es la primera vez que se toca esta combinación, UPDATE si ya
    existía una fila (sin importar cuál era su estado anterior).
    """
    query = """
        INSERT INTO f4_reportes_directivo (cod_modular, tipo_reporte, estado, n_docentes_evaluados, reporte_json)
        VALUES (%(cod_modular)s, %(tipo_reporte)s, %(estado)s, %(n)s, %(reporte)s)
        ON CONFLICT (cod_modular, tipo_reporte) DO UPDATE SET
            estado = EXCLUDED.estado,
            n_docentes_evaluados = EXCLUDED.n_docentes_evaluados,
            reporte_json = COALESCE(EXCLUDED.reporte_json, f4_reportes_directivo.reporte_json),
            fecha_actualizacion = now()
        RETURNING id
    """
    async with pool_cache.connection() as conn:
        row = await (await conn.execute(query, {
            "cod_modular": cod_modular,
            "tipo_reporte": tipo_reporte,
            "estado": estado,
            "n": n_docentes_evaluados,
            "reporte": Jsonb(reporte_json) if reporte_json is not None else None,
        })).fetchone()
        await conn.commit()

    return row["id"]


async def registrar_consulta(
    cod_modular: str, userid_directivo: int, courseid: int, cmid: int, id_reporte_directivo: int,
    tipo_reporte: str = "RI2",
) -> None:
    """Asocia un directivo con el reporte de su IE para un tipo de
    reporte dado. Un registro único por (cod_modular, userid_directivo,
    tipo_reporte) -- cada consulta posterior actualiza fecha_consulta (y
    courseid/cmid/id_reporte_directivo, por si cambian) en vez de
    ignorarse, para que la columna refleje la ÚLTIMA consulta real y no
    solo la primera."""
    query = """
        INSERT INTO f4_consultas_directivo
            (cod_modular, tipo_reporte, userid_directivo, courseid, cmid, id_reporte_directivo)
        VALUES (%(cod_modular)s, %(tipo_reporte)s, %(userid)s, %(courseid)s, %(cmid)s, %(id_reporte)s)
        ON CONFLICT (cod_modular, userid_directivo, tipo_reporte) DO UPDATE SET
            courseid = EXCLUDED.courseid,
            cmid = EXCLUDED.cmid,
            id_reporte_directivo = EXCLUDED.id_reporte_directivo,
            fecha_consulta = now()
    """
    async with pool_cache.connection() as conn:
        await conn.execute(query, {
            "cod_modular": cod_modular,
            "tipo_reporte": tipo_reporte,
            "userid": userid_directivo,
            "courseid": courseid,
            "cmid": cmid,
            "id_reporte": id_reporte_directivo,
        })
        await conn.commit()


# =============================================================================
# Puntos de entrada
# =============================================================================

def _debe_esperar_mas_docentes(n_actual: int, n_total: int) -> bool:
    """Decide si /reporte/consultar debe indicarle al frontend que espere
    más docentes evaluados en vez de generar el reporte ya.

    Delega en _clasificar_cobertura (adenda, sección 4): espera mientras
    no se alcance COB_SUFICIENTE.

    RE-03/RE-04 de la adenda: tanto COB_SUFICIENTE como COB_BAJO_UMBRAL se
    emiten ya (este último con la advertencia obligatoria de la sección
    8.1, ver _construir_advertencia_cobertura_ri2) -- solo COB_CONFIDENCIAL
    sigue esperando, porque su tratamiento (sección 8.3, camino reducido
    sin cifras) todavía no está implementado.

    Excepción dentro de COB_CONFIDENCIAL: la banda "B0" (n_total < 5)
    nunca sale de ese estado -- no tiene umbral numérico definido
    (RE-05/CP18). Se conserva el comportamiento anterior: una vez
    evaluada TODA la plana docente, no tiene sentido seguir esperando."""
    estado = _clasificar_cobertura(n_actual, n_total)["estado_cobertura"]
    if estado != "COB_CONFIDENCIAL":
        return False
    return not (n_total > 0 and n_actual >= n_total)


# =============================================================================
# RI2 -- Reporte Institucional 2 (comparación V1→V2). Este servicio es
# exclusivamente RI2: no hay un "RI1" en este proyecto (vive en otro
# despliegue, independiente de este). V1 se lee únicamente como línea
# base para la comparación -- nunca se emite un reporte de V1 por sí
# sola acá.
#
# Reutiliza funciones genéricas del módulo (comparten el mismo esquema
# de niveles/criterios/brechas que la vista entrega para cualquier
# versión de sesión): PRIORIDAD_TIPO, agrupar_por_docente, filas_validas,
# seleccionar_brechas_globales, calcular_distribucion_por_criterio,
# calcular_frecuencia_brechas, calcular_enfasis_lectura,
# calcular_lectura_por_nivel, calcular_aspectos_contraste. De comun.py:
# NIVELES_ORDEN, GRUPO_FAVORABLE, BRECHA_A_CRITERIO,
# UMBRAL_NECESIDAD_FRECUENTE_PCT (= UMBRAL_FRECUENCIA_PCT),
# UMBRAL_MUESTRA_INSUFICIENTE.
#
# tipo_cambio se calcula SOLO por variación de niveles (nivel_v1 vs
# nivel_v2) -- decisión explícita: la Ficha de Verificación original de
# F2 pide comparar el contenido descriptivo de V1/V2 para distinguir
# mejora_real de mejora_formal, pero eso exigiría que un LLM juzgue
# similitud semántica de texto, rompiendo el principio del proyecto de
# que el LLM nunca clasifica -- solo redacta prosa a partir de cifras ya
# resueltas por Python. BC2 (depende de "referencia explícita a
# evidencia de aula") y progresion_global_referencial quedan fuera de
# alcance: no existe ningún dato estructurado del que derivarlos (se
# verificó que el campo "evidencia" de rubricevaluation está poblado
# siempre y describe el documento, no la implementación real).
# =============================================================================

def _clasificar_tipo_cambio(nivel_v1: str, nivel_v2: str) -> str:
    """Instrumento RI2, Parte D del documento de F2 (simplificada a
    variación de niveles, ver nota del módulo arriba):
      - nivel_v2 < nivel_v1               -> "retroceso"
      - nivel_v2 == nivel_v1              -> "sin_cambio"
      - sube, cruzando de grupo desfavorable (Inicio/En desarrollo) a
        favorable (Logrado/Destacado)     -> "mejora_real"
      - sube, pero se queda dentro del mismo grupo de lectura
        (ej. Inicio->En desarrollo, o Logrado->Destacado) -> "mejora_formal"
    """
    orden_v1, orden_v2 = NIVELES_ORDEN[nivel_v1], NIVELES_ORDEN[nivel_v2]
    if orden_v2 < orden_v1:
        return "retroceso"
    if orden_v2 == orden_v1:
        return "sin_cambio"
    cruza_a_favorable = nivel_v1 not in GRUPO_FAVORABLE and nivel_v2 in GRUPO_FAVORABLE
    return "mejora_real" if cruza_a_favorable else "mejora_formal"


def _clasificar_estado_evidencia(filas_v1_docente: list[dict], filas_v2_docente: list[dict]) -> str:
    """Instrumento RI2, sección "Fuente primaria" / Ficha F2 sección 5
    (estado_evidencia). Determina si la comparación de este docente entra
    a la población de avance ("valida"), queda como cobertura rechazada
    ("no_valida"), queda como cobertura sin entrega ("sin_entrega_v2") o
    ni siquiera tiene línea base ("sin_referencia_v1").

    - Sin ninguna fila V1 con status='success' -> "sin_referencia_v1"
      (no hay línea base con la cual comparar -- RF del documento F2:
      "el AA no simula una comparación").
    - Con V1 válida y alguna fila V2 con status='success' -> "valida".
    - Con V1 válida y alguna fila V2 con status='validation_failed' (sin
      ninguna 'success') -> "no_valida" -- el docente SÍ entregó su
      Sesión V2, pero el documento no corresponde al producto solicitado.
    - Con V1 válida y sin ninguna fila V2 'success' ni 'validation_failed'
      (status='failed', o ausencia total de filas V2) -> "sin_entrega_v2"
      -- no hay evidencia real que evaluar, se trata como sin entrega
      (mismo criterio que RI1 usaba para distinguir n_no_validas de
      n_sin_entrega). Separar estos dos estados importa porque no es lo
      mismo que un docente haya entregado algo rechazado a que
      simplemente no haya entregado nada."""
    v1_valida = any(f["status"] == "success" for f in filas_v1_docente)
    if not v1_valida:
        return "sin_referencia_v1"
    if any(f["status"] == "success" for f in filas_v2_docente):
        return "valida"
    if any(f["status"] == "validation_failed" for f in filas_v2_docente):
        return "no_valida"
    return "sin_entrega_v2"


def _cruzar_comparaciones_por_criterio(filas_v1_docente: list[dict], filas_v2_docente: list[dict]) -> list[dict]:
    """comparacion_por_criterio[] del Instrumento RI2: cruza las filas
    VÁLIDAS de V1 y V2 de un mismo docente por criterion_index. Se asume
    estado_evidencia="valida" (el llamador es responsable de filtrar
    antes). Un criterio sin fila en V1 o en V2 se omite (no se puede
    comparar lo que no existe)."""
    v1_por_criterio = {f["criterion_index"]: f["nivel_obtenido"] for f in filas_v1_docente if f["status"] == "success"}
    v2_por_criterio = {f["criterion_index"]: f["nivel_obtenido"] for f in filas_v2_docente if f["status"] == "success"}

    comparaciones = []
    for criterio_idx in sorted(set(v1_por_criterio) & set(v2_por_criterio)):
        nivel_v1 = v1_por_criterio[criterio_idx]
        nivel_v2 = v2_por_criterio[criterio_idx]
        comparaciones.append({
            "criterio_id": f"C{criterio_idx}",
            "nivel_v1": nivel_v1,
            "nivel_v2": nivel_v2,
            "tipo_cambio": _clasificar_tipo_cambio(nivel_v1, nivel_v2),
        })
    return comparaciones


def _calcular_brechas_comparativas(comparacion_por_criterio: list[dict]) -> list[str]:
    """Instrumento RI2, Parte D (BC1, BC3, BC4 -- BC2 omitida, ver nota
    del módulo). Máximo 2, priorizadas por PRIORIDAD_TIPO (Crítica >
    Estructural > De ajuste), mismo patrón que seleccionar_brechas_globales
    ya usa para B1-B5."""
    tipos_cambio = [c["tipo_cambio"] for c in comparacion_por_criterio]
    n_mejora_real = tipos_cambio.count("mejora_real")
    n_sin_cambio = tipos_cambio.count("sin_cambio")

    candidatas = []
    if "mejora_formal" in tipos_cambio:
        candidatas.append(("BC1", "Crítica"))
    if "retroceso" in tipos_cambio:
        candidatas.append(("BC3", "Estructural"))
    if n_mejora_real >= 2 and n_sin_cambio >= 2:
        candidatas.append(("BC4", "De ajuste"))

    candidatas_ordenadas = sorted(candidatas, key=lambda c: -PRIORIDAD_TIPO.get(c[1], 0))
    return [codigo for codigo, _tipo in candidatas_ordenadas[:2]]


def _calcular_brechas_persistentes(brechas_v1_docente: list[str], comparacion_por_criterio: list[dict]) -> list[str]:
    """Instrumento RI2, Parte F: una brecha B1-B5 activa en V1 persiste si
    su criterio asociado (BRECHA_A_CRITERIO) no alcanzó tipo_cambio =
    "mejora_real" en la comparación V1->V2. Si el criterio no tiene
    comparación disponible (ver _cruzar_comparaciones_por_criterio), se
    considera persistente por precaución -- no se puede afirmar que se
    resolvió sin datos que lo confirmen."""
    tipo_cambio_por_criterio = {c["criterio_id"]: c["tipo_cambio"] for c in comparacion_por_criterio}

    persistentes = []
    for brecha_id in brechas_v1_docente:
        criterio_idx = BRECHA_A_CRITERIO.get(brecha_id)
        criterio_id = f"C{criterio_idx}" if criterio_idx else None
        tipo_cambio = tipo_cambio_por_criterio.get(criterio_id)
        if tipo_cambio != "mejora_real":
            persistentes.append(brecha_id)
    return persistentes


def _calcular_avance_institucional(comparaciones_validas: list[list[dict]]) -> list[dict]:
    """Instrumento RI2, Parte D (avance respecto de la línea de base).
    Recibe comparacion_por_criterio[] de todos los docentes con
    estado_evidencia="valida" (población de avance). Por criterio,
    calcula el % de V1 y de V2 en el grupo favorable (Logrado+Destacado)
    sobre esa misma población, y el avance en puntos porcentuales."""
    por_criterio: dict[str, dict[str, int]] = {}
    for comparacion_docente in comparaciones_validas:
        for c in comparacion_docente:
            conteos = por_criterio.setdefault(c["criterio_id"], {"n": 0, "favorable_v1": 0, "favorable_v2": 0})
            conteos["n"] += 1
            if c["nivel_v1"] in GRUPO_FAVORABLE:
                conteos["favorable_v1"] += 1
            if c["nivel_v2"] in GRUPO_FAVORABLE:
                conteos["favorable_v2"] += 1

    resultado = []
    for criterio_id, conteos in sorted(por_criterio.items()):
        n = conteos["n"]
        if n == 0:
            continue
        pct_favorable_v1 = round(100 * conteos["favorable_v1"] / n)
        pct_favorable_v2 = round(100 * conteos["favorable_v2"] / n)
        resultado.append({
            "criterio_id": criterio_id,
            "n": n,
            "pct_favorable_v1": pct_favorable_v1,
            "pct_favorable_v2": pct_favorable_v2,
            "avance_pct": pct_favorable_v2 - pct_favorable_v1,
        })
    return resultado


def _calcular_logros_consolidados(
    comparaciones_validas: list[list[dict]],
    brechas_persistentes_por_docente: list[list[str]],
) -> list[dict]:
    """Instrumento RI2, Parte G. Por criterio: logro consolidado si
    mejora_real alcanza UMBRAL_NECESIDAD_FRECUENTE_PCT (30%) de la
    población de avance Y, entre esos casos con mejora_real, la brecha
    persistente asociada a ese criterio está ausente en la mayoría
    (>50%). "Práctica efectiva transferible" es el subconjunto de esos
    casos que además alcanza nivel_v2="destacado". Si ningún criterio
    cumple, retorna lista vacía -- no se fuerza una fortaleza artificial."""
    criterio_a_brecha = {f"C{idx}": brecha_id for brecha_id, idx in BRECHA_A_CRITERIO.items()}

    por_criterio: dict[str, list[tuple[dict, list[str]]]] = {}
    for comparacion_docente, brechas_persistentes_docente in zip(comparaciones_validas, brechas_persistentes_por_docente):
        for c in comparacion_docente:
            por_criterio.setdefault(c["criterio_id"], []).append((c, brechas_persistentes_docente))

    resultado = []
    for criterio_id, casos in sorted(por_criterio.items()):
        n_total = len(casos)
        if n_total == 0:
            continue
        casos_mejora_real = [(c, brechas) for c, brechas in casos if c["tipo_cambio"] == "mejora_real"]
        pct_mejora_real = round(100 * len(casos_mejora_real) / n_total)
        if pct_mejora_real < UMBRAL_NECESIDAD_FRECUENTE_PCT or not casos_mejora_real:
            continue

        brecha_asociada = criterio_a_brecha.get(criterio_id)
        n_sin_brecha_persistente = sum(
            1 for _c, brechas in casos_mejora_real
            if brecha_asociada is None or brecha_asociada not in brechas
        )
        if n_sin_brecha_persistente <= len(casos_mejora_real) / 2:
            continue

        n_destacado = sum(1 for c, _brechas in casos_mejora_real if c["nivel_v2"] == "destacado")
        resultado.append({
            "criterio_id": criterio_id,
            "n_mejora_real": len(casos_mejora_real),
            "n_total": n_total,
            "pct_mejora_real": pct_mejora_real,
            "practica_efectiva_transferible": n_destacado > 0,
            "n_destacado": n_destacado,
        })
    return resultado


# =============================================================================
# RI2 -- agregación institucional, denominadores y denominaciones visibles
# =============================================================================

# Denominación visible de BC1/BC3/BC4 (Instrumento RI2, Parte D -- BC2
# omitida, ver nota del módulo). "refiere_a" sigue el mismo patrón que
# BRECHAS en comun.py: texto grounding para que el LLM no copie/mezcle
# contenido entre ids al redactar la manifestación de cada una.
BRECHAS_COMPARATIVAS = {
    "BC1": {
        "nombre": "Ajustes que todavía no modifican de forma sustantiva el diseño de la sesión",
        "refiere_a": (
            "el nivel subió en la etiqueta, pero el ajuste se mantiene dentro del mismo "
            "grupo de lectura (por ejemplo, de Inicio a En desarrollo, o de Logrado a Destacado), "
            "sin cruzar hacia un desempeño cualitativamente distinto."
        ),
    },
    "BC3": {
        "nombre": "Aspectos que retrocedieron respecto de la versión anterior",
        "refiere_a": "el nivel obtenido en la Sesión V2 es menor al que se tenía en la Sesión V1.",
    },
    "BC4": {
        "nombre": "Mejoras concentradas en algunos aspectos sin que se explicite un criterio de priorización",
        "refiere_a": (
            "varios aspectos mejoraron de forma genuina mientras otros se mantuvieron sin "
            "cambio, sin que se explicite por qué se priorizaron unos sobre otros."
        ),
    },
}


def _nombre_brecha_comparativa(bc_id: str) -> str:
    """'BC1' -> denominación visible, nunca el código (mismo principio que
    _nombre_brecha para B1-B5)."""
    info = BRECHAS_COMPARATIVAS.get(bc_id)
    return info["nombre"] if info else bc_id


def _frecuencia_a_lista_ordenada(frecuencia: dict[str, dict]) -> list[dict]:
    """El Instrumento RI2 NO fija un umbral de inclusión para la tabla de
    'naturaleza de los cambios' ni para 'dificultades que persisten' --
    solo pide priorizar las de mayor frecuencia en la NARRATIVA (Parte H),
    no ocultar filas de la tabla de datos. Por eso acá se listan todas,
    solo ordenadas por frecuencia descendente, sin filtrar por %."""
    lista = [{"id": id_, "n": info["conteo"], "pct": info["pct"]} for id_, info in frecuencia.items()]
    lista.sort(key=lambda x: -x["pct"])
    return lista


def _primera_oracion_avance(c: dict) -> str:
    """Arma en Python la primera oración del avance institucional (3-bis):
    cifras exactas, el LLM nunca las toca (mismo principio que
    _primera_oracion_interpretacion)."""
    signo = "+" if c["avance_pct"] >= 0 else ""
    return (
        f"El porcentaje de comparaciones con resultado favorable (Logrado o Destacado) pasó de "
        f"{c['pct_favorable_v1']}% en la Sesión V1 a {c['pct_favorable_v2']}% en la Sesión V2 "
        f"({signo}{c['avance_pct']} puntos porcentuales, sobre {c['n']} comparaciones válidas)."
    )


def _construir_advertencia_cobertura_ri2(
    n_comparaciones_validas: int, umbral_requerido: int, n_docentes_total: int, pct_cobertura: int
) -> str:
    """Advertencia obligatoria para COB_BAJO_UMBRAL (Adenda, sección 8.1,
    variante RI2) -- texto fijo, armado en Python con las cifras ya
    calculadas; el LLM nunca la construye ni la toca."""
    return (
        f"Este reporte se elaboró con {n_comparaciones_validas} de las {umbral_requerido} "
        f"comparaciones válidas entre la Versión 1 y la Versión 2 requeridas para representar a "
        f"su institución ({n_comparaciones_validas} de {n_docentes_total} docentes participantes; "
        f"{pct_cobertura}%). Los resultados describen únicamente las comparaciones revisadas y no "
        "pueden generalizarse al conjunto de docentes. Por ello, los hallazgos de este reporte "
        "deben tratarse solo como hipótesis por contrastar con otras fuentes de su institución "
        "—evidencias de implementación, matrices de análisis, evidencias de seguimiento y "
        "acompañamiento— antes de utilizarse en la RTC 2."
    )


# =============================================================================
# RI2 -- redacción narrativa (LLM). Python resuelve todas las cifras, el
# LLM solo redacta prosa cualitativa a partir de ellas. Primer borrador
# del prompt -- es razonable que necesite ajustes de calibración una vez
# que se vea generar contra datos reales.
# =============================================================================

_MANIFESTACION_GENERICA = "resultados que conviene revisar con mayor detalle junto con otras evidencias"


def _interpretacion_fallback_ri2(c: dict) -> str:
    return (
        f"{_primera_oracion_interpretacion(c)} Esto se reporta a partir de los datos disponibles. "
        "Para comprender mejor esta situación, conviene contrastarla con evidencias de "
        "implementación."
    )


def _avance_comentario_fallback(c: dict) -> str:
    return (
        f"{_primera_oracion_avance(c)} Este cambio describe lo observado entre ambas versiones y "
        "no permite, por sí solo, atribuir una causa específica."
    )


def _sintesis_fallback_ri2(variables: dict) -> str:
    return (
        f"La comparación de las Sesiones V1 y V2 de {variables.get('nombre_ie', 'la institución')} "
        f"reúne {variables.get('n_comparaciones_validas')} de {variables.get('n_docentes_total')} "
        f"comparaciones válidas ({variables.get('pct_cobertura_v2')}% de cobertura). Estos "
        "resultados describen cambios observados entre ambas versiones y requieren contrastarse "
        "con otras evidencias institucionales durante la RTC 2."
    )


def _quitar_oracion_numerica_inicial(texto: str) -> str:
    """Quita la primera oración si el LLM repitió cifras ya calculadas.

    Las interpretaciones RI2 reciben desde Python una primera oración con
    los conteos y porcentajes exactos. Aunque el prompt le pide al modelo
    que no la repita, se conserva únicamente la continuación cualitativa
    cuando su primera oración contiene cifras.
    """
    texto = re.sub(r"\s+", " ", texto).strip()
    if not texto:
        return ""

    # La primera puntuación terminal separa la oración que, en ocasiones,
    # el modelo repite antes de la explicación solicitada.
    coincidencia = re.search(r"[.!?](?:\s|$)", texto)
    if not coincidencia:
        return texto

    primera_oracion = texto[:coincidencia.end()]
    if not re.search(r"\d", primera_oracion):
        return texto
    return texto[coincidencia.end():].strip()


def _secciones_narrativas_fallback_ri2(variables: dict) -> dict:
    return {
        "interpretaciones": {
            c["criterio_id"]: _interpretacion_fallback_ri2(c) for c in variables["distribucion_v2_por_aspecto"]
        },
        "avance_comentarios": {
            c["criterio_id"]: _avance_comentario_fallback(c) for c in variables["avance_institucional"]
        },
        "manifestaciones": {
            "naturaleza_cambios": {b["id"]: _MANIFESTACION_GENERICA for b in variables["naturaleza_cambios"]},
            "dificultades_persisten": {b["id"]: _MANIFESTACION_GENERICA for b in variables["dificultades_persisten"]},
            "logros_consolidados": {l["criterio_id"]: _MANIFESTACION_GENERICA for l in variables["logros_consolidados"]},
        },
        "preguntas_rtc": list(PREGUNTAS_RTC_FIJAS),
        "sintesis_institucional": _sintesis_fallback_ri2(variables),
    }


def _completar_huecos_narrativos_ri2(variables: dict, data: dict) -> dict:
    """Completa SOLO los campos puntuales que falten, sin descartar el
    resto de la respuesta del LLM por un campo faltante."""
    interpretaciones = dict(data.get("interpretaciones") or {})
    avance_comentarios = dict(data.get("avance_comentarios") or {})
    manifestaciones_crudas = data.get("manifestaciones") or {}
    manifestaciones = {
        "naturaleza_cambios": dict(manifestaciones_crudas.get("naturaleza_cambios") or {}),
        "dificultades_persisten": dict(manifestaciones_crudas.get("dificultades_persisten") or {}),
        "logros_consolidados": dict(manifestaciones_crudas.get("logros_consolidados") or {}),
    }
    preguntas_rtc = data.get("preguntas_rtc")
    sintesis = data.get("sintesis_institucional")

    faltantes = []

    for c in variables["distribucion_v2_por_aspecto"]:
        cuerpo_llm = interpretaciones.get(c["criterio_id"])
        if cuerpo_llm:
            cuerpo_llm = _quitar_oracion_numerica_inicial(cuerpo_llm)
            interpretaciones[c["criterio_id"]] = f"{_primera_oracion_interpretacion(c)} {cuerpo_llm}"
        else:
            interpretaciones[c["criterio_id"]] = _interpretacion_fallback_ri2(c)
            faltantes.append(f"interpretaciones.{c['criterio_id']}")

    for c in variables["avance_institucional"]:
        cuerpo_llm = avance_comentarios.get(c["criterio_id"])
        if cuerpo_llm:
            avance_comentarios[c["criterio_id"]] = f"{_primera_oracion_avance(c)} {cuerpo_llm}"
        else:
            avance_comentarios[c["criterio_id"]] = _avance_comentario_fallback(c)
            faltantes.append(f"avance_comentarios.{c['criterio_id']}")

    for b in variables["naturaleza_cambios"]:
        if not manifestaciones["naturaleza_cambios"].get(b["id"]):
            manifestaciones["naturaleza_cambios"][b["id"]] = _MANIFESTACION_GENERICA
            faltantes.append(f"manifestaciones.naturaleza_cambios.{b['id']}")

    for b in variables["dificultades_persisten"]:
        if not manifestaciones["dificultades_persisten"].get(b["id"]):
            manifestaciones["dificultades_persisten"][b["id"]] = _MANIFESTACION_GENERICA
            faltantes.append(f"manifestaciones.dificultades_persisten.{b['id']}")

    for l in variables["logros_consolidados"]:
        if not manifestaciones["logros_consolidados"].get(l["criterio_id"]):
            manifestaciones["logros_consolidados"][l["criterio_id"]] = _MANIFESTACION_GENERICA
            faltantes.append(f"manifestaciones.logros_consolidados.{l['criterio_id']}")

    if not isinstance(preguntas_rtc, list) or not preguntas_rtc:
        preguntas_rtc = list(PREGUNTAS_RTC_FIJAS)
        faltantes.append("preguntas_rtc")

    if not sintesis:
        sintesis = _sintesis_fallback_ri2(variables)
        faltantes.append("sintesis_institucional")

    if faltantes:
        log.warning(
            "ri2_llm_secciones_narrativas_incompletas",
            cod_modular=variables.get("cod_modular"),
            campos_completados_con_fallback=faltantes,
        )

    return {
        "interpretaciones": interpretaciones,
        "avance_comentarios": avance_comentarios,
        "manifestaciones": manifestaciones,
        "preguntas_rtc": preguntas_rtc,
        "sintesis_institucional": sintesis,
    }


def _construir_prompt_secciones_narrativas_ri2(v: dict) -> str:
    def _bloque_aspectos(items, descriptor_fn):
        if not items:
            return "(sin datos)"
        return "\n".join(f"- {c['criterio_id']} ({_nombre_criterio(c['criterio_id'])}): {descriptor_fn(c)}" for c in items)

    aspectos_v2_texto = _bloque_aspectos(
        v["distribucion_v2_por_aspecto"],
        lambda c: f"{CRITERIOS.get(c['criterio_id'], {}).get('que_evalua', '')} -- {_primera_oracion_interpretacion(c)}",
    )
    avance_texto = _bloque_aspectos(v["avance_institucional"], lambda c: _primera_oracion_avance(c))

    def _bloque_frecuencia(items, nombre_fn, refiere_fn):
        if not items:
            return "(ninguna con frecuencia registrada)"
        return "\n".join(
            f"- {i['id']} ({nombre_fn(i['id'])}): {i['n']} de {v['n_comparaciones_validas']} ({i['pct']}%) -- {refiere_fn(i['id'])}"
            for i in items
        )

    naturaleza_texto = _bloque_frecuencia(
        v["naturaleza_cambios"], _nombre_brecha_comparativa,
        lambda id_: BRECHAS_COMPARATIVAS.get(id_, {}).get("refiere_a", ""),
    )
    dificultades_texto = _bloque_frecuencia(
        v["dificultades_persisten"], _nombre_brecha,
        lambda id_: BRECHAS.get(id_, {}).get("refiere_a", ""),
    )

    if v["logros_consolidados"]:
        logros_texto = "\n".join(
            f"- {l['criterio_id']} ({_nombre_criterio(l['criterio_id'])}): {l['n_mejora_real']} de "
            f"{l['n_total']} comparaciones con mejora real ({l['pct_mejora_real']}%)"
            f"{' -- incluye casos con nivel Destacado (práctica efectiva transferible)' if l['practica_efectiva_transferible'] else ''}"
            for l in v["logros_consolidados"]
        )
    else:
        logros_texto = "(ningún aspecto cumple las condiciones de logro consolidado -- comunicarlo de forma transparente, sin forzar una fortaleza artificial)"

    retroalimentaciones_texto = "\n".join(f"- {r}" for r in v["retroalimentaciones"]) or "(sin retroalimentaciones registradas)"

    ids_criterios_v2 = ", ".join(f'"{c["criterio_id"]}"' for c in v["distribucion_v2_por_aspecto"])
    ids_criterios_avance = ", ".join(f'"{c["criterio_id"]}"' for c in v["avance_institucional"])

    def _schema(items, id_key="id"):
        if not items:
            return "{}"
        return "{ " + ", ".join(f'"{i[id_key]}": "fragmento breve"' for i in items) + " }"

    schema_naturaleza = _schema(v["naturaleza_cambios"])
    schema_dificultades = _schema(v["dificultades_persisten"])
    schema_logros = _schema(v["logros_consolidados"], id_key="criterio_id")

    return f"""
Eres un redactor pedagógico para el sistema educativo peruano. Vas a
redactar piezas del Reporte Institucional 2 (comparación entre la Sesión
de aprendizaje V1 y la V2) para el directivo de una IE, a partir de datos
YA CALCULADOS (no recalcules ni inventes cifras).

Datos generales:
- IE: {v.get('nombre_ie')}
- Comparaciones V1→V2 válidas: {v.get('n_comparaciones_validas')} de {v.get('n_docentes_total')} docentes registrados ({v.get('pct_cobertura_v2')}% de cobertura)
- Muestra suficiente (>= 5 comparaciones válidas): {v.get('muestra_suficiente')}

Resultado general de las Sesiones V2 por aspecto:
{aspectos_v2_texto}

Avance institucional respecto de la línea de base (V1→V2, en puntos porcentuales):
{avance_texto}

Naturaleza de los cambios registrados (puede estar vacío):
{naturaleza_texto}

Dificultades identificadas en V1 que persisten en V2 (puede estar vacío):
{dificultades_texto}

Logros consolidados y prácticas efectivas transferibles:
{logros_texto}

Retroalimentaciones registradas para los docentes de esta IE (texto libre
de F2, ya agregado -- NUNCA estén asociadas a un docente identificable en
tu respuesta, úsalas solo para reconocer temas recurrentes):
{retroalimentaciones_texto}

Responde ÚNICAMENTE con un objeto JSON válido, sin texto adicional antes
ni después, con esta forma exacta:
{{
  "interpretaciones": {{ {ids_criterios_v2}: "SOLO explicación, sin la primera oración ni cifras", ... }},
  "avance_comentarios": {{ {ids_criterios_avance}: "SOLO explicación, sin la primera oración ni cifras", ... }},
  "manifestaciones": {{
    "naturaleza_cambios": {schema_naturaleza},
    "dificultades_persisten": {schema_dificultades},
    "logros_consolidados": {schema_logros}
  }},
  "preguntas_rtc": ["pregunta 1", "pregunta 2", "..."],
  "sintesis_institucional": "texto de síntesis"
}}

Para cada "interpretaciones.<id>", escribe ÚNICAMENTE la continuación de
la oración ya resuelta que aparece junto a ese aspecto arriba -- no la
repitas, el sistema ya la antepone. Tu texto empieza directo con la
explicación cualitativa (nunca con una cifra), apoyada en lo que evalúa
ese aspecto, y cierra sugiriendo con qué otra evidencia conviene
contrastarlo.

Para cada "avance_comentarios.<id>", escribe la explicación del cambio en
puntos porcentuales ya resuelto arriba -- nunca repitas ni recalcules el
número. Describe el cambio como una diferencia observada, SIN atribuirle
una causa única ni afirmar que un cambio positivo ya está consolidado
(eso lo determina únicamente la sección de logros consolidados).

Para cada "manifestaciones.naturaleza_cambios.<id>",
"manifestaciones.dificultades_persisten.<id>" y
"manifestaciones.logros_consolidados.<id>", escribe SOLO el fragmento de
manifestación pedagógica (frase breve en minúscula, sin repetir el
nombre del aspecto ni las cifras), basado ESPECÍFICAMENTE en la
descripción de ESE id -- nunca mezcles el contenido de un id con el de
otro. Nunca menciones los códigos técnicos (BC1, B1, etc.) en el texto
visible.

Para "preguntas_rtc", genera de 4 a 6 preguntas movilizadoras abiertas
para la RTC 2, orientadas a analizar la implementación y los cambios
observados entre V1 y V2 (no a asignar responsabilidades individuales).
Nunca cites una cifra exacta dentro de una pregunta.

Para "sintesis_institucional", integra en un solo párrafo los avances más
relevantes, los logros consolidados (si existen) y las dificultades que
persisten, cerrando con una invitación a contrastar esta información
durante la RTC 2. No atribuyas causalidad a partir de las diferencias
descriptivas entre V1 y V2, y no conviertas la síntesis en una
recomendación prescriptiva cerrada.

Reglas generales (aplican a TODO lo que redactes en esta respuesta):
- Nunca uses los términos "brecha", "alerta", "patrón", "clúster",
  "semaforización" ni "nivel global".
- Nunca muestres códigos internos (C1-C5, BC1-BC4, B1-B5).
- Nunca emitas recomendaciones dirigidas a un docente en particular ni
  generes rankings entre personas o niveles educativos.
- Usa siempre "propósito de aprendizaje", nunca "objetivos de aprendizaje".
"""


async def redactar_secciones_narrativas_ri2(variables: dict) -> dict:
    """Llama al LLM UNA vez para redactar las interpretaciones por
    aspecto, el comentario de avance por aspecto, las manifestaciones y
    la síntesis institucional. Reintenta si el JSON no es parseable;
    degrada a texto plantillado si falla dos veces; completa huecos
    puntuales sin descartar el resto de la respuesta."""
    prompt = _construir_prompt_secciones_narrativas_ri2(variables)

    for intento in (1, 2):
        inicio = time.monotonic()
        try:
            crudo = await generar_texto_narrativo(prompt, formato_json=True)
            data = json.loads(crudo)
            if not isinstance(data, dict):
                raise ValueError("la respuesta del LLM no es un objeto JSON")
        except Exception:
            duracion_ms = round((time.monotonic() - inicio) * 1000)
            log.error(
                "ri2_llm_secciones_narrativas_fallidas",
                cod_modular=variables.get("cod_modular"),
                intento=intento,
                duracion_ms=duracion_ms,
                exc_info=True,
            )
            if intento == 2:
                return _secciones_narrativas_fallback_ri2(variables)
            continue

        duracion_ms = round((time.monotonic() - inicio) * 1000)
        log.info(
            "ri2_llm_secciones_narrativas_generadas",
            cod_modular=variables.get("cod_modular"),
            duracion_ms=duracion_ms,
        )
        return _completar_huecos_narrativos_ri2(variables, data)


def _sin_punto_final(texto: str) -> str:
    """Evita duplicar el punto que agrega la plantilla al fragmento."""
    return texto.strip().rstrip(".").rstrip()


# =============================================================================
# RI2 -- orquestador de generación
# =============================================================================

def _agregar_comparaciones_ie(filas_v1: list[dict], filas_v2: list[dict]) -> dict:
    """Pura (sin DB): dado el conjunto crudo de filas V1 y V2 de una IE
    completa (ya pasadas por _excluir_docentes_con_registro_incompleto),
    clasifica a cada docente por estado_evidencia y arma las listas/dicts
    que necesita la agregación institucional. Extraída de
    generar_reporte_ri2 para poder testearla con datos de ejemplo, sin
    tocar la base de datos."""
    filas_v1_por_docente = agrupar_por_docente(filas_v1)
    filas_v2_por_docente = agrupar_por_docente(filas_v2)
    todos_los_user_ids = set(filas_v1_por_docente) | set(filas_v2_por_docente)

    comparaciones_validas: list[list[dict]] = []
    brechas_persistentes_por_docente: list[list[str]] = []
    naturaleza_cambios_por_docente: dict[int, list[str]] = {}
    persistentes_por_docente: dict[int, list[str]] = {}
    n_sin_referencia_v1 = 0
    n_no_validas = 0
    n_sin_entrega_v2 = 0

    for user_id in todos_los_user_ids:
        filas_v1_docente = filas_v1_por_docente.get(user_id, [])
        filas_v2_docente = filas_v2_por_docente.get(user_id, [])
        estado = _clasificar_estado_evidencia(filas_v1_docente, filas_v2_docente)

        if estado == "sin_referencia_v1":
            n_sin_referencia_v1 += 1
            continue
        if estado == "no_valida":
            n_no_validas += 1
            continue
        if estado == "sin_entrega_v2":
            n_sin_entrega_v2 += 1
            continue

        # estado == "valida"
        comparacion = _cruzar_comparaciones_por_criterio(filas_v1_docente, filas_v2_docente)
        if not comparacion:
            # Tiene V1 y V2 válidas (ambas 'success') pero ningún
            # criterio cruza entre ambas (caso límite, ej. datos
            # incompletos por criterio) -- no entra a la población de
            # avance. Se cuenta como n_no_validas, no n_sin_entrega_v2:
            # sí hubo una entrega válida de V2, el problema es que los
            # datos no permiten comparar ningún criterio.
            n_no_validas += 1
            continue

        brechas_v1_docente = seleccionar_brechas_globales(filas_validas(filas_v1_docente))
        brechas_persistentes_docente = _calcular_brechas_persistentes(brechas_v1_docente, comparacion)

        comparaciones_validas.append(comparacion)
        brechas_persistentes_por_docente.append(brechas_persistentes_docente)
        naturaleza_cambios_por_docente[user_id] = _calcular_brechas_comparativas(comparacion)
        persistentes_por_docente[user_id] = brechas_persistentes_docente

    return {
        "comparaciones_validas": comparaciones_validas,
        "brechas_persistentes_por_docente": brechas_persistentes_por_docente,
        "naturaleza_cambios_por_docente": naturaleza_cambios_por_docente,
        "persistentes_por_docente": persistentes_por_docente,
        "n_sin_referencia_v1": n_sin_referencia_v1,
        "n_no_validas": n_no_validas,
        "n_sin_entrega_v2": n_sin_entrega_v2,
    }


async def generar_reporte_ri2(cod_modular: str, nombre_ie: str, ugel: str, region: str, distrito: str) -> dict:
    """
    Genera el Reporte Institucional 2 (comparación V1→V2). Python calcula
    todas las cifras; un único call al LLM redacta la prosa cualitativa a
    partir de ellas ya resueltas.
    """
    filas_v1 = await obtener_filas_vista(nombre_ie, ugel, region, distrito, version_sesion="V1")
    filas_v2 = await obtener_filas_vista(nombre_ie, ugel, region, distrito, version_sesion="V2")
    if not filas_v1 and not filas_v2:
        raise SinDatosDisponiblesError(f"Sin datos en la vista para {cod_modular}")

    filas_v1 = _excluir_docentes_con_registro_incompleto(filas_v1)
    filas_v2 = _excluir_docentes_con_registro_incompleto(filas_v2)

    n_docentes_total = await contar_docentes_total(nombre_ie, ugel, region, distrito)

    agregado = _agregar_comparaciones_ie(filas_v1, filas_v2)
    comparaciones_validas = agregado["comparaciones_validas"]
    brechas_persistentes_por_docente = agregado["brechas_persistentes_por_docente"]
    naturaleza_cambios_por_docente = agregado["naturaleza_cambios_por_docente"]
    persistentes_por_docente = agregado["persistentes_por_docente"]
    n_sin_referencia_v1 = agregado["n_sin_referencia_v1"]
    n_no_validas = agregado["n_no_validas"]
    n_sin_entrega_v2 = agregado["n_sin_entrega_v2"]
    filas_v2_por_docente = agrupar_por_docente(filas_v2)

    n_comparaciones_validas = len(comparaciones_validas)
    pct_cobertura_v2 = round(100 * n_comparaciones_validas / n_docentes_total) if n_docentes_total > 0 else 0
    muestra_suficiente = n_comparaciones_validas >= UMBRAL_MUESTRA_INSUFICIENTE

    clasificacion_cobertura = _clasificar_cobertura(n_comparaciones_validas, n_docentes_total)
    advertencia_cobertura = None
    if clasificacion_cobertura["estado_cobertura"] == "COB_BAJO_UMBRAL":
        advertencia_cobertura = _construir_advertencia_cobertura_ri2(
            n_comparaciones_validas, clasificacion_cobertura["umbral_requerido"], n_docentes_total, pct_cobertura_v2,
        )

    filas_v2_validas_total = filas_validas(filas_v2)
    distribucion_v2 = calcular_distribucion_por_criterio(filas_v2_validas_total)
    distribucion_v2_por_id = {c["criterio_id"]: c for c in distribucion_v2}

    avance_institucional = _calcular_avance_institucional(comparaciones_validas)

    frecuencia_naturaleza = calcular_frecuencia_brechas(naturaleza_cambios_por_docente)
    frecuencia_persistentes = calcular_frecuencia_brechas(persistentes_por_docente)
    naturaleza_cambios = _frecuencia_a_lista_ordenada(frecuencia_naturaleza)
    dificultades_persisten = _frecuencia_a_lista_ordenada(frecuencia_persistentes)

    logros_consolidados = _calcular_logros_consolidados(comparaciones_validas, brechas_persistentes_por_docente)

    enfasis_lectura = calcular_enfasis_lectura(distribucion_v2_por_id)
    lectura_por_nivel = calcular_lectura_por_nivel(filas_v2_validas_total)
    fortalezas_adaptadas = [
        {"id": l["criterio_id"], "pct": l["pct_mejora_real"]} for l in logros_consolidados
    ]
    aspectos_contraste = calcular_aspectos_contraste(distribucion_v2, fortalezas_adaptadas)

    variables = {
        "cod_modular": cod_modular,
        "nombre_ie": nombre_ie,
        "n_docentes_total": n_docentes_total,
        "n_comparaciones_validas": n_comparaciones_validas,
        "pct_cobertura_v2": pct_cobertura_v2,
        "muestra_suficiente": muestra_suficiente,
        "distribucion_v2_por_aspecto": _enriquecer_distribucion_con_nombre(distribucion_v2),
        "avance_institucional": _enriquecer_distribucion_con_nombre(avance_institucional),
        "naturaleza_cambios": [{**b, "nombre": _nombre_brecha_comparativa(b["id"])} for b in naturaleza_cambios],
        "dificultades_persisten": _enriquecer_necesidades_con_nombre(dificultades_persisten),
        "logros_consolidados": logros_consolidados,
        "retroalimentaciones": _extraer_retroalimentaciones(filas_v2_por_docente),
    }
    narrativas = await redactar_secciones_narrativas_ri2(variables)

    variables["naturaleza_cambios"] = [
        {**b, "manifestacion": _sin_punto_final(narrativas["manifestaciones"]["naturaleza_cambios"].get(b["id"], ""))}
        for b in variables["naturaleza_cambios"]
    ]
    variables["dificultades_persisten"] = [
        {**b, "manifestacion": _sin_punto_final(narrativas["manifestaciones"]["dificultades_persisten"].get(b["id"], ""))}
        for b in variables["dificultades_persisten"]
    ]
    variables["logros_consolidados"] = [
        {**l, "nombre": _nombre_criterio(l["criterio_id"]),
         "manifestacion": _sin_punto_final(narrativas["manifestaciones"]["logros_consolidados"].get(l["criterio_id"], ""))}
        for l in variables["logros_consolidados"]
    ]

    ahora_utc = datetime.now(timezone.utc)
    return {
        "reporte_id": f"{cod_modular}-RI2-{ahora_utc.strftime('%Y%m%dT%H%M%S')}",
        "cod_modular": cod_modular,
        "institucion_id": cod_modular,
        "nombre_ie": nombre_ie,
        "tipo_reporte": "REPORTE_INSTITUCIONAL_2",
        "hito_asociado": "HITO_2_DIRECTIVO",
        "version_evidencia": "V1_V2",
        "fecha_generacion": ahora_utc.isoformat(),
        "n_docentes_total": n_docentes_total,
        "n_comparaciones_validas": n_comparaciones_validas,
        "n_sin_referencia_v1": n_sin_referencia_v1,
        "n_no_validas": n_no_validas,
        "n_sin_entrega_v2": n_sin_entrega_v2,
        "pct_cobertura_v2": pct_cobertura_v2,
        "muestra_suficiente": muestra_suficiente,
        "banda_aplicada": clasificacion_cobertura["banda_aplicada"],
        "umbral_requerido": clasificacion_cobertura["umbral_requerido"],
        "estado_cobertura": clasificacion_cobertura["estado_cobertura"],
        "advertencia_cobertura": advertencia_cobertura,
        "distribucion_v2_por_aspecto": variables["distribucion_v2_por_aspecto"],
        "avance_institucional": variables["avance_institucional"],
        "naturaleza_cambios": variables["naturaleza_cambios"],
        "dificultades_persisten": variables["dificultades_persisten"],
        "logros_consolidados": variables["logros_consolidados"],
        "enfasis_lectura": enfasis_lectura,
        "lectura_por_nivel": lectura_por_nivel,
        "aspectos_contraste": aspectos_contraste,
        "preguntas_rtc": narrativas["preguntas_rtc"],
        "interpretaciones_por_aspecto": narrativas["interpretaciones"],
        "avance_comentarios": narrativas["avance_comentarios"],
        "sintesis_institucional": narrativas["sintesis_institucional"],
        "identificacion_individual_incluida": False,
    }


# =============================================================================
# RI2 -- puntos de entrada (GET /reporte/consultar y POST /reporte/generar)
# =============================================================================

async def consultar_reporte_existente_ri2(userid: int) -> dict:
    """Endpoint rápido de solo lectura (nunca toca el LLM). n_actual se
    aproxima con contar_docentes_evaluados sobre los cmids de V2, que
    cuenta Sesiones V2 'success' completas; no distingue aún
    sin_referencia_v1 de no_valida (ambas cuentan como "no cambió" para
    efectos de invalidar caché, que es lo único que este conteo liviano
    necesita resolver -- la clasificación completa por
    _clasificar_estado_evidencia ocurre recién al generar el reporte)."""
    inicio = time.monotonic()
    log.info("ri2_reporte_consultado", userid=userid)

    ie = await resolver_cod_modular(userid)
    cod_modular = ie["cod_modular"]

    guardado = await obtener_reporte_guardado(cod_modular, tipo_reporte="RI2")

    n_actual = await contar_docentes_evaluados(ie["nombre_ie"], ie["ugel"], ie["region"], ie["distrito"], version_sesion="V2")
    n_total = await contar_docentes_total(ie["nombre_ie"], ie["ugel"], ie["region"], ie["distrito"])
    pct_cobertura_actual = round(100 * n_actual / n_total) if n_total > 0 else 0
    clasificacion = _clasificar_cobertura(n_actual, n_total)

    duracion_ms = round((time.monotonic() - inicio) * 1000)

    campos_cobertura = {
        "n_docentes_evaluados": n_actual,
        "n_docentes_total": n_total,
        "pct_cobertura_actual": pct_cobertura_actual,
        **clasificacion,
    }

    if guardado is None:
        if _debe_esperar_mas_docentes(n_actual, n_total):
            log.info("ri2_consulta_esperar_docentes", cod_modular=cod_modular, duracion_ms=duracion_ms)
            return {"accion": "esperar_docentes", "reporte": None, **campos_cobertura}
        log.info("ri2_consulta_debe_generar", cod_modular=cod_modular, motivo="sin_registro_previo", duracion_ms=duracion_ms)
        return {"accion": "generar", "reporte": None, **campos_cobertura}

    if n_actual != guardado["n_docentes_evaluados"]:
        log.info("ri2_consulta_debe_generar", cod_modular=cod_modular, motivo="nueva_actividad_docente", duracion_ms=duracion_ms)
        return {"accion": "generar", "reporte": None, **campos_cobertura}

    log.info("ri2_consulta_mostrar_vigente", cod_modular=cod_modular, duracion_ms=duracion_ms)
    return {"accion": "mostrar", "reporte": guardado["reporte_json"], **campos_cobertura}


async def obtener_o_generar_reporte_ri2(userid: int, courseid: int, cmid: int) -> dict:
    """Pipeline completo: resuelve IE, verifica caché con invalidación por
    n_docentes_evaluados, genera con LLM si hace falta, guarda y registra
    la asociación directivo-reporte. Ver consultar_reporte_existente_ri2
    sobre la aproximación de n_actual."""
    inicio = time.monotonic()
    log.info("ri2_reporte_solicitado", userid=userid, courseid=courseid, cmid=cmid)

    ie = await resolver_cod_modular(userid)
    cod_modular = ie["cod_modular"]
    nombre_ie = ie["nombre_ie"]
    ugel = ie["ugel"]
    region = ie["region"]
    distrito = ie["distrito"]

    guardado = await obtener_reporte_guardado(cod_modular, tipo_reporte="RI2")

    debe_generar = guardado is None or guardado["estado"] != "generado"

    if guardado is not None and guardado["estado"] == "generado":
        n_actual = await contar_docentes_evaluados(nombre_ie, ugel, region, distrito, version_sesion="V2")
        debe_generar = n_actual != guardado["n_docentes_evaluados"]
        if debe_generar:
            log.info(
                "ri2_cache_invalidado", cod_modular=cod_modular,
                n_docentes_guardado=guardado["n_docentes_evaluados"], n_docentes_actual=n_actual,
            )

    if not debe_generar:
        log.info("ri2_cache_hit", cod_modular=cod_modular)
        id_reporte = guardado["id"]
        reporte = guardado["reporte_json"]
    else:
        log.info("ri2_cache_miss_o_reintento", cod_modular=cod_modular)
        try:
            reporte = await generar_reporte_ri2(cod_modular, nombre_ie, ugel, region, distrito)
        except SinDatosDisponiblesError:
            if guardado is not None and guardado["estado"] == "generado":
                log.warning(
                    "ri2_vista_vacia_transitoria_se_mantiene_reporte_anterior",
                    cod_modular=cod_modular, n_docentes_guardado=guardado["n_docentes_evaluados"],
                )
                id_reporte = guardado["id"]
                reporte = guardado["reporte_json"]
            else:
                await guardar_estado_reporte(cod_modular, "sin_datos", 0, tipo_reporte="RI2")
                raise
        else:
            id_reporte = await guardar_estado_reporte(
                cod_modular, "generado", reporte["n_comparaciones_validas"], reporte, tipo_reporte="RI2",
            )

    await registrar_consulta(cod_modular, userid, courseid, cmid, id_reporte, tipo_reporte="RI2")

    duracion_ms = round((time.monotonic() - inicio) * 1000)
    log.info("ri2_reporte_completado", userid=userid, cod_modular=cod_modular, duracion_ms=duracion_ms)

    return reporte
