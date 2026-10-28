"""Scénario d'événement : objets posés sur la carte (WC, ravitos, secours, bénévoles, goulots).

Chaque objet est un point géographique accroché au parcours (abscisse curviligne `progress_m`
et écart `offset_m`). Le scénario s'exporte et se recharge en GeoJSON.

Effet sur la simulation :
- `water`      : poste de ravitaillement (postes simultanés, durée de service) → files sur le parcours ;
- `bottleneck` : rétrécissement local (largeur utile, longueur) → densité ;
- `toilets`    : cabines (zone de départ) → file d'attente avant le départ (`queues.py`) ;
- `medical`    : équipes et délai de mobilisation → temps d'intervention (`queues.py`).
Les types `sponge`, `doctor`, `ambulance` et `volunteer` n'agissent pas sur la foule : ils sont conservés dans le
scénario, comptés dans le bilan et contrôlés par le moteur de règles (`rules.py`).
"""
from __future__ import annotations

import json
import math
import uuid

import numpy as np

# kind -> métadonnées d'affichage et signification des deux paramètres génériques p1 / p2.
KINDS = {
    "water": {
        "label": "Point d'eau", "emoji": "💧", "color": "#1d4ed8", "engine": True,
        "p1": ("Postes de service simultanés", 25.0), "p2": ("Durée moyenne d'un service (s)", 5.0),
    },
    "sponge": {
        "label": "Épongeage / rafraîchissement", "emoji": "🧽", "color": "#0891b2", "engine": False,
        "p1": ("Postes de service", 10.0), "p2": ("Durée moyenne d'un service (s)", 3.0),
    },
    "toilets": {
        "label": "Toilettes", "emoji": "🚻", "color": "#7c3aed", "engine": True,
        "p1": ("Cabines", 10.0), "p2": ("Durée d'usage moyenne (s)", 90.0),
    },
    "medical": {
        "label": "Poste de secours", "emoji": "⛑️", "color": "#dc2626", "engine": True,
        "p1": ("Équipes de secouristes", 2.0), "p2": ("Délai de mobilisation (s)", 60.0),
    },
    "doctor": {
        "label": "Médecin", "emoji": "🩺", "color": "#be123c", "engine": False,
        "p1": ("Médecins", 1.0), "p2": ("Infirmiers associés", 0.0),
    },
    "ambulance": {
        "label": "Ambulance", "emoji": "🚑", "color": "#b91c1c", "engine": False,
        "p1": ("Ambulances", 1.0), "p2": ("Transport vers un hôpital (1 = oui, 0 = non)", 1.0),
    },
    "volunteer": {
        "label": "Bénévoles", "emoji": "🙋", "color": "#059669", "engine": False,
        "p1": ("Nombre de bénévoles", 4.0), "p2": ("Rayon d'action (m)", 50.0),
    },
    "bottleneck": {
        "label": "Rétrécissement", "emoji": "🚧", "color": "#ea580c", "engine": True,
        "p1": ("Largeur utile (m)", 3.0), "p2": ("Longueur du rétrécissement (m)", 40.0),
    },
}


# ---- géométrie ------------------------------------------------------------------------
def to_local(lat, lon, origin):
    """(lat, lon) -> (x, y) en mètres, même projection que `simulator.geographic_to_local`."""
    lat0, lon0 = origin
    cos_lat = max(1e-6, math.cos(math.radians(lat0)))
    return (lon - lon0) * 111_320 * cos_lat, (lat - lat0) * 110_540


def project_on_route(route_local, x, y):
    """Projette (x, y) sur la polyligne : retourne (abscisse curviligne m, écart au tracé m)."""
    pts = np.asarray(route_local, dtype=float)
    a, b = pts[:-1], pts[1:]
    ab = b - a
    seg2 = np.maximum((ab ** 2).sum(axis=1), 1e-12)
    p = np.array([x, y], dtype=float)
    t = np.clip(((p - a) * ab).sum(axis=1) / seg2, 0.0, 1.0)
    proj = a + t[:, None] * ab
    dist = np.linalg.norm(proj - p, axis=1)
    i = int(np.argmin(dist))
    seg_len = np.sqrt(seg2)
    cum = np.concatenate(([0.0], np.cumsum(seg_len)))
    return float(cum[i] + t[i] * seg_len[i]), float(dist[i])


def route_length(route_local):
    pts = np.asarray(route_local, dtype=float)
    return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())


# ---- objets ---------------------------------------------------------------------------
def new_object(kind, lat, lon, route_local, origin, name=None, p1=None, p2=None, obj_id=None):
    if kind not in KINDS:
        raise ValueError(f"Type d'objet inconnu : {kind}")
    meta = KINDS[kind]
    obj = {
        "id": obj_id or uuid.uuid4().hex[:8],
        "kind": kind,
        "name": name or meta["label"],
        "lat": float(lat), "lon": float(lon),
        "p1": float(meta["p1"][1] if p1 is None else p1),
        "p2": float(meta["p2"][1] if p2 is None else p2),
    }
    refresh_projection(obj, route_local, origin)
    return obj


def refresh_projection(obj, route_local, origin):
    x, y = to_local(obj["lat"], obj["lon"], origin)
    progress, offset = project_on_route(route_local, x, y)
    obj["progress_m"] = round(progress, 1)
    obj["offset_m"] = round(offset, 1)
    return obj


def default_name(kind, objects):
    n = sum(1 for o in objects if o["kind"] == kind) + 1
    return f"{KINDS[kind]['label']} {n}"


# ---- lien avec le moteur --------------------------------------------------------------
def engine_inputs(objects, length_m):
    """Retourne (points d'eau, rétrécissements) au format consommé par le moteur mésoscopique.

    points d'eau : liste de dicts {pos_m, slots, service_s, name}
    rétrécissements : liste de tuples (début_m, fin_m, largeur_m)
    """
    stations, overrides = [], []
    for o in objects:
        if o["kind"] == "water" and 0 < o["progress_m"] < length_m:
            stations.append({"pos_m": float(o["progress_m"]), "slots": max(1, int(round(o["p1"]))),
                             "service_s": max(0.0, float(o["p2"])), "name": o["name"]})
        elif o["kind"] == "bottleneck":
            half = max(1.0, float(o["p2"])) / 2.0
            overrides.append((max(0.0, o["progress_m"] - half), min(length_m, o["progress_m"] + half),
                              max(0.5, float(o["p1"]))))
    stations.sort(key=lambda s: s["pos_m"])
    return stations, overrides


# ---- bilan ----------------------------------------------------------------------------
def max_gap_m(objects, kind, length_m):
    """Plus grand tronçon du parcours (départ et arrivée inclus) sans objet de ce type."""
    pos = sorted(o["progress_m"] for o in objects if o["kind"] == kind)
    if not pos:
        return None
    marks = [0.0] + pos + [float(length_m)]
    return float(max(b - a for a, b in zip(marks[:-1], marks[1:])))


def equipment_summary(objects, n_runners, length_m):
    """Compte et ratios bruts. Aucun seuil réglementaire n'est appliqué ici."""
    by = lambda k: [o for o in objects if o["kind"] == k]  # noqa: E731
    cabins = sum(o["p1"] for o in by("toilets"))
    out = {
        "water_points": len(by("water")),
        "toilet_sites": len(by("toilets")),
        "toilet_cabins": int(cabins),
        "toilet_cabins_per_1000": round(cabins / max(1, n_runners) * 1000.0, 2),
        "medical_posts": len(by("medical")),
        "medical_teams": int(sum(o["p1"] for o in by("medical"))),
        "volunteers": int(sum(o["p1"] for o in by("volunteer"))),
        "volunteers_per_1000": round(sum(o["p1"] for o in by("volunteer")) / max(1, n_runners) * 1000.0, 2),
        "bottlenecks": len(by("bottleneck")),
        "sponge_posts": len(by("sponge")),
        "doctors": int(sum(o["p1"] for o in by("doctor"))),
        "nurses": int(sum(o["p2"] for o in by("doctor"))),
        "ambulances": int(sum(o["p1"] for o in by("ambulance"))),
        "max_gap_water_m": max_gap_m(objects, "water", length_m),
        "max_gap_medical_m": max_gap_m(objects, "medical", length_m),
    }
    far = [o["name"] for o in objects if o["offset_m"] > 60.0]
    out["objects_far_from_route"] = far
    return out


# ---- GeoJSON --------------------------------------------------------------------------
def to_geojson(route_geo, objects, settings=None):
    features = []
    if route_geo:
        features.append({
            "type": "Feature",
            "properties": {"role": "route"},
            "geometry": {"type": "LineString", "coordinates": [[lon, lat] for lat, lon in route_geo]},
        })
    for o in objects:
        features.append({
            "type": "Feature",
            "properties": {k: o[k] for k in ("id", "kind", "name", "p1", "p2", "progress_m", "offset_m")},
            "geometry": {"type": "Point", "coordinates": [o["lon"], o["lat"]]},
        })
    return json.dumps({
        "type": "FeatureCollection",
        "crowdrunning": {"version": 1, "settings": settings or {}},
        "features": features,
    }, ensure_ascii=False, indent=2)


def from_geojson(text):
    """Lit un scénario : retourne (route_geo ou None, objets sans projection recalculée)."""
    data = json.loads(text)
    if data.get("type") != "FeatureCollection":
        raise ValueError("Le fichier n'est pas un scénario GeoJSON (FeatureCollection attendu).")
    route_geo, objects = None, []
    for f in data.get("features", []):
        geom, props = f.get("geometry") or {}, f.get("properties") or {}
        if geom.get("type") == "LineString" and props.get("role") == "route":
            route_geo = [(float(c[1]), float(c[0])) for c in geom["coordinates"]]
        elif geom.get("type") == "Point" and props.get("kind") in KINDS:
            lon, lat = geom["coordinates"][:2]
            objects.append({
                "id": str(props.get("id") or uuid.uuid4().hex[:8]), "kind": props["kind"],
                "name": str(props.get("name") or KINDS[props["kind"]]["label"]),
                "lat": float(lat), "lon": float(lon),
                "p1": float(props.get("p1", KINDS[props["kind"]]["p1"][1])),
                "p2": float(props.get("p2", KINDS[props["kind"]]["p2"][1])),
                "progress_m": 0.0, "offset_m": 0.0,
            })
    return route_geo, objects