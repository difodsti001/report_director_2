"""
core.py

Lógica de negocio para el asistente de reporte institucional del directivo.

Dos bases de datos, dos pools separados:
- pool_datos:  base con public."base_ebr_IA" y public.vw_processing_brechas_ebr
- pool_cache:  base con reportes_directivo y consultas_directivo
"""

from datetime import datetime
from typing import Optional
import os
import time
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import psycopg
 
from llm_client import generar_texto_narrativo
from logging_config import get_logger
 
log = get_logger(__name__)
 
DSN_DATOS = os.environ["DSN_DATOS"]
DSN_CACHE = os.environ["DSN_CACHE"]

_KEEPALIVE_PARAMS = "keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=3"
 
def _con_keepalives(dsn: str) -> str:
    separador = "&" if "?" in dsn else "?"
    return f"{dsn}{separador}{_KEEPALIVE_PARAMS}"
 
DSN_DATOS = _con_keepalives(DSN_DATOS)
DSN_CACHE = _con_keepalives(DSN_CACHE)

_MAX_IDLE = int(os.environ.get("POOL_MAX_IDLE_SECONDS", "300"))
 
pool_datos = AsyncConnectionPool(
    conninfo=DSN_DATOS, min_size=1, max_size=10,
    kwargs={"row_factory": dict_row}, open=False,
    max_idle=_MAX_IDLE,
    check=AsyncConnectionPool.check_connection,
)
pool_cache = AsyncConnectionPool(
    conninfo=DSN_CACHE, min_size=1, max_size=10,
    kwargs={"row_factory": dict_row}, open=False,
    max_idle=_MAX_IDLE,
    check=AsyncConnectionPool.check_connection,
)


async def abrir_pools() -> None:
    """
    Llamar en el evento startup de FastAPI.
    """
    await pool_datos.open()
    await pool_cache.open()
    await asegurar_schema_cache()


async def asegurar_schema_cache() -> None:
    """
    Crea las tablas de caché si no existen todavía. Idempotente -- seguro
    de correr en cada arranque del servicio.
    """
    ddl = """
        CREATE TABLE IF NOT EXISTS reportes_directivo (
            id SERIAL PRIMARY KEY,
            cod_modular VARCHAR NOT NULL UNIQUE,
            n_docentes_evaluados INTEGER NOT NULL,
            reporte_json JSONB NOT NULL,
            fecha_generacion TIMESTAMP NOT NULL DEFAULT now(),
            fecha_actualizacion TIMESTAMP NOT NULL DEFAULT now()
        );
 
        CREATE TABLE IF NOT EXISTS consultas_directivo (
            id SERIAL PRIMARY KEY,
            cod_modular VARCHAR NOT NULL,
            userid_directivo INTEGER NOT NULL,
            courseid INTEGER NOT NULL,
            cmid INTEGER NOT NULL,
            id_reporte_directivo INTEGER NOT NULL REFERENCES reportes_directivo(id),
            fecha_consulta TIMESTAMP NOT NULL DEFAULT now(),
            CONSTRAINT uq_cod_modular_userid UNIQUE (cod_modular, userid_directivo)
        );
 
        CREATE INDEX IF NOT EXISTS idx_consultas_cod_modular ON consultas_directivo (cod_modular);
        CREATE INDEX IF NOT EXISTS idx_consultas_userid ON consultas_directivo (userid_directivo);
        CREATE INDEX IF NOT EXISTS idx_consultas_reporte ON consultas_directivo (id_reporte_directivo);
    """
    async with pool_cache.connection() as conn:
        await conn.execute(ddl)
        await conn.commit()
 
 
async def cerrar_pools() -> None:
    """Llamar en el evento shutdown de FastAPI."""
    await pool_datos.close()
    await pool_cache.close()


# ---------------------------------------------------------------------------
# Filtro principal de institución.
# ---------------------------------------------------------------------------

CAMPO_FILTRO_IE = "cod_modular"  # cambiar a "IE" si se requiere ese campo


# ---------------------------------------------------------------------------
# Cursos y cmids de DOCENTES
# ---------------------------------------------------------------------------

CMIDS_DOCENTES_POR_CURSO: dict[int, list[int] | None] = {
    2800: [204186, 204187, 204188, 204202, 204192],
    2920: None,
    2927: None,
    2932: None,
    2935: None,
    2938: None,
    2941: None,
    2944: None,
    2947: None,
    2950: None,
    2953: None,
    2956: None,
    2959: None,
    2962: None,
    2965: None,
    2968: None,
    2971: None,
    2974: None,
    2977: None,
    2980: None,
    2983: None,
    2986: None,
    2989: None,
    2992: None,
    2995: None,
}


def obtener_cmids_docentes(course_id: int) -> list[int]:
    """
    Retorna los cmids válidos configurados para un course_id de docentes.
    """
    cmids = CMIDS_DOCENTES_POR_CURSO[course_id]
    if cmids is None:
        raise CursoDocentesNoConfiguradoError(
            f"course_id {course_id} está registrado pero sin cmids válidos definidos aún"
        )
    return cmids


class CursoDocentesNoConfiguradoError(Exception):
    """El course_id de docentes no tiene cmids válidos configurados en
    CMIDS_DOCENTES_POR_CURSO."""



# ---------------------------------------------------------------------------
# Cargos que cuentan como docente en base_ebr_IA
# ---------------------------------------------------------------------------
CARGOS_DOCENTE = ("docente", "jerarquico")

# ---------------------------------------------------------------------------
# Reglas de prioridad de brechas
# ---------------------------------------------------------------------------

PRIORIDAD_TIPO = {"Crítica": 3, "Estructural": 2, "De ajuste": 1}
PRIORIDAD_NIVEL = {"inicio": 2, "en_proceso": 1}


class DirectivoNoEncontradoError(Exception):
    """El userid no resuelve a ninguna IE en base_ebr_IA."""

class SinDatosDisponiblesError(Exception):
    """No hay filas en vw_processing_brechas_ebr para ese cod_modular."""

# ---------------------------------------------------------------------------
# Paso 1: resolver cod_modular desde userid (base_ebr_IA)
# ---------------------------------------------------------------------------

async def resolver_cod_modular(userid: int) -> dict:
    """
    Busca la IE / cod_modular asociada al userid del directivo.
    Retorna {"cod_modular": ..., "nombre_ie": ...}
    """
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
    """
    Cuenta el total de docentes (cargo = docente o jerárquico) registrados.
    """
    query = f"""
        SELECT COUNT(*) AS total
        FROM public."base_ebr_IA"
        WHERE {CAMPO_FILTRO_IE} = %(valor_filtro)s
          AND cargo = ANY(%(cargos)s)
    """
    async with pool_datos.connection() as conn:
        row = await (await conn.execute(
            query, {"valor_filtro": cod_modular, "cargos": list(CARGOS_DOCENTE)}
        )).fetchone()
 
    return row["total"] if row else 0


# ---------------------------------------------------------------------------
# Paso 2: traer filas crudas de la vista
# ---------------------------------------------------------------------------

async def obtener_filas_vista(cod_modular: str) -> list[dict]:
    """
    Trae todas las filas (una por criterio evaluado, por docente) de la IE.
    Solo evidencias con status = 'success'.
    """
    cmids_validos = [
        cmid
        for cmids_del_curso in CMIDS_DOCENTES_POR_CURSO.values()
        if cmids_del_curso is not None
        for cmid in cmids_del_curso
    ]
 
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


# ---------------------------------------------------------------------------
# Paso 3: selección de brechas globales por docente
# ---------------------------------------------------------------------------

def seleccionar_brechas_globales(filas_docente: list[dict]) -> list[str]:
    """
    Aplica las 5 reglas de prioridad y retorna hasta 2 IDs de brecha.
    """
    candidatas = [f for f in filas_docente if f.get("brecha") is not None]
 
    if not candidatas:
        return []
 
    candidatas_ordenadas = sorted(
        candidatas,
        key=lambda f: (
            -PRIORIDAD_TIPO.get(f["tipo_brecha"], 0),
            -PRIORIDAD_NIVEL.get(f["nivel_obtenido"], 0),
            f["criterion_index"],
        ),
    )
 
    seleccion = candidatas_ordenadas[:2]

    # Regla 5: si ninguna candidata es Crítica ni Estructural, no se selecciona ninguna brecha.
    hay_critica_o_estructural = any(
        f["tipo_brecha"] in ("Crítica", "Estructural") for f in candidatas
    )
    if not hay_critica_o_estructural:
        return []
 
    return [f["brecha"] for f in seleccion]


def agrupar_por_docente(filas: list[dict]) -> dict[int, list[dict]]:
    """Agrupa las filas crudas de la vista por user_id (docente)."""
    agrupado: dict[int, list[dict]] = {}
    for fila in filas:
        agrupado.setdefault(fila["user_id"], []).append(fila)
    return agrupado


# ---------------------------------------------------------------------------
# Paso 4: agregación institucional (nivel 2)
# ---------------------------------------------------------------------------

def calcular_distribucion_por_criterio(filas: list[dict]) -> list[dict]:
    """
    Distribución de niveles (inicial/en_proceso/logrado) por criterio,
    sobre todas las filas crudas de la IE (no sobre brechas reducidas).
    """
    por_criterio: dict[int, dict[str, int]] = {}
 
    for fila in filas:
        c = fila["criterion_index"]
        nivel = fila["nivel_obtenido"]
        por_criterio.setdefault(c, {"inicio": 0, "en_proceso": 0, "logrado": 0})
        if nivel in por_criterio[c]:
            por_criterio[c][nivel] += 1
 
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
    """
    Cuenta la frecuencia de cada brecha SOBRE LAS LISTAS YA REDUCIDAS
    """
    n_docentes = len(brechas_por_docente)
    if n_docentes == 0:
        return {}
 
    conteos: dict[str, int] = {}
    for lista_brechas in brechas_por_docente.values():
        for brecha_id in lista_brechas:
            conteos[brecha_id] = conteos.get(brecha_id, 0) + 1
 
    return {
        brecha_id: {
            "conteo": conteo,
            "pct": round(100 * conteo / n_docentes),
        }
        for brecha_id, conteo in conteos.items()
    }


def clasificar_fortalezas_y_oportunidades(distribucion: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Fortaleza predominante: criterio con >= 60% en nivel logrado.
    Oportunidad de mejora común: criterio con >= 50% en proceso,
    que no haya calificado ya como fortaleza.
    """
    fortalezas = []
    oportunidades = []
 
    for c in distribucion:
        if c["pct_logrado"] >= 60:
            fortalezas.append({"id": c["criterio_id"], "pct": c["pct_logrado"]})
        elif c["pct_en_proceso"] >= 50:
            oportunidades.append({"id": c["criterio_id"], "pct": c["pct_en_proceso"]})
 
    return fortalezas, oportunidades


def clasificar_desafios_prioritarios(distribucion: list[dict], frecuencia_brechas: dict[str, dict]) -> list[dict]:
    """
    Desafío prioritario: criterio con >= 40% en nivel inicial,
    O brecha crítica/estructural presente en >= 30% de las entregas.
    """
    desafios = []
 
    for c in distribucion:
        if c["pct_inicial"] >= 40:
            desafios.append({"tipo": "criterio", "id": c["criterio_id"], "pct": c["pct_inicial"]})
 
    for brecha_id, info in frecuencia_brechas.items():
        if info["pct"] >= 30:
            desafios.append({"tipo": "brecha", "id": brecha_id, "pct": info["pct"]})
 
    return desafios


# ---------------------------------------------------------------------------
# Redacción del texto narrativo (LLM)
# ---------------------------------------------------------------------------

async def redactar_texto_reporte(variables: dict) -> str:
    """
    Llama al LLM para redactar texto_reporte a partir de las variables ya
    calculadas.
    """
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


def _construir_prompt_reporte(v: dict) -> str:
    """
    Arma el prompt con las restricciones duras de la ficha técnica:
    - Nunca nombrar ni permitir inferir un docente específico.
    - Tono institucional, sin lenguaje evaluativo ni comparativo entre IEs.
    - Si muestra_insuficiente, evitar lenguaje categórico ("la mayoría").
    - Español, contextualizado al sistema educativo peruano.
    - No inventar placeholders de carta formal (fecha, destinatario, firma).
    - Usar exactamente los porcentajes dados, sin recalcular ni mezclar.
    """
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


# ---------------------------------------------------------------------------
# Orquestador de generación
# ---------------------------------------------------------------------------

async def generar_reporte(cod_modular: str, nombre_ie: str) -> dict:
    filas = await obtener_filas_vista(cod_modular)
 
    if not filas:
        raise SinDatosDisponiblesError(f"Sin datos en la vista para {cod_modular}")
 
    filas_por_docente = agrupar_por_docente(filas)
    n_docentes_evaluados = len(filas_por_docente)
    muestra_insuficiente = n_docentes_evaluados < 5
 
    n_docentes_total = await contar_docentes_total(cod_modular)
    pct_cobertura = round(100 * n_docentes_evaluados / n_docentes_total) if n_docentes_total > 0 else 0
 
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
        "muestra_insuficiente": muestra_insuficiente,
        "distribucion_por_criterio": distribucion,
        "fortalezas_predominantes": fortalezas,
        "oportunidades_comunes": oportunidades,
        "desafios_prioritarios": desafios,
    }
 
    variables["texto_reporte"] = await redactar_texto_reporte(variables)
 
    return variables


#  ---------------------------------------------------------------------------
# Caché: leer, decidir invalidación, guardar
# ---------------------------------------------------------------------------
 
async def obtener_reporte_guardado(cod_modular: str) -> Optional[dict]:
    query = """
        SELECT id, cod_modular, n_docentes_evaluados, reporte_json,
               fecha_generacion, fecha_actualizacion
        FROM reportes_directivo
        WHERE cod_modular = %(cod_modular)s
    """
    async with pool_cache.connection() as conn:
        return await (await conn.execute(query, {"cod_modular": cod_modular})).fetchone()
 
 
async def guardar_reporte(cod_modular: str, n_docentes_evaluados: int, reporte_json: dict, es_actualizacion: bool) -> int:
    if es_actualizacion:
        query = """
            UPDATE reportes_directivo
            SET n_docentes_evaluados = %(n)s,
                reporte_json = %(reporte)s,
                fecha_actualizacion = now()
            WHERE cod_modular = %(cod_modular)s
            RETURNING id
        """
    else:
        query = """
            INSERT INTO reportes_directivo (cod_modular, n_docentes_evaluados, reporte_json)
            VALUES (%(cod_modular)s, %(n)s, %(reporte)s)
            RETURNING id
        """
    async with pool_cache.connection() as conn:
        row = await (await conn.execute(query, {
            "cod_modular": cod_modular,
            "n": n_docentes_evaluados,
            "reporte": Jsonb(reporte_json),
        })).fetchone()
        await conn.commit()
 
    return row["id"]
 
 
async def registrar_consulta(cod_modular: str, userid_directivo: int, courseid: int, cmid: int, id_reporte_directivo: int) -> None:
    """
    Guarda la trazabilidad de la consulta.
    """
    query = """
        INSERT INTO consultas_directivo
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
 
# ---------------------------------------------------------------------------
# Función principal usada por el endpoint
# ---------------------------------------------------------------------------
 
class ReporteNoExisteError(Exception):
    """No hay reporte guardado todavía para esta IE -- distinto de
    SinDatosDisponiblesError, que es cuando ni siquiera hay datos en la
    vista para generar uno. Este error es específico del endpoint de
    solo-consulta: significa 'aún no se generó', no 'no hay cómo generar'.
    """
 
 
async def consultar_reporte_existente(userid: int) -> dict:
    """
    Punto de entrada para GET /reporte/consultar: solo resuelve la IE y 
    lee el caché tal cual está guardado, sin verificar si está desactualizado y sin tocar el LLM.
 
    Lanza ReporteNoExisteError si no hay nada guardado todavía.
    """
    inicio = time.monotonic()
    log.info("reporte_consultado", userid=userid)
 
    ie = await resolver_cod_modular(userid)
    cod_modular = ie["cod_modular"]
 
    guardado = await obtener_reporte_guardado(cod_modular)
 
    duracion_ms = round((time.monotonic() - inicio) * 1000)
 
    if guardado is None:
        log.info("reporte_consultado_no_existe", cod_modular=cod_modular, duracion_ms=duracion_ms)
        raise ReporteNoExisteError(f"No hay reporte generado todavía para {cod_modular}")
 
    log.info("reporte_consultado_encontrado", cod_modular=cod_modular, duracion_ms=duracion_ms)
    return guardado["reporte_json"]
 
 
# ---------------------------------------------------------------------------
# Función principal usada por el endpoint de generación (pesado)
# ---------------------------------------------------------------------------
 
async def obtener_o_generar_reporte(userid: int, courseid: int, cmid: int) -> dict:
    """
    Punto de entrada para el endpoint POST /reporte.
 
    1. Resuelve cod_modular desde userid.
    2. Verifica si existe reporte guardado para ese cod_modular.
    3. Si no existe -> genera y guarda.
    4. Si existe -> compara n_docentes_evaluados actual vs guardado;
       si cambió, recalcula y sobreescribe; si no, sirve el guardado.
    5. Registra la consulta del directivo (courseid solo como trazabilidad).
    """
    inicio = time.monotonic()
    log.info("reporte_solicitado", userid=userid, courseid=courseid, cmid=cmid)
 
    ie = await resolver_cod_modular(userid)
    cod_modular = ie["cod_modular"]
    nombre_ie = ie["nombre_ie"]
 
    guardado = await obtener_reporte_guardado(cod_modular)
 
    if guardado is None:
        log.info("cache_miss", cod_modular=cod_modular, motivo="sin_registro_previo")
        reporte = await generar_reporte(cod_modular, nombre_ie)
        id_reporte = await guardar_reporte(cod_modular, reporte["n_docentes_evaluados"], reporte, es_actualizacion=False)
    else:
        filas_actuales = await obtener_filas_vista(cod_modular)
        n_actual = len(agrupar_por_docente(filas_actuales))
 
        if n_actual != guardado["n_docentes_evaluados"]:
            log.info(
                "cache_invalidado",
                cod_modular=cod_modular,
                n_docentes_guardado=guardado["n_docentes_evaluados"],
                n_docentes_actual=n_actual,
            )
            reporte = await generar_reporte(cod_modular, nombre_ie)
            id_reporte = await guardar_reporte(cod_modular, reporte["n_docentes_evaluados"], reporte, es_actualizacion=True)
        else:
            log.info("cache_hit", cod_modular=cod_modular, n_docentes=n_actual)
            reporte = guardado["reporte_json"]
            id_reporte = guardado["id"]
 
    await registrar_consulta(cod_modular, userid, courseid, cmid, id_reporte)
 
    duracion_ms = round((time.monotonic() - inicio) * 1000)
    log.info(
        "reporte_completado",
        userid=userid,
        cod_modular=cod_modular,
        duracion_ms=duracion_ms,
    )
 
    return reporte