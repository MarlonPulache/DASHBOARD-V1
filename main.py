"""
Burro BOSS Dashboard — Backend
================================
Servidor FastAPI que:
1. Autentica con Burro BOSS API usando email/password + api-key
2. Hace polling cada 10 segundos al robot 8585 con todos los status disponibles
3. Sirve el dashboard HTML y expone un endpoint /api/status con los datos frescos
4. Refresca el token automáticamente antes de que expire

Requisitos:
    pip install fastapi uvicorn httpx python-dotenv

Variables de entorno (crear archivo .env):
    BURRO_API_KEY=tu-api-key
    BURRO_EMAIL=tu-email
    BURRO_PASSWORD=tu-password
    BURRO_SERIAL=8585
"""

import os
import time
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Configuración ──────────────────────────────────────────────────────────────
BASE_URL    = "https://boss-api.burro.ai/v1"
API_KEY     = os.getenv("BURRO_API_KEY", "PEGA_AQUI_TU_API_KEY")
EMAIL       = os.getenv("BURRO_EMAIL",   "PEGA_AQUI_TU_EMAIL")
PASSWORD    = os.getenv("BURRO_PASSWORD","PEGA_AQUI_TU_PASSWORD")
SERIAL      = os.getenv("BURRO_SERIAL",  "8585")
POLL_SEC    = int(os.getenv("POLL_SECONDS", "10"))

# ── Estado compartido ──────────────────────────────────────────────────────────
state = {
    "token":          None,
    "refresh_token":  None,
    "token_expires":  0,        # ms unix timestamp
    "robot_data":     None,
    "last_poll":      None,
    "poll_error":     None,
    # mapa de red
    "network_id":     None,     # uid detectado automáticamente del robot
    "network_paths":  None,     # GeoJSON FeatureCollection de paths
    "network_dests":  None,     # GeoJSON FeatureCollection de destinations
    "network_loaded": False,
}

# ── Auth helpers ───────────────────────────────────────────────────────────────

async def login(client: httpx.AsyncClient):
    log.info("Haciendo login en Burro BOSS API...")
    r = await client.post(
        f"{BASE_URL}/authentication/email/login",
        json={"email": EMAIL, "password": PASSWORD},
        headers={"x-api-key": API_KEY},
    )
    r.raise_for_status()
    data = r.json()
    state["token"]         = data["token"]
    state["refresh_token"] = data["refreshToken"]
    state["token_expires"] = data["tokenExpires"]
    log.info(f"Login OK. Token expira: {datetime.fromtimestamp(data['tokenExpires']/1000, tz=timezone.utc)}")


async def refresh_token(client: httpx.AsyncClient):
    log.info("Refrescando token...")
    r = await client.get(
        f"{BASE_URL}/authentication/refresh",
        headers={"Authorization": f"Bearer {state['refresh_token']}"},
    )
    r.raise_for_status()
    data = r.json()
    state["token"]         = data["token"]
    state["refresh_token"] = data["refreshToken"]
    state["token_expires"] = data["tokenExpires"]
    log.info("Token refrescado OK.")


async def ensure_valid_token(client: httpx.AsyncClient):
    """Refresca el token si le quedan menos de 5 minutos."""
    now_ms = time.time() * 1000
    margin_ms = 5 * 60 * 1000  # 5 minutos
    if state["token"] is None:
        await login(client)
    elif (state["token_expires"] - now_ms) < margin_ms:
        try:
            await refresh_token(client)
        except Exception:
            log.warning("Refresh falló, intentando login de nuevo...")
            await login(client)


def auth_headers() -> dict:
    return {
        "Accept":        "application/json",
        "x-api-key":     API_KEY,
        "Authorization": f"Bearer {state['token']}",
    }


# ── Polling ────────────────────────────────────────────────────────────────────

ALL_STATUS_PARAMS = {
    "batteryStatus":            "true",
    "localizationStatus":       "true",
    "networkStatus":            "true",
    "missionState":             "true",
    "motionGovernorStatus":     "true",
    "obstacleHandlingStatus":   "true",
    "failSafeStatus":           "true",
    "platformStatus":           "true",
    "velocityStatus":           "true",
    "driveSystemStopFlagsStatus": "true",
    "pathFollowStatus":         "true",
    "routePlannerStatus":       "true",
}


async def load_network(client: httpx.AsyncClient, network_id: str):
    """Carga paths y destinos del network una sola vez (o cuando cambia el mapa)."""
    log.info(f"Cargando network {network_id}...")

    # Paths
    r_paths = await client.get(
        f"{BASE_URL}/networks/{network_id}/paths",
        headers=auth_headers(),
        params={"limit": 500},
        timeout=20.0,
    )
    r_paths.raise_for_status()
    state["network_paths"] = r_paths.json().get("data")

    # Destinations
    r_dests = await client.get(
        f"{BASE_URL}/networks/{network_id}/destinations",
        headers=auth_headers(),
        params={"limit": 500},
        timeout=20.0,
    )
    r_dests.raise_for_status()
    state["network_dests"] = r_dests.json().get("data")

    state["network_id"]     = network_id
    state["network_loaded"] = True
    log.info(f"Network cargado: {network_id}")


async def poll_robot(client: httpx.AsyncClient):
    await ensure_valid_token(client)
    r = await client.get(
        f"{BASE_URL}/robots/{SERIAL}",
        headers=auth_headers(),
        params=ALL_STATUS_PARAMS,
        timeout=15.0,
    )
    r.raise_for_status()
    data = r.json()
    state["robot_data"] = data
    state["last_poll"]  = datetime.now(tz=timezone.utc).isoformat()
    state["poll_error"] = None
    log.info(f"Poll OK — {state['last_poll']}")

    # Detectar networkId automáticamente y cargar mapa si cambió
    try:
        network_uid = (
            data.get("data", {})
                .get("status", {})
                .get("network_map", {})
                .get("map", {})
                .get("uid")
        )
        if network_uid and network_uid != state["network_id"]:
            await load_network(client, network_uid)
    except Exception as e:
        log.warning(f"No se pudo cargar el network: {e}")


async def polling_loop():
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await poll_robot(client)
            except Exception as e:
                state["poll_error"] = str(e)
                log.error(f"Error en poll: {e}")
            await asyncio.sleep(POLL_SEC)


# ── Ciclo de vida ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(polling_loop())
    yield
    task.cancel()


app = FastAPI(title="Burro Dashboard", lifespan=lifespan)


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    return JSONResponse({
        "serial":     SERIAL,
        "last_poll":  state["last_poll"],
        "poll_error": state["poll_error"],
        "data":       state["robot_data"],
    })


@app.get("/api/network")
async def get_network():
    return JSONResponse({
        "network_id":     state["network_id"],
        "network_loaded": state["network_loaded"],
        "paths":          state["network_paths"],
        "destinations":   state["network_dests"],
    })


@app.get("/api/health")
async def health():
    return {"ok": True, "last_poll": state["last_poll"]}


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
