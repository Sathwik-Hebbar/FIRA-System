"""
backend/init_osm_data.py
Utility script to fetch shelters and hospitals from OpenStreetMap (via Overpass) and populate the ``shelter`` table.

The script is intended to be run once (e.g. ``python -m backend.init_osm_data``) after the
database has been created. It:

1. Queries the Overpass API for all nodes with ``amenity=shelter`` or ``amenity=hospital``
   inside the administrative area "Karnataka" (India).
2. Extracts the name, latitude, longitude and optional ``capacity`` tag.
3. Inserts the records into the ``Shelter`` SQLAlchemy model, clearing any existing rows
   to keep the demo data deterministic.

No API key is required. The script respects the Overpass rate‑limit by using a short
timeout and a modest ``[out:json]`` response.
"""

import json
import logging
from typing import List, Dict

import requests

from database import SessionLocal
from models import Shelter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Overpass QL query – fetch shelters and hospitals inside Karnataka
OVERPASS_QUERY = """
[out:json][timeout:60];
area["name"="Karnataka"][admin_level=4];
(
  node["amenity"="shelter"](area);
  node["amenity"="hospital"](area);
);
out body;
"""


def fetch_amenities() -> List[Dict]:
    """Call Overpass and return a list of amenity objects.

    Each returned dict contains ``name``, ``lat``, ``lon`` and optional ``capacity``.
    If a feature lacks a name we generate a placeholder based on its OSM id.
    """
    logger.info("Requesting Overpass data for Karnataka shelters & hospitals")
    try:
        response = requests.post(OVERPASS_URL, data={"data": OVERPASS_QUERY}, timeout=30)
        response.raise_for_status()
    except Exception as exc:
        logger.error("Overpass request failed: %s", exc)
        raise RuntimeError("Failed to fetch OSM data") from exc

    data = response.json()
    elements = data.get("elements", [])
    amenities: List[Dict] = []
    for el in elements:
        if el.get("type") != "node":
            continue
        tags = el.get("tags", {})
        amenity_type = tags.get("amenity")
        # We only care about shelter or hospital – the query already filters but keep guard
        if amenity_type not in {"shelter", "hospital"}:
            continue
        name = tags.get("name") or f"{amenity_type.title()} #{el.get('id')}"
        lat = el.get("lat")
        lon = el.get("lon")
        # Capacity is not always present; default to 0 (unknown)
        capacity_raw = tags.get("capacity")
        try:
            capacity = int(capacity_raw) if capacity_raw else 0
        except ValueError:
            capacity = 0
        amenities.append({
            "name": name,
            "lat": lat,
            "lng": lon,
            "capacity": capacity,
        })
    logger.info("Fetched %d amenity nodes", len(amenities))
    return amenities


def populate_shelters(db_session) -> None:
    """Clear existing shelters and insert the fetched OSM amenities.

    The function is deliberately simple – it drops all rows to make the demo
    deterministic. In a production system you would up‑sert and preserve IDs.
    """
    # Remove any existing records
    deleted = db_session.query(Shelter).delete()
    logger.info("Deleted %d existing shelter records", deleted)

    amenities = fetch_amenities()
    shelter_objects = []
    for a in amenities:
        shelter = Shelter(
            name=a["name"],
            lat=a["lat"],
            lng=a["lng"],
            capacity=a["capacity"],
        )
        shelter_objects.append(shelter)

    if shelter_objects:
        db_session.bulk_save_objects(shelter_objects)
        db_session.commit()
        logger.info("Inserted %d new shelters/hospitals", len(shelter_objects))
    else:
        logger.warning("No shelter/hospital data to insert – check Overpass query")


if __name__ == "__main__":
    session = SessionLocal()
    try:
        populate_shelters(session)
    finally:
        session.close()
