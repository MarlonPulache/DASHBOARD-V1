# Burro BOSS Dashboard

Dashboard de monitoreo en tiempo real para el robot Burro #8586.

## Archivos

```
burro_dashboard/
├── main.py           # Backend FastAPI — polling + API
├── index.html        # Dashboard frontend (un solo archivo)
├── requirements.txt  # Dependencias Python
├── .env.example      # Variables de entorno (copiar como .env)
└── README.md
```

## Correr localmente

```bash
# 1. Instalar dependencias
pip install -r requirements.txt

# 2. Crear .env con tus credenciales
cp .env.example .env
# editar .env con tu editor

# 3. Correr
python main.py
# Abre http://localhost:8000
```

## Desplegar en Render (gratis)

1. Crea cuenta en https://render.com
2. "New" → "Web Service"
3. Conecta tu repositorio de GitHub (o sube los archivos)
4. Configura:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python main.py`
5. En "Environment Variables", agrega:
   - `BURRO_API_KEY` = tu api key
   - `BURRO_EMAIL`   = tu email
   - `BURRO_PASSWORD`= tu password
   - `BURRO_SERIAL`  = 8586
   - `PORT`          = 10000  ← Render usa el 10000 por defecto
6. Click "Create Web Service"

Render te dará una URL pública tipo `https://burro-dashboard.onrender.com`.

## Qué muestra el dashboard

| Sección | Datos |
|---|---|
| Batería | % de carga, voltaje, corriente, potencia, Wh consumidos, tiempo y rango restantes, alarmas |
| GPS | Lat/Lng, heading, fix status (Fixed/Float/3D), confianza, satélites, RTK, base station |
| Velocidad | Lineal (m/s) y angular (rad/s) en tiempo real |
| Motion Governor | Estado OK/SLOWDOWN/STOP, razones, autonomía habilitada |
| Obstáculos | Estado de detección, tipo de obstáculos (persona/otro) |
| Drive Flags | E-stop, position hold, detenido |
| Fail Safe | Fallas activas, severidad |
| Misión | Progreso, tarea actual, tareas completadas y pendientes |
| Mapa | Mapa cargado, revisión, estado on_map |
| Plataforma | Versión de software, conectividad, IP, disco, métricas de hoy y sesión |

El frontend hace polling al backend cada 5 segundos.
El backend hace polling a Burro BOSS API cada 10 segundos.
