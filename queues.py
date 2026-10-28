"""Files d'attente des toilettes avant le départ et dispositif de secours.

Les deux modèles s'appuient sur la simulation mésoscopique (`meso.MesoResult`) et sur les objets
posés sur la carte (`scenario.py`). Ce sont des modèles d'ordre de grandeur : les durées de
service, les vitesses d'intervention et la répartition des incidents sont des hypothèses à
calibrer, et aucune norme réglementaire n'est appliquée ici.

Toilettes
---------
- Une part des partants utilise les WC avant le départ ; chacun arrive dans la zone de départ
  entre `window_min` minutes et quelques minutes avant le franchissement de la ligne par sa vague.
- Chaque site est une file FIFO à plusieurs cabines ; un arrivant rejoint le site où l'attente
  prévue est la plus courte.
- Seuls les sites situés à moins de `START_ZONE_M` du départ sont modélisés. Les toilettes
  placées plus loin sur le parcours sont comptées dans le bilan mais pas simulées ici.

Secours
-------
- Le nombre d'incidents suit une loi de Poisson (taux pour 1 000 partants × effectif).
- Leur position et leur instant sont tirés dans l'occupation réelle du parcours (simulée), avec
  un poids croissant avec la distance parcourue ; une part configurable survient à l'arrivée.
- Une équipe se déplace le long du tracé à vitesse constante après un délai de mobilisation ; si
  toutes les équipes sont occupées, l'incident attend. Les équipes ne sont pas retardées par la
  foule, et le trajet se fait uniquement le long du parcours (plus l'écart du poste au tracé).
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

import numpy as np

START_ZONE_M = 500.0       # rayon (le long du tracé) de la zone de départ pour les toilettes
LATE_MARGIN_S = 120.0      # temps nécessaire pour rejoindre le sas après les toilettes
FINISH_ZONE_M = 300.0      # longueur de la zone d'arrivée pour les incidents « à l'arrivée »
WC_TARGET_SHARE = 0.95     # part d'usagers qui doivent attendre moins que la tolérance


# =====================================================================================
# Toilettes
# =====================================================================================
@dataclass
class WcResult:
    n_users: int
    sites: list
    excluded_sites: list
    cabins_total: int
    tolerance_s: float
    window_min: float
    wait_mean_s: float = 0.0
    wait_p95_s: float = 0.0
    wait_max_s: float = 0.0
    share_over_tolerance: float = 0.0
    share_late_for_start: float = 0.0
    peak_queue: int = 0
    peak_queue_time_min: float = 0.0
    timeline_min: np.ndarray = None
    timeline_queue: np.ndarray = None
    per_site: list = field(default_factory=list)
    cabins_needed: int | None = None
    service_mean_s: float = 0.0

    def summary(self):
        return {
            "users": self.n_users, "cabins_modelled": self.cabins_total,
            "wait_mean_s": round(self.wait_mean_s, 1), "wait_p95_s": round(self.wait_p95_s, 1),
            "wait_max_s": round(self.wait_max_s, 1),
            "share_over_tolerance": round(self.share_over_tolerance, 4),
            "share_late_for_start": round(self.share_late_for_start, 4),
            "peak_queue": self.peak_queue, "peak_queue_time_min_from_first_gun": round(self.peak_queue_time_min, 1),
            "cabins_needed_estimate": self.cabins_needed, "tolerance_s": self.tolerance_s,
            "arrival_window_min": self.window_min, "sites": self.per_site,
            "excluded_sites_beyond_start_zone": self.excluded_sites,
        }


def toilet_sites(objects):
    """Sépare les toilettes de la zone de départ (modélisées) des autres."""
    modelled, excluded = [], []
    for o in objects:
        if o["kind"] != "toilets":
            continue
        site = {"name": o["name"], "cabins": max(1, int(round(o["p1"]))),
                "service_s": max(1.0, float(o["p2"])), "progress_m": float(o["progress_m"])}
        (modelled if o["progress_m"] <= START_ZONE_M else excluded).append(site)
    return modelled, excluded


def _fifo_sites(arrivals, cabins, service_mean, rng):
    """Files FIFO multi-cabines avec choix du site à l'attente prévue minimale.

    `arrivals` doit être trié. Retourne (attente, début de service, fin de service, site).
    """
    n = len(arrivals)
    heaps = [[-math.inf] * int(c) for c in cabins]
    weights = np.asarray(cabins, dtype=float)
    draws = rng.lognormal(-0.08, 0.4, n)          # durées de service de moyenne ~1
    tie = rng.random(n)
    waits, starts, ends = np.empty(n), np.empty(n), np.empty(n)
    site_of = np.empty(n, dtype=np.int32)
    for i in range(n):
        a = arrivals[i]
        best_wait, best = math.inf, []
        for j, h in enumerate(heaps):
            w = h[0] - a
            w = w if w > 0.0 else 0.0
            if w < best_wait - 1e-9:
                best_wait, best = w, [j]
            elif abs(w - best_wait) <= 1e-9:
                best.append(j)
        if len(best) == 1:
            j = best[0]
        else:  # égalité : tirage pondéré par le nombre de cabines
            cw = np.cumsum(weights[best])
            j = best[int(np.searchsorted(cw, tie[i] * cw[-1], side="right"))]
        h = heaps[j]
        free = heapq.heappop(h)
        start = a if a > free else free
        end = start + service_mean[j] * draws[i]
        heapq.heappush(h, end)
        waits[i], starts[i], ends[i], site_of[i] = start - a, start, end, j
    return waits, starts, ends, site_of


def simulate_toilets(objects, release_s, *, use_share=0.6, tolerance_min=10.0, window_min=90.0,
                     seed=42, estimate_needed=True):
    """Simule l'attente aux toilettes de la zone de départ. Retourne un `WcResult` ou None."""
    sites, excluded = toilet_sites(objects)
    if not sites:
        return None
    rng = np.random.default_rng(seed + 101)
    n_total = len(release_s)
    n_users = int(round(n_total * float(np.clip(use_share, 0.0, 1.0))))
    users = rng.choice(n_total, size=n_users, replace=False) if n_users else np.array([], dtype=int)
    window_s = max(60.0, float(window_min) * 60.0)
    # Arrivée : entre `window_s` et ~5 min avant la ligne, plutôt dans la première moitié de la fenêtre.
    lead = 300.0 + (window_s - 300.0) * rng.beta(2.0, 2.5, n_users)
    arrivals_raw = release_s[users] - lead
    order = np.argsort(arrivals_raw)
    arrivals = arrivals_raw[order]
    line_time = release_s[users][order]

    cabins = [s["cabins"] for s in sites]
    service = [s["service_s"] for s in sites]
    cabins_total = int(sum(cabins))
    tol_s = float(tolerance_min) * 60.0
    res = WcResult(n_users=n_users, sites=[s["name"] for s in sites],
                   excluded_sites=[s["name"] for s in excluded], cabins_total=cabins_total,
                   tolerance_s=tol_s, window_min=float(window_min),
                   service_mean_s=float(np.average(service, weights=cabins)))
    if n_users == 0:
        res.timeline_min, res.timeline_queue = np.array([0.0]), np.array([0])
        return res

    waits, starts, ends, site_of = _fifo_sites(arrivals, cabins, service, np.random.default_rng(seed + 102))
    res.wait_mean_s = float(waits.mean())
    res.wait_p95_s = float(np.percentile(waits, 95))
    res.wait_max_s = float(waits.max())
    res.share_over_tolerance = float(np.mean(waits > tol_s))
    res.share_late_for_start = float(np.mean(ends > line_time - LATE_MARGIN_S))

    t_min = math.floor(arrivals[0] / 60.0)
    t_max = math.ceil(ends.max() / 60.0)
    grid = np.arange(t_min, t_max + 1) * 60.0
    queue = np.searchsorted(np.sort(arrivals), grid, side="right") - np.searchsorted(np.sort(starts), grid, side="right")
    res.timeline_min, res.timeline_queue = grid / 60.0, queue.astype(int)
    res.peak_queue = int(queue.max())
    res.peak_queue_time_min = float(res.timeline_min[int(np.argmax(queue))])

    for j, s in enumerate(sites):
        sel = site_of == j
        res.per_site.append({
            "name": s["name"], "cabins": s["cabins"], "service_s": s["service_s"],
            "users": int(sel.sum()),
            "wait_mean_s": round(float(waits[sel].mean()), 1) if sel.any() else 0.0,
            "wait_max_s": round(float(waits[sel].max()), 1) if sel.any() else 0.0,
        })

    if estimate_needed:
        res.cabins_needed = _cabins_needed(arrivals, res.service_mean_s, tol_s, seed)
    return res


def _cabins_needed(arrivals, service_mean_s, tol_s, seed, cap=3000):
    """Plus petit nombre de cabines (en un seul bloc) tel que ≥ 95 % des usagers attendent moins que la tolérance."""
    def ok(c):
        w, *_ = _fifo_sites(arrivals, [c], [service_mean_s], np.random.default_rng(seed + 103))
        return float(np.mean(w <= tol_s)) >= WC_TARGET_SHARE
    hi = 1
    while not ok(hi):
        hi *= 2
        if hi > cap:
            return None
    lo = hi // 2 + 1 if hi > 1 else 1
    while lo < hi:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid + 1
    return int(hi)


# =====================================================================================
# Secours
# =====================================================================================
@dataclass
class MedResult:
    posts: list
    target_s: float
    n_reps: int
    mean_incidents: float
    response_p50_s: float = 0.0
    response_p95_s: float = 0.0
    response_max_s: float = 0.0
    share_within_target: float = 0.0
    prob_any_over_target: float = 0.0
    share_waiting_for_team: float = 0.0
    team_busy_share: float = 0.0
    coverage_km: np.ndarray = None        # centres de cellules (km)
    coverage_s: np.ndarray = None         # temps d'intervention sans attente, par cellule
    uncovered_m: float = 0.0
    example_incidents: list = field(default_factory=list)  # [(km, instant_min, réponse_s)]

    def summary(self):
        return {
            "posts": self.posts, "target_s": self.target_s, "monte_carlo_runs": self.n_reps,
            "mean_incidents": round(self.mean_incidents, 2),
            "response_p50_s": round(self.response_p50_s, 1), "response_p95_s": round(self.response_p95_s, 1),
            "response_max_s": round(self.response_max_s, 1),
            "share_within_target": round(self.share_within_target, 4),
            "prob_at_least_one_over_target": round(self.prob_any_over_target, 4),
            "share_incidents_waiting_for_team": round(self.share_waiting_for_team, 4),
            "route_length_beyond_target_m": round(self.uncovered_m, 1),
        }


def medical_posts(objects):
    return [{"name": o["name"], "pos_m": float(o["progress_m"]), "offset_m": float(o["offset_m"]),
             "teams": max(1, int(round(o["p1"]))), "mobilisation_s": max(0.0, float(o["p2"]))}
            for o in objects if o["kind"] == "medical"]


def simulate_medical(meso, objects, *, incidents_per_1000=4.0, target_min=5.0, team_speed_m_s=4.0,
                     treatment_min=20.0, finish_share=0.3, n_reps=200, seed=42):
    """Temps d'intervention des secours (Monte Carlo). Retourne un `MedResult` ou None sans poste."""
    posts = medical_posts(objects)
    if not posts:
        return None
    rng = np.random.default_rng(seed + 202)
    L, cell = meso.route_length_m, meso.cell_m
    speed = max(0.5, float(team_speed_m_s))
    target_s = float(target_min) * 60.0
    treat_s = max(1.0, float(treatment_min) * 60.0)
    pos_p = np.array([p["pos_m"] for p in posts])
    off_p = np.array([p["offset_m"] for p in posts])
    mob_p = np.array([p["mobilisation_s"] for p in posts])
    teams_p = [p["teams"] for p in posts]

    # Profil de couverture sans attente : meilleur poste pour un incident dans chaque cellule.
    centres = (np.arange(meso.n_cells) + 0.5) * cell
    reach = mob_p[None, :] + (np.abs(centres[:, None] - pos_p[None, :]) + off_p[None, :]) / speed
    coverage = reach.min(axis=1)
    uncovered_m = float(np.sum(coverage > target_s) * cell)

    # Loi conjointe (instant, cellule) ∝ nombre de coureurs sur la cellule × poids de fatigue.
    counts = meso.rho_t.astype(float) * cell * meso.width_cells[None, :]
    weight = 1.0 + centres / max(L, 1.0)
    prob = (counts * weight[None, :]).ravel()
    total = prob.sum()
    if total <= 0:
        return None
    prob /= total
    finishers = meso.finish_s[~np.isnan(meso.finish_s)]
    lam = float(incidents_per_1000) * meso.n_runners / 1000.0
    dt_rec = float(meso.record_s)

    all_resp, n_wait, n_inc, busy_total, over_any = [], 0, 0, 0.0, 0
    example = []
    for rep in range(int(n_reps)):
        k = int(rng.poisson(lam))
        if k == 0:
            continue
        flat = rng.choice(prob.size, size=k, p=prob)
        r_idx, c_idx = np.divmod(flat, meso.n_cells)
        t_inc = meso.times_s[r_idx] + rng.uniform(0.0, dt_rec, k)
        x_inc = (c_idx + rng.random(k)) * cell
        at_finish = rng.random(k) < float(finish_share)
        if at_finish.any() and finishers.size:
            m = int(at_finish.sum())
            t_inc[at_finish] = rng.choice(finishers, size=m) + rng.uniform(0.0, 600.0, m)
            x_inc[at_finish] = L - rng.uniform(0.0, min(FINISH_ZONE_M, L), m)
        x_inc = np.clip(x_inc, 0.0, L)
        order = np.argsort(t_inc)
        heaps = [[-math.inf] * t for t in teams_p]
        busy = 0.0
        resp_rep = []
        for i in order:
            t, x = float(t_inc[i]), float(x_inc[i])
            best, best_arr, best_travel = -1, math.inf, 0.0
            for j, h in enumerate(heaps):
                travel = (abs(x - pos_p[j]) + off_p[j]) / speed
                arr = max(t, h[0]) + mob_p[j] + travel
                if arr < best_arr:
                    best, best_arr, best_travel = j, arr, travel
            free = heapq.heappop(heaps[best])
            if free > t:
                n_wait += 1
            service = treat_s * rng.lognormal(-0.08, 0.4)
            heapq.heappush(heaps[best], best_arr + service + best_travel)
            busy += service + 2 * best_travel
            resp_rep.append(best_arr - t)
            if rep == 0:
                example.append((x / 1000.0, t / 60.0, best_arr - t))
        n_inc += len(resp_rep)
        all_resp.extend(resp_rep)
        busy_total += busy
        if max(resp_rep) > target_s:
            over_any += 1

    res = MedResult(posts=posts, target_s=target_s, n_reps=int(n_reps), mean_incidents=lam,
                    coverage_km=centres / 1000.0, coverage_s=coverage, uncovered_m=uncovered_m,
                    example_incidents=example)
    if all_resp:
        arr = np.asarray(all_resp)
        res.response_p50_s = float(np.percentile(arr, 50))
        res.response_p95_s = float(np.percentile(arr, 95))
        res.response_max_s = float(arr.max())
        res.share_within_target = float(np.mean(arr <= target_s))
        res.share_waiting_for_team = n_wait / n_inc
        res.prob_any_over_target = over_any / max(1, int(n_reps))
        horizon = max(1.0, meso.duration_s)
        res.team_busy_share = float(busy_total / max(1, n_reps) / (sum(teams_p) * horizon))
    else:
        res.share_within_target = 1.0
    return res