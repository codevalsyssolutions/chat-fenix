import os
import logging
import traceback
import requests
from fastapi import FastAPI, Request, HTTPException, status
from pydantic import BaseModel, Field
from google import genai
from google.genai import types


# ==========================================
# 02. LOGGING Y TRAZABILIDAD
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("TapiceriaFenix")

# ==========================================
# 03. SEGURIDAD Y CREDENCIALES
# ==========================================
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "token_fenix")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Validar que existan las credenciales críticas antes de iniciar
if not all([META_ACCESS_TOKEN, PHONE_NUMBER_ID, GEMINI_API_KEY]):
    logger.warning("¡ALERTA! Faltan variables de entorno críticas por configurar.")

app = FastAPI(title="Chatbot Tapicería Fenix - Producción")

# Inicialización del cliente de IA
client = genai.Client(api_key=GEMINI_API_KEY)
sessions = {}

INFORMACION_EMPRESA = """
Eres el asistente virtual de Tapicería Fenix, expertos en forros y tapicería automotriz.
Sé amable, conciso y vendedor. Tu objetivo es asesorar sobre forros de asientos, volantes y pisos.
Si un usuario muestra frustración o pide hablar con un humano, indícale que un asesor lo contactará pronto.
"""

# ==========================================
# 06. ESCALACIÓN Y FALLBACKS
# ==========================================
MENSAJE_FALLBACK_HUMANO = (
    "En este momento nuestro asistente automático presenta un inconveniente técnico. "
    "Un asesor humano de Tapicería Fenix revisará tu mensaje muy pronto."
)

def get_or_create_chat(user_id: str):
    if user_id not in sessions:
        sessions[user_id] = client.chats.create(
            model='gemini-2.5-flash',
            config=types.GenerateContentConfig(
                system_instruction=INFORMACION_EMPRESA,
                temperature=0.3
            )
        )
    return sessions[user_id]

# ==========================================
# 01 & 04. MANEJO DE ERRORES Y VALIDACIONES
# ==========================================
def send_whatsapp_message(to: str, text: str) -> bool:
    """Envía mensaje por la API de Meta con reintento/fallo controlado."""
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        if response.status_code == 200:
            logger.info(f"Mensaje enviado con éxito a {to}")
            return True
        else:
            logger.error(f"Error Meta API [{response.status_code}]: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Excepción al enviar mensaje a Meta: {str(e)}")
        return False

# ==========================================
# RUTAS DE LA APLICACIÓN
# ==========================================
@app.get("/")
def home():
    return {"status": "ok", "service": "Tapicería Fenix Bot"}

@app.get("/webhook")
def verify_webhook(request: Request):
    """Verificación de Webhook para Meta."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook verificado exitosamente por Meta.")
        return int(challenge)
    
    logger.warning("Intento fallido de verificación de Webhook.")
    raise HTTPException(status_code=403, detail="Token inválido")

@app.post("/webhook")
async def receive_webhook(request: Request):
    """Procesamiento seguro de mensajes entrantes."""
    try:
        body = await request.json()
    except Exception:
        logger.error("JSON malformado recibido en el webhook")
        return {"status": "invalid json"}

    # Extraer estructuras de Meta de forma segura
    try:
        entries = body.get("entry", [])
        if not entries:
            return {"status": "ignored"}
            
        changes = entries[0].get("changes", [])
        if not changes:
            return {"status": "ignored"}

        value = changes[0].get("value", {})
        messages = value.get("messages", [])

        # Si es una notificación de estado (entregado/leído), ignorar de forma limpia
        if not messages:
            return {"status": "event_ignored"}

        message_data = messages[0]
        sender_id = message_data.get("from")
        msg_type = message_data.get("type")

        # Solo procesar mensajes de texto
        if msg_type == "text":
            user_text = message_data.get("text", {}).get("body", "").strip()
            
            # Sanitización / Validación de entrada (03 & 04)
            if not user_text or len(user_text) > 1000:
                logger.warning(f"Mensaje rechazado por tamaño o vacío de {sender_id}")
                return {"status": "message_rejected"}

            logger.info(f"Mensaje recibido de {sender_id}: '{user_text}'")

            # Procesar respuesta con IA + Fallback de seguridad (01 & 06)
            try:
                chat = get_or_create_chat(sender_id)
                ai_response = chat.send_message(user_text).text
            except Exception as ai_err:
                logger.error(f"Fallo en motor Gemini: {str(ai_err)}")
                ai_response = MENSAJE_FALLBACK_HUMANO

            # Responder al cliente
            send_whatsapp_message(sender_id, ai_response)

        return {"status": "success"}

    except Exception as err:
        logger.error(f"Error crítico en procesamiento de webhook: {str(err)}")
        logger.error(traceback.format_exc())
        return {"status": "internal_error"}