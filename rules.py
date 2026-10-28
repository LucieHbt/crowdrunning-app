"""Moteur de règles : confronte un scénario à des règles sourcées (`rules.yaml`).

Entrées
-------
- `Context`  : nature de l'épreuve (route / trail), effectif, longueur, date, voie publique, etc. ;
- les objets posés sur la carte (`scenario.py`) ;
- les résultats de simulation (facultatifs : seule la règle de délai d'intervention en a besoin) ;
- les confirmations manuelles de l'organisateur pour ce que l'application ne peut pas vérifier.

Sortie : une liste de `Finding`, chacun avec son statut :
- `alert` : écart avec la règle ;
- `check` : à vérifier ou à confirmer (non calculable, ou règle qui ne chiffre pas le nombre exigé) ;
- `ok`    : règle respectée d'après le scénario ;
- `info`  : information ou échéance (rien à signaler pour l'instant).

Le moteur ne contient aucun seuil réglementaire : tout vient de `rules.yaml`.
Il aide à préparer le dossier ; il ne remplace pas l'avis de la FFA, de la préfecture ou des services de secours.
"""
from __future__ import annotations

import calendar
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

import yaml

RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.yaml")
STATUSES = ("alert", "check", "ok", "info")
FORCES = ("obligation", "recommandation", "information")
EVENT_TYPES = ("route", "trail")
MAX_PAGE = 55
SOON_DAYS = 21          # une échéance à moins de 21 jours passe en « à vérifier »


# ------------------------------------------------------------------------------------
# Contexte et résultat
# ------------------------------------------------------------------------------------
@dataclass
class Context:
    runners: int
    length_m: float
    event_date: date
    today: date
    event_type: str = "route"          # route | trail
    d_plus_m: float = 0.0
    competition: bool = True           # chronométrage ou classement
    public_road: bool = True           # voie publique ou ouverte à la circulation
    multi_department: bool = False
    ffa_affiliated: bool = True        # organisateur affilié à la FFA

    @property
    def length_km(self):
        return self.length_m / 1000.0

    @property
    def km_effort(self):
        """Art. III.A.3 : un km de plus par 100 m de dénivelé positif."""
        return self.length_km + max(0.0, self.d_plus_m) / 100.0


@dataclass
class Finding:
    id: str
    theme: str
    title: str
    status: str
    force: str
    detail: str
    article: str
    page: int
    text: str = ""
    quote: str = ""
    recommendation: str = ""
    interpretation: str = ""
    note: str = ""
    derived: bool = False
    manual: bool = False
    confirm: str = ""
    confirmed: bool = False
    due: str = ""                      # échéance au format AAAA-MM-JJ (règles de type « deadline » seulement)


@dataclass
class Scene:
    objects: list
    length_m: float
    result: dict | None = None
    manual: dict = field(default_factory=dict)


# ------------------------------------------------------------------------------------
# Conditions d'application
# ------------------------------------------------------------------------------------
_CONDITIONS = {
    "event_type": lambda c, v: c.event_type in (v if isinstance(v, list) else [v]),
    "runners_gt": lambda c, v: c.runners > v,
    "runners_gte": lambda c, v: c.runners >= v,
    "runners_lt": lambda c, v: c.runners < v,
    "runners_lte": lambda c, v: c.runners <= v,
    "length_km_gt": lambda c, v: c.length_km > v,
    "length_km_gte": lambda c, v: c.length_km >= v,
    "length_km_lt": lambda c, v: c.length_km < v,
    "length_km_lte": lambda c, v: c.length_km <= v,
    "km_effort_gt": lambda c, v: c.km_effort > v,
    "competition": lambda c, v: c.competition == bool(v),
    "public_road": lambda c, v: c.public_road == bool(v),
    "multi_department": lambda c, v: c.multi_department == bool(v),
    "ffa_affiliated": lambda c, v: c.ffa_affiliated == bool(v),
}


def _all_hold(when, ctx):
    return all(_CONDITIONS[k](ctx, v) for k, v in (when or {}).items())


def applies(rule, ctx):
    if not _all_hold(rule.get("when"), ctx):
        return False
    any_of = rule.get("when_any")
    return True if not any_of else any(_all_hold(w, ctx) for w in any_of)


# ------------------------------------------------------------------------------------
# Utilitaires de format
# ------------------------------------------------------------------------------------
def _num(x):
    x = float(x)
    return str(int(round(x))) if abs(x - round(x)) < 1e-9 else f"{x:.1f}".replace(".", ",")


def _km(m):
    return f"{m / 1000.0:.1f}".replace(".", ",") + " km"


def _kmn(km):
    return f"{km:.1f}".replace(".", ",")


def _date(d):
    return d.strftime("%d/%m/%Y")


def _months_before(d, n):
    year, month = d.year, d.month - int(n)
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _frange(start, stop, step):
    out, x = [], start
    while x < stop - 1e-9:
        out.append(round(x, 3))
        x += step
    return out


def _km_list(values):
    return ", ".join(_kmn(v) for v in values)


# ------------------------------------------------------------------------------------
# Contrôles : chacun retourne (statut, détail, recommandation)
# ------------------------------------------------------------------------------------
def _in_zone(o, zone, radius, length_m):
    return o["progress_m"] <= radius if zone == "start" else o["progress_m"] >= length_m - radius


def check_count(rule, p, ctx, scene):
    kinds = p["kind"] if isinstance(p["kind"], list) else [p["kind"]]
    objs = [o for o in scene.objects if o["kind"] in kinds]
    zone = p.get("within")
    where = ""
    if zone:
        radius = float(zone.get("radius_m", 300))
        objs = [o for o in objs if _in_zone(o, zone["zone"], radius, scene.length_m)]
        where = f" à moins de {_num(radius)} m {'du départ' if zone['zone'] == 'start' else 'de l’arrivée'}"
    flt = p.get("filter") or {}
    if "p2_min" in flt:
        objs = [o for o in objs if o["p2"] >= flt["p2_min"]]
    measure = p.get("measure", "objects")
    n = len(objs) if measure == "objects" else sum(o[measure] for o in objs)
    required = float(p.get("min", 1))
    if p.get("tiers"):
        required = max([float(t["min"]) for t in p["tiers"] if ctx.runners > t["runners_gt"]] or [0.0])
    noun = p.get("noun", "objet(s)")
    if n >= required:
        if p.get("adapted") and required > 0:
            return ("check",
                    f"{_num(n)} {noun} posé(s){where}. Le règlement exige un nombre « adapté » sans le chiffrer : "
                    "à dimensionner avec votre responsable des secours ou la Préfecture.", "")
        return "ok", f"{_num(n)} {noun} posé(s){where} (minimum : {_num(required)}).", ""
    return ("alert", f"{_num(n)} {noun} posé(s){where} ; minimum requis : {_num(required)}.", rule.get("fix", ""))


def suggested_water_km(length_m, p):
    """Positions de référence des points d'eau (km) : premier vers `first_km`, puis tous les `max_gap_km`."""
    first_km, gap_km = float(p.get("first_km", 5)), float(p.get("max_gap_km", 5))
    return _frange(first_km, length_m / 1000.0 - 1.0, gap_km)


def check_water_spacing(rule, p, ctx, scene):
    L = scene.length_m
    pos = sorted(o["progress_m"] for o in scene.objects if o["kind"] == "water" and 0 < o["progress_m"] < L)
    first_km, ftol = float(p.get("first_km", 5)), float(p.get("first_tol_km", 1))
    gap_km, gtol = float(p.get("max_gap_km", 5)), float(p.get("gap_tol_km", 1))
    suggested = suggested_water_km(L, p)
    if not pos:
        return ("alert", "Aucun point d'eau posé sur le parcours.",
                f"Poser des points d'eau vers les km {_km_list(suggested)}." if suggested else rule.get("fix", ""))
    problems = []
    if not (first_km - ftol <= pos[0] / 1000.0 <= first_km + ftol):
        problems.append(f"premier point d'eau au km {_kmn(pos[0] / 1000)} (attendu vers le km {_num(first_km)}, ± {_num(ftol)})")
    pts = pos + [L]          # le dernier tronçon se termine au ravitaillement d'arrivée
    gaps = [(b - a, a, b) for a, b in zip(pts[:-1], pts[1:])]
    worst, a, b = max(gaps)
    if worst / 1000.0 > gap_km + gtol:
        problems.append(f"intervalle de {_km(worst)} entre le km {_kmn(a / 1000)} et le km {_kmn(b / 1000)} "
                        f"(attendu environ {_num(gap_km)} km, ± {_num(gtol)})")
    if problems:
        return ("alert", "Écart : " + " ; ".join(problems) + ".",
                f"Repères conformes : points d'eau vers les km {_km_list(suggested)}.")
    return ("ok", f"{len(pos)} points d'eau ; premier au km {_kmn(pos[0] / 1000)}, plus grand intervalle {_km(worst)}.", "")


def check_sponge_midpoints(rule, p, ctx, scene):
    water = sorted(o["progress_m"] for o in scene.objects if o["kind"] == "water")
    sponge = [o["progress_m"] for o in scene.objects if o["kind"] == "sponge"]
    frac = float(p.get("tol_fraction", 0.25))
    if len(water) < 2:
        return ("check", "Il faut au moins deux points d'eau pour situer les postes d'épongeage à mi-chemin.",
                "Poser d'abord les points d'eau.")
    gaps = list(zip(water[:-1], water[1:]))
    missing = [(a, b) for a, b in gaps
               if not any(abs(s - (a + b) / 2.0) <= frac * (b - a) for s in sponge)]
    if missing:
        mids = [(a + b) / 2000.0 for a, b in missing]
        return ("alert", f"{len(missing)} intervalle(s) sur {len(gaps)} sans poste d'épongeage ou de rafraîchissement "
                         f"près du milieu (km {_km_list(mids)}).", rule.get("fix", ""))
    return "ok", f"Un poste d'épongeage ou de rafraîchissement est posé à mi-chemin dans chacun des {len(gaps)} intervalles.", ""


def check_response_time(rule, p, ctx, scene):
    limit = float(p["limit_min"]) * 60.0
    if not any(o["kind"] == "medical" for o in scene.objects):
        return "alert", "Aucun poste de secours posé : le délai ne peut pas être respecté.", rule.get("fix", "")
    res = scene.result
    med = res.get("med") if res else None
    if med is None:
        return "check", "Lancez la simulation pour calculer les délais d'intervention depuis vos postes.", ""
    cov, km = med.coverage_s, med.coverage_km
    bad = cov > limit
    extra = (f" Sur {med.n_reps} tirages, 95 % des interventions durent moins de {med.response_p95_s / 60:.0f} min "
             f"(maximum {med.response_max_s / 60:.0f} min, files d'attente d'équipes comprises).")
    if not bad.any():
        return "ok", f"Délai maximal sans attente d'équipe : {cov.max() / 60:.0f} min (limite : {_num(limit / 60)} min)." + extra, ""
    ranges, start = [], None
    for i, flag in enumerate(bad):
        if flag and start is None:
            start = i
        if (not flag or i == len(bad) - 1) and start is not None:
            end = i if flag else i - 1
            ranges.append((km[start], km[end]))
            start = None
    shown = ", ".join(f"km {_kmn(a)}–{_kmn(b)}" for a, b in ranges[:4]) + (" …" if len(ranges) > 4 else "")
    return ("alert", f"Délai maximal sans attente d'équipe : {cov.max() / 60:.0f} min (limite : {_num(limit / 60)} min) ; "
                     f"dépassé sur {_km(bad.sum() * (km[1] - km[0]) * 1000 if len(km) > 1 else 0)} ({shown})." + extra,
            rule.get("fix", ""))


def check_manual(rule, p, ctx, scene):
    if scene.manual.get(rule["id"], False):
        return "ok", "Confirmé par l'organisateur (non vérifiable par l'application).", ""
    return "check", "À confirmer par l'organisateur : l'application ne peut pas le vérifier.", ""


def deadline_date(p, ctx):
    """Date limite d'une règle de type « deadline » pour la date d'épreuve du contexte."""
    value = p["value"]
    if ctx.multi_department and "value_if_multi_department" in p:
        value = p["value_if_multi_department"]
    kind = p["kind"]
    if kind == "months":
        return _months_before(ctx.event_date, value)
    if kind == "days":
        return ctx.event_date - timedelta(days=int(value))
    if kind == "jours_francs":
        return ctx.event_date - timedelta(days=int(value) + 1)
    raise ValueError(f"deadline : kind inconnu « {kind} »")


def check_deadline(rule, p, ctx, scene):
    deadline = deadline_date(p, ctx)
    left = (deadline - ctx.today).days
    label = p.get("label", "Échéance")
    if left < 0:
        return ("alert", f"{label} : au plus tard le {_date(deadline)} — date dépassée de {-left} jour(s).", rule.get("fix", ""))
    if left <= SOON_DAYS:
        return "check", f"{label} : au plus tard le {_date(deadline)} (dans {left} jour(s)).", ""
    return "info", f"{label} : au plus tard le {_date(deadline)} ({left} jours restants).", ""


def _info_age_categories(rule, p, ctx):
    value = ctx.km_effort if ctx.event_type == "trail" else ctx.length_km
    basis = "Km-effort du parcours" if ctx.event_type == "trail" else "Longueur du parcours"
    allowed = [c["category"] for c in p["limits"] if value <= c["max_km"]]
    excluded = [f"{c['category']} (max {_num(c['max_km'])} km)" for c in p["limits"] if value > c["max_km"]]
    out = f"{basis} : {_kmn(value)} km. Ouvert aux {p.get('always_open', 'adultes')}"
    out += (", " + ", ".join(allowed)) if allowed else ""
    out += "."
    if excluded:
        out += " Non ouvert aux : " + ", ".join(excluded) + "."
    return "info", out, ""


def _info_official_distance(rule, p, ctx):
    L = ctx.length_km
    best = min(p["distances_km"], key=lambda d: abs(d["km"] - L) / d["km"])
    dev = (L - best["km"]) / best["km"]
    if abs(dev) > float(p.get("window", 0.05)):
        return ("info", f"Le tracé ({_kmn(L)} km) ne correspond à aucune distance officielle : le mesurage officiel "
                        "n'est exigé que pour annoncer une distance officielle.", "")
    tol = float(p.get("tolerance", 0.001))
    delta_m = (L - best["km"]) * 1000.0
    if abs(dev) <= tol:
        return "info", (f"Le tracé GPX ({_km(L * 1000)}) est à {abs(delta_m):.0f} m du {best['name']} : dans la tolérance "
                        "de mesurage de 0,1 %. Le GPX ne remplace pas le certificat de mesurage."), ""
    sens = "plus long" if delta_m > 0 else "plus court"
    return ("check", f"Le tracé GPX est {sens} de {abs(delta_m):.0f} m que le {best['name']} ({dev * 100:+.2f} %). "
                     "Une distance officielle exige un mesurage par un officiel, jamais inférieur à la distance annoncée, "
                     "avec une imprécision de 0,1 % au plus.", "Faire mesurer le parcours (bicyclette étalonnée) avant d'annoncer la distance.")


def _info_first_aid_area(rule, p, ctx):
    area = float(p.get("m2_per_1000", 15)) * ctx.runners / 1000.0
    return "info", f"Surface recommandée du poste de secours principal : environ {area:.0f} m² pour {ctx.runners} sportifs.", ""


_INFO = {"age_categories": _info_age_categories, "official_distance": _info_official_distance,
         "first_aid_area": _info_first_aid_area}


def check_info(rule, p, ctx, scene):
    compute = p.get("compute")
    if compute:
        return _INFO[compute](rule, p, ctx)
    return "info", "", ""


CHECKS = {
    "count": check_count, "water_spacing": check_water_spacing, "sponge_midpoints": check_sponge_midpoints,
    "response_time": check_response_time, "manual": check_manual, "deadline": check_deadline, "info": check_info,
}


# ------------------------------------------------------------------------------------
# Chargement, validation, évaluation
# ------------------------------------------------------------------------------------
def validate_rules(data):
    """Retourne la liste des défauts du fichier de règles (vide si tout est correct)."""
    errors = []
    meta = data.get("meta") or {}
    for key in ("document", "rules_version", "jurisdiction", "themes", "page_numbering"):
        if not meta.get(key):
            errors.append(f"meta.{key} manquant")
    seen = set()
    for i, r in enumerate(data.get("rules") or []):
        rid = r.get("id", f"#{i}")
        if rid in seen:
            errors.append(f"{rid} : identifiant en double")
        seen.add(rid)
        for key in ("id", "theme", "title", "article", "page", "force", "text"):
            if r.get(key) in (None, ""):
                errors.append(f"{rid} : champ « {key} » manquant")
        if r.get("theme") not in meta.get("themes", []):
            errors.append(f"{rid} : thème inconnu « {r.get('theme')} »")
        if r.get("force") not in FORCES:
            errors.append(f"{rid} : force inconnue « {r.get('force')} »")
        page = r.get("page")
        if not isinstance(page, int) or not 1 <= page <= MAX_PAGE:
            errors.append(f"{rid} : page invalide « {page} »")
        check = r.get("check") or {"type": "info"}
        if check.get("type") not in CHECKS:
            errors.append(f"{rid} : type de contrôle inconnu « {check.get('type')} »")
        if check.get("type") == "manual" and not check.get("confirm"):
            errors.append(f"{rid} : un contrôle manuel doit avoir un texte « confirm »")
        if check.get("compute") and check["compute"] not in _INFO:
            errors.append(f"{rid} : calcul inconnu « {check['compute']} »")
        for w in [r.get("when") or {}] + list(r.get("when_any") or []):
            for k in w:
                if k not in _CONDITIONS:
                    errors.append(f"{rid} : condition inconnue « {k} »")
            if "event_type" in w:
                vals = w["event_type"] if isinstance(w["event_type"], list) else [w["event_type"]]
                if any(v not in EVENT_TYPES for v in vals):
                    errors.append(f"{rid} : type d'épreuve inconnu dans « when »")
    return errors


def load_rules(path=RULES_PATH):
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    errors = validate_rules(data)
    if errors:
        raise ValueError("rules.yaml invalide :\n- " + "\n- ".join(errors))
    return data


def evaluate(data, ctx, objects, result=None, manual=None):
    """Évalue toutes les règles applicables. `result` : dict de résultats de simulation à jour (ou None)."""
    scene = Scene(objects=list(objects), length_m=float(ctx.length_m), result=result, manual=dict(manual or {}))
    findings = []
    for r in data["rules"]:
        if not applies(r, ctx):
            continue
        check = r.get("check") or {"type": "info"}
        try:
            status, detail, reco = CHECKS[check["type"]](r, check, ctx, scene)
        except Exception as exc:       # une règle défaillante ne doit pas empêcher d'afficher les autres
            status, detail, reco = "check", f"Règle non évaluable ({type(exc).__name__} : {exc}).", ""
        is_manual = check["type"] == "manual"
        due = ""
        if check["type"] == "deadline":
            try:
                due = deadline_date(check, ctx).isoformat()
            except Exception:
                due = ""
        findings.append(Finding(
            id=r["id"], theme=r["theme"], title=r["title"], status=status, force=r["force"], detail=detail,
            article=r["article"], page=int(r["page"]), text=r.get("text", ""), quote=r.get("quote", ""),
            recommendation=reco, interpretation=r.get("interpretation", ""), note=r.get("note", ""),
            derived=bool(r.get("derived", False)), manual=is_manual, confirm=check.get("confirm", ""),
            confirmed=bool(scene.manual.get(r["id"], False)) if is_manual else False, due=due))
    return findings


def summarize(findings):
    out = {s: 0 for s in STATUSES}
    for f in findings:
        out[f.status] += 1
    return out


def findings_to_dicts(findings):
    return [asdict(f) for f in findings]