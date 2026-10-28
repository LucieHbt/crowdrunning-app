"""Moteur mésoscopique : tous les partants, un diagramme fondamental, une densité par tronçon.

Principe
--------
Chaque coureur de l'effectif complet est suivi en 1D le long du parcours (abscisse curviligne).
À chaque pas de temps, les coureurs sont comptés par cellule de ~10 m ; le comptage est lissé,
divisé par la largeur utile du couloir, et donne une densité (coureurs/m²). La vitesse de chaque
coureur est sa vitesse libre multipliée par un facteur décroissant avec cette densité.

Contrairement au moteur microscopique (`simulator.py`), les densités ne sont donc pas calculées
sur un échantillon d'agents mais sur la totalité des partants. Le moteur microscopique reste
utile pour des zones locales (départ, arrivée, goulots) ; il n'est plus utilisé pour la densité.

Limites assumées (à calibrer sur des observations réelles) :
- relation vitesse-densité inspirée de Weidmann, normalisée pour valoir 1 sous la densité libre ;
  ce n'est pas une reproduction exacte du modèle original ;
- dépassements non limités par la largeur (1D) : la densité ralentit tout le monde de façon
  identique, quelle que soit la vitesse libre ;
- les coureurs en attente à un point d'eau sont comptés en amont du poste (≈2 pers./m²) ;
- la zone d'arrivée (file après la ligne) n'est pas modélisée.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from simulator import _prepare_polyline

# Paramètres de la forme de Weidmann (valeurs de la littérature, utilisées ici de façon illustrative).
GAMMA = 1.913
RHO_MAX = 5.4
MIN_SPEED_FACTOR = 0.08

WAITING, RUNNING, QUEUED, SERVING, DONE = 0, 1, 2, 3, 4


def _weidmann_shape(rho):
    rho = np.maximum(np.asarray(rho, dtype=float), 1e-6)
    return np.clip(1.0 - np.exp(-GAMMA * (1.0 / rho - 1.0 / RHO_MAX)), 0.0, 1.0)


def speed_factor(rho, rho_free=0.3, sensitivity=2.0):
    """Facteur de vitesse (0..1) en fonction de la densité locale (coureurs/m²).

    Vaut 1 sous `rho_free`, puis suit le rapport F(rho)/F(rho_free) de la forme de Weidmann.
    `sensitivity` = 2 donne le rapport tel quel ; l'exposant est sensitivity / 2.
    """
    rho = np.asarray(rho, dtype=float)
    rho_free = max(0.05, float(rho_free))
    ref = max(float(_weidmann_shape(rho_free)), 1e-9)
    ratio = np.clip(_weidmann_shape(rho) / ref, 0.0, 1.0)
    factor = np.where(rho <= rho_free, 1.0, ratio ** max(0.1, float(sensitivity) / 2.0))
    return np.clip(factor, MIN_SPEED_FACTOR, 1.0)


@dataclass
class StationSpec:
    """Point d'eau : position le long du parcours, postes de service simultanés, durée de service."""
    pos_m: float
    slots: int = 25
    service_mean_s: float = 5.0
    name: str = ""


@dataclass
class MesoResult:
    cell_m: float
    route_length_m: float
    n_runners: int
    release_s: np.ndarray
    finish_s: np.ndarray
    record_s: float
    times_s: np.ndarray
    rho_t: np.ndarray                 # [records, cellules] densité lissée (coureurs/m²)
    rho_max_cell: np.ndarray          # maximum exact sur tous les pas
    t_rho_max_cell: np.ndarray        # instant du maximum
    minutes_above_03_cell: np.ndarray
    minutes_above_05_cell: np.ndarray
    sample_ids: np.ndarray
    sample_progress: np.ndarray       # [records, échantillon]
    sample_state: np.ndarray
    sample_speed: np.ndarray
    sample_rho: np.ndarray
    sample_lateral_frac: np.ndarray
    stations: list = field(default_factory=list)
    station_stats: list = field(default_factory=list)
    width_cells: np.ndarray = None
    cutoff_s: float = 0.0
    duration_s: float = 0.0
    queue_len_t: np.ndarray = None    # [records, points d'eau] coureurs en file (hors service)

    # ---- agrégations pour l'affichage -------------------------------------------------
    @property
    def n_cells(self):
        return int(self.rho_max_cell.size)

    def _edges(self, n_bins):
        n_bins = int(max(1, min(n_bins, self.n_cells)))
        return np.linspace(0, self.n_cells, n_bins + 1).round().astype(int)

    def profile_max(self, n_bins):
        """Maximum de densité par tronçon (n_bins tronçons égaux) : liste de floats."""
        edges = self._edges(n_bins)
        return np.maximum.reduceat(self.rho_max_cell, edges[:-1]).astype(float).tolist()

    def peak(self):
        """(densité max, km, instant en s) sur toute la simulation."""
        i = int(np.argmax(self.rho_max_cell))
        km = min((i + 0.5) * self.cell_m, self.route_length_m) / 1000.0
        return float(self.rho_max_cell[i]), float(km), float(self.t_rho_max_cell[i])

    def heatmap(self, n_dist_bins=160, n_time_bins=180):
        """Densité max par (temps, distance) : retourne z, x en km, y en minutes."""
        edges = self._edges(n_dist_bins)
        z_rec = np.maximum.reduceat(self.rho_t, edges[:-1], axis=1)
        time_bin = max(float(self.record_s), math.ceil((self.times_s[-1] + 1) / n_time_bins / 30.0) * 30.0)
        bin_idx = (self.times_s // time_bin).astype(int)
        n_t = int(bin_idx.max()) + 1
        z = np.zeros((n_t, z_rec.shape[1]), dtype=float)
        for b in range(n_t):
            sel = bin_idx == b
            if sel.any():
                z[b] = z_rec[sel].max(axis=0)
        x = np.clip((edges[:-1] + edges[1:]) / 2.0 * self.cell_m, 0, self.route_length_m) / 1000.0
        y = (np.arange(n_t) * time_bin + time_bin / 2.0) / 60.0
        return z, x, y

    def summary(self):
        chip = self.finish_s - self.release_s
        done = ~np.isnan(self.finish_s)
        peak_rho, peak_km, peak_t = self.peak()
        out = {
            "runners": int(self.n_runners),
            "finishers_in_horizon": int(done.sum()),
            "not_finished_at_end": int((~done).sum()),
            "after_cutoff_gun_time": int((done & (self.finish_s > self.cutoff_s)).sum()) if self.cutoff_s else 0,
            "peak_density_p_m2": round(peak_rho, 3),
            "peak_density_km": round(peak_km, 3),
            "peak_density_time_s": round(peak_t, 1),
            "length_above_0_3_m": float(np.sum(self.rho_max_cell > 0.3) * self.cell_m),
            "length_above_0_5_m": float(np.sum(self.rho_max_cell > 0.5) * self.cell_m),
            "worst_minutes_above_0_5": round(float(self.minutes_above_05_cell.max()), 2),
            "simulated_duration_s": round(float(self.duration_s), 1),
        }
        if done.any():
            q = np.percentile(chip[done], [5, 50, 95])
            out.update({"chip_time_p05_s": round(float(q[0]), 1),
                        "chip_time_median_s": round(float(q[1]), 1),
                        "chip_time_p95_s": round(float(q[2]), 1),
                        "last_finish_gun_s": round(float(np.nanmax(self.finish_s)), 1)})
        return out


class MesoSimulation:
    def __init__(
        self, route, n_runners=3000, desired_speed=2.9, *, speed_sigma=0.18,
        density_target=0.3, density_sensitivity=2.0, corridor_width=5.0,
        start_width=None, finish_width=None, waves=5, wave_gap_s=300.0, flow_max=1.5,
        stations=(), width_overrides=(), p_drink_base=0.6, temperature_c=14.0,
        temperature_rise_c_per_hour=2.0, cutoff_s=0.0, seed=42, dt=2.0, n_sample=100,
        wave_mixing=0.08, cell_m=10.0, max_records=500, min_duration_s=0.0,
        wave_percentages=None, wave_median_times_s=None,
    ):
        self.rng = np.random.default_rng(seed)
        self.route, self.route_distances, self.route_length = _prepare_polyline(route)
        self.n = int(max(1, n_runners))
        self.desired_speed = float(desired_speed)
        self.rho_free = max(0.05, float(density_target))
        self.sensitivity = float(density_sensitivity)
        self.corridor_width = float(corridor_width)
        self.start_width = float(start_width if start_width is not None else corridor_width)
        self.finish_width = float(finish_width if finish_width is not None else corridor_width)
        self.p_drink_base = float(np.clip(p_drink_base, 0.0, 1.0))
        self.temperature_rise = float(temperature_rise_c_per_hour)
        self.cutoff_s = float(cutoff_s)
        self.dt = float(dt)
        self.max_records = int(max_records)
        self.stations = sorted(
            [s for s in stations if 0.0 < s.pos_m < self.route_length], key=lambda s: s.pos_m)

        self.cell_m = float(max(cell_m, self.route_length / 4000.0))
        self.n_cells = int(max(1, math.ceil(self.route_length / self.cell_m)))
        centres = (np.arange(self.n_cells) + 0.5) * self.cell_m
        self.width_cells = self._width_profile(centres, width_overrides)
        # Noyau triangulaire de ±3 cellules : lisse le comptage sans étaler la congestion.
        k = np.array([1, 2, 3, 4, 3, 2, 1], dtype=float)
        self._kernel = k / k.sum()

        # Répartition explicite des partants entre vagues, par la méthode des plus forts restes.
        actual_waves = int(max(1, min(int(waves), self.n)))
        if wave_percentages is None:
            shares = np.full(actual_waves, 1.0 / actual_waves, dtype=float)
        else:
            shares_in = np.asarray(wave_percentages, dtype=float)
            if (
                shares_in.size != int(waves)
                or not np.all(np.isfinite(shares_in))
                or np.any(shares_in < 0)
                or not np.isclose(shares_in.sum(), 100.0)
            ):
                raise ValueError(
                    "wave_percentages doit contenir une part non négative par vague et totaliser 100 %."
                )
            shares = shares_in[:actual_waves]
            shares = shares / max(float(shares.sum()), 1e-12)

        exact_counts = shares * self.n
        wave_counts = np.floor(exact_counts).astype(int)
        remaining = self.n - int(wave_counts.sum())
        if remaining:
            remainders_order = np.argsort(-(exact_counts - wave_counts))
            wave_counts[remainders_order[:remaining]] += 1
        wave_ids = np.repeat(np.arange(actual_waves), wave_counts)
        if wave_ids.size != self.n:
            raise RuntimeError("La répartition des partants entre vagues est incohérente.")

        # Un temps médian cible par vague détermine sa vitesse libre médiane sur la distance
        # du tracé. La densité et les arrêts peuvent ensuite allonger le temps réellement obtenu.
        if wave_median_times_s is not None:
            medians = np.asarray(wave_median_times_s, dtype=float)
            if (
                medians.size != int(waves)
                or not np.all(np.isfinite(medians))
                or np.any(medians <= 0)
            ):
                raise ValueError("wave_median_times_s doit contenir un temps positif par vague.")
            medians = medians[:actual_waves]
            wave_speeds = self.route_length / medians
        else:
            medians = None
            wave_speeds = np.full(actual_waves, self.desired_speed, dtype=float)

        self.v0 = np.empty(self.n, dtype=float)
        self.release_s = np.zeros(self.n, dtype=float)
        rate = max(0.1, float(flow_max) * max(1.0, self.start_width))
        self.wave_ids = wave_ids
        self.wave_counts = wave_counts.tolist()
        self.wave_percentages = (shares * 100.0).tolist()
        self.wave_median_times_s = medians.tolist() if medians is not None else None
        for w in range(actual_waves):
            ids = np.flatnonzero(wave_ids == w)
            if ids.size == 0:
                continue
            median_speed = max(0.1, float(wave_speeds[w]))
            self.v0[ids] = self.rng.lognormal(
                math.log(median_speed), max(0.0, float(speed_sigma)), ids.size
            )
            # Mélange aléatoire uniquement à l'intérieur de la vague.
            order_in_wave = ids[self.rng.permutation(ids.size)]
            self.release_s[order_in_wave] = w * float(wave_gap_s) + np.arange(ids.size) / rate

        self.v0 = np.clip(self.v0, 0.8, 6.0)
        self.latest_release_s = float(self.release_s.max())
        self._order = np.argsort(self.release_s)

        n_sample = int(max(1, min(n_sample, self.n)))
        picks = np.unique(np.linspace(0, self.n - 1, n_sample).round().astype(int))
        self.sample_ids = self._order[picks]
        self.lateral_frac = self.rng.uniform(-1.0, 1.0, self.n)

        configured_course = (
            max(self.wave_median_times_s) if self.wave_median_times_s
            else self.route_length / max(0.5, self.desired_speed)
        )
        est_course = max(
            self.route_length / max(0.5, self.desired_speed * 0.70),
            float(configured_course) / 0.70,
        )
        # Marge pour les points d'eau : service individuel + temps d'écoulement de la file
        # si le poste est sous-dimensionné (n coureurs × part qui boit × durée / postes).
        p_est = min(1.0, self.p_drink_base + 0.1)
        est_stops = sum(p_est * s.service_mean_s * (1.0 + self.n / max(1, int(s.slots)))
                        for s in self.stations)
        self.horizon_s = max(float(min_duration_s), self.latest_release_s + 1.4 * (est_course + est_stops))
        raw_record = self.horizon_s / max(1, self.max_records)
        self.record_s = max(self.dt, math.ceil(raw_record / self.dt) * self.dt)

    # ---- géométrie ---------------------------------------------------------------------
    def _width_profile(self, s, overrides):
        L = self.route_length
        ramp = min(40.0, max(5.0, L / 4.0))
        s = np.asarray(s, dtype=float)
        w = np.full(s.shape, self.corridor_width)
        head = s < ramp
        w[head] = self.start_width * (1 - s[head] / ramp) + self.corridor_width * (s[head] / ramp)
        tail = s > L - ramp
        f = (s[tail] - (L - ramp)) / ramp
        w[tail] = self.corridor_width * (1 - f) + self.finish_width * f
        for start_m, end_m, width_m in overrides:
            w[(s >= start_m) & (s <= end_m)] = max(0.5, float(width_m))
        return np.maximum(w, 0.5)

    def route_xy(self, progress, lateral_m=None):
        """Positions (x, y) en mètres pour un tableau d'abscisses curvilignes."""
        s = np.clip(np.asarray(progress, dtype=float), 0.0, self.route_length)
        i = np.clip(np.searchsorted(self.route_distances, s, side="right") - 1, 0, len(self.route) - 2)
        seg = self.route[i + 1] - self.route[i]
        seg_len = np.maximum(np.linalg.norm(seg, axis=1), 1e-9)
        frac = (s - self.route_distances[i]) / np.maximum(self.route_distances[i + 1] - self.route_distances[i], 1e-9)
        pos = self.route[i] + frac[:, None] * seg
        if lateral_m is not None:
            tangent = seg / seg_len[:, None]
            normal = np.stack([-tangent[:, 1], tangent[:, 0]], axis=1)
            pos = pos + normal * np.asarray(lateral_m, dtype=float)[:, None]
        return pos

    def width_at(self, progress):
        idx = np.clip((np.asarray(progress) / self.cell_m).astype(int), 0, self.n_cells - 1)
        return self.width_cells[idx]

    # ---- densité -----------------------------------------------------------------------
    def _rho_field(self, counts):
        smoothed = np.convolve(counts, self._kernel, mode="same")
        return np.minimum(smoothed / (self.cell_m * self.width_cells), RHO_MAX)

    def _p_drink(self, t):
        return min(1.0, self.p_drink_base + max(0.0, self.temperature_rise * t / 3600.0) * 0.03)

    # ---- simulation --------------------------------------------------------------------
    def run(self, progress_cb=None):
        n, L, dt, C = self.n, self.route_length, self.dt, self.n_cells
        state = np.zeros(n, dtype=np.int8)
        progress = np.zeros(n)
        next_station = np.zeros(n, dtype=np.int32)
        queue_since = np.zeros(n)
        stop_until = np.zeros(n)
        finish_s = np.full(n, np.nan)
        queues = [deque() for _ in self.stations]
        serving = [[] for _ in self.stations]
        stats = [{"users": 0, "served": 0, "max_queue": 0, "wait_sum": 0.0, "wait_max": 0.0}
                 for _ in self.stations]

        rho_max = np.zeros(C)
        t_rho_max = np.zeros(C)
        above03 = np.zeros(C)
        above05 = np.zeros(C)
        times, rho_rec, s_prog, s_state, s_speed, s_rho, q_rec = [], [], [], [], [], [], []
        sid = self.sample_ids
        t, next_record, step_i = 0.0, 0.0, 0

        while True:
            state[(state == WAITING) & (self.release_s <= t)] = RUNNING
            on = (state >= RUNNING) & (state <= SERVING)
            cell = np.minimum((progress / self.cell_m).astype(np.int64), C - 1)
            # Les coureurs en file d'attente s'étalent en amont du point d'eau (≈2 pers./m²)
            # au lieu de s'empiler dans une seule cellule.
            for k, st in enumerate(self.stations):
                if queues[k]:
                    q = np.fromiter(queues[k], dtype=np.int64, count=len(queues[k]))
                    w_st = float(self.width_cells[min(int(st.pos_m / self.cell_m), C - 1)])
                    eff = np.maximum(0.0, st.pos_m - np.arange(q.size) / (2.0 * w_st))
                    cell[q] = np.minimum((eff / self.cell_m).astype(np.int64), C - 1)
            counts = np.bincount(cell[on], minlength=C).astype(float)
            rho = self._rho_field(counts)
            rho_run = rho[cell]
            speed = np.where(state == RUNNING,
                             self.v0 * speed_factor(rho_run, self.rho_free, self.sensitivity), 0.0)

            better = rho > rho_max
            rho_max[better] = rho[better]
            t_rho_max[better] = t
            above03 += (rho > 0.3) * (dt / 60.0)
            above05 += (rho > 0.5) * (dt / 60.0)

            finished_all = bool(np.all(state == DONE))
            end = finished_all or t >= self.horizon_s
            if t >= next_record - 1e-9 or end:
                times.append(t)
                rho_rec.append(rho.astype(np.float32))
                s_prog.append(progress[sid].astype(np.float32))
                s_state.append(state[sid].copy())
                s_speed.append(speed[sid].astype(np.float32))
                s_rho.append(rho_run[sid].astype(np.float32))
                q_rec.append([len(q) for q in queues])
                while next_record <= t + 1e-9:
                    next_record += self.record_s
            if end:
                break

            moving = state == RUNNING
            new = np.where(moving, np.minimum(L, progress + speed * dt), progress)
            now = t + dt

            for k, st in enumerate(self.stations):
                cross = moving & (next_station == k) & (progress < st.pos_m) & (new >= st.pos_m)
                ids = np.flatnonzero(cross)
                if ids.size:
                    next_station[ids] = k + 1
                    drinkers = ids[self.rng.random(ids.size) < self._p_drink(t)]
                    if drinkers.size and st.service_mean_s > 0:
                        new[drinkers] = st.pos_m
                        state[drinkers] = QUEUED
                        queue_since[drinkers] = now
                        queues[k].extend(drinkers.tolist())
                        stats[k]["users"] += int(drinkers.size)
                if serving[k]:
                    sv = np.asarray(serving[k])
                    done = stop_until[sv] <= now
                    if done.any():
                        state[sv[done]] = RUNNING
                        serving[k] = sv[~done].tolist()
                while queues[k] and len(serving[k]) < max(1, int(st.slots)):
                    i = queues[k].popleft()
                    wait = now - queue_since[i]
                    stats[k]["wait_sum"] += wait
                    stats[k]["wait_max"] = max(stats[k]["wait_max"], wait)
                    stats[k]["served"] += 1
                    state[i] = SERVING
                    stop_until[i] = now + st.service_mean_s * self.rng.lognormal(-0.08, 0.4)
                    serving[k].append(i)
                stats[k]["max_queue"] = max(stats[k]["max_queue"], len(queues[k]))

            arrived = moving & (state == RUNNING) & (new >= L - 1e-9)
            state[arrived] = DONE
            finish_s[arrived] = now
            progress = new
            t = now
            step_i += 1
            if progress_cb is not None and step_i % 50 == 0:
                progress_cb(min(1.0, t / max(1.0, self.horizon_s)), t)

        station_stats = []
        for st, s in zip(self.stations, stats):
            station_stats.append({
                "name": st.name, "km": round(st.pos_m / 1000.0, 3), "slots": int(st.slots),
                "users": s["users"], "max_queue": s["max_queue"],
                "mean_wait_s": float(round(s["wait_sum"] / s["served"], 1)) if s["served"] else 0.0,
                "max_wait_s": float(round(s["wait_max"], 1)),
            })

        return MesoResult(
            cell_m=self.cell_m, route_length_m=L, n_runners=n, release_s=self.release_s,
            finish_s=finish_s, record_s=self.record_s, times_s=np.asarray(times),
            rho_t=np.vstack(rho_rec), rho_max_cell=rho_max, t_rho_max_cell=t_rho_max,
            minutes_above_03_cell=above03, minutes_above_05_cell=above05,
            sample_ids=sid, sample_progress=np.vstack(s_prog), sample_state=np.vstack(s_state),
            sample_speed=np.vstack(s_speed), sample_rho=np.vstack(s_rho),
            sample_lateral_frac=self.lateral_frac[sid], stations=list(self.stations),
            station_stats=station_stats, width_cells=self.width_cells,
            cutoff_s=self.cutoff_s, duration_s=t,
            queue_len_t=np.asarray(q_rec, dtype=np.int32).reshape(len(times), len(self.stations)),
        )