"""
Burro BOSS Dashboard — Backend
================================
Servidor FastAPI que:
1. Autentica con Burro BOSS API usando email/password + api-key
2. Hace polling cada 10 segundos al robot 8394 con todos los status disponibles
3. Sirve el dashboard HTML y expone un endpoint /api/status con los datos frescos
4. Refresca el token automáticamente antes de que expire

Requisitos:
    pip install fastapi uvicorn httpx python-dotenv

Variables de entorno (crear archivo .env):
    BURRO_API_KEY=tu-api-key
    BURRO_EMAIL=tu-email
    BURRO_PASSWORD=tu-password
    BURRO_SERIAL=8394
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
SERIAL      = os.getenv("BURRO_SERIAL",  "8394")
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
    "network_id":       None,   # uid detectado automáticamente del robot
    "network_revision": None,   # revisión de la network (cambia al editar destinos)
    "network_loaded_at": None,  # cuándo se cargó por última vez
    "network_name":   None,     # nombre del network (ej. "Fundo Valerie - ICA")
    "network_paths":  None,     # GeoJSON FeatureCollection de paths
    "network_dests":  None,     # GeoJSON FeatureCollection de destinations
    "network_loaded": False,
    "loaded_mission_uuid": None,   # UUID de la última misión cargada (para play/resume)
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


async def load_network(client: httpx.AsyncClient, network_id: str, revision=None):
    """Carga paths y destinos de la network. Se recarga cuando cambia la revisión."""
    log.info(f"Cargando network {network_id} (rev {revision})...")

    # Paths — límite alto para no truncar caminos en redes grandes
    r_paths = await client.get(
        f"{BASE_URL}/networks/{network_id}/paths",
        headers=auth_headers(),
        params={"limit": 2000},
        timeout=30.0,
    )
    r_paths.raise_for_status()
    state["network_paths"] = r_paths.json().get("data")

    # Destinations
    r_dests = await client.get(
        f"{BASE_URL}/networks/{network_id}/destinations",
        headers=auth_headers(),
        params={"limit": 2000},
        timeout=30.0,
    )
    r_dests.raise_for_status()
    state["network_dests"] = r_dests.json().get("data")

    state["network_id"]       = network_id
    state["network_revision"] = revision
    state["network_loaded"]   = True
    state["network_loaded_at"] = datetime.now(tz=timezone.utc).isoformat()

    # Intentar extraer el nombre del network desde los datos recibidos
    try:
        map_data = (
            state["robot_data"].get("data", {})
                .get("status", {})
                .get("network_map", {})
                .get("map", {})
        )
        state["network_name"] = map_data.get("name") or network_id[:8]
    except Exception:
        state["network_name"] = network_id[:8]

    n_paths = len(state["network_paths"].get("features", [])) if state["network_paths"] else 0
    n_dests = len(state["network_dests"].get("features", [])) if state["network_dests"] else 0
    log.info(f"Network '{state['network_name']}' cargada: {n_paths} paths, {n_dests} destinos (rev {revision})")


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

    # Detectar cambios en la network y recargar cuando corresponde.
    # IMPORTANTE: al agregar o editar destinos, el uid de la network NO cambia
    # pero sí incrementa su "revision". Hay que vigilar ambos.
    try:
        map_info = (
            data.get("data", {})
                .get("status", {})
                .get("network_map", {})
                .get("map", {})
        )
        network_uid = map_info.get("uid")
        revision    = map_info.get("revision")

        if network_uid:
            cambio_uid = network_uid != state["network_id"]
            cambio_rev = revision is not None and revision != state["network_revision"]
            if cambio_uid or cambio_rev:
                if cambio_rev and not cambio_uid:
                    log.info(f"Revisión de network cambió: {state['network_revision']} → {revision}. Recargando destinos...")
                await load_network(client, network_uid, revision)
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
        "loaded_mission_uuid": state["loaded_mission_uuid"],
    })


@app.get("/api/network")
async def get_network():
    return JSONResponse({
        "network_id":       state["network_id"],
        "network_name":     state["network_name"],
        "network_revision": state["network_revision"],
        "network_loaded":   state["network_loaded"],
        "loaded_at":        state["network_loaded_at"],
        "paths":            state["network_paths"],
        "destinations":     state["network_dests"],
    })


@app.post("/api/network/reload")
async def reload_network():
    """Fuerza la recarga de paths y destinos de la network actual."""
    if not state["network_id"]:
        return JSONResponse({"ok": False, "error": "No hay network detectada aún"}, status_code=400)
    try:
        async with httpx.AsyncClient() as client:
            await ensure_valid_token(client)
            await load_network(client, state["network_id"], state["network_revision"])
        n_dests = len(state["network_dests"].get("features", [])) if state["network_dests"] else 0
        n_paths = len(state["network_paths"].get("features", [])) if state["network_paths"] else 0
        return JSONResponse({
            "ok": True,
            "destinations": n_dests,
            "paths": n_paths,
            "network_name": state["network_name"],
        })
    except Exception as e:
        log.error(f"Error recargando network: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/mission/load")
async def mission_load(body: dict):
    """
    Recibe una lista de tareas y las carga como misión en el robot.

    Body esperado:
    {
      "tasks": [
        {
          "destination_uid": "uid-del-destino",
          "name": "Nombre opcional",
          "duration_ms": 60000   // 0 = WaitForInputTransition
        }
      ]
    }
    """
    tasks = body.get("tasks", [])
    if not tasks:
        return JSONResponse({"ok": False, "error": "No hay tareas"}, status_code=400)

    import uuid
    from datetime import timedelta

    mission_tasks = []
    for t in tasks:
        dur_sec = (t.get("duration_ms") or 0) / 1000.0
        transition = (
            {
                "type": "AfterDurationTransition",
                "parameters": {"duration_sec": dur_sec}
            }
            if dur_sec > 0
            else {"type": "WaitForInputTransition"}
        )
        mission_tasks.append({
            "id":   str(uuid.uuid4()),
            "type": "DestinationTask",
            "name": t.get("name", "Destino"),
            "parameters": {
                "destination": {
                    "id":   t["destination_uid"],
                    "name": t.get("name", "Destino"),
                }
            },
            "after": transition,
        })

    expiration = (datetime.now(tz=timezone.utc) + timedelta(hours=24)).isoformat()

    # UUID que generamos para la misión: ESTE es el que mission/play necesita
    # (según la doc oficial de Burro, play usa el mismo id del cliente, no el
    #  que el robot reporta en su missionState, que puede venir reformateado).
    mission_uuid = str(uuid.uuid4())

    payload = {
        "expiration": expiration,
        "mission": {
            "id":         mission_uuid,
            "properties": {},
            "defaults":   {},
            "tasks": {
                "id":   str(uuid.uuid4()),
                "type": "TaskGroup",
                "parameters": {
                    "tasks": mission_tasks,
                },
            }
        }
    }

    log.info(f"Mission load payload: {payload}")

    async with httpx.AsyncClient() as client:
        await ensure_valid_token(client)
        r = await client.post(
            f"{BASE_URL}/robots/{SERIAL}/command/mission/load",
            headers={**auth_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=15.0,
        )

    log.info(f"Mission load -> {r.status_code}: {r.text[:200]}")

    # Recordar el UUID de esta misión para usarlo en play
    if r.status_code < 300:
        state["loaded_mission_uuid"] = mission_uuid

    return JSONResponse({
        "ok": r.status_code < 300,
        "status": r.status_code,
        "mission_id": mission_uuid,      # ← devolver el UUID al frontend
        "body": r.json() if r.content else {},
    })


@app.post("/api/mission/play")
async def mission_play(body: dict):
    """Ejecuta la misión cargada. Requiere mission_id en el body."""
    from datetime import timedelta
    mission_id = body.get("mission_id")
    if not mission_id:
        return JSONResponse({"ok": False, "error": "mission_id requerido"}, status_code=400)

    expiration = (datetime.now(tz=timezone.utc) + timedelta(hours=24)).isoformat()

    payload = {
        "expiration": expiration,
        "mission_id": mission_id,
    }

    async with httpx.AsyncClient() as client:
        await ensure_valid_token(client)
        r = await client.post(
            f"{BASE_URL}/robots/{SERIAL}/command/mission/play",
            headers={**auth_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=15.0,
        )
    log.info(f"Mission play → mission_id enviado: {mission_id}")
    log.info(f"Mission play → respuesta [{r.status_code}]: {r.text[:400]}")
    return JSONResponse({"ok": r.status_code < 300, "status": r.status_code, "body": r.json() if r.content else {}})


@app.get("/api/mission/status")
async def mission_status():
    """
    Verifica el estado actual de la misión activa y la alcanzabilidad
    de los destinos usando routePlannerStatus.
    """
    async with httpx.AsyncClient() as client:
        await ensure_valid_token(client)
        r = await client.get(
            f"{BASE_URL}/robots/{SERIAL}",
            headers=auth_headers(),
            params={"missionState": "true", "routePlannerStatus": "true"},
            timeout=15.0,
        )
    if r.status_code != 200:
        return JSONResponse({"ok": False, "error": r.text})

    data    = r.json().get("data", {})
    mission = data.get("state", {}).get("mission")
    route   = data.get("status", {}).get("route_planner", {})
    reachable_count = route.get("reachable_destinations_count")

    if not mission:
        return JSONResponse({
            "ok":               True,
            "mission":          None,
            "reachable_count":  reachable_count,
        })

    next_task   = mission.get("next_task") or {}
    task_status = next_task.get("status")
    future      = mission.get("future_tasks", [])

    return JSONResponse({
        "ok":               True,
        "mission_id":       mission.get("mission", {}).get("id"),
        "mission_name":     mission.get("mission", {}).get("name"),
        "is_complete":      mission.get("is_complete"),
        "progress":         mission.get("overall_progress"),
        "current_task":     (mission.get("current_task") or {}).get("task", {}).get("name"),
        "next_task":        next_task.get("task", {}).get("name"),
        "task_status":      task_status,
        "pending_tasks":    len(future),
        "reachable_count":  reachable_count,
    })


@app.delete("/api/mission/{mission_id}")
async def mission_delete(mission_id: str):
    """Elimina una misión por su ID."""
    async with httpx.AsyncClient() as client:
        await ensure_valid_token(client)
        r = await client.delete(
            f"{BASE_URL}/missions/{mission_id}",
            headers=auth_headers(),
            timeout=15.0,
        )
    log.info(f"Mission delete {mission_id} → {r.status_code}: {r.text[:200]}")
    return JSONResponse({"ok": r.status_code < 300, "status": r.status_code})


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
