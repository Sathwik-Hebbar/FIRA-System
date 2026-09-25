'''backend/data_collector.py
Purpose: Pull live weather and flood data for every Zone and store it in the SQLite DB.

The collector uses the free Open‑Meteo APIs (weather + flood). No API key is required.
It is intended to be run at startup or on a short timer (e.g. every 5 min).
'''

import logging
from typing import Tuple

import requests
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Zone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helper functions – Open‑Meteo Weather API (precipitation & soil moisture)
# ---------------------------------------------------------------------------
WEATHER_ENDPOINT = "https://api.open-meteo.com/v1/forecast"

def fetch_weather(lat: float, lon: float) -> Tuple[float, float]:
    """Return the latest hourly precipitation (mm) and soil moisture (m³/m³).

    The API returns a list of hourly values; we use the most recent entry.
    If the request fails or the response is malformed we raise RuntimeError.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "precipitation,soil_moisture",
        "timezone": "auto",
    }
    try:
        resp = requests.get(WEATHER_ENDPOINT, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        hourly = data.get("hourly", {})
        precip = hourly.get("precipitation")
        soil = hourly.get("soil_moisture")
        if not precip or not soil:
            raise ValueError("Missing precipitation or soil moisture data")
        return float(precip[-1]), float(soil[-1])
    except Exception as exc:
        logger.error("Weather fetch failed for %s,%s: %s", lat, lon, exc)
        raise RuntimeError("Weather fetch failed") from exc

# ---------------------------------------------------------------------------
# Helper functions – Open‑Meteo Flood API (river discharge)
# ---------------------------------------------------------------------------
FLOOD_ENDPOINT = "https://flood-api.open-meteo.com/v1/flood"

def fetch_flood(lat: float, lon: float) -> float:
    """Return an estimated river level (m).

    The Flood API provides river discharge (m³/s). For the demo we map it to a
    pseudo‑level using a simple linear conversion: level = discharge / 100.
    This keeps the values in the same range as the seeded data.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "river_discharge",
        "timezone": "auto",
    }
    try:
        resp = requests.get(FLOOD_ENDPOINT, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        discharge = daily.get("river_discharge")
        if not discharge:
            raise ValueError("Missing river_discharge data")
        # Use the most recent daily value
        level = float(discharge[-1]) / 100.0
        return level
    except Exception as exc:
        logger.error("Flood fetch failed for %s,%s: %s", lat, lon, exc)
        raise RuntimeError("Flood fetch failed") from exc

# ---------------------------------------------------------------------------
# Core collector – updates every Zone in the DB
# ---------------------------------------------------------------------------
def update_zones(db: Session) -> None:
    """Iterate over all ``Zone`` rows, fetch live data and store it.

    Commits after each successful zone update; errors are logged and the loop
    continues so a single failing API call does not abort the whole run.
    """
    zones = db.query(Zone).all()
    logger.info("Updating %d zones with live weather/flood data", len(zones))
    for zone in zones:
        try:
            rainfall_mm, _ = fetch_weather(zone.lat, zone.lng)
            river_level = fetch_flood(zone.lat, zone.lng)
            zone.rainfall_mm = rainfall_mm
            zone.river_level_m = river_level
            db.add(zone)
            db.commit()
            db.refresh(zone)
            logger.info(
                "Zone %s (id=%s) updated: rainfall=%.1f mm, river_level=%.2f m",
                zone.name,
                zone.id,
                rainfall_mm,
                river_level,
            )
        except Exception as exc:
            db.rollback()
            logger.warning(
                "Skipping zone %s (id=%s) due to error: %s", zone.name, zone.id, exc
            )
            continue

# ---------------------------------------------------------------------------
# Allow running as a script: python -m backend.data_collector
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    session = SessionLocal()
    try:
        update_zones(session)
    finally:
        session.close()
