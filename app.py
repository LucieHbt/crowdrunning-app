"""CrowdRunning : simulation de foule pour courses à pied, avec éditeur de scénario sur carte.
Seed = 42 permet de produire exactement la même suite de nombres "aléatoires" à chaque fois que vous relancez le programme.
Deux couches :
- `scenario.py` : objets posés sur la carte (points d'eau, WC, secours, bénévoles, rétrécissements) ;
- `meso.py`     : moteur mésoscopique, densités calculées sur la totalité des partants.
"""
from __future__ import annotations

import html as htmllib
import json
import math
import base64
from pathlib import Path
from datetime import date, timedelta
import textwrap

import folium
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components
from streamlit_folium import st_folium
from branca.element import MacroElement
from jinja2 import Template

import plan_operationnel as po
import rules as rl
import scenario as sc
from meso import MesoSimulation, StationSpec
from queues import simulate_medical, simulate_toilets
from simulator import geographic_to_local, load_gpx, local_to_geographic

st.set_page_config(page_title="CrowdRunning", page_icon="📍", layout="wide")

DEMO_ROUTE = [
    (48.0000, -1.0000), (48.0007, -1.0000), (48.0007, -0.9992), (48.0012, -0.9992),
    (48.0012, -0.9985), (48.0002, -0.9985), (48.0002, -0.9993), (48.0000, -1.0000),
]
COLOR_FLUID, COLOR_CONSTRAINED, COLOR_CONGESTED, COLOR_WATER = "#A9D8CF", "#E1621E", "#D11EE1", "#15569A"
# Points affichés sur la carte animée : 
# (≈ 55 Mo pour 1 000 points). Les densités, elles, utilisent toujours tous les partants.
MAX_ANIMATED = 300

def js_json(obj) -> str:
    """JSON sûr à insérer dans un <script> (neutralise « </script> »)."""
    return json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")

def render_html(content: str, height: int):
    """Affiche du HTML autonome (st.iframe sur Streamlit récent, components.html sinon)."""
    if hasattr(st, "iframe"):
        st.iframe(content, height=height)
    else:
        components.html(content, height=height, scrolling=False)

def duration_seconds_hhmm(label: str) -> int:
    """Convertit une durée totale HH:MM en secondes."""
    hours, minutes = str(label).split(":", 1)
    hours, minutes = int(hours), int(minutes)
    if hours < 0 or not 0 <= minutes < 60:
        raise ValueError(f"Durée HH:MM invalide : {label!r}")
    return hours * 3600 + minutes * 60


def format_pace_seconds_per_km(seconds_per_km: float) -> str:
    """Formate une allure calculée en min/km (mm:ss)."""
    total_seconds = max(0, int(round(float(seconds_per_km))))
    minutes, seconds = divmod(total_seconds, 60)
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d}"


def wave_pace_reference(route_length_m: float, time_labels, percentages):
    """Calcule l'allure de chaque vague et l'allure globale pondérée par les parts de coureurs."""
    distance_km = float(route_length_m) / 1000.0
    if distance_km <= 0:
        raise ValueError("La distance du parcours doit être positive pour calculer les allures.")
    if len(time_labels) != len(percentages) or not time_labels:
        raise ValueError("Il faut un temps et un pourcentage pour chaque vague.")

    durations_s = [duration_seconds_hhmm(label) for label in time_labels]
    if any(value <= 0 for value in durations_s):
        raise ValueError("Les temps médians des vagues doivent être positifs.")
    weights = [float(value) for value in percentages]
    if any(value < 0 for value in weights) or sum(weights) <= 0:
        raise ValueError("Les pourcentages des vagues sont invalides.")

    paces_s_per_km = [duration / distance_km for duration in durations_s]
    reference_s_per_km = sum(pace * weight for pace, weight in zip(paces_s_per_km, weights)) / sum(weights)
    pace_labels = [format_pace_seconds_per_km(pace) for pace in paces_s_per_km]
    return durations_s, pace_labels, reference_s_per_km, format_pace_seconds_per_km(reference_s_per_km)

def water_stations_from_length(length_m: float, interval_km: float = 5.0, finish_tolerance_m: float = 50.0):
    """Points d'eau tous les 5 km (utilisés seulement si aucun point d'eau n'est posé sur la carte)."""
    if length_m <= 0:
        return []
    interval_m = interval_km * 1000.0
    count = int(length_m // interval_m)
    stations_m = [k * interval_m for k in range(1, count + 1)]
    last = stations_m[-1] if stations_m else 0.0
    if not stations_m or length_m - last > finish_tolerance_m:
        stations_m.append(float(length_m))
    # Un point d'eau exactement sur la ligne d'arrivée n'a pas de sens pour le moteur : on l'écarte.
    return [round(x / 1000.0, 3) for x in sorted(set(stations_m)) if 0 < x < length_m - 1e-6]

def geographic_point_at_distance(route_geo, route_local, distance_m):
    loc = np.asarray(route_local, dtype=float)
    geo = np.asarray(route_geo, dtype=float)
    lengths = np.linalg.norm(np.diff(loc, axis=0), axis=1)
    cum = np.concatenate(([0.0], np.cumsum(lengths)))
    d = float(np.clip(distance_m, 0.0, cum[-1]))
    idx = min(int(np.searchsorted(cum, d, side="right") - 1), len(lengths) - 1)
    frac = (d - cum[idx]) / max(lengths[idx], 1e-9)
    p = geo[idx] + frac * (geo[idx + 1] - geo[idx])
    return [float(p[0]), float(p[1])]


# ---- état de session ------------------------------------------------------------------
ss = st.session_state
ss.setdefault("result", None)
ss.setdefault("objects", [])
ss.setdefault("editor_version", 0)
ss.setdefault("last_click", None)
ss.setdefault("scenario_route", None)   # tracé chargé depuis un scénario GeoJSON
ss.setdefault("active_tab", "Édition")
ss.setdefault("tab_navigation", ss.get("active_tab", "Édition"))
ss.setdefault("map_style", "Plan")

# Fonds de carte proposés (communs aux onglets Édition et Simulation) : nom -> (modèle d'URL, attribution, zoom max).
MAP_STYLES = {
    "Plan": ("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", "&copy; OpenStreetMap contributors", 19),
    "Satellite": ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", "Imagery &copy; Esri", 19),
    "Relief": ("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", "&copy; OpenStreetMap contributors, SRTM | &copy; OpenTopoMap", 17),
}


def current_map_style():
    name = ss.get("map_style", "Plan")
    return (name if name in MAP_STYLES else "Plan"), MAP_STYLES.get(name, MAP_STYLES["Plan"])


def map_style_button(key: str):
    """Bouton qui fait défiler les fonds de carte (Plan, Clair, Satellite, Relief)."""
    names = list(MAP_STYLES)
    name, _ = current_map_style()
    nxt = names[(names.index(name) + 1) % len(names)]
    if st.button(f"👀 Fond : {name}", key=key, help=f"Changer le fond de carte (suivant : {nxt})"):
        ss.map_style = nxt
        st.rerun()

# Les cases « à confirmer » et le filtre de l'onglet Conformité ne s'affichent que dans cet onglet :
# on réaffecte leur valeur à chaque exécution pour que Streamlit ne les oublie pas en changeant d'onglet.
for _k in [k for k in ss.keys() if isinstance(k, str) and (k.startswith("manual_") or k.startswith("plan_") or k in ("cmp_filter", "show_rule_text"))]:
    ss[_k] = ss[_k]

# La navigation est contrôlée par un radio horizontal stylé en onglets : contrairement à
# st.tabs, elle peut être sélectionnée par le code après la soumission du formulaire.
if ss.get("_navigate_to_tab"):
    _target_tab = ss.pop("_navigate_to_tab")
    ss["tab_navigation"] = _target_tab
    ss["active_tab"] = _target_tab

# Image de fond facultative : placer fond.jpg dans le dossier assets/ à côté de app.py.
# L'image est intégrée en base64 pour fonctionner aussi dans les déploiements Streamlit.
# Image de fond : appliquée à toute l'application, y compris la zone d'en-tête Streamlit.
_background_css = ""
_background_path = Path(__file__).resolve().parent / "assets" / "fond.jpg"
if _background_path.exists():
    _image_b64 = base64.b64encode(_background_path.read_bytes()).decode("ascii")
    _background_css = f"""
    .stApp {{
      background-image: linear-gradient(rgba(248,250,252,.70), rgba(248,250,252,.78)),
                        url('data:image/jpeg;base64,{_image_b64}');
      background-size: cover;
      background-position: center;
      background-attachment: fixed;
    }}
    /* L'en-tête natif est transparent : la photo continue jusqu'au bord supérieur. */
    header[data-testid="stHeader"],
    .stAppHeader,
    [data-testid="stToolbar"] {{
      background: transparent !important;
    }}
    [data-testid="stSidebar"] > div {{
      background: #fff;
    }}
    [data-testid="stMainBlockContainer"] {{
        padding: 2rem 1rem 2rem !important; max-width: 100% !important;
    }}
"""

NAV_CSS = r'''
/* Navigation : segments pleine largeur, pastille d'état à côté du libellé. */
.st-key-crowdrunning_tabs { margin: .1rem 0 .9rem 0; width: 100%; }
.st-key-crowdrunning_tabs .stElementContainer:has(style) {
  position: absolute !important; width: 0 !important; height: 0 !important; margin: 0 !important; padding: 0 !important; overflow: hidden !important;
}
.st-key-crowdrunning_tabs .stElementContainer, .st-key-crowdrunning_tabs [data-testid="stRadio"] { width: 100% !important; }
.st-key-crowdrunning_tabs [data-testid="stRadio"] > label { display: none !important; }
.st-key-crowdrunning_tabs [role="radiogroup"] {
  position: relative; width: 100%; padding: 4px; box-sizing: border-box;
  display: grid !important; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0 !important;
  border: 1px solid #d9dee5; border-radius: 12px; background: #fff;
}
/* Segment actif : une seule pastille discrète qui glisse d'un onglet à l'autre. */
.st-key-crowdrunning_tabs [role="radiogroup"]::before {
  content: ""; position: absolute; z-index: 0; top: 4px; bottom: 4px; left: 4px; width: calc((100% - 8px) / 4);
  border-radius: 9px; background: #ff4a4a; box-shadow: none;
  transition: transform .22s ease;
}
.st-key-crowdrunning_tabs [role="radiogroup"]:has(> :nth-child(2) input:checked)::before { transform: translateX(100%); }
.st-key-crowdrunning_tabs [role="radiogroup"]:has(> :nth-child(3) input:checked)::before { transform: translateX(200%); }
.st-key-crowdrunning_tabs [role="radiogroup"]:has(> :nth-child(4) input:checked)::before { transform: translateX(300%); }
.st-key-crowdrunning_tabs [role="radiogroup"] > * { position: relative; z-index: 1; display: flex; flex-direction: row !important; align-items: center; justify-content: center; gap: .5rem; min-width: 0; margin: 0 !important; }
.st-key-crowdrunning_tabs [role="radiogroup"] label {
  flex: 0 1 auto; display: flex !important; align-items: center; justify-content: center; margin: 0 !important;
  min-height: 2.6rem; padding: .5rem .4rem !important; cursor: pointer; border: 0 !important; border-radius: 9px;
  background: transparent !important; box-shadow: none !important; outline: none !important; opacity: .8; transition: opacity .15s ease;
}
.st-key-crowdrunning_tabs [role="radiogroup"] > *:hover label { opacity: 1; }
.st-key-crowdrunning_tabs [role="radiogroup"] label:has(input:checked) { opacity: 1; }
.st-key-crowdrunning_tabs [role="radiogroup"] label > div { display: flex; align-items: center; justify-content: center; gap: .4rem; }
.st-key-crowdrunning_tabs [role="radiogroup"] label p { margin: 0 !important; font-weight: 600; font-size: .95rem; white-space: nowrap; }
.st-key-crowdrunning_tabs [role="radiogroup"] label:has(input:checked) p { font-weight: 700; }
.st-key-crowdrunning_tabs [role="radiogroup"] label, .st-key-crowdrunning_tabs [role="radiogroup"] label * { color: #4b5563 !important; }
.st-key-crowdrunning_tabs [role="radiogroup"] label:has(input:checked), .st-key-crowdrunning_tabs [role="radiogroup"] label:has(input:checked) * { color: #fff !important; }
/* Le rond du radio : masqué dans l'ancienne et la nouvelle structure de Streamlit. */
.st-key-crowdrunning_tabs label[data-baseweb="radio"] > div:first-child,
.st-key-crowdrunning_tabs label[data-testid="stRadioOption"] > div > div:first-child:not([data-testid="stMarkdownContainer"]) { display: none !important; }
/* Pastilles d'état : petits compteurs plats, juste après le libellé. */
.st-key-crowdrunning_tabs [role="radiogroup"] > *::after {
  min-width: 20px; height: 20px; padding: 0 6px; box-sizing: border-box; border-radius: 10px;
  font: 700 .72rem/20px system-ui, sans-serif; text-align: center; pointer-events: none;
  background: var(--cr-badge, #64748b); color: var(--cr-badge-fg, #fff);
}
.st-key-crowdrunning_tabs [role="radiogroup"] > *:has(input:checked)::after { background: #fff; color: var(--cr-badge-sel, #475569); }
@media (max-width: 720px) { .st-key-crowdrunning_tabs [role="radiogroup"] label p { font-size: .78rem; } .st-key-crowdrunning_tabs [role="radiogroup"] > * { gap: .2rem; } }
@media (prefers-reduced-motion: reduce) { .st-key-crowdrunning_tabs [role="radiogroup"]::before { transition: none !important; } }
'''

CARD_CSS = r'''
/* ───── Tableau de bord de conformité ───── */
.cr-card { border: 1px solid #d9dee5; background: #fff !important; color: #1f2937; border-radius: 8px; padding: 1.1rem 1.3rem; box-sizing: border-box; box-shadow: none; }
.cr-card *, .cr-act * { text-shadow: none; }
.cr-two { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-bottom: 1rem; align-items: stretch; }
@media (max-width: 900px) { .cr-two { grid-template-columns: 1fr; } }
.cr-card h4 { margin: 0 0 .8rem 0; padding: 0 0 .5rem 0; border-bottom: 1px solid #e5e9ef; font: 700 .9rem/1.2 system-ui, sans-serif; letter-spacing: 0; text-transform: none; opacity: .9; }
.cr-hero-card { display: flex; gap: 2rem; align-items: center; flex-wrap: wrap; padding: 1.4rem 1.6rem; margin-bottom: 1rem; }
.cr-donut { position: relative; width: 168px; height: 168px; flex: 0 0 auto; }
.cr-donut svg { transform: rotate(-90deg); display: block; }
.cr-seg { animation: none; }
@keyframes cr-seg { from { stroke-dasharray: 0 999; } }
.cr-donut-center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
.cr-donut-pct { font-size: 2.55rem; font-weight: 800; line-height: 1; letter-spacing: -.035em; }
.cr-donut-lab { margin-top: .3rem; font-size: .72rem; font-weight: 600; letter-spacing: 0; opacity: .6; }
.cr-hero-text { flex: 1 1 320px; min-width: 0; }
.cr-headline { margin: 0; font-size: 1.75rem !important; font-weight: 700; letter-spacing: 0; line-height: 1.15; }
.cr-headsub { margin: .35rem 0 0 0; opacity: .72; font-size: 1.05rem !important; }
.cr-chips { display: flex; flex-wrap: wrap; gap: .45rem; margin-top: 1rem; }
.cr-chip { background: #fff; display: inline-flex; align-items: center; gap: .35rem; padding: .22rem .6rem; border-radius: 4px; font-size: .8rem; font-weight: 600; white-space: nowrap;
  border: 1px solid rgba(127,127,127,.35); background: transparent; }
.cr-chip.alert { color: #ef4444; border-color: rgba(239,68,68,.55); background: transparent; }
.cr-chip.check { color: #d97706; border-color: rgba(245,158,11,.6); background: transparent; }
.cr-chip.ok { color: #16a34a; border-color: rgba(34,197,94,.55); background: transparent; }
.cr-chip.info { color: #0d9488; border-color: rgba(20,184,166,.5); background: transparent; }
.cr-foot { margin: 1.05rem 0 0 0; font-size: .76rem !important; opacity: .55; line-height: 1.45; }
.cr-row { display: grid; grid-template-columns: minmax(120px, 1.35fr) 2fr auto; gap: .8rem; align-items: center; margin: .62rem 0; }
.cr-row-l { font-size: .88rem; font-weight: 650; line-height: 1.25; }
.cr-row-r { font-size: .78rem; opacity: .75; white-space: nowrap; }
.cr-bar { display: flex; gap: 1px; height: 10px; border-radius: 2px; overflow: hidden; background: rgba(127,127,127,.2); }
.cr-bar > i { display: block; height: 100%; }
@keyframes cr-grow { from { width: 0; } }
.cr-tl { position: relative; height: 84px; margin: .3rem .9rem .2rem; }
.cr-tl-line { position: absolute; left: 0; right: 0; top: 48px; height: 2px; background: rgba(127,127,127,.5); }
.cr-tl-dot { position: absolute; top: 41px; width: 16px; height: 16px; margin-left: -8px; border-radius: 50%; border: 3px solid rgba(255,255,255,.9); box-sizing: border-box; cursor: default; }
.cr-tl-dot span { position: absolute; bottom: 24px; left: 50%; transform: translateX(-50%); font-size: .68rem; font-weight: 800; white-space: nowrap; opacity: .85; }
.cr-tl-end { position: absolute; top: 62px; font-size: .68rem; font-weight: 700; opacity: .65; white-space: nowrap; }
.cr-dl { display: flex; align-items: center; gap: .65rem; margin: .5rem 0; }
.cr-dl .cr-dot { width: 10px; height: 10px; border-radius: 50%; flex: 0 0 auto; }
.cr-dl .t { flex: 1; min-width: 0; font-size: .88rem; font-weight: 650; line-height: 1.25; }
.cr-dl .t small { display: block; font-weight: 500; opacity: .6; font-size: .74rem; }
.cr-section { margin: 1.5rem 0 .7rem 0; padding-bottom: .4rem; border-bottom: 1px solid rgba(127,127,127,.3); display: flex; align-items: baseline; gap: .6rem; font-size: 1.15rem; font-weight: 700; letter-spacing: 0; }
.cr-section small { font-size: .78rem; font-weight: 500; opacity: .6; letter-spacing: 0; }
.cr-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; grid-auto-rows: 1fr; }
@media (max-width: 1100px) { .cr-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 700px) { .cr-grid { grid-template-columns: 1fr; } }
.cr-act { border: 1px solid #d9dee5; border-left: 4px solid var(--c, #64748b); border-radius: 8px; padding: 1rem 1.15rem; background: #fff !important; color: #1f2937; min-height: 190px; box-sizing: border-box; }

.cr-act b { display: block; margin-bottom: .3rem; font-size: .95rem; line-height: 1.3; }
.cr-act .d { font-size: .84rem; opacity: .8; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
.cr-act .r { margin-top: .5rem; font-size: .84rem; line-height: 1.4; }
.cr-act .s { margin-top: .55rem; font-size: .7rem; opacity: .55; }
.cr-act.adv .d { display: block; -webkit-line-clamp: unset; overflow: visible; }
.cr-act.adv .d strong { font-weight: 700; }
.adv-grid { margin-bottom: 1.1rem; }
.cr-note { margin: .9rem 0 0 0; font-size: .82rem !important; opacity: .65; }
.cr-banner { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; padding: .85rem 1.2rem; height: auto; }
.cr-banner .m { flex: 1 1 260px; font-size: .92rem; line-height: 1.35; }
.cr-banner .m b { font-weight: 800; }
'''

_edit_css = ""
if ss.get("tab_navigation", "Édition") == "Édition":
    # La carte passe en pleine largeur : marges de page réduites.
    _edit_css = (
        '[data-testid="stMainBlockContainer"] { max-width: 100% !important; padding: 2.2rem .75rem 1rem !important; }'
        '.st-key-crowdrunning_tabs { margin-bottom: .6rem !important; }'
    )
_MAIN = '[data-testid="stMainBlockContainer"] '
_conf_css = ""
if ss.get("tab_navigation") in ("Conformité", "Résultats"):
    # Cadres blancs uniformes (détail des règles, files d'attente, secours, paramètres).
    _conf_css = (
        _MAIN + '[data-testid="stExpander"] details { background: #fff !important; border: 1px solid #d9dee5 !important; border-radius: 8px !important; box-shadow: none !important; }'
        + _MAIN + '[data-testid="stExpander"] details, ' + _MAIN + '[data-testid="stExpander"] details * { color: #1f2937 !important; }'
        + _MAIN + '[data-testid="stExpander"] summary, ' + _MAIN + '[data-testid="stExpander"] summary:focus { background: #fff !important; }'
        + _MAIN + '[data-testid="stExpander"] summary:hover { background: #f8fafc !important; }'
    )
if ss.get("tab_navigation") == "Résultats":
    _conf_css += (
        # Indicateurs : cartes blanches de même gabarit.
        _MAIN + '[data-testid="stMetric"] { background: #fff; border: 1px solid #d9dee5; border-radius: 8px; padding: 1rem 1.15rem; height: 146px; box-sizing: border-box; }'
        + _MAIN + '[data-testid="stMetric"] * { color: #1f2937 !important; }'
        + _MAIN + '[data-testid="stMetricDelta"] * { opacity: .7; font-size: .78rem; white-space: normal !important; overflow: visible !important; text-overflow: clip !important; }'
        + _MAIN + '[data-testid="stMetricValue"], ' + _MAIN + '[data-testid="stMetricValue"] * { font-size: 1.7rem !important; white-space: normal !important; overflow: visible !important; }'
        # Graphiques et tableaux : même cadre.
        + _MAIN + '[data-testid="stPlotlyChart"] { background: #fff; border: 1px solid #d9dee5; border-radius: 8px; padding: .5rem; box-sizing: border-box; overflow: hidden; }'
        + _MAIN + '[data-testid="stDataFrame"] { border: 1px solid #d9dee5; border-radius: 8px; overflow: hidden; }'
    )
st.markdown(f"""<style>
{_background_css}

/* Cartes : rayon identique dans l'éditeur et dans la simulation. */
.st-key-editor_map,
.st-key-editor_map [data-testid="stIFrame"],
.st-key-editor_map iframe {{
  border-radius: 8px !important;
  overflow: hidden !important;
}}

/* Simulation : rapproche le ruban de densité de la carte animée. */
.st-key-simulation_density_ribbon {{ margin-bottom: -2.5rem !important; padding-bottom: 0 !important; }}
.st-key-simulation_density_ribbon [data-testid="stIFrame"] {{ margin-bottom: 0 !important; }}

/* Éditeur cartographique façon Google Maps : la carte occupe la page, les commandes flottent par-dessus. */
.st-key-map_stage {{ position: relative; }}
.st-key-map_ov_left, .st-key-map_ov_right {{ position: absolute !important; z-index: 30; width: auto !important; gap: .5rem !important; }}
.st-key-map_ov_left {{ top: 12px; left: 12px; width: 236px !important; gap: .4rem !important; }}
.st-key-map_ov_right {{ top: 12px; left: 262px; right: 12px; }}
.st-key-map_ov_left .stElementContainer, .st-key-map_ov_right .stElementContainer {{ width: 100% !important; }}
/* Boutons de gauche : même boîte blanche et mêmes états que les objets à poser. « Simuler » est l'action principale (rouge). */
.st-key-map_ov_left [data-testid="stBaseButton-primary"], .st-key-map_ov_left button[kind="primary"], .st-key-map_ov_left [data-testid="stBaseButton-secondary"], .st-key-map_ov_left .stDownloadButton button {{
  width: 100%; min-height: 0; height: 40px; border-radius: 12px; border: 1px solid #d9dee5 !important; background: #fff !important;
  box-shadow: 0 1px 4px rgba(60,64,67,.25); padding: 0 .9rem; transition: background .15s ease;
}}
.st-key-map_ov_left [data-testid="stBaseButton-primary"], .st-key-map_ov_left button[kind="primary"] {{ height: 52px; background: #ff4a4a !important; border-color: #ff4a4a !important; }}
.st-key-map_ov_left [data-testid="stBaseButton-secondary"] *, .st-key-map_ov_left .stDownloadButton button * {{ color: #4b5563 !important; font-weight: 600; font-size: .9rem; }}
.st-key-map_ov_left [data-testid="stBaseButton-primary"] *, .st-key-map_ov_left button[kind="primary"] * {{ color: #fff !important; font-weight: 700; font-size: 1rem; }}
.st-key-map_ov_left [data-testid="stBaseButton-secondary"]:hover, .st-key-map_ov_left .stDownloadButton button:hover {{ background: #f3f4f6 !important; }}
.st-key-map_ov_left [data-testid="stBaseButton-primary"]:hover, .st-key-map_ov_left button[kind="primary"]:hover {{ background: #f03a3a !important; }}
/* Scène de la simulation. */
.st-key-map_sim_stage {{ position: relative; }}
.st-key-map_ov_sim {{ position: absolute !important; z-index: 30; right: 12px; width: 190px !important; }}
/* Simulation : le bouton de fond de carte flotte en haut à droite, même style. */
.st-key-map_ov_sim [data-testid="stBaseButton-primary"], .st-key-map_ov_sim button[kind="primary"], .st-key-map_ov_sim [data-testid="stBaseButton-secondary"], .st-key-map_ov_sim .stDownloadButton button {{
  width: 100%; min-height: 0; height: 40px; border-radius: 12px; border: 1px solid #d9dee5 !important; background: #fff !important;
  box-shadow: 0 1px 4px rgba(60,64,67,.25); padding: 0 .9rem; transition: background .15s ease;
}}
.st-key-map_ov_sim [data-testid="stBaseButton-primary"], .st-key-map_ov_sim button[kind="primary"] {{ height: 52px; background: #ff4a4a !important; border-color: #ff4a4a !important; }}
.st-key-map_ov_sim [data-testid="stBaseButton-secondary"] *, .st-key-map_ov_sim .stDownloadButton button * {{ color: #4b5563 !important; font-weight: 600; font-size: .9rem; }}
.st-key-map_ov_sim [data-testid="stBaseButton-primary"] *, .st-key-map_ov_sim button[kind="primary"] * {{ color: #fff !important; font-weight: 700; font-size: 1rem; }}
.st-key-map_ov_sim [data-testid="stBaseButton-secondary"]:hover, .st-key-map_ov_sim .stDownloadButton button:hover {{ background: #f3f4f6 !important; }}
.st-key-map_ov_sim [data-testid="stBaseButton-primary"]:hover, .st-key-map_ov_sim button[kind="primary"]:hover {{ background: #f03a3a !important; }}
/* Objets à poser : même boîte blanche et même segment actif rouge que la barre d'onglets. */
.st-key-map_ov_right {{ background: #fff; border: 1px solid #d9dee5; border-radius: 12px; padding: 4px; box-shadow: 0 1px 4px rgba(60,64,67,.25); box-sizing: border-box; }}
.st-key-map_ov_right [data-testid="stPills"], .st-key-map_ov_right [data-testid="stButtonGroup"] [role="radiogroup"] {{
  display: flex !important; flex-wrap: wrap !important; gap: 4px; overflow: visible; padding: 0;
}}
.st-key-map_ov_right [data-testid="stPills"]::-webkit-scrollbar, .st-key-map_ov_right [role="radiogroup"]::-webkit-scrollbar {{ display: none; }}
.st-key-map_ov_right [data-testid="stWidgetLabel"] {{ display: none !important; }}
.st-key-map_ov_right .stElementContainer {{ margin: 0 !important; }}
.st-key-map_ov_right .stElementContainer, .st-key-map_ov_right [data-testid="stButtonGroup"], .st-key-map_ov_right [data-testid="stPills"], .st-key-map_ov_right [data-testid="stButtonGroup"] [role="radiogroup"] {{ width: 100% !important; max-width: 100% !important; }}
.st-key-map_ov_right button[data-variant="pills"], .st-key-map_ov_right [data-testid="stPills"] button {{
  flex: 1 1 auto; height: 2.6rem; min-height: 0; border-radius: 9px; padding: 0 .8rem; border: 0 !important; background: transparent !important;
  box-shadow: none !important; transition: background .15s ease;
}}
.st-key-map_ov_right button[data-variant="pills"] *, .st-key-map_ov_right [data-testid="stPills"] button * {{ color: #4b5563 !important; font-weight: 600; white-space: nowrap; }}
.st-key-map_ov_right button[data-variant="pills"]:hover, .st-key-map_ov_right [data-testid="stPills"] button:hover {{ background: #f3f4f6 !important; }}
.st-key-map_ov_right button[data-variant="pills"][aria-checked="true"], .st-key-map_ov_right [data-testid="stPills"] button[aria-pressed="true"] {{ background: #ff4a4a !important; }}
.st-key-map_ov_right button[data-variant="pills"][aria-checked="true"] *, .st-key-map_ov_right [data-testid="stPills"] button[aria-pressed="true"] * {{ color: #fff !important; font-weight: 700; }}
@media (max-width: 760px) {{
  .st-key-map_ov_left {{ width: 190px !important; }} .st-key-map_ov_right {{ left: 212px; }}
}}
{_edit_css}
{_conf_css}

{NAV_CSS}
{CARD_CSS}
</style>""", unsafe_allow_html=True)

TAB_ORDER = ["Édition", "Simulation", "Résultats", "Conformité"]
TAB_LABEL = {"Édition": "🗺️ Édition", "Simulation": "🎬 Simulation", "Résultats": "📊 Résultats", "Conformité": "📜 Conformité"}
selected_tab = ss.tab_navigation
ss.active_tab = selected_tab

# La barre d'onglets est rendue plus bas (après le calcul de l'état) : ses pastilles en dépendent.
nav_slot = st.container(key="crowdrunning_tabs")

# En-tête dynamique : il suit l'onglet sélectionné.
_tab_meta = {
    "Édition": ("", "", ""),
    "Simulation": ("", "", ""),
    "Résultats": ("", "", ""),
    "Conformité": ("", "", ""),
}
_k, _t, _sub = _tab_meta[selected_tab]
# ---- barre latérale -------------------------------------------------------------------
with st.sidebar:
    # Le GPX est hors du formulaire : la carte d'édition doit connaître le tracé avant le lancement.
    gpx_file = st.file_uploader("Tracé GPX", type=["gpx"]) #ajouter les détails distance et nom du fichier
    event_date = st.date_input("Date de la prochaine édition", value=date.today() + timedelta(days=120),
                               help="Sert aux échéances administratives (déclaration, assurance…) de l'onglet Conformité.")
    start_time = st.text_input("Heure du 1er départ (hh:mm)", "09:30")
    animated_agents = st.slider("Coureurs attendus", 100, 10000, 1000, 10,
                                help="Nombre des partants simulés, réparti sur toutes les vagues. Les densités, elles, utilisent tout le monde. La carte animée en affiche au plus 300.")
        
    with st.expander("🏁 Coureurs", expanded=True):
        n_waves = st.slider("Vagues de départ", 1, 10, 5)
        wave_gap = st.slider("Intervalle entre vagues (s)", 0, 600, 300, 10)

        dispersion = st.slider(
            "Dispersion des allures", 0.08, 0.35, 0.18, 0.01
        )

        cutoff_options = [
            f"{m // 60:02d}:{m % 60:02d}"
            for m in range(120, 301, 5)
        ]
        cutoff_label = st.select_slider(
            "Barrière horaire (hh:mm)",
            options=cutoff_options,
            value="03:00",
            help="Temps depuis le départ de la première vague."
        )

        density_target = st.slider(
            "Densité cible (coureurs/m²)",
            0.2, 0.5, 0.3, 0.1
        )

        st.markdown("**Répartition, temps médian et allure par vague**")
        st.caption(
            "L'allure est calculée automatiquement à partir du temps médian estimé et de la distance réelle du parcours."
            )

        # Déterminer le parcours courant sans utiliser le parcours de démonstration
        # pour calculer une allure de course.
        wave_route_geo = None

        if gpx_file is not None:
            try:
                candidate_route = load_gpx(
                    gpx_file.getvalue(), geographic=True
                )
                if len(candidate_route) >= 2:
                    wave_route_geo = candidate_route
            except Exception:
                wave_route_geo = None

        elif ss.get("scenario_route"):
            try:
                candidate_route = list(ss.scenario_route)
                if len(candidate_route) >= 2:
                    wave_route_geo = candidate_route
            except Exception:
                wave_route_geo = None

        wave_route_length_m = None

        if wave_route_geo is not None:
            coords = np.asarray(wave_route_geo, dtype=float)
            demo_coords = np.asarray(DEMO_ROUTE, dtype=float)

            is_demo_for_wave = (
                coords.shape == demo_coords.shape
                and np.allclose(coords, demo_coords, rtol=0.0, atol=1e-8)
            )

            if not is_demo_for_wave:
                try:
                    wave_route_local = geographic_to_local(
                        [tuple(p) for p in wave_route_geo]
                    )
                    length = float(sc.route_length(wave_route_local))

                    if math.isfinite(length) and length > 0:
                        wave_route_length_m = length
                except (TypeError, ValueError, IndexError):
                    wave_route_length_m = None

        # Convertir un temps total HH:MM en allure MM:SS par kilomètre.
        def _wave_pace_label(time_label, distance_m):
            if distance_m is None or distance_m <= 0:
                return "X"

            try:
                hours, minutes = map(
                    int, str(time_label).split(":")[:2]
                )
                total_seconds = hours * 3600 + minutes * 60

                if total_seconds <= 0:
                    return "X"

                seconds_per_km = round(
                    total_seconds / (distance_m / 1000.0)
                )

                pace_minutes = seconds_per_km // 60
                pace_seconds = seconds_per_km % 60

                return f"{pace_minutes}:{pace_seconds:02d}"

            except (TypeError, ValueError, AttributeError):
                return "X"

        default_shares = [
            100 // n_waves + (1 if i < 100 % n_waves else 0)
            for i in range(n_waves)
        ]

        wave_time_options = [
            f"{m // 60:02d}:{m % 60:02d}"
            for m in range(5, 12 * 60 + 1, 5)
        ]
        default_wave_time = "02:00"

        if "wave_config" not in st.session_state:
            st.session_state.wave_config = {}

        # Synchroniser immédiatement les modifications du tableau avant
        # de recalculer les allures lors du prochain rendu Streamlit.
        wave_editor_key = f"wave_editor_{n_waves}"

        def _sync_wave_config_from_editor(editor_key, row_count):
            widget_state = st.session_state.get(editor_key, {})
            edited_rows = (
                widget_state.get("edited_rows", {})
                if isinstance(widget_state, dict)
                else {}
            )

            current = st.session_state.get("wave_config", {})
            updated = {
                int(i): dict(row)
                for i, row in current.items()
            }

            for row_idx, changes in edited_rows.items():
                i = int(row_idx)
                if 0 <= i < row_count:
                    updated.setdefault(i, {}).update(changes)

            st.session_state.wave_config = updated

        previous = st.session_state.wave_config
        rows = []

        for i in range(n_waves):
            old = previous.get(i, {})

            old_time = old.get(
                "Temps estimé (hh:mm)", default_wave_time
            )
            if old_time not in wave_time_options:
                old_time = default_wave_time

            old_share = old.get(
                "Coureurs (%)", default_shares[i]
            )

            rows.append({
                "Vague": f"Vague {i + 1}",
                "Coureurs (%)": old_share,
                "Temps estimé (hh:mm)": old_time,
                "Allure calculée (min/km)": _wave_pace_label(
                    old_time, wave_route_length_m
                ),
            })

        wave_df = pd.DataFrame(rows)

        edited_waves = st.data_editor(
            wave_df,
            hide_index=True,
            disabled=[
                "Vague",
                "Allure calculée (min/km)",
            ],
            column_config={
                "Vague": st.column_config.TextColumn(
                    "Vague"
                ),
                "Coureurs (%)": st.column_config.NumberColumn(
                    "Coureurs (%)",
                    min_value=0,
                    max_value=100,
                    step=1,
                    format="%d%%",
                    required=True,
                ),
                "Temps estimé (hh:mm)": st.column_config.SelectboxColumn(
                    "Temps estimé (hh:mm)",
                    options=wave_time_options,
                    required=True,
                ),
                "Allure calculée (min/km)": st.column_config.TextColumn(
                    "Allure calculée (min/km)",
                    help=(
                        "Temps médian total divisé par la distance du parcours. "
                        "Un tiret indique qu'aucun parcours réel exploitable "
                        "n'est disponible."
                    ),
                ),
            },
            key=wave_editor_key,
            on_change=_sync_wave_config_from_editor,
            args=(wave_editor_key, n_waves),
        )

        # Conserver les valeurs effectivement éditées.
        st.session_state.wave_config = {
            i: row
            for i, row in enumerate(
                edited_waves.to_dict("records")
            )
        }

        pct_series = pd.to_numeric(
            edited_waves["Coureurs (%)"],
            errors="coerce"
        )

        wave_percentages = [
            int(v) if pd.notna(v) and float(v).is_integer() else -1
            for v in pct_series.tolist()
        ]

        wave_median_labels = [
            str(v) if pd.notna(v) else ""
            for v in edited_waves["Temps estimé (hh:mm)"].tolist()
        ]

        wave_counts_valid = (
            len(wave_percentages) == int(n_waves)
            and all(0 <= p <= 100 for p in wave_percentages)
        )

        wave_times_valid = (
            len(wave_median_labels) == int(n_waves)
            and all(label in wave_time_options for label in wave_median_labels)
        )

        total_percentage = (
            sum(wave_percentages) if wave_counts_valid else -1
        )

        wave_settings_valid = (
            wave_counts_valid
            and wave_times_valid
            and total_percentage == 100
        )

        if not wave_counts_valid:
            st.error(
                "Saisis un pourcentage entier compris entre 0 et 100 "
                "pour chaque vague."
            )
        elif total_percentage != 100:
            st.warning(
                f"Le total des vagues est de {total_percentage} %. "
                "Ajuste les pourcentages pour atteindre exactement 100 % "
                "avant de simuler."
            )
        elif not wave_times_valid:
            st.error(
                "Choisis un temps médian estimé valide pour chaque vague."
            )
        else:
            st.success("Répartition des coureurs : 100 %.")

        if wave_route_length_m is None:
            st.caption(
                "L'allure sera affichée après le chargement d'un GPX réel "
                "ou d'un scénario contenant un tracé exploitable."
            )

        # Conservé pour le calcul de référence effectué après la résolution
        # définitive du parcours dans le reste de l'application.
        pace_reference_placeholder = st.empty()

    with st.expander("🛣️ Infrastructure", expanded=False):
        width = st.slider("Largeur moyenne du couloir (m)", 2.0, 20.0, 5.0, 0.5,
            help="Largeur de référence. Les rétrécissements posés sur la carte la remplacent localement.")
        default_width = float(min(10.0, max(3.0, round(width * 2) / 2)))
        start_width = st.slider("Largeur de la zone de départ (m)", 3.0, 10.0, default_width, 0.5)
        finish_width = st.slider("Largeur du couloir d'arrivée (m)", 3.0, 10.0, default_width, 0.5)

    with st.expander("🌤️ Météo", expanded=False):
        temperature = st.slider("Température au départ (°C)", -5, 38, 14)
        temperature_rise = st.slider("Montée de la température (°C/h)", 0.0, 6.0, 2.0, 0.5)
        drink_percent = st.slider("Part des coureurs qui boivent (%)", 10, 100, 60, 5)

    with st.expander("❓ Hypothèses terrain", expanded=False):
        st.caption("Hypothèses enregistrées dans le scénario ; elles ne constituent pas un dimensionnement réglementaire.")
        medical_incidents = st.number_input("Prises en charge médicales / 1000 partants", 0.5, 30.0, 4.0, 0.5)
        response_minutes = st.slider("Délai d'intervention visé (min)", 2.0, 15.0, 5.0, 0.5)
        wc_percent = st.slider("Part utilisant les WC avant le départ", 0.2, 0.9, 0.6, 0.05)
        wc_wait = st.slider("Attente max tolérée aux WC (min)", 3.0, 25.0, 10.0, 1.0)
        flow_max = st.slider("Capacité d'une voie (coureurs / mètre / s)", 1.0, 2.5, 1.5, 0.1,
            help="Débit de libération des coureurs à la ligne de départ. Hypothèse à calibrer.")
        stop_seconds = st.slider("Arrêt moyen au point d'eau automatique (s)", 5.0, 30.0, 10.0, 0.5,
            help="Utilisé seulement pour les points d'eau automatiques (tous les 5 km). Les points posés sur la carte ont leur propre durée.")
        auto_slots = st.slider("Postes de service d'un point d'eau automatique", 5, 100, 25, 5)
        st.caption("Toilettes et secours (hypothèses à calibrer)")
        wc_window = st.slider("Fenêtre d'arrivée avant le départ (min)", 30, 180, 90, 10,
            help="Les usagers des WC arrivent entre cette durée et ~5 min avant le franchissement de la ligne par leur vague.")
        team_speed = st.slider("Vitesse d'intervention des équipes (m/s)", 1.5, 8.0, 4.0, 0.5,
            help="Déplacement le long du parcours (≈ 4 m/s : vélo ou quad ; ≈ 2 m/s : à pied).")
        treatment_min = st.slider("Durée moyenne d'une prise en charge (min)", 5, 60, 20, 5,
            help="Temps pendant lequel une équipe reste mobilisée par un incident (trajets de retour en plus).")
        finish_share = st.slider("Part des incidents survenant à l'arrivée (%)", 0, 80, 30, 5,
            help="Hypothèse : les prises en charge se concentrent souvent après la ligne. Le reste est réparti sur le parcours selon l'occupation simulée.")

    with st.expander("📜 Réglementation", expanded=False):
        st.caption("Contexte de l'épreuve : il détermine les règles du règlement FFA 2026 qui s'appliquent (onglet Conformité).")
        event_type_label = st.selectbox("Type d'épreuve", ["Course sur route", "Trail / course nature"],
            help="Les règles santé-secours du titre IV (trail) sont plus strictes que celles de la route.")
        competition = st.checkbox("Épreuve chronométrée ou classée (compétition)", value=True)
        public_road = st.checkbox("Parcours sur voie publique ou ouverte à la circulation", value=True)
        multi_department = st.checkbox("Parcours sur plusieurs départements", value=False)
        ffa_affiliated = st.checkbox("Organisateur affilié à la FFA", value=True,
            help="Un organisateur non affilié doit demander l'avis et, dans certains cas, l'autorisation de la FFA.")
        d_plus = st.number_input("Dénivelé positif cumulé (m)", 0, 20000, 0, 50,
            help="Sert au km-effort (distance + 1 km par 100 m de D+) pour les trails.")

    with st.expander("🎞️ Charger un scénario", expanded=False):
        loaded = st.file_uploader("Charger un scénario", type=["geojson", "json"],
                                  key=f"load_scenario_{ss.editor_version}", label_visibility="collapsed", help="Remplace le tracé et les objets posés par ceux du fichier GeoJSON ou JSON."
                                  )
        
    expected_runners = animated_agents


# ---- réglementation : évaluation et affichage -----------------------------------------
def evaluate_rules(objects, result):
    """Évalue le scénario avec le contexte de la barre latérale. Retourne (règles, constats, erreur)."""
    try:
        data = rl.load_rules()
    except Exception as exc:
        return None, [], f"Fichier de règles illisible : {exc}"
    ctx = rl.Context(
        runners=int(expected_runners), length_m=float(route_len_m), event_date=event_date, today=date.today(),
        event_type="trail" if event_type_label.startswith("Trail") else "route", d_plus_m=float(d_plus),
        competition=bool(competition), public_road=bool(public_road), multi_department=bool(multi_department),
        ffa_affiliated=bool(ffa_affiliated))
    manual = {r["id"]: bool(ss.get(f"manual_{r['id']}", False)) for r in data["rules"]}
    return data, rl.evaluate(data, ctx, objects, result, manual), None


RULE_ICON = {"alert": "🔴", "check": "🟡", "ok": "🟢", "info": "ℹ️"}
RULE_FORCE = {"obligation": "Obligation", "recommandation": "Recommandation", "information": "Information"}


def plot_white(fig):
    """Graphique dans un cadre blanc, identique en thème clair et sombre."""
    fig.update_layout(paper_bgcolor="#ffffff", plot_bgcolor="#ffffff", font={"color": "#1f2937"},
                      margin={"l": 15, "r": 15, "t": 25, "b": 60})
    fig.update_xaxes(gridcolor="#e5e9ef", zerolinecolor="#d9dee5", automargin=True)
    fig.update_yaxes(gridcolor="#e5e9ef", zerolinecolor="#d9dee5", automargin=True)
    st.plotly_chart(fig, width="stretch", theme=None)


def render_compliance(findings, meta, interactive=True, only_open=False, show_text=False, statuses=None):
    """Constats par thème. `interactive` : cases « à confirmer » ; `only_open` : écarts et points à vérifier seulement."""
    for theme in meta["themes"]:
        items = [f for f in findings if f.theme == theme and (not only_open or f.status in ("alert", "check"))
                 and (statuses is None or f.status in statuses)]
        if not items:
            continue
        c = rl.summarize(items)
        badge = " · ".join(f"{RULE_ICON[k]} {c[k]}" for k in ("alert", "check", "ok", "info") if c[k])
        with st.expander(f"{theme} - {badge}", expanded=c["alert"] > 0):
            for n, f in enumerate(items):
                st.markdown(f"{RULE_ICON[f.status]} **{f.title}**" + (f"  \n{f.detail}" if f.detail else ""))
                if f.recommendation:
                    st.markdown(f"➡️ {f.recommendation}")
                if f.manual and interactive:
                    st.checkbox(f.confirm, key=f"manual_{f.id}")
                source = f"{RULE_FORCE[f.force]} · art. {f.article} · p. {f.page} du règlement"
                if f.derived:
                    source += " · échéance déduite du texte"
                st.caption(source)
                if show_text or f.status in ("alert", "check"):
                    if f.text:
                        st.caption("Règle : " + f.text)
                    if f.quote:
                        st.caption("« " + f.quote + " »")
                if f.interpretation:
                    st.caption("Interprétation de l'application : " + f.interpretation)
                if f.note:
                    st.caption("Remarque : " + f.note)
                if n < len(items) - 1:
                    st.divider()


# ---- tableau de bord de conformité : fonctions d'affichage ----------------------------
STATUS_COLOR = {"alert": "#ef4444", "check": "#f59e0b", "ok": "#22c55e", "info": "#64748b"}
STATUS_ICON = {"alert": "🔴", "check": "🟡", "ok": "🟢", "info": "ℹ️"}
STATUS_WORD = {"alert": "Écart", "check": "À vérifier", "ok": "Conforme", "info": "Information"}
DEADLINE_COLOR = {"alert": "#ef4444", "check": "#f59e0b", "ok": "#22c55e", "info": "#14b8a6"}


def _e(x) -> str:
    """Échappe un texte pour l'insérer dans du HTML ; neutralise aussi `$` (formules) et les retours à la ligne."""
    return htmllib.escape(str(x), quote=True).replace("$", "&#36;").replace("\n", " ")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n <= 1 else many}"


def _fr_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def donut_svg(counts: dict, size: int = 168, stroke: int = 17) -> str:
    """Anneau : part des règles évaluables (conformes / à vérifier / écarts). Les informations n'y figurent pas."""
    r = (size - stroke) / 2
    c = 2 * math.pi * r
    mid = size / 2
    total = counts["ok"] + counts["check"] + counts["alert"]
    ring = (f'<circle cx="{mid}" cy="{mid}" r="{r:.2f}" fill="none" stroke="rgba(127,127,127,.22)" stroke-width="{stroke}"/>')
    segs, start = "", 0.0
    if total:
        for key in ("ok", "check", "alert"):
            n = counts[key]
            if not n:
                continue
            length = c * n / total
            drawn = max(length - (3.0 if n < total else 0.0), 0.5)
            segs += (f'<circle class="cr-seg" cx="{mid}" cy="{mid}" r="{r:.2f}" fill="none" stroke="{STATUS_COLOR[key]}" '
                     f'stroke-width="{stroke}" stroke-dasharray="{drawn:.2f} {c:.2f}" stroke-dashoffset="{-start:.2f}"/>')
            start += length
    label = f"{counts['ok']} règles conformes, {counts['check']} à vérifier, {counts['alert']} écarts"
    return f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img" aria-label="{label}">{ring}{segs}</svg>'


def compliance_hero_html(counts: dict, meta: dict, info: dict) -> str:
    ok, chk, alt = counts["ok"], counts["check"], counts["alert"]
    total = ok + chk + alt
    pct = f"{round(100 * ok / total)}%" if total else "-"
    if alt:
        head = f"{_plural(alt, 'écart', 'écarts')} à corriger"
        sub = (f"puis {_plural(chk, 'point', 'points')} à vérifier" if chk else "avant de déposer le dossier")
    elif chk:
        head, sub = "Aucun écart détecté", f"{_plural(chk, 'point', 'points')} à vérifier ou à confirmer"
    elif total:
        head, sub = "Tous les contrôles sont validés", "Relis tout de même les échéances ci-dessous."
    else:
        head, sub = "Aucune règle évaluable", "Vérifiez le contexte de l'épreuve dans la barre latérale."
    chips = "".join(f'<span class="cr-chip {k}">{STATUS_ICON[k]} {_plural(counts[k], one, many)}</span>'
                    for k, one, many in (("alert", "écart", "écarts"), ("check", "à vérifier", "à vérifier"),
                                         ("ok", "conforme", "conformes"), ("info", "information", "informations")) if counts[k])
    days = info["days_to_event"]
    when = f"{_fr_date(info['event_date'])} · " + (f"J-{days}" if days >= 0 else f"passée depuis {-days} j")
    ctx = "".join(f'<span class="cr-chip">{_e(t)}</span>' for t in (
        info["type_label"], f"{info['runners']:,} partants".replace(",", " "), f"{info['km']:.1f} km", when))
    return (
        '<div class="cr-card cr-hero-card">'
        f'<div class="cr-donut">{donut_svg(counts)}<div class="cr-donut-center">'
        f'<div class="cr-donut-pct">{pct}</div><div class="cr-donut-lab">validées</div></div></div>'
        f'<div class="cr-hero-text"><div class="cr-headline">{_e(head)}</div><div class="cr-headsub">{_e(sub)}</div>'
        f'<div class="cr-chips">{chips}</div><div class="cr-chips">{ctx}</div>'
        f'<div class="cr-foot">{_e(meta["document"])} · adopté par le {_e(meta["adopted"])} · règles v{_e(meta["rules_version"])}. '
        'Aide à la préparation du dossier : elle ne remplace ni l\'avis de la FFA, ni la préfecture, ni les services de secours. '
        'L\'anneau compare les règles validées aux écarts et aux points à vérifier ; les informations n\'y comptent pas.</div>'
        '</div></div>')


def theme_bars_html(findings: list, meta: dict) -> str:
    rows = ""
    for theme in meta["themes"]:
        items = [f for f in findings if f.theme == theme]
        if not items:
            continue
        c = rl.summarize(items)
        n = len(items)
        bar = "".join(f'<i style="width:{100 * c[k] / n:.2f}%;background:{STATUS_COLOR[k]}" title="{STATUS_WORD[k]} : {c[k]}"></i>'
                      for k in ("alert", "check", "ok", "info") if c[k])
        txt = " · ".join(f"{STATUS_ICON[k]} {c[k]}" for k in ("alert", "check", "ok", "info") if c[k])
        rows += f'<div class="cr-row"><div class="cr-row-l">{_e(theme)}</div><div class="cr-bar">{bar}</div><div class="cr-row-r">{txt}</div></div>'
    return f'<div class="cr-card"><h4>Par thème</h4>{rows}</div>'


def deadlines_html(findings: list, event_date: date, today: date) -> str:
    items = sorted((f for f in findings if getattr(f, "due", "")), key=lambda f: getattr(f, "due", ""))
    if not items:
        return '<div class="cr-card"><h4>Échéances administratives</h4><div class="cr-note">Aucune échéance pour ce type d\'épreuve.</div></div>'
    dues = [date.fromisoformat(f.due) for f in items]
    t0, t1 = min([today] + dues), max([event_date] + dues)
    span = max((t1 - t0).days, 1)
    pos = lambda d: min(100.0, max(0.0, 100.0 * (d - t0).days / span))  # noqa: E731
    dots, rows = "", ""
    for f, d in zip(items, dues):
        left = (d - today).days
        lab = f"dans {left} j" if left >= 0 else f"dépassée de {-left} j"
        color = DEADLINE_COLOR.get(f.status, "#14b8a6")
        dots += (f'<div class="cr-tl-dot" style="left:{pos(d):.2f}%;background:{color}" title="{_e(f.title)} - {_fr_date(d)} ({lab})">'
                 f'<span>{left if left >= 0 else "−" + str(-left)} j</span></div>')
        rows += (f'<div class="cr-dl"><span class="cr-dot" style="background:{color}"></span>'
                 f'<div class="t">{_e(f.title)}<small>au plus tard le {_fr_date(d)} · art. {_e(f.article)}</small></div>'
                 f'<span class="cr-chip {f.status}">{lab}</span></div>')
    ends = (f'<div class="cr-tl-end" style="left:{pos(today):.2f}%;transform:translateX(-{0 if pos(today) < 8 else 50}%)">Aujourd\'hui</div>'
            f'<div class="cr-tl-end" style="right:0">Jour J · {_fr_date(event_date)}</div>')
    return (f'<div class="cr-card"><h4>Échéances administratives</h4>'
            f'<div class="cr-tl"><div class="cr-tl-line"></div>{dots}{ends}</div>{rows}</div>')


def actions_html(findings: list, limit: int = 40) -> str:
    todo = [f for f in findings if f.status == "alert"] + [f for f in findings if f.status == "check" and not f.manual]
    if not todo:
        return ""
    cards = ""
    for f in todo[:limit]:
        reco = f'<div class="r">➡️ {_e(f.recommendation)}</div>' if f.recommendation else ""
        cards += (f'<div class="cr-act" style="--c:{STATUS_COLOR[f.status]}"><b>{STATUS_ICON[f.status]} {_e(f.title)}</b>'
                  f'<div class="d">{_e(f.detail)}</div>{reco}'
                  f'<div class="s">art. {_e(f.article)} · p. {_e(f.page)} du règlement</div></div>')
    return f'<div class="cr-grid">{cards}</div>'


def advice_html(items: list) -> str:
    """Cartes de conseils d'effectifs : (titre, règle de calcul, besoin, posé, unité, détail)."""
    cards = ""
    for title, rule, need, posed, unit, detail in items:
        status = "ok" if posed >= need else "check"
        gap = (f"Il manque environ <strong>{need - posed:g} {unit}</strong>." if status == "check"
               else "Effectif posé suffisant.")
        cards += (f'<div class="cr-act adv" style="--c:{STATUS_COLOR[status]}"><b>{STATUS_ICON[status]} {_e(title)}</b>'
                  f'<div class="d">Recommandé : <strong>{need:g} {unit}</strong> ({_e(rule)}).<br>Posé : <strong>{posed:g}</strong>. {gap}</div>'
                  f'<div class="r">{detail}</div></div>')
    return f'<div class="cr-grid adv-grid">{cards}</div>'


def loader_html(title: str, detail: str, pct: float) -> str:
    """Écran de chargement centré : anneau de progression rouge, pourcentage et message."""
    pct = max(0.0, min(100.0, float(pct)))
    return (
        '<style>'
        '.cr-load { position: fixed; inset: 0; z-index: 999999; display: flex; align-items: center; justify-content: center; background: rgba(17,24,39,.45); }'
        '.cr-load-card { display: flex; flex-direction: column; align-items: center; gap: 1.1rem; width: min(420px, 86vw); padding: 2.2rem 2rem 2rem; box-sizing: border-box;'
        ' background: #fff; color: #1f2937; border: 1px solid #d9dee5; border-radius: 14px; box-shadow: 0 12px 40px rgba(0,0,0,.28); text-align: center; }'
        '.cr-load-ring { position: relative; width: 132px; height: 132px; border-radius: 50%; display: flex; align-items: center; justify-content: center; }'
        '.cr-load-ring::after { content: ""; position: absolute; inset: 0; border-radius: 50%; border: 3px solid transparent; border-top-color: rgba(255,74,74,.35); animation: cr-spin 1.1s linear infinite; }'
        '.cr-load-inner { width: 106px; height: 106px; border-radius: 50%; background: #fff; display: flex; align-items: center; justify-content: center; font-size: 1.9rem; font-weight: 800; color: #1f2937; }'
        '.cr-load-title { font-size: 1.25rem; font-weight: 700; color: #1f2937; }'
        '.cr-load-detail { font-size: .92rem; color: #6b7280; min-height: 1.3em; }'
        '@keyframes cr-spin { to { transform: rotate(360deg); } }'
        '@media (prefers-reduced-motion: reduce) { .cr-load-ring::after { animation: none; } }'
        '</style>'
        f'<div class="cr-load" role="status" aria-live="polite"><div class="cr-load-card">'
        f'<div class="cr-load-ring" style="background: conic-gradient(#ff4a4a {pct * 3.6:.1f}deg, #eceff3 0deg)">'
        f'<div class="cr-load-inner">{pct:.0f}&nbsp;%</div></div>'
        f'<div class="cr-load-title">{_e(title)}</div><div class="cr-load-detail">{_e(detail)}</div></div></div>'
    )


def banner_html(counts: dict, findings: list) -> str:
    first = next((f for f in findings if f.status == "alert"), None)
    if first:
        msg = f'<b>{_plural(counts["alert"], "écart", "écarts")}</b> · premier à traiter : {_e(first.title)}'
    elif counts["check"]:
        msg = f'<b>Aucun écart</b> · {_plural(counts["check"], "point", "points")} à vérifier'
    else:
        msg = '<b>Tous les contrôles sont validés</b>'
    chips = "".join(f'<span class="cr-chip {k}">{STATUS_ICON[k]} {counts[k]}</span>' for k in ("alert", "check", "ok") if counts[k])
    return f'<div class="cr-card cr-banner"><div class="m">📜 {msg}</div><div class="cr-chips" style="margin:0">{chips}</div></div>'


def nav_badges_css(badges: dict) -> str:
    """CSS dynamique des pastilles : {position 1-based: (texte, fond, texte, couleur quand l'onglet est actif)}."""
    out = []
    for i, (txt, bg, fg, sel) in badges.items():
        out.append(f'.st-key-crowdrunning_tabs [role="radiogroup"] > :nth-child({i}) {{ --cr-badge: {bg}; --cr-badge-fg: {fg}; --cr-badge-sel: {sel}; }}'
                   f'.st-key-crowdrunning_tabs [role="radiogroup"] > :nth-child({i})::after {{ content: "{txt}"; }}')
    return "".join(out)


# ---- tracé courant --------------------------------------------------------------------
route_error = None
if gpx_file is not None:
    try:
        gpx_bytes = gpx_file.getvalue()
        route_geo = load_gpx(gpx_bytes, geographic=True)
        if len(route_geo) < 2:
            raise ValueError("Le GPX doit contenir au moins deux points de tracé.")
        route_source = f"GPX « {gpx_file.name} »"
    except Exception as exc:
        route_error = f"Lecture du GPX impossible : {exc}"
        route_geo, route_source = list(DEMO_ROUTE), "parcours de démonstration"
elif ss.scenario_route:
    route_geo, route_source = list(ss.scenario_route), "tracé du scénario chargé"
else:
    route_geo, route_source = list(DEMO_ROUTE), "parcours de démonstration"
if route_error:
    st.error(route_error)
route_geo = [tuple(p) for p in route_geo]
origin = route_geo[0]
route_local = geographic_to_local(route_geo)
route_len_m = sc.route_length(route_local)
# Reconnaît aussi le tracé de démonstration s'il a été sauvegardé puis rechargé comme GeoJSON.
_current_coords = np.asarray(route_geo, dtype=float)
_demo_coords = np.asarray(DEMO_ROUTE, dtype=float)
is_demo_route = (
    route_source == "parcours de démonstration"
    or (
        _current_coords.shape == _demo_coords.shape
        and np.allclose(_current_coords, _demo_coords, rtol=0.0, atol=1e-8)
    )
)

# L'allure n'est plus un réglage : elle est déduite des temps HH:MM par vague
# et de la distance réelle du tracé, puis agrégée selon la part de chaque vague.
wave_median_times_s = []
wave_pace_labels = []
reference_pace_seconds_per_km = None
reference_pace_label = "X"
if route_error:
    pace_reference_placeholder.warning(
        "Allure de référence indisponible : corrige le fichier GPX pour calculer les allures."
    )
elif is_demo_route:
    pace_reference_placeholder.warning(
        f"Aucun tracé réel n'est chargé. Le parcours de démonstration ne mesure que "
        f"{route_len_m / 1000.0:.2f} km : les temps HH:MM saisis ne permettent donc pas "
        "d'afficher une allure de course pertinente. Charge un fichier GPX ou un scénario "
        "contenant le tracé réel."
    )
elif wave_settings_valid:
    try:
        (wave_median_times_s, wave_pace_labels,
         reference_pace_seconds_per_km, reference_pace_label) = wave_pace_reference(
            route_len_m, wave_median_labels, wave_percentages
        )
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        pace_reference_placeholder.caption(f"Allure calculée indisponible : {exc}")

# Un changement de tracé invalide les projections des objets et le résultat précédent.
route_signature = (len(route_geo), round(route_geo[0][0], 6), round(route_geo[0][1], 6), round(route_len_m))
if ss.get("route_signature") != route_signature:
    ss.route_signature = route_signature
    for o in ss.objects:
        sc.refresh_projection(o, route_local, origin)
    ss.result = None
# Invalide aussi les résultats précédemment calculés sur le tracé de démonstration :
# ils pourraient sinon conserver d'anciennes allures incohérentes dans l'onglet Résultats.
if is_demo_route and ss.result is not None:
    ss.result = None

# ---- conformité (pré-calcul) et barre d'onglets ----------------------------------------
# Évalué ici, avant les onglets : la pastille de l'onglet Conformité et le bandeau de l'éditeur en dépendent.
fresh_result = ss.result if (ss.result is not None and ss.objects == ss.result.get("objects")) else None
rules_data, findings, rules_error = evaluate_rules(ss.objects, fresh_result)
rule_counts = rl.summarize(findings) if findings else {"alert": 0, "check": 0, "ok": 0, "info": 0}

_badges = {}
if ss.objects:
    _badges[1] = (str(len(ss.objects)), "#64748b", "#fff", "#475569")
if ss.result is not None:
    _badges[2] = ("✓", "#22c55e", "#fff", "#15803d")
    _badges[3] = ("✓", "#22c55e", "#fff", "#15803d")
if rules_error is None and findings:
    if rule_counts["alert"]:
        _badges[4] = (str(rule_counts["alert"]), "#ef4444", "#fff", "#dc2626")
    elif rule_counts["check"]:
        _badges[4] = (str(rule_counts["check"]), "#f59e0b", "#3b2300", "#b45309")
    else:
        _badges[4] = ("✓", "#22c55e", "#fff", "#15803d")
with nav_slot:
    st.radio("Navigation principale", TAB_ORDER, format_func=TAB_LABEL.get,
             horizontal=True, key="tab_navigation", label_visibility="collapsed")
    st.markdown(f"<style>{nav_badges_css(_badges)}</style>", unsafe_allow_html=True)

# `run` existe quelle que soit la vue ; il ne devient vrai que si l'utilisateur lance le calcul.
run = False

if selected_tab == "Édition":
    # ---- éditeur de scénario : la carte devient l'élément principal -----------------------
    kind_labels = {k: f"{v['emoji']} {v['label']}" for k, v in sc.KINDS.items()}
    tool_labels = {
        "volunteer": "🙋 Bénévoles",
        "water": "💧 Eau",
        "toilets": "🚻 Toilettes",
        "bottleneck": "🚧 Rétrécissement",
        "medical": "⛑️ Secours",
        "doctor": "🩺 Médecin",
        "ambulance": "🚑 Ambulance",
        "sponge": "🧽 Épongeage",
    }
    
    # Scène cartographique : la carte occupe toute la largeur, les commandes flottent par-dessus (style Google Maps).
    with st.container(key="map_stage"):
        # En haut à gauche : « Simuler » (à la place de la barre de recherche) et les commandes secondaires.
        with st.container(key="map_ov_left"):
            run = st.button("▶  Simuler", type="primary")
            map_style_button("map_style_btn_edit")
            st.download_button(
                "↓ Enregistrer le scénario",
                sc.to_geojson(route_geo, ss.objects, {
                    "expected_runners": int(expected_runners),
                    "waves": int(n_waves),
                    "wave_gap_s": int(wave_gap),
                    "wave_percentages": wave_percentages,
                    "wave_median_times_hh_mm": wave_median_labels,
                    "route_source": route_source,
                    "corridor_width_m": float(width),
                }),
                "scenario_crowdrunning.geojson", "application/geo+json",
            )
            if ss.objects and st.button("🗑 Vider", help="Supprimer tous les objets posés"):
                ss.objects = []
                ss.editor_version += 1
                ss.result = None
                st.rerun()

        # En haut à droite : les objets à poser, en pastilles défilantes.
        with st.container(key="map_ov_right"):
            place_kind = st.pills(
                "Objet à poser sur le parcours", options=list(tool_labels),
                selection_mode="single", default="water", format_func=tool_labels.get,
                key="place_kind_pills", label_visibility="collapsed",
            )
            if place_kind is None:
                place_kind = "water"

        meta = sc.KINDS[place_kind]

        # Même rendu que la carte de l'onglet Simulation (fond OpenStreetMap, tracé, repères, pastilles) ;
        # seul le zoom passe en bas à droite pour libérer le coin supérieur gauche.
        _style_name, (_tile_url, _tile_attr, _tile_zoom) = current_map_style()
        base_map = folium.Map(location=route_geo[0], zoom_start=15, tiles=None, control_scale=False, zoom_control=False)
        folium.TileLayer(tiles=_tile_url, attr=_tile_attr, max_zoom=_tile_zoom, name=_style_name).add_to(base_map)

        class _ZoomBottomRight(MacroElement):
            _template = Template("{% macro script(this, kwargs) %}L.control.zoom({position: 'bottomright'}).addTo({{ this._parent.get_name() }});{% endmacro %}")
        _ZoomBottomRight().add_to(base_map)

        base_map.get_root().header.add_child(folium.Element(
            "<style>.leaflet-container{cursor:crosshair!important;border-radius:8px!important;overflow:hidden!important;}</style>"
        ))
        folium.PolyLine(route_geo, color="#2563eb", weight=5, opacity=0.9).add_to(base_map)
        _pin = "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png"
        for _pt, _tip in ((route_geo[0], "Départ"), (route_geo[-1], "Arrivée")):
            folium.Marker(_pt, tooltip=_tip, icon=folium.CustomIcon(_pin, icon_size=(25, 41), icon_anchor=(12, 41))).add_to(base_map)
        lats, lons = [p[0] for p in route_geo], [p[1] for p in route_geo]
        base_map.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]], padding=(25, 25))

        objects_layer = folium.FeatureGroup(name="Objets posés")
        for o in ss.objects:
            m = sc.KINDS[o["kind"]]
            tip = f"{htmllib.escape(o['name'])} · km {o['progress_m'] / 1000:.2f}"
            # Tous les équipements partagent le même marqueur circulaire blanc avec bord coloré.
            # Le point d'eau utilise une goutte bleue au centre plutôt qu'un simple cercle plein.
            marker_emoji = "💧" if o["kind"] == "water" else m["emoji"]
            marker_color = "#1d4ed8" if o["kind"] == "water" else m["color"]
            marker_font = 17 if o["kind"] == "water" else 16
            icon_html = (
                f'<div style="font-size:{marker_font}px;line-height:22px;text-align:center;width:24px;height:24px;'
                f'background:#fff;border:2px solid {marker_color};border-radius:50%;'
                f'box-shadow:0 1px 3px rgba(0,0,0,.35);box-sizing:border-box">{marker_emoji}</div>'
            )
            folium.Marker([o["lat"], o["lon"]], tooltip=tip,
                          icon=folium.DivIcon(html=icon_html, icon_size=(24, 24), icon_anchor=(12, 12))).add_to(objects_layer)
            if o["kind"] == "volunteer" and o["p2"] > 0:
                folium.Circle([o["lat"], o["lon"]], radius=float(o["p2"]), color=m["color"], weight=1,
                              fill=True, fill_opacity=0.08).add_to(objects_layer)

        # Seul le clic est renvoyé : déplacer ou zoomer la carte ne relance donc pas l'application.
        map_state = st_folium(
            base_map, feature_group_to_add=objects_layer, returned_objects=["last_clicked"],
            height=700, use_container_width=True, key="editor_map",
        )

    if loaded is not None:
        try:
            new_route, new_objects = sc.from_geojson(loaded.getvalue().decode("utf-8"))
            if new_route and gpx_file is None:
                ss.scenario_route = new_route
                route_geo_new = [tuple(p) for p in new_route]
                loc_new = geographic_to_local(route_geo_new)
                for o in new_objects:
                    sc.refresh_projection(o, loc_new, route_geo_new[0])
            else:
                for o in new_objects:
                    sc.refresh_projection(o, route_local, origin)
            ss.objects = new_objects
            ss.editor_version += 1
            ss.result = None
            st.rerun()
        except Exception as exc:
            st.error(f"Scénario illisible : {exc}")

    # Un clic déjà reçu ne doit jamais créer de doublon au rerun.
    click = (map_state or {}).get("last_clicked")
    if click:
        click_key = (round(click["lat"], 7), round(click["lng"], 7))
        if click_key != ss.last_click:
            ss.last_click = click_key
            obj = sc.new_object(place_kind, click["lat"], click["lng"], route_local, origin,
                                name=sc.default_name(place_kind, ss.objects))
            ss.objects.append(obj)
            ss.editor_version += 1
            ss.result = None
            st.rerun()

    # Tableau des objets : édition des paramètres et suppression.
    if ss.objects:
        table = pd.DataFrame([{
            "Supprimer": False, "Type": kind_labels[o["kind"]], "Nom": o["name"],
            "Paramètre 1": o["p1"], "Paramètre 2": o["p2"],
            "Signification": f"{sc.KINDS[o['kind']]['p1'][0]} / {sc.KINDS[o['kind']]['p2'][0]}",
            "km": round(o["progress_m"] / 1000.0, 3), "Écart au tracé (m)": o["offset_m"],
        } for o in ss.objects])
        edited = st.data_editor(
            table, key=f"objects_table_{ss.editor_version}", hide_index=True, width="stretch",
            disabled=["Type", "Signification", "km", "Écart au tracé (m)"],
            column_config={
                "Supprimer": st.column_config.CheckboxColumn(width="small"),
                "Paramètre 1": st.column_config.NumberColumn(min_value=0.5, step=1.0),
                "Paramètre 2": st.column_config.NumberColumn(min_value=0.0, step=1.0),
            })
        if edited["Supprimer"].any():
            keep = [o for o, d in zip(ss.objects, edited["Supprimer"]) if not d]
            ss.objects = keep
            ss.editor_version += 1
            ss.result = None
            st.rerun()
        changed = False
        for o, (_, row) in zip(ss.objects, edited.iterrows()):
            name, p1, p2 = str(row["Nom"]), float(row["Paramètre 1"]), float(row["Paramètre 2"])
            if (name, p1, p2) != (o["name"], o["p1"], o["p2"]):
                o["name"], o["p1"], o["p2"] = name, p1, p2
                changed = True
        if changed:
            ss.result = None
            st.rerun()

        summ = sc.equipment_summary(ss.objects, int(expected_runners), route_len_m)
        b1, b2, b3, b4 = st.columns(4)
        b1.metric("Points d'eau", summ["water_points"],
                  None if summ["max_gap_water_m"] is None else f"écart max {summ['max_gap_water_m'] / 1000:.1f} km", delta_color="off")
        b2.metric("Cabines WC", summ["toilet_cabins"], f"{summ['toilet_cabins_per_1000']} / 1000 partants", delta_color="off")
        b3.metric("Postes de secours", summ["medical_posts"],
                  None if summ["max_gap_medical_m"] is None else f"écart max {summ['max_gap_medical_m'] / 1000:.1f} km", delta_color="off")
        b4.metric("Bénévoles", summ["volunteers"], f"{summ['volunteers_per_1000']} / 1000 partants", delta_color="off")
        if summ["objects_far_from_route"]:
            st.warning("Objets à plus de 60 m du tracé (leur position le long du parcours est celle du point le plus proche) : "
                       + ", ".join(summ["objects_far_from_route"]))
        st.caption(f"Médecins : {summ['doctors']} · infirmiers : {summ['nurses']} · ambulances : {summ['ambulances']} · "
                   f"postes d'épongeage : {summ['sponge_posts']}. Bilan brut : la conformité au règlement est évaluée dans l'onglet Conformité.")
    else:
        st.info("Aucun objet posé. Sans point d'eau posé, la simulation place un point d'eau automatique tous les 5 km.")

# ---- onglet Conformité : tableau de bord ------------------------------------------------
# Placé avant l'arrêt « aucun résultat » : cet onglet n'a pas besoin de simulation.
if selected_tab == "Conformité":
    if rules_error:
        st.error(rules_error)
    else:
        rules_meta = rules_data["meta"]
        _info = {
            "type_label": event_type_label, "runners": int(expected_runners), "km": route_len_m / 1000.0,
            "event_date": event_date, "days_to_event": (event_date - date.today()).days,
        }
        st.markdown(compliance_hero_html(rule_counts, rules_meta, _info), unsafe_allow_html=True)
        st.markdown('<div class="cr-two">' + theme_bars_html(findings, rules_meta)
                    + deadlines_html(findings, event_date, date.today()) + '</div>', unsafe_allow_html=True)

        actions = actions_html(findings)
        if actions:
            st.markdown('<div class="cr-section">À traiter en priorité<small>écarts, puis points à vérifier</small></div>',
                        unsafe_allow_html=True)
            st.markdown(actions, unsafe_allow_html=True)

        st.markdown('<div class="cr-section">Conseils d\'effectifs<small>repères de l\'application, à ajuster avec l\'arrêté et le relevé terrain</small></div>',
                    unsafe_allow_html=True)
        try:
            _conv = po.convergence_points(route_local)
        except Exception:
            _conv = []
        _zones = ["Départ", "Arrivée"]
        _zones += [o["name"] for o in ss.objects if o["kind"] == "water"]
        _zones += [o["name"] for o in ss.objects if o["kind"] == "bottleneck"]
        _crit = []
        if fresh_result is not None:
            try:
                _crit = po.critical_sections(fresh_result["meso"])
            except Exception:
                _crit = []
        _zones += [f"densité élevée km {c['a'] / 1000:.1f}-{c['b'] / 1000:.1f}" for c in _crit]
        ia, ib = st.columns([2, 5], vertical_alignment="center")
        n_inter = ia.number_input("Intersections avec la voie publique", min_value=0, max_value=999,
                                  value=len(_conv), step=1, key="advice_intersections",
                                  help="Carrefours et traversées à garder. Par défaut : les points où le tracé repasse près de lui-même ; l'application ne connaît pas le réseau routier, saisissez le nombre relevé sur le terrain.")
        ib.caption("Zones sensibles prises en compte : " + ", ".join(_zones) + (". Lancez une simulation pour y ajouter les tronçons à forte densité." if fresh_result is None else "."))
        _km = route_len_m / 1000.0
        _rescue_posed = sum(2 * o["p1"] for o in ss.objects if o["kind"] == "medical") + sum(o["p1"] + o["p2"] for o in ss.objects if o["kind"] == "doctor")
        _volunteers = sum(o["p1"] for o in ss.objects if o["kind"] == "volunteer")
        st.markdown(advice_html([
            ("Signaleurs aux intersections", "2 par intersection", float(2 * n_inter), float(_volunteers), "signaleurs",
             "Bénévoles posés sur la carte comptés comme signaleurs. Deux par carrefour : un par axe à tenir."),
            ("Signaleurs en zones sensibles", f"4 par zone, {len(_zones)} zone(s)", float(4 * len(_zones)), float(_volunteers), "signaleurs",
             "Départ, arrivée, points d'eau, rétrécissements et tronçons de forte densité."),
            ("Prise en charge par les secours", f"2 par km, {_km:.1f} km", float(math.ceil(2 * _km)), float(_rescue_posed), "intervenants",
             "Posé = 2 secouristes par équipe de secours, plus médecins et infirmiers."),
        ]), unsafe_allow_html=True)

        with st.expander("Ce que ce règlement ne couvre pas"):
            for line in rules_meta["not_covered"]:
                st.markdown(f"- {line}")
            st.caption(rules_meta["scope"] + " " + rules_meta["page_numbering"])

# ---- simulation -----------------------------------------------------------------------
# Ne pas lancer une simulation de course avec le petit tracé de démonstration par défaut :
# une cible de 1 h à 2 h sur ce tracé ferait apparaître des allures absurdes et serait
# ensuite incompatible avec la vitesse minimale appliquée par le moteur.
if run and is_demo_route:
    st.error(
        "Simulation non lancée : charge un fichier GPX ou un scénario avec un tracé réel. "
        "Le parcours de démonstration sert uniquement à prévisualiser l'interface ; il ne "
        "doit pas servir à calibrer les temps d'arrivée des vagues."
    )
    run = False

if run and not wave_settings_valid:
    if not wave_counts_valid:
        st.error("Simulation non lancée : saisis un pourcentage entier valide pour chaque vague.")
    elif total_percentage != 100:
        st.error("Simulation non lancée : la somme des pourcentages doit être exactement égale à 100 %.")
    else:
        st.error("Simulation non lancée : vérifie le temps médian estimé de chaque vague.")
    run = False

if run:
    try:
        if reference_pace_seconds_per_km is None:
            raise ValueError("Impossible de calculer l'allure globale à partir des temps des vagues.")
        # Vitesse de repli du moteur, dérivée de l'allure pondérée ; chaque vague utilise
        # ensuite sa propre vitesse médiane calculée à partir de son temps HH:MM.
        speed = 1000.0 / reference_pace_seconds_per_km
        engine_stations, width_overrides = sc.engine_inputs(ss.objects, route_len_m)
        if engine_stations:
            stations = [StationSpec(s["pos_m"], s["slots"], s["service_s"], s["name"]) for s in engine_stations]
            water_source = "points d'eau posés sur la carte"
        else:
            stations = [StationSpec(km * 1000.0, int(auto_slots), float(stop_seconds), f"Point d'eau auto km {km:g}")
                        for km in water_stations_from_length(route_len_m)]
            water_source = "points d'eau automatiques (tous les 5 km)"

        sim = MesoSimulation(
            route_local, n_runners=int(expected_runners), desired_speed=speed,
            speed_sigma=float(dispersion), density_target=float(density_target),
            corridor_width=float(width),
            start_width=float(start_width), finish_width=float(finish_width),
            wave_percentages=wave_percentages,
            wave_median_times_s=wave_median_times_s,
            waves=int(n_waves), wave_gap_s=float(wave_gap), flow_max=float(flow_max),
            stations=stations, width_overrides=width_overrides,
            p_drink_base=float(drink_percent) / 100.0, temperature_c=float(temperature),
            temperature_rise_c_per_hour=float(temperature_rise),
            cutoff_s=float(int(cutoff_label.split(":")[0]) * 3600 + int(cutoff_label.split(":")[1]) * 60),
            seed=42, n_sample=min(int(animated_agents), MAX_ANIMATED),
        )
        ss.plan_pdf = None
        loader = st.empty()
        loader.markdown(loader_html("Simulation en cours", "Calcul de l'écoulement des coureurs…", 0), unsafe_allow_html=True)
        _last_pct = [-1]

        def on_progress(frac, t):
            pct = int(frac * 90)       # le calcul de l'écoulement occupe 90 % de la barre ; le reste sert aux étapes suivantes
            if pct != _last_pct[0]:
                _last_pct[0] = pct
                loader.markdown(loader_html("Simulation en cours", f"{int(t):,} s simulées".replace(",", " "), pct), unsafe_allow_html=True)

        meso = sim.run(progress_cb=on_progress)
        loader.markdown(loader_html("Simulation en cours", "Files d'attente des toilettes et dispositif de secours…", 93), unsafe_allow_html=True)
        if True:
            wc_res = simulate_toilets(
                ss.objects, meso.release_s, use_share=float(wc_percent), tolerance_min=float(wc_wait),
                window_min=float(wc_window), seed=42)
            med_res = simulate_medical(
                meso, ss.objects, incidents_per_1000=float(medical_incidents), target_min=float(response_minutes),
                team_speed_m_s=float(team_speed), treatment_min=float(treatment_min),
                finish_share=float(finish_share) / 100.0, seed=42)

        loader.markdown(loader_html("Simulation en cours", "Préparation de l'animation…", 97), unsafe_allow_html=True)
        # Échantillon animé : positions GPS, état et densité réellement subie par chaque coureur.
        sample_ids = meso.sample_ids
        n_s = len(sample_ids)
        frames, rows = [], []
        widths_s = sim.width_at(meso.sample_progress)
        for r, t in enumerate(meso.times_s):
            prog = meso.sample_progress[r].astype(float)
            lateral = meso.sample_lateral_frac * np.maximum(0.1, widths_s[r] / 2.0 - 0.3)
            xy = sim.route_xy(prog, lateral)
            state = meso.sample_state[r]
            agents = []
            for j in range(n_s):
                lat, lon = local_to_geographic((xy[j, 0], xy[j, 1]), origin)
                released = bool(state[j] != 0)
                arrived = bool(state[j] == 4)
                stopped = bool(state[j] in (2, 3))
                agents.append({"id": int(j), "lat": round(lat, 6), "lon": round(lon, 6), "arrived": arrived,
                               "released": released, "stopped": stopped,
                               "speed": round(float(meso.sample_speed[r, j]), 3)})
                rows.append({
                    "time_s": round(float(t), 1), "agent_id": int(sample_ids[j]), "x_m": round(float(xy[j, 0]), 2),
                    "y_m": round(float(xy[j, 1]), 2), "progress_m": round(float(prog[j]), 2),
                    "speed_m_s": round(float(meso.sample_speed[r, j]), 3),
                    "density_p_m2": round(float(meso.sample_rho[r, j]), 3),
                    "released": released, "stopped": stopped, "arrived": arrived,
                    "release_at_s": round(float(meso.release_s[sample_ids[j]]), 1),
                })
            frames.append({"time_s": float(t), "agents": agents})

        station_points = [geographic_point_at_distance(route_geo, route_local, s.pos_m) for s in stations]
        summary_meso = meso.summary()
        settings = {
            "event_date": event_date.isoformat(), "start_time": start_time,
            "expected_runners": int(expected_runners), "waves": int(n_waves),
            "wave_gap_s": int(wave_gap),
            "reference_pace_min_per_km": reference_pace_label,
            "wave_paces_min_per_km": list(wave_pace_labels),
            "wave_percentages": list(wave_percentages),
            "wave_median_times_hh_mm": list(wave_median_labels),
            "wave_counts": list(sim.wave_counts),
            "wave_median_times_s": list(wave_median_times_s),
            "speed_m_s": round(speed, 4), "pace_dispersion_sigma": float(dispersion),
            "cutoff_time_hh_mm": cutoff_label, "density_target_p_m2": float(density_target),
            "corridor_width_m": float(width), "start_width_m": float(start_width),
            "finish_width_m": float(finish_width), "temperature_start_c": int(temperature),
            "temperature_rise_c_per_hour": float(temperature_rise),
            "drink_probability_percent": int(drink_percent),
            "medical_incidents_per_1000": float(medical_incidents),
            "target_response_minutes": float(response_minutes),
            "wc_use_share": float(wc_percent), "max_wc_wait_minutes": float(wc_wait),
            "flow_capacity_per_m_per_s": float(flow_max), "animated_agents": int(n_s),
            "wc_arrival_window_min": float(wc_window), "team_speed_m_s": float(team_speed),
            "treatment_min": float(treatment_min), "finish_incident_share": float(finish_share) / 100.0,
            "seed": 42, "water_source": water_source,
            "route_length_km": round(route_len_m / 1000.0, 3),
            "route_source": route_source,
            "event_type": "trail" if event_type_label.startswith("Trail") else "route",
            "competition": bool(competition), "public_road": bool(public_road),
            "multi_department": bool(multi_department), "ffa_affiliated": bool(ffa_affiliated),
            "d_plus_m": float(d_plus),
        }
        ss.result = {
            "meso": meso, "history": pd.DataFrame(rows), "frames": frames,
            "route_geo": route_geo, "water_points": station_points,
            "station_names": [s.name for s in stations], "n_agents": n_s,
            "duration": int(meso.times_s[-1]),
            "frame_interval": float(meso.record_s), "route_length_m": float(route_len_m),
            "settings": settings, "summary_meso": summary_meso, "water_source": water_source,
            "objects": [dict(o) for o in ss.objects],
            "wc": wc_res, "med": med_res,
        }
        # Conformité figée au lancement (utilise le résultat qui vient d'être calculé).
        _rd, _fd, _err = evaluate_rules(ss.objects, ss.result)
        ss.result["rules_meta"] = None if _rd is None else _rd["meta"]
        ss.result["findings"] = _fd
    except Exception as exc:
        if "loader" in locals():
            loader.empty()
        st.error(f"Erreur pendant la simulation : {exc}")
        st.stop()
    # Après le calcul, navigue automatiquement vers l'onglet Simulation.
    ss["_navigate_to_tab"] = "Simulation"
    st.rerun()

result = ss.result
if result is None:
    if selected_tab == "Édition":
        st.info("Configurez le parcours et les équipements, puis cliquez sur **Simuler** : l'application ouvrira automatiquement l'onglet Simulation.")
    elif selected_tab == "Simulation":
        st.info("Aucune simulation disponible. Retournez dans **Édition**, puis cliquez sur **Simuler**")
    elif selected_tab == "Résultats":
        st.info("Aucun résultat à afficher. Lancez d'abord une simulation depuis l'onglet **Édition**.")
    st.stop()

meso = result["meso"]
df = result["history"]
settings = result["settings"]
sm = result["summary_meso"]
latest = df[df["time_s"] == df["time_s"].max()]
route_length_m = float(result["route_length_m"])
route_length_km = route_length_m / 1000.0

if selected_tab == "Résultats":
    st.caption("Analyse complète : densité dans l'espace et le temps, état final de l'échantillon animé et exports.")
    if ss.objects != result.get("objects"):
        st.warning("Le scénario a été modifié depuis cette simulation : relancez-la pour mettre les résultats à jour.")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Partants simulés", f"{sm['runners']:,}".replace(",", " "))
    c2.metric("Densité maximale", f"{sm['peak_density_p_m2']:.2f} p/m²",
              f"km {sm['peak_density_km']:.2f} · {sm['peak_density_time_s'] / 60:.0f} min", delta_color="off")
    c3.metric("Longueur > 0,5 p/m²", f"{sm['length_above_0_5_m'] / 1000:.2f} km",
              f"dont pire tronçon : {sm['worst_minutes_above_0_5']:.0f} min", delta_color="off")
    median = sm.get("chip_time_median_s")
    c4.metric("Temps médian (net)", "-" if median is None else f"{int(median // 3600)} h {int(median % 3600 // 60):02d}")
    c5.metric("Arrivés / hors délai", f"{sm['finishers_in_horizon']:,} / {sm['after_cutoff_gun_time']:,}".replace(",", " "),
              f"barrière {cutoff_label} (temps réel depuis le 1er départ)", delta_color="off")
    st.caption(f"Densités calculées sur les {sm['runners']:,} partants (pas sur l'échantillon animé). Points d'eau : {result['water_source']}.".replace(",", " "))

    with st.expander("⚙️ Paramètres appliqués", expanded=True):
        left, right = st.columns(2)
        with left:
            st.write(f"**Édition :** {settings['event_date']} à {settings['start_time']}")
            _reference_pace = settings.get(
                "reference_pace_min_per_km", settings.get("median_pace_min_per_km", "X")
            )
            st.write(f"**Allure de référence calculée (moyenne pondérée) :** {_reference_pace} min/km")
            st.write(f"**Vagues :** {settings['waves']} · intervalle {settings['wave_gap_s']} s")
            _n_waves_summary = int(settings.get("waves", 1))
            _wave_pct_summary = settings.get("wave_percentages", [100 / _n_waves_summary] * _n_waves_summary)
            _wave_time_summary = settings.get("wave_median_times_hh_mm", ["X"] * _n_waves_summary)
            _wave_count_summary = settings.get("wave_counts", [0] * _n_waves_summary)
            _wave_pace_summary = settings.get("wave_paces_min_per_km", [])
            for i, (pct, target, count) in enumerate(zip(
                _wave_pct_summary, _wave_time_summary, _wave_count_summary
            ), start=1):
                _pace_detail = (f" · allure cible {_wave_pace_summary[i - 1]} min/km"
                                if i - 1 < len(_wave_pace_summary) else "")
                st.write((f"- **Vague {i} :** {pct:g}% ({count:,} coureurs) · temps cible {target}"
                          f"{_pace_detail}").replace(",", " "))
            st.write(f"**Largeurs :** départ {settings['start_width_m']} m · parcours {settings['corridor_width_m']} m · arrivée {settings['finish_width_m']} m")
        with right:
            st.write(f"**Météo :** {settings['temperature_start_c']} °C au départ, +{settings['temperature_rise_c_per_hour']} °C/h")
            st.write(f"**Eau :** {settings['drink_probability_percent']} % boivent en base")
            st.write(f"**Sanitaires :** {settings['wc_use_share'] * 100:.0f} % de partants susceptibles de les utiliser ; attente cible max {settings['max_wc_wait_minutes']} min")
            st.write(f"**Secours :** {settings['medical_incidents_per_1000']} prises en charge / 1 000 · délai visé {settings['target_response_minutes']} min")
        st.caption("Les files d'attente des toilettes et les délais d'intervention des secours sont modélisés séparément ; ils n'influencent pas en retour le déplacement des coureurs.")

    if meso.station_stats:
        with st.expander("💧 Files d'attente aux points d'eau", expanded=False):
            sdf = pd.DataFrame(meso.station_stats).rename(columns={
                "name": "Point", "km": "km", "slots": "Postes", "users": "Coureurs servis",
                "max_queue": "File max (coureurs)", "mean_wait_s": "Attente moyenne (s)", "max_wait_s": "Attente max (s)"})
            st.dataframe(sdf, hide_index=True, width="stretch")
            st.caption("Un poste de service sert un coureur à la fois pendant la durée de service. Une file qui se forme signale un point d'eau sous-dimensionné pour ce flux.")
            if meso.queue_len_t is not None and meso.queue_len_t.size and meso.queue_len_t.max() > 0:
                qfig = go.Figure()
                for j, s_ in enumerate(meso.stations):
                    if meso.queue_len_t[:, j].max() > 0:
                        qfig.add_trace(go.Scatter(x=meso.times_s / 60.0, y=meso.queue_len_t[:, j], mode="lines", name=s_.name))
                qfig.update_layout(height=300, margin={"l": 15, "r": 15, "t": 25, "b": 35},
                                   xaxis={"title": "Temps depuis le 1er départ (min)"},
                                   yaxis={"title": "Coureurs en file"}, legend={"orientation": "h"})
                plot_white(qfig)

    # ---- toilettes avant le départ ---------------------------------------------------------
    wc = result.get("wc")
    with st.expander("🚻 Toilettes avant le départ", expanded=False):
        if wc is None:
            toilets_far = [o["name"] for o in result["objects"] if o["kind"] == "toilets"]
            if toilets_far:
                st.info("Les toilettes posées sont à plus de 500 m du départ : elles ne sont pas modélisées (zone de départ uniquement).")
            else:
                st.info("Aucune toilette posée. Placez des toilettes à moins de 500 m du départ pour simuler leur file d'attente.")
        else:
            w1, w2, w3, w4, w5 = st.columns(5)
            w1.metric("Usagers", f"{wc.n_users:,}".replace(",", " "), f"{wc.cabins_total} cabines", delta_color="off")
            w2.metric("Attente moyenne", f"{wc.wait_mean_s / 60:.1f} min", f"95 % sous {wc.wait_p95_s / 60:.1f} min", delta_color="off")
            w3.metric(f"Attente > {wc.tolerance_s / 60:.0f} min", f"{wc.share_over_tolerance * 100:.0f} %")
            w4.metric("En retard pour leur vague", f"{wc.share_late_for_start * 100:.0f} %",
                      "fin d'usage < 2 min avant la ligne", delta_color="off")
            needed = "> 3 000" if wc.cabins_needed is None else f"≈ {wc.cabins_needed}"
            w5.metric("Cabines estimées nécessaires", needed, f"{wc.cabins_total} posées", delta_color="off")
            st.caption(f"Estimation : {needed} cabines (durée de service moyenne {wc.service_mean_s:.0f} s, en un seul bloc) pour que 95 % des usagers attendent moins de {wc.tolerance_s / 60:.0f} min. Modèle sans norme réglementaire ; hypothèses d'arrivée et de durée d'usage à calibrer.")
            wfig = go.Figure(go.Scatter(x=wc.timeline_min, y=wc.timeline_queue, mode="lines", fill="tozeroy",
                                        line={"color": "#7c3aed"}, name="File d'attente"))
            wfig.add_vline(x=0, line_dash="dot", annotation_text="1er départ")
            wfig.update_layout(height=300, margin={"l": 15, "r": 15, "t": 25, "b": 35},
                               xaxis={"title": "Temps par rapport au 1er départ (min)"},
                               yaxis={"title": "Personnes en file"})
            plot_white(wfig)
            st.dataframe(pd.DataFrame(wc.per_site).rename(columns={
                "name": "Site", "cabins": "Cabines", "service_s": "Service moyen (s)", "users": "Usagers",
                "wait_mean_s": "Attente moyenne (s)", "wait_max_s": "Attente max (s)"}), hide_index=True, width="stretch")
            if wc.excluded_sites:
                st.caption("Non modélisées (à plus de 500 m du départ) : " + ", ".join(wc.excluded_sites))

    # ---- secours --------------------------------------------------------------------------
    med = result.get("med")
    with st.expander("⛑️ Secours et temps d'intervention", expanded=False):
        if med is None:
            st.info("Aucun poste de secours posé. Placez des postes sur la carte pour estimer les temps d'intervention.")
        else:
            target_min_ = med.target_s / 60.0
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Incidents attendus", f"{med.mean_incidents:.0f}", f"{med.n_reps} tirages", delta_color="off")
            m2.metric("Intervention médiane", f"{med.response_p50_s / 60:.1f} min", f"95 % sous {med.response_p95_s / 60:.1f} min", delta_color="off")
            m3.metric(f"Dans les {target_min_:.0f} min visées", f"{med.share_within_target * 100:.0f} %")
            m4.metric("Au moins un hors cible", f"{med.prob_any_over_target * 100:.0f} %", "des tirages", delta_color="off")
            m5.metric("Parcours hors cible", f"{med.uncovered_m / 1000:.1f} km", "sans attente d'équipe", delta_color="off")
            st.caption(f"{med.share_waiting_for_team * 100:.0f} % des incidents attendent qu'une équipe se libère ; charge des équipes (demande / capacité) : {med.team_busy_share:.2f}. Au-delà de 1, le dispositif est saturé : les délais explosent quelle que soit la distance.")
            cov = go.Figure()
            cov.add_trace(go.Scatter(x=med.coverage_km, y=med.coverage_s / 60.0, mode="lines", line={"color": "#dc2626"},
                                     name="Délai sans attente d'équipe"))
            cov.add_hline(y=target_min_, line_dash="dash", annotation_text=f"cible {target_min_:.0f} min")
            for p in med.posts:
                cov.add_vline(x=p["pos_m"] / 1000.0, line_dash="dot", line_color="#6b7280", annotation_text=p["name"])
            cov.update_layout(height=320, margin={"l": 15, "r": 15, "t": 25, "b": 35},
                              xaxis={"title": "Position de l'incident sur le parcours (km)", "range": [0, result["route_length_m"] / 1000.0]},
                              yaxis={"title": "Délai d'intervention (min)", "rangemode": "tozero"})
            plot_white(cov)
            st.dataframe(pd.DataFrame(med.posts).rename(columns={
                "name": "Poste", "pos_m": "Position (m)", "offset_m": "Écart au tracé (m)", "teams": "Équipes",
                "mobilisation_s": "Mobilisation (s)"}), hide_index=True, width="stretch")
            st.caption("Les équipes se déplacent uniquement le long du parcours, sans être gênées par les coureurs, à la vitesse choisie. Les incidents sont tirés selon l'occupation simulée, avec un poids croissant avec la distance, et une part à l'arrivée.")

if selected_tab == "Simulation":
    # ---- ruban de densité -----------------------------------------------------------------
    ribbon_bins = max(1, min(260, int(math.ceil(route_length_m / 100.0)), meso.n_cells))
    ribbon_values = meso.profile_max(ribbon_bins)
    ribbon_bin_m = route_length_m / ribbon_bins
    peak_density, peak_km = sm["peak_density_p_m2"], sm["peak_density_km"]

    def ribbon_color(value):
        if value < 0.3:
            return COLOR_FLUID
        if value <= 0.5:
            return COLOR_CONSTRAINED
        return COLOR_CONGESTED

    svg_width, bar_y, bar_h = 1200.0, 22.0, 42.0
    rects = []
    for i, value in enumerate(ribbon_values):
        x0, x1 = svg_width * i / ribbon_bins, svg_width * (i + 1) / ribbon_bins
        rects.append(
            f'<rect x="{x0:.3f}" y="{bar_y}" width="{(x1 - x0) + 0.35:.3f}" height="{bar_h}" fill="{ribbon_color(value)}">'
            f'<title>{i * ribbon_bin_m / 1000:.2f}-{(i + 1) * ribbon_bin_m / 1000:.2f} km : {value:.2f} coureur/m²</title></rect>')

    ticks = []
    tick_step = 5.0 if route_length_km > 12 else (1.0 if route_length_km > 3 else 0.5)
    tick_values = list(np.arange(0.0, route_length_km, tick_step))
    if not tick_values or not math.isclose(tick_values[-1], route_length_km, abs_tol=0.05):
        tick_values.append(route_length_km)
    for km in tick_values:
        x = svg_width * km / route_length_km
        if math.isclose(km, route_length_km, abs_tol=0.001):
            anchor, x_text = "end", min(svg_width - 2, x)
        elif math.isclose(km, 0.0, abs_tol=0.001):
            anchor, x_text = "start", max(2, x)
        else:
            anchor, x_text = "middle", x
        if math.isclose(km, 0.0, abs_tol=0.001):
            label = "km 0"
        elif math.isclose(km, route_length_km, abs_tol=0.001):
            label = f"{km:.1f} km" if not math.isclose(km, round(km), abs_tol=0.05) else f"{km:.0f}"
        else:
            label = f"{km:.0f}" if tick_step >= 1 else f"{km:.1f}"
        ticks.append(
            f'<line x1="{x:.2f}" y1="{bar_y + bar_h}" x2="{x:.2f}" y2="{bar_y + bar_h + 7}" stroke="#30363B" stroke-width="1" />'
            f'<text x="{x_text:.2f}" y="{bar_y + bar_h + 21}" text-anchor="{anchor}" font-size="10" fill="#58606A">{label}</text>')

    water_markers = []
    for s in meso.stations:
        x = svg_width * (s.pos_m / 1000.0) / route_length_km
        water_markers.append(f'<polygon points="{x - 7:.2f},22 {x + 7:.2f},22 {x:.2f},34" fill="{COLOR_WATER}">'
                             f'<title>{htmllib.escape(s.name)} : {s.pos_m / 1000:.2f} km</title></polygon>')
    for o in result["objects"]:
        if o["kind"] == "bottleneck":
            x0 = svg_width * max(0.0, o["progress_m"] - o["p2"] / 2) / route_length_m
            x1 = svg_width * min(route_length_m, o["progress_m"] + o["p2"] / 2) / route_length_m
            water_markers.append(f'<rect x="{x0:.2f}" y="{bar_y + bar_h + 1}" width="{max(2.0, x1 - x0):.2f}" height="4" fill="#ea580c">'
                                 f'<title>{htmllib.escape(o["name"])} : {o["p1"]:g} m de large</title></rect>')

    svg = (f'<svg viewBox="0 0 {svg_width:.0f} 90" width="100%" role="img" aria-label="Densité le long du parcours" '
           'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">' + "".join(rects)
           + f'<rect x="0.5" y="{bar_y}" width="{svg_width - 1}" height="{bar_h}" fill="none" stroke="#30363B" stroke-width="1" />'
           + "".join(water_markers) + "".join(ticks) + "</svg>")


    def legend_item(color, text, border="#87918d"):
        return (f'<span><i style="display:inline-block;width:10px;height:10px;background:{color};border:1px solid {border};'
                f'vertical-align:-2px;margin-right:5px;border-radius:2px"></i>{text}</span>')

    ribbon_html = (
        '<div style="width:100%;font-family:Arial,sans-serif">'
        '<div style="display:flex;justify-content:space-between;gap:1px;align-items:baseline;flex-wrap:wrap;color:#58606A;font-size:10px;margin:0 0 -15px 0">'
        f'<strong style="color:#1E2328;font-size:10px">Pic : {peak_density:.2f} coureur/m² au km {peak_km:.2f}</strong>'
        '</div>' + svg + '</div>')
    # Le ruban est encapsulé pour maîtriser l’espace vertical avant la carte.
    with st.container(key="simulation_density_ribbon"):
        render_html(ribbon_html, 135)

    # ---- carte animée ---------------------------------------------------------------------
    frames_json = js_json(result["frames"])
    route_json = js_json(route_geo)
    water_json = js_json(result["water_points"])
    objects_json = js_json([
        {"lat": o["lat"], "lon": o["lon"], "emoji": sc.KINDS[o["kind"]]["emoji"], "color": sc.KINDS[o["kind"]]["color"],
         "name": htmllib.escape(o["name"])} for o in result["objects"] if o["kind"] != "water"])
    
    # Le curseur est affiché SOUS la carte pour préserver l'espace de visualisation.
    # Sa valeur est lue depuis session_state avant de construire la carte : lorsqu'il est
    # modifié, Streamlit relance le script et la carte récupère immédiatement la nouvelle vitesse.
    playback_widget_key = "target_playback_seconds"
    target_playback_seconds = int(ss.get(playback_widget_key, 120))
    simulated_duration_s = max(1.0, float(result["frames"][-1].get("time_s", result["duration"])))
    rate = max(1.0, simulated_duration_s / float(target_playback_seconds))

    _sim_style, (sim_tile_url, sim_tile_attr, sim_tile_zoom) = current_map_style()
    map_html = f'''<!DOCTYPE html>
    <html><head><meta charset="utf-8">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin=""/>
    <style>
    html, body {{ margin:0; padding:0; font-family:Arial,sans-serif; }}
    #map {{ height:550px; width:100%; border-radius:8px; overflow:hidden; }}
    .controls {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; padding:10px 2px; color:#243447; font-size:14px; }}
    .controls button {{ border:1px solid #cbd5e1; border-radius:5px; padding:7px 12px; background:white; cursor:pointer; }}
    .controls button:hover {{ background:#f1f5f9; }}
    #clock {{ font-weight:700; min-width:130px; }}
    .legend {{ display:flex; gap:12px; align-items:center; margin-left:auto; flex-wrap:wrap; }}
    .dot {{ width:10px; height:10px; display:inline-block; border-radius:50%; margin-right:4px; }}
    .pin {{ font-size:16px; line-height:22px; text-align:center; width:24px; height:24px; background:white; border-radius:50%; box-shadow:0 1px 3px rgba(0,0,0,.35); }}
    </style></head><body>
    <div id="map"></div>
    <div class="controls"><button id="toggle">⏸ Pause</button><button id="restart">↺ Recommencer</button><span id="clock">Temps : 0 s</span>
    <div class="legend"><span><i class="dot" style="background:#f97316"></i>En mouvement</span><span><i class="dot" style="background:#0284c7"></i>Coureur au ravitaillement</span><span><i class="dot" style="background:#16a34a"></i>Arrivés</span><span><i class="dot" style="background:#2563eb"></i>Point d'eau</span></div></div>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
    <script>
    const route={route_json}; const frames={frames_json}; const waterPoints={water_json}; const extraObjects={objects_json}; const playbackRate={rate};
    const map=L.map('map',{{scrollWheelZoom:true}});
    L.tileLayer('{sim_tile_url}',{{maxZoom:{sim_tile_zoom},attribution:'{sim_tile_attr}'}}).addTo(map);
    const line=L.polyline(route,{{color:'#2563eb',weight:5,opacity:0.9}}).addTo(map);
    L.marker(route[0],{{title:'Départ'}}).addTo(map).bindTooltip('Départ');
    L.marker(route[route.length-1],{{title:'Arrivée'}}).addTo(map).bindTooltip('Arrivée');
    const waterIcon=L.divIcon({{className:'',html:'<div class="pin" style="border:2px solid #1d4ed8;color:#1d4ed8;box-sizing:border-box">💧</div>',iconSize:[24,24],iconAnchor:[12,12]}});
    waterPoints.forEach((p,i)=>L.marker(p,{{icon:waterIcon}}).addTo(map).bindTooltip('Point d’eau '+(i+1)));
    extraObjects.forEach(o=>L.marker([o.lat,o.lon],{{icon:L.divIcon({{className:'',html:'<div class="pin" style="border:2px solid '+o.color+'">'+o.emoji+'</div>',iconSize:[24,24],iconAnchor:[12,12]}})}}).addTo(map).bindTooltip(o.name));
    map.fitBounds(line.getBounds(),{{padding:[25,25]}});
    const markers=new Map();
    frames[0].agents.forEach(a=>{{const m=L.circleMarker([a.lat,a.lon],{{radius:4,color:'#9a3412',weight:1,fillColor:'#f97316',fillOpacity:a.released?0.95:0,opacity:a.released?1:0}}).addTo(map);m.bindTooltip('Coureur '+(a.id+1));markers.set(a.id,m);}});
    let playing=true,elapsed=0,previous=null;const clock=document.getElementById('clock'),toggle=document.getElementById('toggle');
    const maxTime=frames[frames.length-1].time_s;
    function formatTime(seconds){{const h=Math.floor(seconds/3600),m=Math.floor((seconds%3600)/60),s=Math.floor(seconds%60);return h>0?(h+' h '+m+' min'):(m>0?(m+' min '+s+' s'):(s+' s'));}}
    function render(t){{if(previous===null)previous=t;const dt=Math.min((t-previous)/1000,0.1);previous=t;if(playing)elapsed+=dt*playbackRate;if(elapsed>=maxTime){{elapsed=maxTime;playing=false;toggle.textContent='▶ Recommencer';}}
    let lo=0,hi=frames.length-1;while(lo<hi){{const mid=(lo+hi+1)>>1;if(frames[mid].time_s<=elapsed)lo=mid;else hi=mid-1;}}const i=lo;const j=Math.min(i+1,frames.length-1);const t0=frames[i].time_s,t1=frames[j].time_s;const alpha=t1>t0?Math.max(0,Math.min(1,(elapsed-t0)/(t1-t0))):0;const current=frames[i].agents,next=frames[j].agents;
    for(let k=0;k<current.length;k++){{const a=current[k],b=next[k],m=markers.get(a.id);if(!m)continue;const released=alpha<0.5?a.released:b.released;if(!released){{m.setStyle({{opacity:0,fillOpacity:0}});continue;}}m.setStyle({{opacity:1,fillOpacity:0.95}});m.setLatLng([a.lat+(b.lat-a.lat)*alpha,a.lon+(b.lon-a.lon)*alpha]);const arrived=alpha<0.5?a.arrived:b.arrived,stopped=alpha<0.5?a.stopped:b.stopped;const color=arrived?'#16a34a':(stopped?'#0284c7':'#f97316');m.setStyle({{color:arrived?'#166534':color,fillColor:color}});m.setRadius(arrived?3:4);}}
    clock.textContent='Temps : '+formatTime(elapsed)+' / '+formatTime(maxTime);requestAnimationFrame(render);}}
    toggle.addEventListener('click',()=>{{if(elapsed>=maxTime){{elapsed=0;playing=true;toggle.textContent='⏸ Pause';return;}}playing=!playing;toggle.textContent=playing?'⏸ Pause':'▶ Reprendre';}});
    document.getElementById('restart').addEventListener('click',()=>{{elapsed=0;playing=true;toggle.textContent='⏸ Pause';}});requestAnimationFrame(render);
    </script></body></html>'''
    with st.container(key="map_sim_stage"):
        with st.container(key="map_ov_sim"):
            map_style_button("map_style_btn_sim")
        render_html(map_html, 625)

    # Contrôle de lecture placé juste sous la carte animée.
    if "target_playback_seconds" not in ss:
        ss["target_playback_seconds"] = 120
    target_playback_seconds = st.slider(
        "Durée souhaitée pour voir toute la simulation (s)",
        min_value=30, max_value=1000, step=10,
        key="target_playback_seconds",
        help="La vitesse s'ajuste automatiquement pour lire toute la chronologie, jusqu'à la dernière image simulée."
    )
    actual_playback_s = simulated_duration_s / rate
    st.caption(
        f"Accélération automatique : ×{rate:,.0f}".replace(",", " ")
        + f" · durée complète estimée : {actual_playback_s:.0f} s"
    )


if selected_tab == "Résultats":
    # ---- carte de chaleur -----------------------------------------------------------------
    st.subheader("Densité dans l'espace et le temps")
    st.caption("Axe horizontal : distance depuis le départ ; axe vertical : temps simulé. La couleur est la densité maximale (coureurs/m²) dans chaque case, avec les mêmes seuils que le ruban. Les cases vides (personne sur le tronçon) restent blanches.")
    z, x_km, y_min = meso.heatmap()
    z_plot = np.where(z <= 1e-6, np.nan, z)
    zmax = max(0.8, float(np.nanmax(z_plot)) if np.isfinite(z_plot).any() else 0.8)
    a, b = 0.3 / zmax, min(0.5 / zmax, 1.0)
    fig = go.Figure(data=go.Heatmap(
        z=z_plot, x=x_km, y=y_min, zmin=0, zmax=zmax,
        colorscale=[[0.0, COLOR_FLUID], [a, COLOR_FLUID], [a, COLOR_CONSTRAINED], [b, COLOR_CONSTRAINED],
                    [b, COLOR_CONGESTED], [1.0, COLOR_CONGESTED]],
        colorbar={"title": "coureurs/m²"}, hoverongaps=False,
        hovertemplate="Temps : %{y:.1f} min<br>Distance : %{x:.2f} km<br>Densité max : %{z:.2f} p/m²<extra></extra>"))
    fig.update_layout(height=520, margin={"l": 15, "r": 15, "t": 25, "b": 35},
                      xaxis={"title": "Distance depuis le départ (km)", "range": [0, route_length_km]},
                      yaxis={"title": "Temps simulé depuis le 1er départ (min)"})
    plot_white(fig)

    st.subheader("Plan opérationnel")
    st.caption("Document PDF d'organisation de l'épreuve, généré à partir de votre scénario et de la simulation : fermetures de routes, personnel, points d'eau, "
               "scénarios de repli, médias, flux aux intersections et dispositif d'urgence.")

    with st.expander("Informations à renseigner pour le plan", expanded=False):
        st.caption("Ce que l'application ne peut pas deviner (organisation, arrêtés, médias, seuils météo). Les champs vides apparaissent « À compléter » dans le document.")
        st.markdown("**Organisation**")
        o1, o2 = st.columns(2)
        o1.text_input("Nom de l'épreuve", key="plan_event_name", placeholder="Marathon de …")
        o2.text_input("Organisateur", key="plan_organizer")
        o1.text_input("Responsable sécurité et parcours", key="plan_safety_lead")
        o2.text_input("Poste de commandement (téléphone)", key="plan_pc_phone")
        o1.text_input("Lieu de départ", key="plan_start_place")
        o2.text_input("Lieu d'arrivée", key="plan_finish_place")
        st.markdown("**Fermetures de routes**")
        f1, f2 = st.columns(2)
        f1.text_input("Arrêté de circulation (numéro et date)", key="plan_prefecture_ref")
        f2.selectbox("Mode de fermeture", [
            "Fermeture totale de la chaussée", "Fermeture partielle (voie réservée aux coureurs)",
            "Circulation alternée avec signaleurs", "Circulation maintenue, priorité aux carrefours"], key="plan_closure_mode")
        ss.setdefault("plan_closure_margin", 15)
        ss.setdefault("plan_reopen_delay", 10)
        ss.setdefault("plan_media_vehicles", 0)
        ss.setdefault("plan_media_moto", 0)
        f1.number_input("Marge avant le passage de la tête (min)", min_value=0, max_value=120, step=5, key="plan_closure_margin",
                        help="Le règlement demande des signaleurs en poste au moins un quart d'heure avant le passage.")
        f2.number_input("Délai de réouverture après le balai (min)", min_value=0, max_value=120, step=5, key="plan_reopen_delay")
        st.text_area("Précisions (déviations, barrières, parkings, riverains)", key="plan_closure_notes", height=80)
        st.markdown("**Scénarios de repli**")
        st.caption("Propositions de départ à adapter : les seuils de déclenchement se fixent avec le médecin de l'épreuve, la préfecture et la météo.")
        if "plan_contingency" not in ss:
            ss.plan_contingency = pd.DataFrame([
                {"Niveau": "Vert", "Déclencheur": "Conditions normales", "Mesures et modification de tracé": "Course maintenue sur le tracé nominal.", "Décision": "Directeur de course"},
                {"Niveau": "Orange", "Déclencheur": "Chaleur ou orage annoncés (seuil à fixer)", "Mesures et modification de tracé": "Renforcer ravitaillements et épongeage, informer les coureurs, pré-alerter les secours.", "Décision": "Directeur de course et médecin"},
                {"Niveau": "Rouge", "Déclencheur": "Danger imminent sur le parcours (seuil à fixer)", "Mesures et modification de tracé": "Neutraliser, raccourcir ou arrêter la course, mettre les coureurs à l'abri.", "Décision": "Préfecture et directeur de course"},
            ])
        cont_df = st.data_editor(ss.plan_contingency, num_rows="dynamic", hide_index=True, width="stretch")
        ss.plan_contingency = cont_df.reset_index(drop=True)
        st.text_area("Tracé de repli (tronçons de remplacement, kilomètres concernés)", key="plan_fallback_route", height=80)
        st.markdown("**Couverture médias**")
        m1, m2 = st.columns(2)
        m1.text_input("Responsable presse (nom, contact)", key="plan_media_contact")
        m2.text_input("Zone presse (lieu)", key="plan_media_zone")
        m1.number_input("Véhicules presse accrédités", min_value=0, max_value=50, step=1, key="plan_media_vehicles")
        m2.number_input("Motos caméra", min_value=0, max_value=20, step=1, key="plan_media_moto")
        m1.text_input("Points de captation (km, séparés par des virgules)", key="plan_media_points", placeholder="0, 21, 42")
        c1_, c2_, c3_ = m2.columns(3)
        c1_.checkbox("Drone", key="plan_media_drone")
        c2_.checkbox("Hélicoptère", key="plan_media_heli")
        c3_.checkbox("Direct", key="plan_media_live")
        st.markdown("**Secours**")
        s1, s2 = st.columns(2)
        s1.text_area("Accès des véhicules de secours (lieux, itinéraires)", key="plan_access_points", height=80)
        s2.text_input("Hôpital de référence", key="plan_hospital")

    plan_info = {
        "event_name": ss.get("plan_event_name", ""), "organizer": ss.get("plan_organizer", ""),
        "safety_lead": ss.get("plan_safety_lead", ""), "pc_phone": ss.get("plan_pc_phone", ""),
        "start_place": ss.get("plan_start_place", ""), "finish_place": ss.get("plan_finish_place", ""),
        "prefecture_ref": ss.get("plan_prefecture_ref", ""), "closure_mode": ss.get("plan_closure_mode", ""),
        "closure_margin_min": ss.get("plan_closure_margin", 15), "reopen_delay_min": ss.get("plan_reopen_delay", 10),
        "closure_notes": ss.get("plan_closure_notes", ""), "fallback_route": ss.get("plan_fallback_route", ""),
        "contingency": [{"level": r.get("Niveau", ""), "trigger": r.get("Déclencheur", ""),
                         "action": r.get("Mesures et modification de tracé", ""), "decision": r.get("Décision", "")}
                        for r in cont_df.fillna("").to_dict("records")],
        "media_contact": ss.get("plan_media_contact", ""), "media_zone": ss.get("plan_media_zone", ""),
        "media_vehicles": ss.get("plan_media_vehicles", 0), "media_moto": ss.get("plan_media_moto", 0),
        "media_points": ss.get("plan_media_points", ""), "media_drone": ss.get("plan_media_drone", False),
        "media_heli": ss.get("plan_media_heli", False), "media_live": ss.get("plan_media_live", False),
        "access_points": ss.get("plan_access_points", ""), "hospital": ss.get("plan_hospital", ""),
    }
    plan_file = "plan_operationnel_" + event_date.isoformat() + ".pdf"

    def _build_plan():
        return po.build_plan_pdf({"info": plan_info, "result": result, "route_local": route_local, "origin": origin})

    # Un seul bouton : le PDF est généré au clic puis téléchargé (génération différée, Streamlit récent).
    if "callable" in (st.download_button.__doc__ or "").lower():
        st.download_button("↓ Générer et télécharger le Plan Opérationnel (PDF)", _build_plan, file_name=plan_file,
                           mime="application/pdf", type="primary", key="btn_plan_download")
    else:
        # Streamlit plus ancien : génération puis téléchargement en deux étapes.
        if st.button("Générer le Plan Opérationnel (PDF)", type="primary", key="btn_plan_generate"):
            try:
                ss.plan_pdf = _build_plan()
            except Exception as exc:
                st.error(f"Impossible de générer le plan opérationnel : {exc}")
        if ss.get("plan_pdf"):
            st.download_button("↓ Télécharger le Plan Opérationnel (PDF)", ss.plan_pdf, plan_file, "application/pdf", key="btn_plan_download")
    st.caption("Le document reflète le scénario et les informations saisies au moment du clic.")

    with st.expander("Limites du prototype"):
        st.markdown(textwrap.dedent("""
    - Les densités sont calculées sur **tous** les partants par un modèle 1D (abscisse le long du parcours) ; le couloir est modélisé par sa largeur, sans obstacles 2D. Les dépassements ne sont pas limités par la largeur.
    - La relation vitesse-densité est inspirée de Weidmann mais n'en est pas une reproduction calibrée ; elle est à calibrer sur des observations réelles.
    - Les points d'eau sont des files d'attente à postes de service simultanés ; les coureurs en attente sont répartis en amont du poste (≈ 2 pers./m²). Le comportement réel (prise en courant, tables longues) peut être très différent.
    - Points d'eau et rétrécissements agissent sur l'écoulement des coureurs. Les toilettes (zone de départ) et les postes de secours ont leurs propres modèles, sans effet en retour sur la foule : les incidents ne bloquent pas le parcours et les équipes ne sont pas gênées par les coureurs. Les bénévoles, médecins, ambulances et postes d'épongeage sont comptés dans le bilan et contrôlés par le moteur de règles, sans effet sur la foule.
    - Toilettes : seuls les sites à moins de 500 m du départ sont simulés ; les arrivées des usagers et les durées d'usage sont des hypothèses, et personne ne renonce ni ne change de site en cours d'attente.
    - Secours : le nombre d'incidents suit un taux par 1 000 partants ; durée de prise en charge, vitesse des équipes et part d'incidents à l'arrivée sont des hypothèses à calibrer ; le trajet se fait seulement le long du parcours.
    - La zone d'arrivée (file après la ligne) n'est pas modélisée. Les vagues sont seedées par allure avec un léger mélange.
    - Les seuils de densité (0,3 / 0,5 coureur/m²), le délai d'intervention visé et l'attente tolérée aux toilettes sont ceux de l'application : le règlement FFA ne fixe rien de tel pour la route (voir « Ce que ce règlement ne couvre pas »).
    - Le tracé est converti dans un repère local approximatif (projection équirectangulaire). Pour une étude opérationnelle : projection métrique adaptée, calibration sur mesures et validation indépendante.
    """))