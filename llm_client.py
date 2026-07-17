"""
llm_client.py

Cliente async hacia Azure OpenAI, usado para redactar texto_reporte a
partir de las variables ya calculadas por core.py. El LLM no decide
fortalezas, oportunidades ni desafíos -- solo redacta la prosa final.

Versión async (AsyncAzureOpenAI): evita bloquear el event loop de FastAPI
mientras se espera la respuesta del modelo.
"""

import os
import warnings
import httpx
from openai import AsyncAzureOpenAI

# Variables de entorno esperadas (ajustar nombres según el despliegue real):
#   AZURE_OPENAI_ENDPOINT    -> https://<recurso>.openai.azure.com/
#   AZURE_OPENAI_API_KEY
#   AZURE_OPENAI_API_VERSION -> ej. "2024-10-21"
#   AZURE_OPENAI_DEPLOYMENT  -> nombre del deployment del modelo (no el modelo en sí)

# ---------------------------------------------------------------------------
# SOLO PARA DESARROLLO LOCAL detrás de un proxy corporativo que intercepta
# TLS con certificado autofirmado. Desactiva la verificación SSL cuando
# DISABLE_SSL_VERIFY_DEV_ONLY=true está en el entorno.
#
# NUNCA activar esta variable en producción -- deja el tráfico (incluida
# la API key de Azure y los datos del prompt) sin protección contra
# intercepción. Es un parche temporal mientras se configura el
# certificado corporativo real (ver setup_certs.py).
# ---------------------------------------------------------------------------

_ssl_verify_disabled = os.environ.get("DISABLE_SSL_VERIFY_DEV_ONLY", "false").lower() == "true"

if _ssl_verify_disabled:
    warnings.warn(
        "DISABLE_SSL_VERIFY_DEV_ONLY está activo: la verificación SSL hacia "
        "Azure OpenAI está DESACTIVADA. Esto es inseguro y solo debe usarse "
        "en desarrollo local. Configura el certificado corporativo real "
        "(setup_certs.py) antes de desplegar a producción.",
        stacklevel=1,
    )
    _http_client = httpx.AsyncClient(verify=False)
else:
    _http_client = None  # usa la verificación SSL normal del sistema

client = AsyncAzureOpenAI(
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    http_client=_http_client,
)

DEPLOYMENT = os.environ["AZURE_OPENAI_DEPLOYMENT"]


async def generar_texto_narrativo(prompt: str) -> str:
    """
    Envía el prompt a Azure OpenAI (async) y retorna el texto narrativo
    generado.
    """
    response = await client.chat.completions.create(
        model=DEPLOYMENT,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800,
        temperature=0.4,
    )
    return response.choices[0].message.content