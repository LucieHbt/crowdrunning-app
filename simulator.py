"""Moteur microscopique de foule progressant le long d'un parcours GPX.

Les agents se déplacent dans un repère métrique local. Les règles de vitesse-densité,
de répulsion et d'arrêt aux points d'eau sont des approximations pédagogiques, pas une
implémentation calibrée ni certifiée de Weidmann ou du Social Force Model.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
import numpy as np


def load_gpx(content: bytes, geographic: bool = False):
    """Extrait les points du GPX sous forme (lat, lon) ou (x, y) en mètres."""
    root = ET.fromstring(content)
    pts = []
    for suffix in ("trkpt", "rtept"):
        for el in root.iter():
            if el.tag.endswith(suffix):
                lat, lon = el.attrib.get("lat"), el.attrib.get("lon")
                if lat is not None and lon is not None:
                    pts.append((float(lat), float(lon)))
        if pts:
            break
    if not pts:
        for el in root.iter():
            if el.tag.endswith("wpt"):
                lat, lon = el.attrib.get("lat"), el.attrib.get("lon")
                if lat is not None and lon is not None:
                    pts.append((float(lat), float(lon)))
    if geographic or not pts:
        return pts
    return geographic_to_local(pts)


def geographic_to_local(points):
    """Projection locale équirectangulaire, adaptée à un parcours de taille limitée."""
    lat0, lon0 = points[0]
    cos_lat = max(1e-6, math.cos(math.radians(lat0)))
    return [((lon - lon0) * 111_320 * cos_lat, (lat - lat0) * 110_540)
            for lat, lon in points]


def local_to_geographic(point, origin):
    """Conversion inverse approximative : (x, y) mètres -> (lat, lon)."""
    x, y = float(point[0]), float(point[1])
    lat0, lon0 = origin
    lat = lat0 + y / 110_540
    lon = lon0 + x / (111_320 * max(1e-6, math.cos(math.radians(lat0))))
    return float(lat), float(lon)


def _prepare_polyline(points, spacing=2.0):
    """Ré-échantillonne la ligne et prépare les distances cumulées."""
    pts = np.asarray(points, dtype=float)
    if len(pts) < 2:
        raise ValueError("Le parcours doit contenir au moins deux points.")
    clean = [pts[0]]
    for point in pts[1:]:
        if np.linalg.norm(point - clean[-1]) > 1e-5:
            clean.append(point)
    pts = np.asarray(clean)
    if len(pts) < 2:
        raise ValueError("Le parcours GPX ne contient pas de distance exploitable.")
    lengths = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(lengths)))
    total = float(cumulative[-1])
    samples = np.arange(0.0, total, spacing)
    samples = np.append(samples, total)
    dense = []
    for s in samples:
        i = min(int(np.searchsorted(cumulative, s, side="right") - 1), len(lengths) - 1)
        frac = (s - cumulative[i]) / max(lengths[i], 1e-9)
        dense.append(pts[i] + frac * (pts[i + 1] - pts[i]))
    dense = np.asarray(dense)
    dense_lengths = np.linalg.norm(np.diff(dense, axis=0), axis=1)
    dense_cumulative = np.concatenate(([0.0], np.cumsum(dense_lengths)))
    return dense, dense_cumulative, float(dense_cumulative[-1])


class Agent:
    """Coureur individuel avec position, vitesse, accélération et champ de vision."""
    def __init__(self, agent_id, position, desired_speed=2.8, radius=0.28, fov_deg=200):
        self.id = int(agent_id)
        self.position = np.asarray(position, dtype=float)
        self.velocity = np.zeros(2, dtype=float)
        self.acceleration = np.zeros(2, dtype=float)
        self.desired_speed = float(desired_speed)
        self.radius = float(radius)
        self.fov_deg = float(fov_deg)
        self.progress_m = 0.0
        self.lateral_m = 0.0
        self.forward_speed = 0.0
        self.lateral_speed = 0.0
        self.release_at_s = 0.0
        self.stop_until_s = 0.0
        self.next_station_idx = 0
        self.arrived = False

    @property
    def speed(self):
        return float(np.linalg.norm(self.velocity))


class CrowdSimulation:
    """Simulation 2D avec vagues de départ, dispersion d'allure et points d'eau."""
    def __init__(
        self, route, n_agents=100, desired_speed=2.8, density_sensitivity=2.0,
        seed=42, corridor_width=8.0, *, speed_sigma=0.18, density_target=0.3,
        start_width=None, finish_width=None, waves=1, wave_gap_s=300,
        expected_runners=None, flow_max=1.5, water_stations_km=(),
        p_drink_base=0.60, temperature_c=14, temperature_rise_c_per_hour=2.0,
        stop_mean_s=5.0, wave_percentages=None, wave_median_times_s=None,
    ):
        self.rng = np.random.default_rng(seed)
        self.route, self.route_distances, self.route_length = _prepare_polyline(route)
        self.n_agents = int(n_agents)
        if self.n_agents < 1:
            raise ValueError("n_agents doit être supérieur ou égal à 1.")
        self.desired_speed = float(desired_speed)
        self.density_sensitivity = float(density_sensitivity)
        self.density_target = max(0.05, float(density_target))
        self.corridor_width = float(corridor_width)
        self.start_width = float(start_width if start_width is not None else corridor_width)
        self.finish_width = float(finish_width if finish_width is not None else corridor_width)
        self.waves = max(1, int(waves))
        self.wave_gap_s = max(0.0, float(wave_gap_s))
        self.expected_runners = max(self.n_agents, int(expected_runners or self.n_agents))
        self.flow_max = max(0.1, float(flow_max))
        self.p_drink_base = float(np.clip(p_drink_base, 0.0, 1.0))
        self.temperature_c = float(temperature_c)
        self.temperature_rise_c_per_hour = float(temperature_rise_c_per_hour)
        self.stop_mean_s = max(0.0, float(stop_mean_s))
        self.time_s = 0.0
        self.water_stations_m = sorted({
            float(km) * 1000.0 for km in water_stations_km
            if 0 < float(km) * 1000.0 < self.route_length
        })
        self.agents = []

        # Les parts peuvent être personnalisées vague par vague. Si elles ne sont pas
        # fournies, on conserve le comportement historique avec une répartition égale.
        actual_waves = self.waves
        if wave_percentages is None:
            shares = np.full(actual_waves, 1.0 / actual_waves, dtype=float)
        else:
            shares_in = np.asarray(wave_percentages, dtype=float)
            if (
                shares_in.size != actual_waves
                or not np.all(np.isfinite(shares_in))
                or np.any(shares_in < 0)
                or not np.isclose(shares_in.sum(), 100.0)
            ):
                raise ValueError(
                    "wave_percentages doit fournir un pourcentage non négatif par vague "
                    "et totaliser exactement 100 %."
                )
            shares = shares_in / 100.0

        # Attribution d'un nombre entier d'agents à chaque vague par la méthode des
        # plus forts restes : la somme est toujours exactement n_agents.
        exact_counts = shares * self.n_agents
        wave_counts = np.floor(exact_counts).astype(int)
        remaining = self.n_agents - int(wave_counts.sum())
        if remaining:
            order_remainders = np.argsort(-(exact_counts - wave_counts))
            wave_counts[order_remainders[:remaining]] += 1
        wave_ids = np.repeat(np.arange(actual_waves, dtype=int), wave_counts)
        if wave_ids.size != self.n_agents:
            raise RuntimeError("La répartition des agents entre les vagues est incohérente.")

        # Un temps médian cible par vague donne une vitesse libre médiane propre à cette
        # vague. En l'absence de temps, desired_speed conserve son rôle historique.
        if wave_median_times_s is not None:
            medians = np.asarray(wave_median_times_s, dtype=float)
            if (
                medians.size != actual_waves
                or not np.all(np.isfinite(medians))
                or np.any(medians <= 0)
            ):
                raise ValueError(
                    "wave_median_times_s doit contenir un temps positif par vague."
                )
            wave_speeds = self.route_length / medians
        else:
            medians = None
            wave_speeds = np.full(actual_waves, max(0.1, self.desired_speed), dtype=float)

        # Expose les paramètres effectifs pour les résumés, exports ou diagnostics.
        self.wave_ids = wave_ids
        self.wave_counts = wave_counts.tolist()
        self.wave_percentages = (shares * 100.0).tolist()
        self.wave_median_times_s = medians.tolist() if medians is not None else None
        self.wave_speeds_m_s = wave_speeds.tolist()
        self.wave_paces_min_per_km = [
            1000.0 / max(float(speed), 1e-9) / 60.0
            for speed in wave_speeds
        ]
        pace_seconds_per_km = [pace * 60.0 for pace in self.wave_paces_min_per_km]
        self.reference_pace_min_per_km = (
            sum(pace * share for pace, share in zip(pace_seconds_per_km, shares))
            / max(float(shares.sum()), 1e-12) / 60.0
        )

        # Compatibilité : la vitesse générale expose la vitesse correspondant à l'allure
        # pondérée calculée, mais les agents utilisent bien leur vitesse propre à la vague.
        if medians is not None and self.reference_pace_min_per_km > 0:
            self.desired_speed = 1000.0 / (self.reference_pace_min_per_km * 60.0)

        # L'effectif complet détermine l'étalement simulé ; seuls des agents échantillons
        # sont affichés pour garder une animation fluide avec des courses de grande taille.
        representative_scale = self.expected_runners / max(1, self.n_agents)
        flow_rate = max(0.1, self.flow_max * max(1.0, self.start_width))
        within_wave_counts = [0] * actual_waves
        release_times = np.zeros(self.n_agents, dtype=float)
        speeds = np.empty(self.n_agents, dtype=float)

        for wave_idx in range(actual_waves):
            ids = np.flatnonzero(wave_ids == wave_idx)
            if not ids.size:
                continue
            median_speed = max(0.1, float(wave_speeds[wave_idx]))
            speeds[ids] = self.rng.lognormal(
                mean=math.log(median_speed),
                sigma=max(0.0, float(speed_sigma)),
                size=ids.size,
            )
            # Mélange des libérations à l'intérieur de chaque vague.
            release_order = ids[self.rng.permutation(ids.size)]
            for rank, agent_idx in enumerate(release_order):
                release_times[agent_idx] = (
                    wave_idx * self.wave_gap_s + rank * representative_scale / flow_rate
                )

        speeds = np.clip(speeds, 0.5, 6.0)
        self.agents = []
        for i in range(self.n_agents):
            progress = float(self.rng.uniform(0.0, min(5.0, self.route_length * 0.02)))
            center, tangent, normal = self._route_frame(progress)
            half_start = max(0.1, self.start_width / 2 - 0.3)
            lateral = float(self.rng.uniform(-half_start, half_start))
            agent = Agent(i, center + normal * lateral, desired_speed=speeds[i])
            agent.progress_m = progress
            agent.lateral_m = lateral
            agent.wave_id = int(wave_ids[i])
            agent.forward_speed = 0.0
            agent.velocity[:] = 0.0
            agent.release_at_s = float(release_times[i])
            self.agents.append(agent)

    def _route_frame(self, progress):
        """Retourne point, tangente et normale au parcours à une distance donnée."""
        s = float(np.clip(progress, 0.0, self.route_length))
        i = min(int(np.searchsorted(self.route_distances, s, side="right") - 1), len(self.route) - 2)
        seg_len = max(self.route_distances[i + 1] - self.route_distances[i], 1e-9)
        frac = (s - self.route_distances[i]) / seg_len
        tangent = self.route[i + 1] - self.route[i]
        tangent = tangent / max(np.linalg.norm(tangent), 1e-9)
        center = self.route[i] + frac * (self.route[i + 1] - self.route[i])
        normal = np.array([-tangent[1], tangent[0]])
        return center, tangent, normal

    def _width_at_progress(self, progress):
        """Interpolation entre la largeur de départ, la largeur courante et celle d'arrivée."""
        ramp = min(40.0, max(5.0, self.route_length / 4.0))
        if progress < ramp:
            f = np.clip(progress / ramp, 0.0, 1.0)
            return float(self.start_width * (1.0 - f) + self.corridor_width * f)
        if progress > self.route_length - ramp:
            f = np.clip((progress - (self.route_length - ramp)) / ramp, 0.0, 1.0)
            return float(self.corridor_width * (1.0 - f) + self.finish_width * f)
        return self.corridor_width

    @staticmethod
    def _local_density(i, positions, active_mask, radius=2.0):
        if not active_mask[i]:
            return 0.0
        delta = positions - positions[i]
        distances = np.linalg.norm(delta, axis=1)
        count = int(np.sum((distances < radius) & (distances > 1e-9) & active_mask))
        return count / (math.pi * radius * radius)

    def step(self, dt=1.0, substeps=3):
        """Avance la foule ; le calcul emploie de petites sous-étapes pour limiter l'instabilité."""
        h = dt / substeps
        for _ in range(substeps):
            active_mask = np.array([self.time_s >= a.release_at_s and not a.arrived for a in self.agents], dtype=bool)
            positions = np.array([a.position for a in self.agents])
            accelerations = np.zeros_like(positions)
            target_speeds = np.zeros(self.n_agents, dtype=float)
            frames = [self._route_frame(a.progress_m) for a in self.agents]

            # Calcule les distances entre toutes les paires une seule fois par sous-pas.
            # La version précédente les recalculait agent par agent et parcourait en Python
            # chaque voisin proche : cette matrice vectorisée accélère les longues courses.
            pair_delta = positions[:, None, :] - positions[None, :, :]
            pair_dist = np.linalg.norm(pair_delta, axis=2)
            neighbour_mask = (
                (pair_dist < 2.0) & (pair_dist > 1e-9)
                & active_mask[:, None] & active_mask[None, :]
            )
            local_density = neighbour_mask.sum(axis=1) / (math.pi * 2.0 * 2.0)

            close_mask = (
                (pair_dist < 1.25) & (pair_dist > 1e-6)
                & active_mask[None, :]
            )
            repulsion_strength = np.where(
                close_mask,
                1.8 * np.exp(np.clip((0.65 - pair_dist) / 0.35, -5, 5)),
                0.0,
            )
            repulsion = (
                pair_delta / np.maximum(pair_dist[:, :, None], 1e-9)
                * repulsion_strength[:, :, None]
            ).sum(axis=1)

            for i, agent in enumerate(self.agents):
                if not active_mask[i] or self.time_s < agent.stop_until_s:
                    continue
                density = float(local_density[i])
                if density <= self.density_target:
                    speed_factor = 1.0
                else:
                    ratio = density / self.density_target - 1.0
                    speed_factor = float(np.clip(math.exp(-0.35 * self.density_sensitivity * ratio), 0.15, 1.0))
                _, tangent, normal = frames[i]
                target_speed = agent.desired_speed * speed_factor
                target_speeds[i] = target_speed
                acc = tangent * ((target_speed - agent.forward_speed) / 0.6)
                # Force de répulsion vectorisée entre agents actifs proches.
                acc += repulsion[i]

                # Contraintes latérales liées à la largeur à la position de l'agent.
                half = max(0.1, self._width_at_progress(agent.progress_m) / 2 - agent.radius)
                clipped_lateral = float(np.clip(agent.lateral_m, -half, half))
                lateral_error = agent.lateral_m - clipped_lateral
                acc += normal * (-1.2 * agent.lateral_speed - 10.0 * lateral_error)
                accelerations[i] = acc

            for i, agent in enumerate(self.agents):
                if not active_mask[i]:
                    continue
                if self.time_s < agent.stop_until_s:
                    agent.velocity[:] = 0.0
                    agent.forward_speed = 0.0
                    agent.lateral_speed = 0.0
                    continue

                _, tangent, normal = frames[i]
                agent.acceleration = accelerations[i]
                # Intégration exacte de la relaxation longitudinale : stable même avec un
                # pas d'une seconde, donc le moteur peut avancer 2 s par appel avec 2 sous-pas.
                target_speed = target_speeds[i]
                relaxation_acc = tangent * ((target_speed - agent.forward_speed) / 0.6)
                external_acc = agent.acceleration - relaxation_acc
                longitudinal_external = float(np.dot(external_acc, tangent))
                lateral_acc = float(np.dot(agent.acceleration, normal))
                relaxation_decay = math.exp(-h / 0.6)
                relaxed_speed = target_speed + (agent.forward_speed - target_speed) * relaxation_decay
                agent.forward_speed = float(np.clip(relaxed_speed + longitudinal_external * h, 0.0, 5.5))
                agent.lateral_speed = float(np.clip(agent.lateral_speed + lateral_acc * h, -1.8, 1.8))
                previous_progress = agent.progress_m
                proposed_progress = min(self.route_length, previous_progress + agent.forward_speed * h)

                # Arrêts aux points d'eau : la probabilité augmente de 3 points par degré gagné,
                # conformément à l'hypothèse déclarée dans le prototype de planification.
                while agent.next_station_idx < len(self.water_stations_m):
                    station = self.water_stations_m[agent.next_station_idx]
                    if previous_progress < station <= proposed_progress:
                        temp_now = self.temperature_c + self.temperature_rise_c_per_hour * (self.time_s / 3600.0)
                        drink_probability = min(1.0, self.p_drink_base + max(0.0, temp_now - self.temperature_c) * 0.03)
                        agent.next_station_idx += 1
                        if self.rng.random() < drink_probability and self.stop_mean_s > 0:
                            proposed_progress = station
                            agent.stop_until_s = self.time_s + self.stop_mean_s
                        continue
                    if station <= previous_progress:
                        agent.next_station_idx += 1
                        continue
                    break

                agent.progress_m = proposed_progress
                half = max(0.1, self._width_at_progress(agent.progress_m) / 2 - agent.radius)
                agent.lateral_m = float(np.clip(agent.lateral_m + agent.lateral_speed * h, -half, half))
                center, tangent_new, normal_new = self._route_frame(agent.progress_m)
                agent.position = center + normal_new * agent.lateral_m
                agent.velocity = tangent_new * agent.forward_speed + normal_new * agent.lateral_speed
                if agent.progress_m >= self.route_length - 1e-6:
                    agent.progress_m = self.route_length
                    agent.forward_speed = 0.0
                    agent.lateral_speed = 0.0
                    agent.velocity[:] = 0.0
                    agent.arrived = True
            self.time_s += h

    def snapshot(self):
        positions = np.array([a.position for a in self.agents])
        released_mask = np.array([self.time_s >= a.release_at_s for a in self.agents], dtype=bool)
        active_mask = released_mask & np.array([not a.arrived for a in self.agents], dtype=bool)
        pair_delta = positions[:, None, :] - positions[None, :, :]
        pair_dist = np.linalg.norm(pair_delta, axis=2)
        neighbours = (
            (pair_dist < 2.0) & (pair_dist > 1e-9)
            & active_mask[:, None] & active_mask[None, :]
        )
        densities = neighbours.sum(axis=1) / (math.pi * 2.0 * 2.0)

        rows = []
        for i, a in enumerate(self.agents):
            released = bool(released_mask[i])
            stopped = bool(released and self.time_s < a.stop_until_s and not a.arrived)
            rows.append({
                "time_s": round(self.time_s, 2),
                "agent_id": a.id,
                "wave_id": int(getattr(a, "wave_id", 0)) + 1,
                "x_m": round(float(a.position[0]), 3),
                "y_m": round(float(a.position[1]), 3),
                "progress_m": round(float(a.progress_m), 3),
                "speed_m_s": round(a.speed, 3),
                "acceleration_m_s2": round(float(np.linalg.norm(a.acceleration)), 3),
                "density_p_m2": round(float(densities[i]), 3),
                "released": released,
                "stopped": stopped,
                "arrived": bool(a.arrived),
                "release_at_s": round(float(a.release_at_s), 2),
            })
        return rows
