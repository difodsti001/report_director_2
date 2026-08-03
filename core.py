"""
core.py

Lógica de negocio del asistente de reporte institucional.

Dos bases de datos, dos pools separados:
- pool_datos:  base con public."base_ebr_IA" y public.vw_processing_brechas_ebr
- pool_cache:  base con f4_reportes_directivo y f4_consultas_directivo
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional

import structlog
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from llm_client import generar_texto_narrativo


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


class CursoDocentesNoConfiguradoError(Exception):
    """El course_id de docentes existe en el diccionario pero sus cmids
    aún no se definieron (valor None)."""


class ReporteNoExisteError(Exception):
    """No hay reporte guardado todavía para esta IE. Distinto de
    SinDatosDisponiblesError: este es el caso 'aún no se generó', usado
    por el endpoint de solo-consulta."""


class MuestraInsuficienteError(Exception):
    """Aún no hay UMBRAL_MUESTRA_INSUFICIENTE docentes evaluados. No se
    genera el reporte para no gastar una llamada al LLM sin necesidad --
    ver GET /reporte/consultar, que evalúa esto antes de sugerir generar."""


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
    """Crea las tablas de caché si no existen."""
    ddl = """
        CREATE TABLE IF NOT EXISTS f4_reportes_directivo (
            id SERIAL PRIMARY KEY,
            cod_modular VARCHAR NOT NULL UNIQUE,
            estado VARCHAR NOT NULL DEFAULT 'generado',
            n_docentes_evaluados INTEGER NOT NULL,
            reporte_json JSONB,
            fecha_generacion TIMESTAMP NOT NULL DEFAULT now(),
            fecha_actualizacion TIMESTAMP NOT NULL DEFAULT now(),
            CONSTRAINT chk_estado CHECK (estado IN ('generado', 'muestra_insuficiente', 'sin_datos')),
            CONSTRAINT chk_reporte_json_si_generado
                CHECK (estado <> 'generado' OR reporte_json IS NOT NULL)
        );

        CREATE TABLE IF NOT EXISTS f4_consultas_directivo (
            id SERIAL PRIMARY KEY,
            cod_modular VARCHAR NOT NULL,
            userid_directivo INTEGER NOT NULL,
            courseid INTEGER NOT NULL,
            cmid INTEGER NOT NULL,
            id_reporte_directivo INTEGER NOT NULL REFERENCES f4_reportes_directivo(id),
            fecha_consulta TIMESTAMP NOT NULL DEFAULT now(),
            CONSTRAINT uq_cod_modular_userid UNIQUE (cod_modular, userid_directivo)
        );

        CREATE INDEX IF NOT EXISTS idx_consultas_cod_modular ON f4_consultas_directivo (cod_modular);
        CREATE INDEX IF NOT EXISTS idx_consultas_userid ON f4_consultas_directivo (userid_directivo);
        CREATE INDEX IF NOT EXISTS idx_consultas_reporte ON f4_consultas_directivo (id_reporte_directivo);
    """
    async with pool_cache.connection() as conn:
        await conn.execute(ddl)
        await conn.commit()


# =============================================================================
# Configuración de negocio
# =============================================================================
CAMPO_FILTRO_IE = "cod_modular"
# Cargos que cuentan como "docente" en base_ebr_IA para pct_cobertura.
# La comparación en SQL se hace con UPPER() en ambos lados (ver
# contar_docentes_total) para no depender del casing exacto que traiga
# la columna real -- se confirmó que viene en mayúsculas ("DOCENTE",
# "DIRECTIVO", "JERARQUICO" o similar), pero no asumimos que siempre
# será consistente.
CARGOS_DOCENTE = ("DOCENTE", "JERARQUICO")

# Prioridad de las brechas (crítica>estructural>de ajuste) y de niveles (inicio>en_proceso)
PRIORIDAD_TIPO = {"Crítica": 3, "Estructural": 2, "De ajuste": 1}
PRIORIDAD_NIVEL = {"inicio": 2, "en_proceso": 1}

# Umbrales de clasificación institucional
UMBRAL_FORTALEZA_PCT_LOGRADO = 60
UMBRAL_OPORTUNIDAD_PCT_EN_PROCESO = 50
UMBRAL_DESAFIO_PCT_INICIAL = 40
UMBRAL_DESAFIO_PCT_BRECHA = 30
UMBRAL_MUESTRA_INSUFICIENTE = 5

# Cursos y cmids de docentes
CMIDS_DOCENTES_POR_CURSO: dict[int, list[int] | None] = {
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


def _cmids_docentes_configurados() -> list[int]:
    """Aplana todos los cmids de cursos que YA tienen configuración."""
    return [
        cmid
        for cmids in CMIDS_DOCENTES_POR_CURSO.values()
        if cmids is not None
        for cmid in cmids
    ]


# =============================================================================
# Acceso a datos
# =============================================================================

async def resolver_cod_modular(userid: int) -> dict:
    """Busca la IE / cod_modular asociada al userid del directivo.
    Retorna {"cod_modular": ..., "nombre_ie": ...}."""
    query = """
        SELECT cod_modular, nombre_ie
        FROM public."base_ebr_IA"
        WHERE userid = %(userid)s
        LIMIT 1
    """
    async with pool_datos.connection() as conn:
        row = await (await conn.execute(query, {"userid": userid})).fetchone()

    if row is None:
        log.warning("directivo_no_encontrado", userid=userid)
        raise DirectivoNoEncontradoError(f"userid {userid} no tiene IE asociada")

    log.info("cod_modular_resuelto", userid=userid, cod_modular=row["cod_modular"])
    return row


async def contar_docentes_total(cod_modular: str) -> int:
    """Cuenta docentes+jerárquicos en base_ebr_IA para esta IE. UPPER()
    en ambos lados de la comparación de cargo: la columna real trae
    valores en mayúsculas, pero no dependemos de un casing exacto."""
    query = f"""
        SELECT COUNT(*) AS total
        FROM public."base_ebr_IA"
        WHERE {CAMPO_FILTRO_IE} = %(valor_filtro)s
          AND UPPER(cargo) = ANY(%(cargos)s)
    """
    async with pool_datos.connection() as conn:
        row = await (await conn.execute(
            query, {"valor_filtro": cod_modular, "cargos": list(CARGOS_DOCENTE)}
        )).fetchone()

    return row["total"] if row else 0


async def obtener_filas_vista(cod_modular: str) -> list[dict]:
    """Trae todas las filas, solo con evidencias con status = 'success'."""
    cmids_validos = _cmids_docentes_configurados()

    if not cmids_validos:
        log.error("sin_cmids_configurados")
        raise SinDatosDisponiblesError(
            "Ningún curso de docentes tiene cmids válidos configurados todavía"
        )

    query = """
        SELECT user_id, criterion_index, nivel_obtenido, brecha, tipo_brecha
        FROM public.vw_processing_brechas_ebr
        WHERE cod_modular = %(cod_modular)s
          AND cmid = ANY(%(cmids_validos)s)
          AND status = 'success'
        ORDER BY user_id, criterion_index
    """
    async with pool_datos.connection() as conn:
        rows = await (await conn.execute(
            query, {"cod_modular": cod_modular, "cmids_validos": cmids_validos}
        )).fetchall()

    log.info("filas_vista_obtenidas", cod_modular=cod_modular, n_filas=len(rows))
    return rows


async def contar_docentes_evaluados(cod_modular: str) -> int:
    """
    Cuenta cuántos docentes DISTINTOS tienen al menos una evidencia
    'success' para esta IE, sin traer las filas completas. Más liviana
    que obtener_filas_vista + agrupar_por_docente -- usada por el
    endpoint de consulta (que debe ser rápido) para decidir la acción
    sin pagar el costo de traer todo el detalle por criterio.
    """
    cmids_validos = _cmids_docentes_configurados()
    if not cmids_validos:
        return 0

    query = """
        SELECT COUNT(DISTINCT user_id) AS total
        FROM public.vw_processing_brechas_ebr
        WHERE cod_modular = %(cod_modular)s
          AND cmid = ANY(%(cmids_validos)s)
          AND status = 'success'
    """
    async with pool_datos.connection() as conn:
        row = await (await conn.execute(
            query, {"cod_modular": cod_modular, "cmids_validos": cmids_validos}
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
            f["criterion_index"],
        ),
    )
    return [f["brecha"] for f in candidatas_ordenadas[:2]]


def calcular_distribucion_por_criterio(filas: list[dict]) -> list[dict]:
    por_criterio: dict[int, dict[str, int]] = {}

    for fila in filas:
        c = fila["criterion_index"]
        nivel = fila["nivel_obtenido"]
        conteos = por_criterio.setdefault(c, {"inicio": 0, "en_proceso": 0, "logrado": 0})
        if nivel in conteos:
            conteos[nivel] += 1

    resultado = []
    for criterio_id, conteos in sorted(por_criterio.items()):
        total = sum(conteos.values())
        if total == 0:
            continue
        resultado.append({
            "criterio_id": f"C{criterio_id}",
            "pct_inicial": round(100 * conteos["inicio"] / total),
            "pct_en_proceso": round(100 * conteos["en_proceso"] / total),
            "pct_logrado": round(100 * conteos["logrado"] / total),
        })
    return resultado


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


def clasificar_fortalezas_y_oportunidades(distribucion: list[dict]) -> tuple[list[dict], list[dict]]:
    """Fortaleza: criterio >= 60% logrado. Oportunidad: criterio >= 50% en
    proceso."""
    fortalezas, oportunidades = [], []

    for c in distribucion:
        if c["pct_logrado"] >= UMBRAL_FORTALEZA_PCT_LOGRADO:
            fortalezas.append({"id": c["criterio_id"], "pct": c["pct_logrado"]})
        elif c["pct_en_proceso"] >= UMBRAL_OPORTUNIDAD_PCT_EN_PROCESO:
            oportunidades.append({"id": c["criterio_id"], "pct": c["pct_en_proceso"]})

    return fortalezas, oportunidades


def clasificar_desafios_prioritarios(distribucion: list[dict], frecuencia_brechas: dict[str, dict]) -> list[dict]:
    """Desafío: criterio >= 40% en inicial, O brecha presente en >= 30%
    de las entregas (ya reducidas)."""
    desafios = []

    for c in distribucion:
        if c["pct_inicial"] >= UMBRAL_DESAFIO_PCT_INICIAL:
            desafios.append({"tipo": "criterio", "id": c["criterio_id"], "pct": c["pct_inicial"]})

    for brecha_id, info in frecuencia_brechas.items():
        if info["pct"] >= UMBRAL_DESAFIO_PCT_BRECHA:
            desafios.append({"tipo": "brecha", "id": brecha_id, "pct": info["pct"]})

    return desafios


# =============================================================================
# Redacción del texto narrativo (LLM)
# =============================================================================

def _construir_prompt_reporte(v: dict) -> str:
    """Prompt con las restricciones duras: sin identificar docentes, sin
    formato de carta ni placeholders, usando exactamente los porcentajes
    dados (sin mezclar pct_logrado con pct_en_proceso)."""
    return f"""
Redacta un reporte institucional breve para un directivo de una IE del
sistema educativo peruano, en español, tono institucional orientado a la
toma de decisiones colegiadas. No nombres ni des pistas sobre ningún
docente individual: todas las cifras son agregadas.

Datos:
- IE: {v.get('nombre_ie')}
- Docentes totales registrados: {v.get('n_docentes_total')}
- Docentes evaluados: {v.get('n_docentes_evaluados')}
- Cobertura: {v.get('pct_cobertura')}%
- Muestra insuficiente: {v.get('muestra_insuficiente')}
- Distribución por criterio: {v.get('distribucion_por_criterio')}
- Fortalezas predominantes (id de criterio y su % en nivel logrado): {v.get('fortalezas_predominantes')}
- Oportunidades de mejora comunes (id de criterio y su % en proceso): {v.get('oportunidades_comunes')}
- Desafíos prioritarios: {v.get('desafios_prioritarios')}

Reglas estrictas de formato y contenido:
- NO uses formato de carta formal. Sin fecha, sin "Dirigido a:", sin
  "Atentamente", sin nombre de remitente ni destinatario, y sobre todo
  SIN placeholders entre corchetes como "[Nombre del directivo]" o
  "[Fecha del reporte]". Si no tienes un dato, simplemente no lo
  menciones -- nunca inventes un marcador de posición.
- Al citar el porcentaje de una fortaleza, usa EXACTAMENTE el % que viene
  junto a ese criterio en "Fortalezas predominantes" (es su % en nivel
  logrado). Al citar una oportunidad de mejora, usa EXACTAMENTE el % que
  viene junto a ese criterio en "Oportunidades de mejora comunes" (es su
  % en proceso, NO el % logrado). No calcules ni copies un porcentaje de
  otra sección de los datos.
- Si "Muestra insuficiente" es verdadero, evita expresiones categóricas
  como "la mayoría" o "el patrón institucional"; usa formulaciones
  referenciales ("de los docentes evaluados hasta la fecha...").
- No compares esta IE con otras instituciones. No uses lenguaje
  evaluativo sobre docentes individuales.
""".strip()


async def redactar_texto_reporte(variables: dict) -> str:
    """Llama al LLM para redactar texto_reporte a partir de las variables
    ya calculadas. El LLM no decide clasificaciones, solo redacta prosa."""
    prompt = _construir_prompt_reporte(variables)
    inicio = time.monotonic()

    try:
        texto = await generar_texto_narrativo(prompt)
    except Exception:
        duracion_ms = round((time.monotonic() - inicio) * 1000)
        log.error(
            "llm_llamada_fallida",
            cod_modular=variables.get("cod_modular"),
            duracion_ms=duracion_ms,
            exc_info=True,
        )
        raise

    duracion_ms = round((time.monotonic() - inicio) * 1000)
    log.info(
        "llm_texto_generado",
        cod_modular=variables.get("cod_modular"),
        duracion_ms=duracion_ms,
        chars_generados=len(texto),
    )
    return texto


# =============================================================================
# Orquestador de generación
# =============================================================================

async def generar_reporte(cod_modular: str, nombre_ie: str) -> dict:
    filas = await obtener_filas_vista(cod_modular)
    if not filas:
        raise SinDatosDisponiblesError(f"Sin datos en la vista para {cod_modular}")

    filas_por_docente = agrupar_por_docente(filas)
    n_docentes_evaluados = len(filas_por_docente)

    if n_docentes_evaluados < UMBRAL_MUESTRA_INSUFICIENTE:
        log.info(
            "generacion_bloqueada_muestra_insuficiente",
            cod_modular=cod_modular,
            n_docentes_evaluados=n_docentes_evaluados,
        )
        raise MuestraInsuficienteError(
            f"{cod_modular} tiene {n_docentes_evaluados} docentes evaluados, "
            f"mínimo requerido {UMBRAL_MUESTRA_INSUFICIENTE}"
        )

    n_docentes_total = await contar_docentes_total(cod_modular)
    pct_cobertura = (
        round(100 * n_docentes_evaluados / n_docentes_total) if n_docentes_total > 0 else 0
    )

    brechas_por_docente = {
        user_id: seleccionar_brechas_globales(filas_docente)
        for user_id, filas_docente in filas_por_docente.items()
    }

    distribucion = calcular_distribucion_por_criterio(filas)
    frecuencia_brechas = calcular_frecuencia_brechas(brechas_por_docente)
    fortalezas, oportunidades = clasificar_fortalezas_y_oportunidades(distribucion)
    desafios = clasificar_desafios_prioritarios(distribucion, frecuencia_brechas)

    variables = {
        "cod_modular": cod_modular,
        "nombre_ie": nombre_ie,
        "fecha_generacion": datetime.now().isoformat(),
        "n_docentes_total": n_docentes_total,
        "n_docentes_evaluados": n_docentes_evaluados,
        "pct_cobertura": pct_cobertura,
        "muestra_insuficiente": False,  # siempre False aquí: generar_reporte
        # ya bloqueó arriba si n_docentes_evaluados < UMBRAL_MUESTRA_INSUFICIENTE
        "distribucion_por_criterio": distribucion,
        "fortalezas_predominantes": fortalezas,
        "oportunidades_comunes": oportunidades,
        "desafios_prioritarios": desafios,
    }
    variables["texto_reporte"] = await redactar_texto_reporte(variables)
    return variables


# =============================================================================
# Caché
# =============================================================================

async def obtener_reporte_guardado(cod_modular: str) -> Optional[dict]:
    """Trae la fila de f4_reportes_directivo para esta IE, sin importar el
    estado (generado, muestra_insuficiente, sin_datos) -- o None si esta
    IE nunca tuvo ni un intento de generación."""
    query = """
        SELECT id, cod_modular, estado, n_docentes_evaluados, reporte_json,
               fecha_generacion, fecha_actualizacion
        FROM f4_reportes_directivo
        WHERE cod_modular = %(cod_modular)s
    """
    async with pool_cache.connection() as conn:
        return await (await conn.execute(query, {"cod_modular": cod_modular})).fetchone()


async def guardar_estado_reporte(
    cod_modular: str,
    estado: str,
    n_docentes_evaluados: int,
    reporte_json: Optional[dict] = None,
) -> int:
    """
    Upsert de la fila de f4_reportes_directivo para esta IE. estado es uno
    de 'generado', 'muestra_insuficiente', 'sin_datos'. reporte_json
    solo se pasa (y solo debe pasarse) cuando estado='generado'.

    Como hay una sola fila por cod_modular, esto cubre tanto el primer
    intento (exitoso o no) como los siguientes -- INSERT si es la
    primera vez que se toca esta IE, UPDATE si ya existía una fila
    (sin importar cuál era su estado anterior).
    """
    query = """
        INSERT INTO f4_reportes_directivo (cod_modular, estado, n_docentes_evaluados, reporte_json)
        VALUES (%(cod_modular)s, %(estado)s, %(n)s, %(reporte)s)
        ON CONFLICT (cod_modular) DO UPDATE SET
            estado = EXCLUDED.estado,
            n_docentes_evaluados = EXCLUDED.n_docentes_evaluados,
            reporte_json = COALESCE(EXCLUDED.reporte_json, f4_reportes_directivo.reporte_json),
            fecha_actualizacion = now()
        RETURNING id
    """
    async with pool_cache.connection() as conn:
        row = await (await conn.execute(query, {
            "cod_modular": cod_modular,
            "estado": estado,
            "n": n_docentes_evaluados,
            "reporte": Jsonb(reporte_json) if reporte_json is not None else None,
        })).fetchone()
        await conn.commit()

    return row["id"]


async def registrar_consulta(
    cod_modular: str, userid_directivo: int, courseid: int, cmid: int, id_reporte_directivo: int
) -> None:
    """Asocia un directivo con el reporte de su IE. Un registro único por
    (cod_modular, userid_directivo)"""
    query = """
        INSERT INTO f4_consultas_directivo
            (cod_modular, userid_directivo, courseid, cmid, id_reporte_directivo)
        VALUES (%(cod_modular)s, %(userid)s, %(courseid)s, %(cmid)s, %(id_reporte)s)
        ON CONFLICT (cod_modular, userid_directivo) DO NOTHING
    """
    async with pool_cache.connection() as conn:
        await conn.execute(query, {
            "cod_modular": cod_modular,
            "userid": userid_directivo,
            "courseid": courseid,
            "cmid": cmid,
            "id_reporte": id_reporte_directivo,
        })
        await conn.commit()


# =============================================================================
# Puntos de entrada
# =============================================================================

async def consultar_reporte_existente(userid: int) -> dict:
    """
    GET /reporte/consultar -- endpoint rápido de solo lectura, que además
    decide qué debe hacer el frontend a continuación, sin generar nada
    él mismo (nunca toca el LLM). Cuatro situaciones:

      1. No hay reporte, y ya hay >= UMBRAL_MUESTRA_INSUFICIENTE docentes
         evaluados -> accion="generar" (el frontend llama a
         POST /reporte/generar automáticamente, sin botón visible).
      2. Hay reporte, y el número de docentes evaluados no cambió desde
         que se generó -> accion="mostrar", reporte vigente tal cual.
      3. Hay reporte, pero el número de docentes evaluados cambió ->
         accion="generar" (el frontend dispara la regeneración).
      4. No hay reporte, y aún no se llega al mínimo de docentes ->
         accion="esperar_docentes". NO se debe llamar a generar en este
         caso -- generar produciría igual un reporte con
         muestra_insuficiente=true, gastando una llamada al LLM sin
         necesidad. El frontend muestra un estado de espera.

    Retorna siempre {"accion": ..., "reporte": ... | None, "n_docentes_evaluados": ...}
    """
    inicio = time.monotonic()
    log.info("reporte_consultado", userid=userid)

    ie = await resolver_cod_modular(userid)
    cod_modular = ie["cod_modular"]

    guardado = await obtener_reporte_guardado(cod_modular)
    n_actual = await contar_docentes_evaluados(cod_modular)

    duracion_ms = round((time.monotonic() - inicio) * 1000)

    if guardado is None:
        if n_actual < UMBRAL_MUESTRA_INSUFICIENTE:
            log.info(
                "consulta_esperar_docentes",
                cod_modular=cod_modular,
                n_docentes_evaluados=n_actual,
                duracion_ms=duracion_ms,
            )
            return {"accion": "esperar_docentes", "reporte": None, "n_docentes_evaluados": n_actual}

        log.info(
            "consulta_debe_generar",
            cod_modular=cod_modular,
            motivo="sin_registro_previo",
            n_docentes_evaluados=n_actual,
            duracion_ms=duracion_ms,
        )
        return {"accion": "generar", "reporte": None, "n_docentes_evaluados": n_actual}

    if n_actual != guardado["n_docentes_evaluados"]:
        log.info(
            "consulta_debe_generar",
            cod_modular=cod_modular,
            motivo="nueva_actividad_docente",
            n_docentes_guardado=guardado["n_docentes_evaluados"],
            n_docentes_evaluados=n_actual,
            duracion_ms=duracion_ms,
        )
        return {"accion": "generar", "reporte": None, "n_docentes_evaluados": n_actual}

    log.info("consulta_mostrar_vigente", cod_modular=cod_modular, duracion_ms=duracion_ms)
    return {"accion": "mostrar", "reporte": guardado["reporte_json"], "n_docentes_evaluados": n_actual}


async def obtener_o_generar_reporte(userid: int, courseid: int, cmid: int) -> dict:
    """
    POST /reporte/generar -- pipeline completo:
      1. Resuelve cod_modular desde userid.
      2. Si no hay fila en reportes_directivo -> intenta generar.
      3. Si hay fila con estado='generado' -> compara n_docentes_evaluados
         actual vs guardado; si cambió, recalcula; si no, sirve el guardado.
      4. Si hay fila con estado distinto de 'generado' (intento previo
         fallido) -> reintenta generar con los datos actuales.
      5. Cualquier intento (éxito o falla) actualiza el estado guardado,
         para que el directivo sepa por qué no tiene informe todavía.
      6. Registra la asociación directivo-reporte solo si se logra
         generar (courseid/cmid: trazabilidad).
    """
    inicio = time.monotonic()
    log.info("reporte_solicitado", userid=userid, courseid=courseid, cmid=cmid)

    ie = await resolver_cod_modular(userid)
    cod_modular = ie["cod_modular"]
    nombre_ie = ie["nombre_ie"]

    guardado = await obtener_reporte_guardado(cod_modular)

    debe_generar = (
        guardado is None
        or guardado["estado"] != "generado"
    )

    if guardado is not None and guardado["estado"] == "generado":
        filas_actuales = await obtener_filas_vista(cod_modular)
        n_actual = len(agrupar_por_docente(filas_actuales))
        debe_generar = n_actual != guardado["n_docentes_evaluados"]
        if debe_generar:
            log.info(
                "cache_invalidado",
                cod_modular=cod_modular,
                n_docentes_guardado=guardado["n_docentes_evaluados"],
                n_docentes_actual=n_actual,
            )

    if not debe_generar:
        log.info("cache_hit", cod_modular=cod_modular, n_docentes=guardado["n_docentes_evaluados"])
        id_reporte = guardado["id"]
        reporte = guardado["reporte_json"]
    else:
        log.info("cache_miss_o_reintento", cod_modular=cod_modular)
        try:
            reporte = await generar_reporte(cod_modular, nombre_ie)
        except MuestraInsuficienteError:
            n_actual = await contar_docentes_evaluados(cod_modular)
            await guardar_estado_reporte(cod_modular, "muestra_insuficiente", n_actual)
            raise
        except SinDatosDisponiblesError:
            await guardar_estado_reporte(cod_modular, "sin_datos", 0)
            raise

        id_reporte = await guardar_estado_reporte(
            cod_modular, "generado", reporte["n_docentes_evaluados"], reporte
        )

    await registrar_consulta(cod_modular, userid, courseid, cmid, id_reporte)

    duracion_ms = round((time.monotonic() - inicio) * 1000)
    log.info("reporte_completado", userid=userid, cod_modular=cod_modular, duracion_ms=duracion_ms)

    return reporte