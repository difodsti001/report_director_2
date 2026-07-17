"""
app.py
Endpoint del asistente de reporte institucional para el directivo.
"""
import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

import os
from logging_config import configurar_logging, get_logger
configurar_logging(nivel=os.environ.get("LOG_LEVEL", "INFO"))
log = get_logger(__name__)
 
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import core
 
 
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("app_startup")
    await core.abrir_pools()
    yield
    log.info("app_shutdown")
    await core.cerrar_pools()
 
 
app = FastAPI(title="Reporte institucional - Hito 1 Directivo", lifespan=lifespan)
 
 
class ReporteRequest(BaseModel):
    userid: int
 
 
@app.get("/health")
async def health():
    return {"status": "ok"}
 
 
@app.get("/reporte/consultar")
async def consultar_reporte(userid: int):
    try:
        reporte = await core.consultar_reporte_existente(userid)
        return reporte
 
    except core.DirectivoNoEncontradoError:
        log.warning("endpoint_consultar_directivo_no_encontrado", userid=userid)
        raise HTTPException(
            status_code=404,
            detail=f"No se encontró IE asociada al userid {userid}",
        )
 
    except core.ReporteNoExisteError:
        log.info("endpoint_consultar_no_existe", userid=userid)
        raise HTTPException(
            status_code=404,
            detail="Aún no se ha generado un reporte para esta IE. Usa POST /reporte/generar.",
        )
 
    except Exception:
        log.error("endpoint_consultar_error_no_manejado", userid=userid, exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno al consultar el reporte")
 
 
@app.post("/reporte/generar")
async def generar_reporte_endpoint(payload: ReporteRequest, courseid: int, cmid: int):
    """
    Pipeline completo: resuelve IE, verifica caché con invalidación por
    n_docentes_evaluados, genera con LLM si hace falta, guarda y registra
    la consulta. Puede tardar varios segundos si dispara la generación.
 
    courseid y cmid llegan como query params desde la URL de Moodle
    (?courseid=123&cmid=456), el aula desde la que el directivo accede. 
    Se usa solo como dato de trazabilidad al registrar la consulta.
    """
    try:
        reporte = await core.obtener_o_generar_reporte(payload.userid, courseid, cmid)
        return reporte
 
    except core.DirectivoNoEncontradoError:
        log.warning("endpoint_generar_directivo_no_encontrado", userid=payload.userid)
        raise HTTPException(
            status_code=404,
            detail=f"No se encontró IE asociada al userid {payload.userid}",
        )
 
    except core.SinDatosDisponiblesError:
        log.warning("endpoint_generar_sin_datos", userid=payload.userid, courseid=courseid)
        raise HTTPException(
            status_code=404,
            detail="No hay evidencias de Sesión V1 registradas todavía para esta IE",
        )
 
    except Exception:
        log.error(
            "endpoint_generar_error_no_manejado",
            userid=payload.userid,
            courseid=courseid,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Error interno al generar el reporte")