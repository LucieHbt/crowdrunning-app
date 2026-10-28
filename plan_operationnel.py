"""Plan Opérationnel : document PDF d'organisation généré à partir du scénario et de la simulation.

Le document rassemble :
  1. les modalités de fermeture de routes (créneaux calculés d'après le passage simulé de la tête et de la queue de course) ;
  2. les niveaux de personnel (bénévoles) ;
  3. l'emplacement des points d'eau et d'épongeage ;
  4. les scénarios de repli et modifications de tracé (météo, incident) ;
  5. la couverture médias ;
  6. la gestion des flux aux intersections et aux points de convergence ;
  7. le déploiement des services d'urgence ;
  puis un état de conformité au règlement et les hypothèses retenues.

Tout ce qui vient de la simulation ou de la carte est calculé ; tout ce que l'application ne peut pas connaître
(arrêtés, contacts, dispositif médias, seuils météo) vient du formulaire et apparaît « à compléter » s'il est vide.
"""
from __future__ import annotations

import io
import math
from datetime import date, datetime, timedelta
from xml.sax.saxutils import escape as _xml_escape

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from PIL import Image as PILImage  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_LEFT, TA_RIGHT  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfgen import canvas as rl_canvas  # noqa: E402
from reportlab.platypus import (BaseDocTemplate, Frame, HRFlowable, Image, KeepTogether, PageBreak,  # noqa: E402
                                PageTemplate, Spacer, Table, TableStyle)
from reportlab.platypus import Paragraph as _Paragraph  # noqa: E402

import scenario as sc  # noqa: E402

def Paragraph(text, style):
    """Paragraph qui remplace l'exposant « ² » (absent des polices standard du PDF) par une balise <super>."""
    return _Paragraph(str(text).replace("\u00b2", "<super>2</super>"), style)


ACCENT = colors.HexColor("#ff4a4a")
INK = colors.HexColor("#1f2937")
MUTED = colors.HexColor("#6b7280")
LINE = colors.HexColor("#d9dee5")
SOFT = colors.HexColor("#f3f4f6")
PAGE_W, PAGE_H = A4
MARGIN_X = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN_X

MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
TODO = "À compléter"

KIND_STYLE = {   # kind -> (marqueur matplotlib, couleur, taille)
    "water": ("o", "#1d4ed8", 70), "sponge": ("o", "#0891b2", 60), "toilets": ("s", "#7c3aed", 62),
    "medical": ("P", "#dc2626", 100), "doctor": ("P", "#be123c", 70), "ambulance": ("D", "#b91c1c", 64),
    "volunteer": ("^", "#059669", 66), "bottleneck": ("X", "#ea580c", 80),
}


# ======================================================================================
# Utilitaires
# ======================================================================================
def esc(x) -> str:
    return _xml_escape(str(x))


def fr_date(d: date) -> str:
    return f"{DAYS[d.weekday()]} {d.day}{'er' if d.day == 1 else ''} {MONTHS[d.month - 1]} {d.year}"


def parse_hhmm(text: str) -> timedelta:
    try:
        h, m = str(text).strip().split(":")[:2]
        return timedelta(hours=int(h), minutes=int(m))
    except Exception:
        return timedelta(hours=9)


def clock(base: timedelta, seconds: float) -> str:
    total = base + timedelta(seconds=float(seconds))
    mins = int(round(total.total_seconds() / 60.0))
    return f"{(mins // 60) % 24:02d}h{mins % 60:02d}"


def fmt_km(m: float) -> str:
    return f"{m / 1000.0:.1f}".replace(".", ",")


def fmt_num(x, nd=0) -> str:
    s = f"{x:,.{nd}f}".replace(",", " ") if nd == 0 else f"{x:,.{nd}f}".replace(",", " ").replace(".", ",")
    return s.replace(" ", " ")


def dec(x, nd=1) -> str:
    """Nombre avec virgule décimale française."""
    return f"{x:.{nd}f}".replace(".", ",")


def pace_from_hhmm(target: str, route_len_m: float):
    """Retourne (libellé min/km, secondes/km) à partir d'un temps total HH:MM."""
    try:
        h, m = (int(part) for part in str(target).strip().split(":", 1))
        if h < 0 or not 0 <= m < 60 or route_len_m <= 0:
            return "X", None
        seconds_per_km = (h * 3600 + m * 60) / (route_len_m / 1000.0)
        total = int(round(seconds_per_km))
        return f"{total // 60}:{total % 60:02d}", seconds_per_km
    except (TypeError, ValueError, ZeroDivisionError):
        return "X", None


def format_pace_seconds(seconds_per_km: float) -> str:
    total = max(0, int(round(float(seconds_per_km))))
    return f"{total // 60}:{total % 60:02d}"


def filled(v, fallback=TODO) -> str:
    v = "" if v is None else str(v).strip()
    return v if v else fallback


# ======================================================================================
# Styles
# ======================================================================================
def make_styles():
    base = dict(fontName="Helvetica", textColor=INK, leading=13.2, fontSize=9.2)
    st = {
        "body": ParagraphStyle("body", **base, spaceAfter=4),
        "small": ParagraphStyle("small", **{**base, "fontSize": 7.8, "leading": 10, "textColor": MUTED}),
        "cell": ParagraphStyle("cell", **{**base, "fontSize": 8.2, "leading": 10.4}),
        "cell_r": ParagraphStyle("cell_r", **{**base, "fontSize": 8.2, "leading": 10.4}, alignment=TA_RIGHT),
        "cell_h": ParagraphStyle("cell_h", **{**base, "fontSize": 7.9, "leading": 10, "fontName": "Helvetica-Bold"}),
        "cell_hr": ParagraphStyle("cell_hr", **{**base, "fontSize": 7.9, "leading": 10, "fontName": "Helvetica-Bold"}, alignment=TA_RIGHT),
        "h1": ParagraphStyle("h1", **{**base, "fontName": "Helvetica-Bold", "fontSize": 17, "leading": 21, "spaceBefore": 4, "spaceAfter": 6}),
        "h2": ParagraphStyle("h2", **{**base, "fontName": "Helvetica-Bold", "fontSize": 11, "leading": 14, "spaceBefore": 9, "spaceAfter": 3}),
        "title": ParagraphStyle("title", **{**base, "fontName": "Helvetica-Bold", "fontSize": 34, "leading": 38}),
        "subtitle": ParagraphStyle("subtitle", **{**base, "fontSize": 17, "leading": 22, "textColor": MUTED}),
        "kpi": ParagraphStyle("kpi", **{**base, "fontName": "Helvetica-Bold", "fontSize": 18, "leading": 22}),
        "kpil": ParagraphStyle("kpil", **{**base, "fontSize": 7.8, "leading": 10, "textColor": MUTED}),
        "note": ParagraphStyle("note", **{**base, "fontSize": 8.6, "leading": 11.6}),
        "toc": ParagraphStyle("toc", **{**base, "fontSize": 10.5, "leading": 18}),
    }
    return st


# ======================================================================================
# Éléments de mise en page
# ======================================================================================
def section_title(styles, number: int, title: str):
    return [HRFlowable(width=26 * mm, thickness=3, color=ACCENT, hAlign="LEFT", spaceBefore=0, spaceAfter=5),
            Paragraph(f"{number}. {esc(title)}", styles["h1"])]


def note_box(styles, html: str, accent=ACCENT):
    t = Table([[Paragraph(html, styles["note"])]], colWidths=[CONTENT_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fafafa")), ("LINEBEFORE", (0, 0), (0, -1), 2.2, accent),
        ("BOX", (0, 0), (-1, -1), 0.4, LINE), ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def data_table(styles, header, rows, widths, right=(), repeat=1, empty_text=None):
    """Tableau sobre : en-tête gris clair, filets fins, retour à la ligne automatique."""
    if not rows:
        return note_box(styles, esc(empty_text or "Aucun élément."), accent=MUTED)
    head = [Paragraph(esc(h), styles["cell_hr"] if i in right else styles["cell_h"]) for i, h in enumerate(header)]
    body = []
    for r in rows:
        body.append([c if not isinstance(c, (str, int, float)) else
                     Paragraph(esc(c), styles["cell_r"] if i in right else styles["cell"]) for i, c in enumerate(r)])
    total = float(sum(widths))
    cw = [CONTENT_W * w / total for w in widths]
    t = Table([head] + body, colWidths=cw, repeatRows=repeat)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), SOFT), ("LINEBELOW", (0, 0), (-1, 0), 1.1, ACCENT),
        ("LINEBELOW", (0, 1), (-1, -1), 0.35, LINE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5)]))
    return t


def kv_table(styles, pairs, widths=(34, 66)):
    rows = [[Paragraph(f"<b>{esc(k)}</b>", styles["cell"]), Paragraph(esc(v), styles["cell"])] for k, v in pairs]
    t = Table(rows, colWidths=[CONTENT_W * widths[0] / 100.0, CONTENT_W * widths[1] / 100.0])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.35, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                           ("LEFTPADDING", (0, 0), (-1, -1), 5), ("BACKGROUND", (0, 0), (0, -1), SOFT)]))
    return t


def kpi_row(styles, items):
    n = len(items)
    cells = [Table([[Paragraph(esc(v), styles["kpi"])], [Paragraph(esc(l), styles["kpil"])]], colWidths=[CONTENT_W / n - 6]) for v, l in items]
    t = Table([cells], colWidths=[CONTENT_W / n] * n)
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, LINE), ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
                           ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                           ("LEFTPADDING", (0, 0), (-1, -1), 8), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


def png_flowable(png: bytes, width=CONTENT_W, max_h=None):
    w, h = PILImage.open(io.BytesIO(png)).size
    height = width * h / float(w)
    if max_h and height > max_h:
        width, height = width * max_h / height, max_h
    img = Image(io.BytesIO(png), width=width, height=height)
    img.hAlign = "CENTER"
    return img


# ======================================================================================
# Cartes et graphiques (matplotlib, sans fond de carte : fonctionne hors ligne)
# ======================================================================================
def _fig_png(fig, dpi=190) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _km_ticks(route_xy, every_m):
    seg = np.linalg.norm(np.diff(route_xy, axis=0), axis=1)
    cum = np.concatenate(([0.0], np.cumsum(seg)))
    out = []
    d = every_m
    while d < cum[-1] - every_m * 0.25:
        i = int(min(np.searchsorted(cum, d, side="right") - 1, len(seg) - 1))
        f = (d - cum[i]) / max(seg[i], 1e-9)
        out.append((d, route_xy[i] + f * (route_xy[i + 1] - route_xy[i])))
        d += every_m
    return out, float(cum[-1])


def render_map(route_xy, origin, objects, kinds=None, meso=None, label_km=False, size=(7.4, 5.4), title=None) -> bytes:
    """Carte schématique du parcours : tracé, kilomètres, équipements, zones de densité."""
    route_xy = np.asarray(route_xy, dtype=float)
    fig, ax = plt.subplots(figsize=size)
    ax.set_aspect("equal")
    ax.plot(route_xy[:, 0], route_xy[:, 1], color="#cbd5e1", lw=7.5, solid_capstyle="round", zorder=1)
    ax.plot(route_xy[:, 0], route_xy[:, 1], color="#1f2937", lw=1.7, solid_capstyle="round", zorder=3)
    if meso is not None:
        seg = np.linalg.norm(np.diff(route_xy, axis=0), axis=1)
        cum = np.concatenate(([0.0], np.cumsum(seg)))
        for lo, hi, col in ((0.3, 0.5, "#f59e0b"), (0.5, 99, "#ef4444")):
            mask = (meso.rho_max_cell > lo) & (meso.rho_max_cell <= hi)
            for c in np.where(mask)[0]:
                a, b = c * meso.cell_m, (c + 1) * meso.cell_m
                idx = np.where((cum >= a) & (cum <= b))[0]
                if len(idx) >= 2:
                    ax.plot(route_xy[idx, 0], route_xy[idx, 1], color=col, lw=5.2, solid_capstyle="butt", zorder=2)
    length = total_len(route_xy)
    ticks, _ = _km_ticks(route_xy, 5000.0 if length > 12000 else 1000.0 if length < 4000 else 2000.0)
    for d, p in ticks:
        ax.scatter([p[0]], [p[1]], s=16, color="white", edgecolor="#6b7280", lw=0.8, zorder=4)
        ax.annotate(f"{d / 1000:g}", (p[0], p[1]), textcoords="offset points", xytext=(5, 5), fontsize=6.6, color="#4b5563", zorder=6)
    ax.scatter([route_xy[0, 0]], [route_xy[0, 1]], marker="s", s=84, color="#16a34a", edgecolor="white", lw=1, zorder=7)
    ax.annotate("Départ", (route_xy[0, 0], route_xy[0, 1]), textcoords="offset points", xytext=(7, -11), fontsize=7.4, fontweight="bold", color="#166534", zorder=8)
    ax.scatter([route_xy[-1, 0]], [route_xy[-1, 1]], marker="s", s=84, color="#111827", edgecolor="white", lw=1, zorder=7)
    ax.annotate("Arrivée", (route_xy[-1, 0], route_xy[-1, 1]), textcoords="offset points", xytext=(7, 6), fontsize=7.4, fontweight="bold", color="#111827", zorder=8)
    seen = {}
    for o in objects:
        k = o["kind"]
        if k == "volunteer": # Exclure les bénévoles de la carte synthétique pour garantir une visibilité correcte
            continue
        if kinds and k not in kinds:
            continue
        mk, col, sz = KIND_STYLE[k]
        x, y = sc.to_local(o["lat"], o["lon"], origin)
        h = ax.scatter([x], [y], marker=mk, s=sz, color=col, edgecolor="white", lw=0.9, zorder=9)
        seen.setdefault(k, h)
        if label_km:
            ax.annotate(f"km {o['progress_m'] / 1000:.1f}".replace(".", ","), (x, y), textcoords="offset points", xytext=(6, -10),
                        fontsize=6.4, color=col, zorder=10)
    handles, labels = [], []
    for k, h in seen.items():
        handles.append(h)
        labels.append(sc.KINDS[k]["label"])
    if meso is not None:
        from matplotlib.lines import Line2D
        handles += [Line2D([0], [0], color="#f59e0b", lw=4), Line2D([0], [0], color="#ef4444", lw=4)]
        labels += ["Densité 0,3 à 0,5 coureur/m²", "Densité supérieure à 0,5 coureur/m²"]
    if handles:
        ax.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, -0.02), ncol=3, frameon=False, fontsize=7.2, handletextpad=0.4,
                  columnspacing=1.4)
    # barre d'échelle et nord
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    span = x1 - x0
    nice = next((v for v in (50, 100, 200, 250, 500, 1000, 2000, 5000) if v >= span * 0.12), 5000)
    ax.plot([x0 + span * 0.02, x0 + span * 0.02 + nice], [y0 + (y1 - y0) * 0.03] * 2, color="#374151", lw=2.2, zorder=11)
    ax.annotate(f"{nice} m" if nice < 1000 else f"{nice // 1000} km", (x0 + span * 0.02, y0 + (y1 - y0) * 0.05), fontsize=6.8, color="#374151")
    ax.annotate("N", (x1 - span * 0.04, y1 - (y1 - y0) * 0.1), fontsize=9, fontweight="bold", ha="center", color="#374151")
    ax.annotate("", (x1 - span * 0.04, y1 - (y1 - y0) * 0.02), (x1 - span * 0.04, y1 - (y1 - y0) * 0.085),
                arrowprops=dict(arrowstyle="-|>", color="#374151", lw=1.2))
    ax.margins(0.06)
    ax.axis("off")
    if title:
        ax.set_title(title, fontsize=9, loc="left", color="#1f2937", fontweight="bold")
    return _fig_png(fig)


def total_len(route_xy) -> float:
    return float(np.linalg.norm(np.diff(np.asarray(route_xy), axis=0), axis=1).sum())


def render_density_profile(meso, objects, size=(7.4, 2.5)) -> bytes:
    n = min(240, meso.n_cells)
    vals = np.array(meso.profile_max(n))
    km = (np.arange(n) + 0.5) / n * meso.route_length_m / 1000.0
    fig, ax = plt.subplots(figsize=size)
    ax.fill_between(km, vals, color="#fecaca", alpha=0.9, lw=0)
    ax.plot(km, vals, color="#ef4444", lw=1.3)
    for y, lab, col in ((0.3, "0,3", "#f59e0b"), (0.5, "0,5", "#dc2626")):
        ax.axhline(y, color=col, lw=0.9, ls="--")
        ax.annotate(f"{lab} coureur/m²", (km[-1], y), xytext=(-2, 3), textcoords="offset points", ha="right", fontsize=6.6, color=col)
    for o in objects:
        if o["kind"] == "water":
            ax.axvline(o["progress_m"] / 1000.0, color="#1d4ed8", lw=0.7, alpha=0.6)
    ax.set_xlim(0, km[-1])
    ax.set_ylim(0, max(0.7, float(vals.max()) * 1.15))
    ax.set_xlabel("Distance depuis le départ (km)", fontsize=7.6)
    ax.set_ylabel("Densité maximale (coureurs/m²)", fontsize=7.6)
    ax.tick_params(labelsize=7)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", color="#e5e9ef", lw=0.6)
    ax.set_title("Densité maximale par tronçon (traits bleus : points d'eau)", fontsize=8.2, loc="left", color="#1f2937")
    return _fig_png(fig)


def render_coverage(med, size=(7.4, 2.4)) -> bytes:
    fig, ax = plt.subplots(figsize=size)
    ax.plot(med.coverage_km, np.asarray(med.coverage_s) / 60.0, color="#dc2626", lw=1.5)
    ax.axhline(med.target_s / 60.0, color="#374151", ls="--", lw=1)
    ax.annotate(f"cible {med.target_s / 60:.0f} min", (med.coverage_km[-1], med.target_s / 60.0), xytext=(-2, 3),
                textcoords="offset points", ha="right", fontsize=7, color="#374151")
    for p in med.posts:
        ax.axvline(p["pos_m"] / 1000.0, color="#6b7280", lw=0.8, ls=":")
    ax.set_xlim(0, float(med.coverage_km[-1]))
    ax.set_ylim(bottom=0)
    ax.set_xlabel("Position d'un incident sur le parcours (km)", fontsize=7.6)
    ax.set_ylabel("Délai d'intervention (min)", fontsize=7.6)
    ax.tick_params(labelsize=7)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", color="#e5e9ef", lw=0.6)
    ax.set_title("Délai d'intervention selon le lieu de l'incident (traits pointillés : postes de secours)", fontsize=8.2, loc="left", color="#1f2937")
    return _fig_png(fig)


# ======================================================================================
# Calculs à partir de la simulation et de la carte
# ======================================================================================
def passage_windows(meso):
    """Pour chaque cellule : instant d'arrivée de la tête et de départ de la queue (s depuis le 1er départ)."""
    present = meso.rho_t > 0.004
    first = np.full(meso.n_cells, np.nan)
    last = np.full(meso.n_cells, np.nan)
    any_p = present.any(axis=0)
    idx_first = present.argmax(axis=0)
    idx_last = present.shape[0] - 1 - present[::-1].argmax(axis=0)
    first[any_p] = meso.times_s[idx_first[any_p]]
    last[any_p] = meso.times_s[idx_last[any_p]]
    return first, last


def closure_sections(meso, route_len_m, start, margin_min, reopen_min):
    first, last = passage_windows(meso)
    L_km = route_len_m / 1000.0
    for seg_km in (1, 2, 2.5, 5, 10):
        if L_km / seg_km <= 9:
            break
    rows, a = [], 0.0
    while a < route_len_m - 1:
        b = min(a + seg_km * 1000.0, route_len_m)
        ca, cb = int(a // meso.cell_m), max(int(a // meso.cell_m) + 1, int(math.ceil(b / meso.cell_m)))
        f, l = first[ca:cb], last[ca:cb]
        if np.isfinite(f).any():
            head, tail = float(np.nanmin(f)), float(np.nanmax(l))
            rows.append({"a": a, "b": b, "head": head, "tail": tail,
                         "close": head - margin_min * 60.0, "open": tail + reopen_min * 60.0})
        a = b
    return rows


def convergence_points(route_xy, min_gap_m=300.0, radius_m=25.0):
    """Points où le tracé repasse près de lui-même (croisement, chevauchement, boucle) à plus de `min_gap_m` d'écart de parcours."""
    route_xy = np.asarray(route_xy, dtype=float)
    seg = np.linalg.norm(np.diff(route_xy, axis=0), axis=1)
    cum = np.concatenate(([0.0], np.cumsum(seg)))
    n = int(cum[-1] // 10.0) + 1
    s = np.linspace(0, cum[-1], n)
    xs, ys = np.interp(s, cum, route_xy[:, 0]), np.interp(s, cum, route_xy[:, 1])
    pts = np.column_stack([xs, ys])
    hits = []
    step = max(1, n // 4000)
    for i in range(0, n, step):
        d = np.hypot(pts[:, 0] - pts[i, 0], pts[:, 1] - pts[i, 1])
        far = np.abs(s - s[i]) > min_gap_m
        j = np.where((d < radius_m) & far & (s > s[i]))[0]
        if len(j):
            jj = j[np.argmin(d[j])]
            hits.append((i, jj))
    clusters = []
    for i, j in hits:
        if clusters and i - clusters[-1]["i1"] <= 60 and abs(j - clusters[-1]["j1"]) <= 60 + 2 * 60:
            c = clusters[-1]
            c["i1"], c["j1"] = i, j
        else:
            clusters.append({"i0": i, "i1": i, "j0": j, "j1": j})
    out = []
    for c in clusters[:8]:
        i = (c["i0"] + c["i1"]) // 2
        j = (c["j0"] + c["j1"]) // 2
        out.append({"km_a": s[i] / 1000.0, "km_b": s[j] / 1000.0, "len_m": max(10.0, (c["i1"] - c["i0"]) * 10.0),
                    "x": float(pts[i, 0]), "y": float(pts[i, 1])})
    return out


def critical_sections(meso, min_rho=0.3, top=6):
    """Tronçons contigus où la densité maximale dépasse `min_rho`, du plus chargé au moins chargé."""
    over = meso.rho_max_cell > min_rho
    out, c, n = [], 0, meso.n_cells
    while c < n:
        if over[c]:
            c0 = c
            while c < n and over[c]:
                c += 1
            seg = slice(c0, c)
            k = c0 + int(np.argmax(meso.rho_max_cell[seg]))
            out.append({"a": c0 * meso.cell_m, "b": min(c * meso.cell_m, meso.route_length_m), "peak": float(meso.rho_max_cell[k]),
                        "t_peak": float(meso.t_rho_max_cell[k]), "min05": float(np.max(meso.minutes_above_05_cell[seg])),
                        "min03": float(np.max(meso.minutes_above_03_cell[seg]))})
        else:
            c += 1
    out.sort(key=lambda r: -r["peak"])
    return out[:top]


def kind_objects(objects, kind):
    return sorted([o for o in objects if o["kind"] == kind], key=lambda o: o["progress_m"])


# ======================================================================================
# Pages
# ======================================================================================
class NumberedCanvas(rl_canvas.Canvas):
    """Canevas qui connaît le nombre total de pages (pied de page « Page X / N »)."""
    header_left = ""
    header_right = ""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._decorate(total)
            super().showPage()
        super().save()

    def _decorate(self, total):
        n = self._pageNumber
        if n == 1:
            return
        self.setStrokeColor(LINE)
        self.setLineWidth(0.5)
        self.line(MARGIN_X, PAGE_H - 15 * mm, PAGE_W - MARGIN_X, PAGE_H - 15 * mm)
        self.setFont("Helvetica-Bold", 7.6)
        self.setFillColor(INK)
        self.drawString(MARGIN_X, PAGE_H - 12.2 * mm, "PLAN OPÉRATIONNEL")
        self.setFont("Helvetica", 7.6)
        self.setFillColor(MUTED)
        self.drawString(MARGIN_X + 33 * mm, PAGE_H - 12.2 * mm, self.header_left)
        self.drawRightString(PAGE_W - MARGIN_X, PAGE_H - 12.2 * mm, self.header_right)
        self.line(MARGIN_X, 14 * mm, PAGE_W - MARGIN_X, 14 * mm)
        self.setFont("Helvetica", 6.8)
        self.drawString(MARGIN_X, 10 * mm, "Document d'aide à la préparation, à valider avec la préfecture, les forces de l'ordre et les secours.")
        self.drawRightString(PAGE_W - MARGIN_X, 10 * mm, f"Page {n} / {total}")


def build_plan_pdf(ctx: dict) -> bytes:
    """Construit le Plan Opérationnel. `ctx` : voir les clés lues ci-dessous (fournies par app.py)."""
    info = ctx["info"]
    result = ctx["result"]
    meso = result["meso"]
    settings = result["settings"]
    objects = result["objects"]
    route_xy = np.asarray(ctx["route_local"], dtype=float)
    origin = ctx["origin"]
    route_len_m = float(result["route_length_m"])
    st = make_styles()

    event_date = date.fromisoformat(settings["event_date"])
    start = parse_hhmm(settings["start_time"])
    cutoff = parse_hhmm(settings.get("cutoff_time_hh_mm", "03:00"))
    name = filled(info.get("event_name"), "Épreuve de course à pied")
    margin = float(info.get("closure_margin_min", 15))
    reopen = float(info.get("reopen_delay_min", 10))
    findings = result.get("findings") or []
    meta = result.get("rules_meta") or {}
    story = []

    # -------------------------------------------------------------- couverture
    story += [Spacer(1, 38 * mm),
              HRFlowable(width=34 * mm, thickness=5, color=ACCENT, hAlign="LEFT", spaceAfter=10),
              Paragraph("PLAN OPÉRATIONNEL", st["title"]), Spacer(1, 4),
              Paragraph(esc(name), st["subtitle"]), Spacer(1, 3),
              Paragraph(esc(f"{fr_date(event_date)} · départ à {clock(start, 0)}"), st["subtitle"]), Spacer(1, 16 * mm)]
    story.append(kpi_row(st, [
        (f"{dec(route_len_m / 1000)} km", "Distance du parcours"),
        (fmt_num(settings["expected_runners"]), "Partants attendus"),
        (f"{settings['waves']} vagues", f"intervalle {settings['wave_gap_s']} s"),
        (f"{cutoff.seconds // 3600} h {cutoff.seconds % 3600 // 60:02d}", "Barrière horaire")]))
    story.append(Spacer(1, 10 * mm))
    story.append(kv_table(st, [
        ("Organisateur", filled(info.get("organizer"))),
        ("Responsable sécurité et parcours", filled(info.get("safety_lead"))),
        ("Poste de commandement (PC course)", filled(info.get("pc_phone"))),
        ("Départ / arrivée", f"{filled(info.get('start_place'))} / {filled(info.get('finish_place'))}"),
        ("Arrêté de circulation", filled(info.get("prefecture_ref"))),
        ("Document généré le", fr_date(date.today())),
    ]))
    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph("Sommaire", st["h2"]))
    for i, t in enumerate(["Synthèse et vue d'ensemble", "Fermetures de routes et circulation", "Niveaux de personnel et bénévoles",
                           "Points d'eau et rafraîchissement", "Scénarios de repli et modifications de tracé", "Couverture médias",
                           "Flux, intersections et points de convergence", "Déploiement des services d'urgence",
                           "Conformité réglementaire et hypothèses"], 1):
        story.append(Paragraph(f"<font color='#ff4a4a'><b>{i}</b></font>&nbsp;&nbsp;&nbsp;{esc(t)}", st["toc"]))
    story.append(PageBreak())

    # -------------------------------------------------------------- 1. Synthèse
    conv = convergence_points(route_xy)
    crit = critical_sections(meso, 0.3)
    peak_rho, peak_km, peak_t = meso.peak()
    sm = result["summary_meso"]
    story += section_title(st, 1, "Synthèse et vue d'ensemble")
    n_by = {k: len(kind_objects(objects, k)) for k in sc.KINDS}
    vol_total = int(sum(round(o["p1"]) for o in objects if o["kind"] == "volunteer"))
    story.append(Paragraph(
        f"L'épreuve réunit <b>{fmt_num(settings['expected_runners'])} partants</b> sur <b>{dec(route_len_m / 1000)} km</b> "
        f"({settings['waves']} vagues, départ à {clock(start, 0)}, barrière horaire à {cutoff.seconds // 3600} h {cutoff.seconds % 3600 // 60:02d}). "
        f"Le dispositif posé sur la carte comprend {n_by['water']} point(s) d'eau, {n_by['sponge']} poste(s) d'épongeage, {n_by['toilets']} site(s) de toilettes, "
        f"{n_by['medical']} poste(s) de secours, {n_by['doctor']} poste(s) médecin, {n_by['ambulance']} ambulance(s) et {vol_total} bénévole(s) placés. "
        f"La simulation situe la densité maximale à <b>{dec(peak_rho, 2)} coureur/m²</b> au km {dec(peak_km)} ({clock(start, peak_t)}).",
        st["body"]))
    story.append(Spacer(1, 2))
    story.append(png_flowable(render_map(route_xy, origin, objects, meso=meso, size=(7.4, 5.6)), max_h=150 * mm))
    story.append(Paragraph("Carte schématique du parcours : kilomètres, équipements posés et tronçons où la densité simulée dépasse 0,3 coureur/m².", st["small"]))
    story.append(PageBreak())

    # -------------------------------------------------------------- 2. Fermetures
    sections = closure_sections(meso, route_len_m, start, margin, reopen)
    pw_first, pw_last = passage_windows(meso)
    story += section_title(st, 2, "Fermetures de routes et circulation")
    story.append(Paragraph(
        "Les créneaux ci-dessous sont déduits du passage simulé de la tête et de la queue de course sur chaque tronçon. Le règlement demande, "
        "dans la mesure du possible, d'interdire la circulation sur le parcours ; à défaut, l'organisateur informe son assureur et les usagers de la voie publique, "
        "et la priorité aux carrefours suppose des signaleurs agréés, en poste au moins un quart d'heure avant le passage et retirés après le véhicule balai "
        "(art. III.A.6.2, p. 19 du règlement).", st["body"]))
    story.append(Spacer(1, 3))
    story.append(kv_table(st, [
        ("Mode de fermeture", filled(info.get("closure_mode"))),
        ("Marge avant le passage de la tête", f"{margin:g} min (signaleurs et barrières en place)"),
        ("Délai après le dernier coureur / balai", f"{reopen:g} min avant réouverture"),
        ("Arrêté / autorité", filled(info.get("prefecture_ref"))),
        ("Précisions de l'organisateur", filled(info.get("closure_notes"), "Aucune précision saisie.")),
    ]))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Créneaux de fermeture par tronçon", st["h2"]))
    rows = [[f"km {fmt_km(s['a'])} à {fmt_km(s['b'])}", clock(start, s["close"]), clock(start, s["head"]), clock(start, s["tail"]),
             clock(start, s["open"]), f"{(s['open'] - s['close']) / 60:.0f} min"] for s in sections]
    story.append(data_table(st, ["Tronçon", "Fermeture", "Passage de la tête", "Passage de la queue", "Réouverture", "Durée"],
                            rows, [22, 14, 17, 17, 16, 13], right=(5,)))
    story.append(Paragraph(
        "Hypothèse : la queue de course est le dernier coureur simulé, suivi du véhicule balai. Les heures sont à confronter aux horaires de l'arrêté "
        "et à la barrière horaire effectivement appliquée.", st["small"]))
    story.append(Paragraph("Signalisation et véhicules", st["h2"]))
    story.append(data_table(st, ["Élément", "Prescription (règlement art. III.A.6.2, p. 19)"], [
        ["Signaleurs", "Majeurs, titulaires du permis de conduire, agréés par l'autorité et nommés dans l'arrêté ; gilet jaune haute visibilité ; copie de l'arrêté sur eux."],
        ["Signalisation manuelle", "Piquet K10 (face rouge/verte), un par signaleur si deux à un carrefour ; un panneau AB4 ou R24 recto-verso si un signaleur seul sur une traversée."],
        ["Véhicules de course", "Voiture ouvreuse avec panneau de début de course et voiture-balai avec panneau de fin de course."],
        ["Barrages", "Barrages K2 « Course » possibles pour pré-signaler un obstacle temporaire."],
    ], [24, 76]))
    story.append(PageBreak())

    # -------------------------------------------------------------- 3. Personnel
    story += section_title(st, 3, "Niveaux de personnel et bénévoles")
    story.append(Paragraph(
        "Effectifs posés sur la carte, comparés à une estimation indicative. L'estimation applique des ratios simples (voir les hypothèses en fin de document) "
        "et ne remplace ni le relevé terrain des carrefours ni l'avis des autorités sur le nombre de signaleurs.", st["body"]))
    staff_rows, tot_set, tot_rec = [], 0, 0
    for o in kind_objects(objects, "volunteer"):
        n = int(round(o["p1"]))
        staff_rows.append([o["name"], f"km {fmt_km(o['progress_m'])}", "Bénévoles de poste (rayon " + f"{o['p2']:.0f} m)", str(n), str(n)])
        tot_set += n
        tot_rec += n
    for o in kind_objects(objects, "water") + kind_objects(objects, "sponge"):
        slots = max(1, int(round(o["p1"])))
        rec = slots + max(1, math.ceil(slots / 5))
        staff_rows.append([o["name"], f"km {fmt_km(o['progress_m'])}", f"{sc.KINDS[o['kind']]['label']} : service + réassort", "-", str(rec)])
        tot_rec += rec
    for o in kind_objects(objects, "toilets"):
        staff_rows.append([o["name"], f"km {fmt_km(o['progress_m'])}", "Toilettes : orientation et entretien", "-", "1"])
        tot_rec += 1
    n_conv = len(conv)
    sig = 2 * n_conv
    if n_conv:
        staff_rows.append(["Points de convergence du tracé", f"{n_conv} point(s)", "Signaleurs (2 par point)", "-", str(sig)])
        tot_rec += sig
    staff_rows.append(["Zones de départ et d'arrivée", "km 0 et arrivée", "Accueil, sas, barrières, remise des médailles", "-", "6"])
    tot_rec += 6
    staff_rows.append(["Véhicules ouvreur et balai", "tête / queue", "Conducteurs-signaleurs", "-", "2"])
    tot_rec += 2
    staff_rows.append(["TOTAL", "", "", str(tot_set), str(tot_rec)])
    story.append(data_table(st, ["Poste", "Position", "Fonction", "Posé (carte)", "Estimé"], staff_rows, [26, 14, 38, 11, 11], right=(3, 4)))
    story.append(Spacer(1, 4))
    story.append(note_box(st, "<b>Signaleurs aux intersections avec la voie publique :</b> leur nombre dépend du relevé des carrefours, que l'application ne possède pas. "
                              "Il est fixé « en concertation avec les services concernés » (art. III.A.6.2). Complétez la liste nominative pour l'arrêté."))
    story.append(PageBreak())

    # -------------------------------------------------------------- 4. Points d'eau
    story += section_title(st, 4, "Points d'eau et rafraîchissement")
    waters = kind_objects(objects, "water")
    stats = {s["name"]: s for s in (meso.station_stats or [])}
    wrows = []
    for o in waters:
        lat, lon = o["lat"], o["lon"]
        s = stats.get(o["name"], {})
        wrows.append([o["name"], f"km {fmt_km(o['progress_m'])}", f"{lat:.5f}, {lon:.5f}".replace(".", ","), f"{int(round(o['p1']))}",
                      fmt_num(s.get("users", 0)) if s else "-", f"{s.get('max_queue', 0):.0f}" if s else "-",
                      f"{s.get('mean_wait_s', 0):.0f} s" if s else "-"])
    if not waters:
        story.append(note_box(st, "Aucun point d'eau posé sur la carte : la simulation a utilisé des points d'eau automatiques (« " + esc(settings.get("water_source", "")) + " »)."))
    else:
        story.append(png_flowable(render_map(route_xy, origin, objects, kinds={"water", "sponge"}, label_km=True, size=(7.4, 4.6)), max_h=110 * mm))
        story.append(Paragraph("Emplacement des points d'eau (bleu foncé) et des postes d'épongeage (cyan).", st["small"]))
        story.append(Spacer(1, 4))
        story.append(data_table(st, ["Point d'eau", "Position", "Coordonnées GPS", "Postes", "Coureurs servis", "File max", "Attente moy."],
                                wrows, [20, 12, 22, 9, 14, 10, 13], right=(3, 4, 5, 6)))
    sponges = kind_objects(objects, "sponge")
    if sponges:
        story.append(Paragraph("Postes d'épongeage et de rafraîchissement", st["h2"]))
        story.append(data_table(st, ["Poste", "Position", "Coordonnées GPS", "Postes de service"],
                                [[o["name"], f"km {fmt_km(o['progress_m'])}", f"{o['lat']:.5f}, {o['lon']:.5f}".replace(".", ","), f"{int(round(o['p1']))}"] for o in sponges],
                                [30, 16, 34, 20], right=(3,)))
    story.append(Spacer(1, 4))
    story.append(Paragraph("Les files d'attente simulées signalent un point d'eau sous-dimensionné : une file qui se forme impose d'ajouter des postes de service ou "
                           "un point supplémentaire en amont.", st["small"]))
    story.append(PageBreak())

    # -------------------------------------------------------------- 5. Repli
    story += section_title(st, 5, "Scénarios de repli et modifications de tracé")
    t0 = settings.get("temperature_start_c")
    rise = settings.get("temperature_rise_c_per_hour")
    t_head = (meso.finish_s[np.isfinite(meso.finish_s)].min() / 3600.0) if np.isfinite(meso.finish_s).any() else 0
    story.append(Paragraph(
        f"Hypothèse météo de la simulation : {t0} °C au départ, +{dec(rise)} °C par heure (soit environ {t0 + rise * t_head:.0f} °C à l'arrivée du premier). "
        + "Le règlement impose d'informer les participants de conditions exceptionnelles (température élevée ou très basse, orage, fortes précipitations) "
          "et de consulter un service météorologique reconnu (art. III.A.6.1, p. 19). Les seuils ci-dessous sont ceux saisis par l'organisateur.", st["body"]))
    scen = info.get("contingency") or []
    srows = [[filled(r.get("level"), "-"), filled(r.get("trigger")), filled(r.get("action")), filled(r.get("decision"))] for r in scen
             if any(str(r.get(k, "")).strip() for k in ("level", "trigger", "action", "decision"))]
    story.append(data_table(st, ["Niveau", "Déclencheur", "Mesures et modification de tracé", "Décision / diffusion"], srows,
                            [10, 28, 38, 24], empty_text="Aucun scénario de repli saisi : à compléter avant dépôt du dossier."))
    story.append(Spacer(1, 5))
    story.append(Paragraph("Tracé de repli", st["h2"]))
    story.append(note_box(st, esc(filled(info.get("fallback_route"), "Aucun tracé de repli décrit. Indiquez les tronçons de remplacement, leur longueur et les "
                                                                   "kilomètres concernés (la distance officielle devra être remesurée si le tracé change)."))))
    story.append(Spacer(1, 4))
    story.append(Paragraph("Un changement de tracé en cours de course impose de prévenir les signaleurs, les secours, la chronométrie et les coureurs, et de "
                           "revoir les créneaux de fermeture de la section 2.", st["small"]))
    story.append(PageBreak())

    # -------------------------------------------------------------- 6. Médias
    story += section_title(st, 6, "Couverture médias")
    media_pairs = [
        ("Responsable presse / contact", filled(info.get("media_contact"))),
        ("Zone presse", filled(info.get("media_zone"))),
        ("Véhicules presse accrédités", str(int(info.get("media_vehicles", 0))) if info.get("media_vehicles") else "Aucun / à définir"),
        ("Motos caméra", str(int(info.get("media_moto", 0))) if info.get("media_moto") else "Aucune / à définir"),
        ("Drone", "Oui (déclaration préalable obligatoire)" if info.get("media_drone") else "Non"),
        ("Hélicoptère", "Oui" if info.get("media_heli") else "Non"),
        ("Diffusion en direct", "Oui" if info.get("media_live") else "Non"),
    ]
    story.append(kv_table(st, media_pairs))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Points de captation", st["h2"]))
    pts = []
    for tok in str(info.get("media_points", "")).replace(";", ",").split(","):
        tok = tok.strip().replace(",", ".")
        try:
            pts.append(float(tok))
        except ValueError:
            continue
    if not pts:
        pts = [0.0, route_len_m / 1000.0]
        pts_note = "Points proposés par défaut : départ et arrivée. Saisissez les kilomètres de vos points de prise de vue."
    else:
        pts_note = "Points saisis par l'organisateur."
    prow = []
    for km in pts:
        c = int(min(max(km * 1000.0 // meso.cell_m, 0), meso.n_cells - 1))
        rho = float(meso.rho_max_cell[c])
        flag = "Zone très dense : éviter tout stationnement" if rho > 0.5 else "Zone dense : vigilance" if rho > 0.3 else "Fluide"
        prow.append([f"km {km:.1f}".replace(".", ","), clock(start, float(np.nan_to_num(pw_first[c]))),
                     clock(start, float(np.nan_to_num(pw_last[c]))), f"{rho:.2f}".replace(".", ","), flag])
    story.append(data_table(st, ["Position", "Tête de course", "Queue de course", "Densité max.", "Observation"], prow, [14, 16, 16, 14, 40], right=(3,)))
    story.append(Paragraph(pts_note + " Les heures de passage viennent de la simulation.", st["small"]))
    story.append(Spacer(1, 4))
    story.append(note_box(st, "Les véhicules et motos des médias circulent sous la responsabilité de l'organisateur : accréditation nominative, consignes de distance à la tête de "
                              "course, interdiction de stationner dans les zones à forte densité et sur les voies d'accès des secours. Le règlement FFA consulté ne fixe pas de règle "
                              "chiffrée sur la couverture médias."))
    story.append(PageBreak())

    # -------------------------------------------------------------- 7. Flux
    story += section_title(st, 7, "Flux, intersections et points de convergence")
    story.append(png_flowable(render_density_profile(meso, objects), max_h=70 * mm))
    story.append(Paragraph("Tronçons les plus chargés", st["h2"]))
    crow = []
    for r in crit:
        niveau = "Critique" if r["peak"] > 0.5 else "Vigilance"
        crow.append([f"km {fmt_km(r['a'])} à {fmt_km(r['b'])}", f"{r['peak']:.2f}".replace(".", ","), clock(start, r["t_peak"]),
                     f"{r['min03']:.0f} min", f"{r['min05']:.0f} min", niveau])
    story.append(data_table(st, ["Tronçon", "Densité max.", "Heure du pic", "Durée > 0,3", "Durée > 0,5", "Niveau"], crow, [24, 14, 14, 14, 14, 14],
                            right=(1, 3, 4), empty_text="Aucun tronçon ne dépasse 0,3 coureur/m² dans la simulation."))
    story.append(Paragraph("Seuils : 0,3 coureur/m² (vigilance) et 0,5 coureur/m² (critique), ceux utilisés par l'application ; ils ne sont pas fixés par le règlement.", st["small"]))
    story.append(Paragraph("Points de convergence du tracé", st["h2"]))
    vrow = [[f"Point {i}", f"km {c['km_a']:.1f}".replace(".", ",") + " et km " + f"{c['km_b']:.1f}".replace(".", ","),
             "Départ / arrivée sur une même zone" if c["km_a"] < 0.5 else "Croisement ou chevauchement du tracé",
             "Séparer les flux (barrières, couloirs), 2 signaleurs"] for i, c in enumerate(conv, 1)]
    story.append(data_table(st, ["Repère", "Position sur le parcours", "Nature", "Mesure proposée"], vrow, [10, 26, 30, 34],
                            empty_text="Le tracé ne croise ni ne chevauche aucune de ses parties : aucun point de convergence détecté."))
    story.append(Paragraph("Détection géométrique : portions du tracé à moins de 25 m l'une de l'autre et distantes de plus de 300 m de parcours. Les intersections avec le réseau routier "
                           "ouvert à la circulation ne sont pas connues de l'application.", st["small"]))
    bns = kind_objects(objects, "bottleneck")
    story.append(Paragraph("Rétrécissements, départ et ravitaillements", st["h2"]))
    frow = [[o["name"], f"km {fmt_km(o['progress_m'])}", f"{o['p1']:.1f} m sur {o['p2']:.0f} m".replace(".", ","), "Signaleur, balisage, éviter la sortie de file"] for o in bns]
    story.append(data_table(st, ["Rétrécissement", "Position", "Largeur utile", "Mesure proposée"], frow, [26, 14, 26, 34],
                            empty_text="Aucun rétrécissement posé sur la carte."))
    story.append(Spacer(1, 4))
    release = np.asarray(meso.release_s, dtype=float)
    fin = meso.finish_s[np.isfinite(meso.finish_s)]
    story.append(Paragraph(
        f"<b>Départ :</b> {int(settings['waves'])} vague(s) espacées de {settings['wave_gap_s']} s, zone de départ de {dec(settings['start_width_m'])} m de large, "
        f"dernier coureur libéré à {clock(start, float(np.nanmax(release)))} ({float(np.nanmax(release)) / 60:.0f} min après le coup de feu). "
        f"<b>Arrivée :</b> couloir de {dec(settings['finish_width_m'])} m"
        + (f" ; premier arrivant à {clock(start, float(fin.min()))}." if fin.size else "."), st["body"]))

    # Tableau des vagues configurées dans l'application : répartition, effectifs réels,
    # début théorique de libération et temps médian cible pour chaque vague.
    story.append(Paragraph("Répartition et horaires des vagues", st["h2"]))
    n_waves = max(1, int(settings.get("waves", 1)))
    wave_gap_s = max(0, int(settings.get("wave_gap_s", 0)))
    wave_percentages = list(settings.get("wave_percentages") or [])
    wave_counts = list(settings.get("wave_counts") or [])
    wave_targets = list(settings.get("wave_median_times_hh_mm") or [])

    # Tolère les anciens résultats qui ne contenaient pas encore les paramètres par vague.
    if len(wave_percentages) < n_waves:
        missing = n_waves - len(wave_percentages)
        base, remainder = divmod(100, n_waves)
        wave_percentages = [base + (1 if i < remainder else 0) for i in range(n_waves)]
    if len(wave_counts) < n_waves:
        wave_counts = wave_counts + [0] * (n_waves - len(wave_counts))
    if len(wave_targets) < n_waves:
        wave_targets = wave_targets + ["À préciser"] * (n_waves - len(wave_targets))

    wave_pace_labels = list(settings.get("wave_paces_min_per_km") or [])
    demo_route = settings.get("route_source") == "parcours de démonstration"
    wave_rows = []
    pace_values_for_average = []
    for i in range(n_waves):
        target = str(wave_targets[i] or "À préciser")
        if demo_route:
            calculated_pace, pace_seconds_per_km = "X", None
        else:
            calculated_pace, pace_seconds_per_km = pace_from_hhmm(target, route_len_m)
        # Préfère la valeur enregistrée lors de la simulation ; recalcule pour
        # les anciens résultats qui ne contenaient pas encore ce champ.
        pace_label = ("X" if demo_route else
                      (str(wave_pace_labels[i]) if i < len(wave_pace_labels)
                       and wave_pace_labels[i] else calculated_pace))
        if pace_seconds_per_km is not None:
            pace_values_for_average.append((pace_seconds_per_km, float(wave_percentages[i])))
        wave_rows.append([
            f"Vague {i + 1}",
            clock(start, i * wave_gap_s),
            f"{float(wave_percentages[i]):g} %",
            fmt_num(int(wave_counts[i])),
            target,
            f"{pace_label} min/km" if pace_label != "X" else "X",
        ])
    story.append(data_table(
        st,
        ["Vague", "Début prévu", "Part des partants", "Effectif", "Temps médian cible", "Allure calculée"],
        wave_rows, [12, 14, 16, 13, 23, 22], right=(2, 3),
    ))
    overall_pace_label = None if demo_route else settings.get("reference_pace_min_per_km")
    if not overall_pace_label and pace_values_for_average:
        weight_total = sum(weight for _, weight in pace_values_for_average)
        if weight_total > 0:
            overall_pace_label = format_pace_seconds(
                sum(pace * weight for pace, weight in pace_values_for_average) / weight_total
            )
    if overall_pace_label:
        story.append(Paragraph(
            f"<b>Allure de référence calculée pour l'ensemble des partants :</b> {esc(overall_pace_label)} min/km "
            "(moyenne pondérée par la part de chaque vague).", st["small"]))
    elif demo_route:
        story.append(Paragraph(
            "Allures non calculées : le résultat utilise le parcours de démonstration, "
            "qui n'est pas représentatif d'une course réelle.", st["small"]))
    story.append(Paragraph(
        "Les horaires indiquent le début théorique de chaque vague, calculé à partir de l'intervalle paramétré. "
        "La libération des coureurs peut se prolonger selon la capacité de la ligne de départ. "
        "L'allure est calculée à partir du temps médian cible et de la distance GPX. Les densités, arrêts et files peuvent allonger le temps réellement observé.",
        st["small"]))
    story.append(PageBreak())

    # -------------------------------------------------------------- 8. Urgence
    story += section_title(st, 8, "Déploiement des services d'urgence")
    med = result.get("med")
    story.append(png_flowable(render_map(route_xy, origin, objects, kinds={"medical", "doctor", "ambulance"}, label_km=True, size=(7.4, 4.6)), max_h=105 * mm))
    story.append(Paragraph("Postes de secours, médecins et ambulances posés sur la carte.", st["small"]))
    erow = []
    for k in ("medical", "doctor", "ambulance"):
        for o in kind_objects(objects, k):
            if k == "medical":
                detail = f"{int(round(o['p1']))} équipe(s) de secouristes, mobilisation {o['p2']:.0f} s"
            elif k == "doctor":
                detail = f"{int(round(o['p1']))} médecin(s), {int(round(o['p2']))} infirmier(s)"
            else:
                detail = f"{int(round(o['p1']))} ambulance(s), " + ("évacuation vers un hôpital" if o["p2"] >= 0.5 else "sans transport")
            erow.append([o["name"], sc.KINDS[k]["label"], f"km {fmt_km(o['progress_m'])}", f"{o['lat']:.5f}, {o['lon']:.5f}".replace(".", ","), detail])
    story.append(data_table(st, ["Poste", "Type", "Position", "Coordonnées GPS", "Moyens"], erow, [18, 16, 11, 22, 33],
                            empty_text="Aucun moyen de secours posé sur la carte."))
    story.append(Spacer(1, 5))
    if med is not None:
        story.append(Paragraph("Performance simulée", st["h2"]))
        story.append(kpi_row(st, [
            (f"{med.mean_incidents:.0f}", "Prises en charge attendues"),
            (f"{med.response_p50_s / 60:.1f} min".replace(".", ","), "Intervention médiane"),
            (f"{med.response_p95_s / 60:.1f} min".replace(".", ","), "95 % des cas sous"),
            (f"{med.share_within_target * 100:.0f} %", f"dans les {med.target_s / 60:.0f} min visées")]))
        story.append(Spacer(1, 5))
        story.append(png_flowable(render_coverage(med), max_h=62 * mm))
        story.append(Paragraph(f"Parcours hors du délai visé : {dec(med.uncovered_m / 1000)} km. Modèle : équipes mobiles le long du parcours, incidents tirés selon l'occupation simulée.", st["small"]))
    else:
        story.append(note_box(st, "Aucun poste de secours posé : le délai d'intervention n'a pas pu être simulé."))
    sec_f = [f for f in findings if f.theme.lower().startswith("dispositif de secours") or "secours" in f.theme.lower()]
    if sec_f:
        story.append(Paragraph("Exigences du règlement sur le dispositif de secours", st["h2"]))
        label = {"alert": "Écart", "check": "À vérifier", "ok": "Conforme", "info": "Info"}
        story.append(data_table(st, ["Statut", "Exigence", "Constat", "Réf."], [[label.get(f.status, f.status), f.title, f.detail, f"{f.article}, p. {f.page}"] for f in sec_f],
                                [10, 32, 40, 14]))
    story.append(Spacer(1, 5))
    story.append(kv_table(st, [
        ("Accès des véhicules de secours", filled(info.get("access_points"))),
        ("Alerte", "Tout incident est signalé au PC course, qui alerte les secours (15 SAMU, 18 sapeurs-pompiers, 112 numéro européen). "
                   "Toute manifestation est déclarée au service local d'urgence compétent."),
        ("Hôpital de référence", filled(info.get("hospital"))),
    ]))
    story.append(PageBreak())

    # -------------------------------------------------------------- 9. Conformité & hypothèses
    story += section_title(st, 9, "Conformité réglementaire et hypothèses")
    if findings:
        cnt = {s: sum(1 for f in findings if f.status == s) for s in ("alert", "check", "ok", "info")}
        story.append(Paragraph(
            f"Évaluation au regard du {esc(meta.get('document', 'règlement'))} (règles v{esc(meta.get('rules_version', '?'))}) : "
            f"<b>{cnt['alert']} écart(s)</b>, {cnt['check']} point(s) à vérifier, {cnt['ok']} conforme(s), {cnt['info']} information(s).", st["body"]))
        todo = [f for f in findings if f.status in ("alert", "check")]
        order = {"alert": 0, "check": 1}
        todo.sort(key=lambda f: order[f.status])
        label = {"alert": "Écart", "check": "À vérifier"}
        story.append(data_table(st, ["Statut", "Règle", "Constat et action", "Réf."], [[label[f.status], f.title, (f.detail + (" " + f.recommendation if f.recommendation else "")).strip(), f"{f.article}, p. {f.page}"] for f in todo],
                                [10, 30, 46, 14], empty_text="Aucun écart ni point à vérifier."))
    else:
        story.append(note_box(st, "Aucune évaluation réglementaire disponible pour cette simulation."))
    story.append(Paragraph("Hypothèses et limites", st["h2"]))
    for t in [
        "Les densités, passages et files d'attente viennent d'un modèle mésoscopique à 1 dimension : il décrit un écoulement moyen le long du parcours, pas des trajectoires individuelles.",
        "Les créneaux de fermeture supposent un véhicule balai derrière le dernier coureur simulé ; ils ne tiennent pas compte des abandons ni des retards de départ.",
        "Les effectifs estimés utilisent des ratios simples : service + 1 réassort pour 5 postes de service aux points d'eau et d'épongeage, 1 bénévole par site de toilettes, "
        "2 signaleurs par point de convergence, 6 personnes pour les zones de départ et d'arrivée, 2 pour les véhicules ouvreur et balai.",
        "Les intersections avec le réseau routier, l'altimétrie, la météo réelle et la configuration exacte des secours ne sont pas connues de l'application.",
        "Ce document est une aide à la préparation. Il ne remplace ni l'avis de la FFA, ni l'arrêté de la préfecture, ni l'avis des services de secours.",
    ]:
        story.append(Paragraph("•&nbsp;&nbsp;" + esc(t), st["body"]))

    merged, i = [], 0
    while i < len(story):          # un sous-titre reste toujours collé à l'élément qui le suit
        f = story[i]
        if isinstance(f, _Paragraph) and f.style.name == "h2" and i + 1 < len(story):
            merged.append(KeepTogether([f, story[i + 1]]))
            i += 2
        else:
            merged.append(f)
            i += 1
    story = merged

    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X, topMargin=22 * mm, bottomMargin=19 * mm,
                          title=f"Plan opérationnel - {name}", author=filled(info.get("organizer"), "CrowdRunning"), subject="Plan opérationnel de course à pied")
    frame = Frame(MARGIN_X, 19 * mm, CONTENT_W, PAGE_H - 41 * mm, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame])])

    class _Canvas(NumberedCanvas):
        header_left = name
        header_right = f"{fr_date(event_date)}"

    doc.build(story, canvasmaker=_Canvas)
    return buf.getvalue()
