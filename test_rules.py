"""Tests du moteur de règles. Lancer : `python test_rules.py` (ou `pytest test_rules.py`).

Le contrôle des citations compare chaque `quote` de rules.yaml au texte du règlement. Il a besoin du PDF :
    REGLEMENT_PDF=/chemin/Reglementation_Running_2026.pdf python test_rules.py
(ou REGLEMENT_TXT=/chemin/texte.txt si le texte est déjà extrait). Sans l'un des deux, ce test est ignoré.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date

import rules as rl

DATA = rl.load_rules()
L_MARATHON = 42_070.0


def ctx(**kw):
    base = dict(runners=3000, length_m=L_MARATHON, event_date=date(2027, 6, 1), today=date(2026, 10, 24))
    base.update(kw)
    return rl.Context(**base)


def obj(kind, km, p1=1.0, p2=0.0, name=None):
    return {"id": f"{kind}{km}", "kind": kind, "name": name or kind, "lat": 0, "lon": 0,
            "p1": float(p1), "p2": float(p2), "progress_m": km * 1000.0, "offset_m": 0.0}


def run(c, objects=(), result=None, manual=None):
    return {f.id: f for f in rl.evaluate(DATA, c, list(objects), result, manual)}


def waters(*kms):
    return [obj("water", k) for k in kms]


# ---------------------------------------------------------------------------- fichier de règles
def test_rules_file_is_valid():
    assert rl.validate_rules(DATA) == []
    assert len(DATA["rules"]) >= 30


def test_every_rule_evaluates_without_error():
    for et in ("route", "trail"):
        for runners in (50, 249, 250, 500, 501, 1000, 1001, 3001, 20000):
            for length in (3_000, 8_000, 21_100, L_MARATHON, 80_000):
                for found in rl.evaluate(DATA, ctx(event_type=et, runners=runners, length_m=length, d_plus_m=2000), []).copy():
                    assert "non évaluable" not in found.detail, (found.id, found.detail)


def test_quotes_are_in_the_regulation():
    txt = os.environ.get("REGLEMENT_TXT")
    pdf = os.environ.get("REGLEMENT_PDF")
    if not txt and pdf:
        txt = pdf + ".txt"
        subprocess.run(["pdftotext", pdf, txt], check=True, capture_output=True)
    if not txt or not os.path.exists(txt):
        print("  (citations non contrôlées : définir REGLEMENT_PDF ou REGLEMENT_TXT)")
        return

    def norm(s):
        s = s.replace("’", "'").replace("‘", "'").replace("­", "").replace(" ", " ")
        return re.sub(r"\s+", " ", s).strip()

    full = norm(open(txt, encoding="utf-8", errors="ignore").read())
    missing = [r["id"] for r in DATA["rules"] if r.get("quote") and norm(r["quote"]) not in full]
    assert not missing, f"citations introuvables dans le règlement : {missing}"


# ---------------------------------------------------------------------------- effectifs et secours (III.A.7)
def test_medical_tiers_route():
    for n, expect_doctor, expect_amb250, expect_secours500 in [
            (249, False, False, False), (250, False, True, False), (500, False, True, False),
            (501, True, False, True), (3000, True, False, True)]:
        r = run(ctx(runners=n, length_m=10_000))
        assert ("med-medecin" in r) == expect_doctor, n
        assert ("med-ambulance-250-500" in r) == expect_amb250, n
        assert ("med-secouristes-500" in r) == expect_secours500, n
        assert ("med-secouristes" in r) == (n <= 500), n


def test_marathon_requires_doctor_even_for_small_fields():
    assert "med-medecin" in run(ctx(runners=200, length_m=42_195))
    assert "med-medecin" in run(ctx(runners=200, length_m=L_MARATHON))
    assert "med-medecin" not in run(ctx(runners=200, length_m=21_100))
    assert "med-cumul" in run(ctx(runners=200, length_m=42_195))


def test_medical_counts_and_status():
    c = ctx(runners=3000)
    r = run(c, [obj("medical", 0.1, 3)])
    assert r["med-medecin"].status == "alert"
    assert r["med-secouristes-500"].status == "check"          # nombre « adapté » : non chiffré par le règlement
    r = run(c, [obj("medical", 0.1, 3), obj("doctor", 0.1, 1, 1), obj("ambulance", 0.2, 1, 0)])
    assert r["med-medecin"].status == "ok"
    assert r["med-ambulances-500"].status == "check"
    r = run(ctx(runners=300, length_m=10_000), [obj("medical", 0.1, 1)])
    assert r["med-ambulance-250-500"].status == "alert"
    r = run(ctx(runners=300, length_m=10_000), [obj("medical", 0.1, 1), obj("ambulance", 5, 1)])
    assert r["med-ambulance-250-500"].status == "ok" and r["med-secouristes"].status == "ok"


# ---------------------------------------------------------------------------- ravitaillement (III.A.5)
def test_water_spacing_ok_and_alerts():
    c = ctx()
    ok = run(c, waters(5, 10, 15, 20, 25, 30, 35, 40))
    assert ok["rav-espacement"].status == "ok"
    assert run(c, waters(5, 10, 15, 20, 25, 30, 35))["rav-espacement"].status == "alert"   # 7,07 km entre le km 35 et l'arrivée
    assert run(c, waters(5, 10, 15, 20, 25, 30, 36.5))["rav-espacement"].status == "alert"  # 5,57 km mais intervalle 6,5 km avant
    assert run(c)["rav-espacement"].status == "alert"
    assert run(c, waters(8, 13, 18, 23, 28, 33, 38))["rav-espacement"].status == "alert"    # premier trop tard
    assert run(c, waters(5, 10, 20, 25, 30, 35, 40))["rav-espacement"].status == "alert"    # trou de 10 km
    assert run(c, waters(4.1, 9, 14, 19, 24, 29, 34, 39))["rav-espacement"].status == "ok"  # tolérance ± 1 km


def test_water_spacing_last_gap_to_finish_is_checked():
    c = ctx()
    assert run(c, waters(5, 10, 15, 20, 25, 30))["rav-espacement"].status == "alert"        # 12 km sans eau avant l'arrivée


def test_water_rules_depend_on_distance_and_event_type():
    assert "rav-espacement" not in run(ctx(length_m=10_000))
    assert "rav-espacement" in run(ctx(length_m=10_100))
    assert "rav-presence" in run(ctx(length_m=8_000)) and "rav-presence" not in run(ctx(length_m=3_000))
    assert "rav-espacement" not in run(ctx(event_type="trail"))


def test_sponge_midpoints():
    c = ctx(length_m=20_000)
    w = waters(5, 10, 15)
    assert run(c, w)["rav-eponge-mi-chemin"].status == "alert"
    s = [obj("sponge", 7.5), obj("sponge", 12.5)]
    assert run(c, w + s)["rav-eponge-mi-chemin"].status == "ok"
    assert run(c, w + [obj("sponge", 7.5)])["rav-eponge-mi-chemin"].status == "alert"
    assert run(c, w + [obj("sponge", 5.2), obj("sponge", 12.5)])["rav-eponge-mi-chemin"].status == "alert"
    assert run(c, waters(10))["rav-eponge-mi-chemin"].status == "check"


# ---------------------------------------------------------------------------- démarches (dates)
def test_deadlines():
    ev = date(2027, 6, 1)
    r = run(ctx(event_date=ev, today=date(2026, 10, 24)))
    assert "01/04/2027" in r["adm-declaration"].detail                                                  # 2 mois
    assert r["adm-declaration"].status == "info"
    r = run(ctx(event_date=ev, multi_department=True))
    assert "01/03/2027" in r["adm-declaration"].detail                                                  # 3 mois
    assert "11/05/2027" in run(ctx(event_date=ev))["adm-signaleurs-arretes"].detail                    # 21 jours
    assert "25/05/2027" in run(ctx(event_date=ev))["adm-assurance"].detail                            # six jours francs = J-7
    assert run(ctx(event_date=date(2026, 12, 1)))["adm-declaration"].status == "alert"               # 2 mois : 1er oct dépassé
    assert run(ctx(event_date=date(2027, 1, 5), today=date(2026, 10, 24)))["adm-declaration"].status == "check"   # échéance le 05/11
    assert rl._months_before(date(2027, 3, 31), 1) == date(2027, 2, 28)
    assert rl._months_before(date(2027, 1, 15), 2) == date(2026, 11, 15)


def test_admin_rules_depend_on_context():
    r = run(ctx(competition=False, runners=120))
    assert "adm-declaration-100" in r and "adm-declaration" not in r and "adm-participants" not in r
    assert "adm-declaration-100" not in run(ctx(competition=False, runners=99))
    assert "adm-declaration" not in run(ctx(public_road=False))
    r = run(ctx(ffa_affiliated=False))
    assert "adm-ffa-autorisation" in r and "adm-ffa-avis" in r
    assert "adm-ffa-autorisation" not in run(ctx(ffa_affiliated=True))


# ---------------------------------------------------------------------------- trail (titre IV)
def test_trail_start_finish_rules():
    c = ctx(event_type="trail", runners=1500, length_m=30_000, d_plus_m=1500)
    r = run(c, [obj("medical", 0.1, 2)])
    assert r["tr-depart-medecin"].status == "alert" and r["tr-arrivee-medecin"].status == "alert"
    assert r["tr-arrivee-secouristes"].status == "alert"
    full = [obj("medical", 0.1, 2), obj("medical", 29.9, 2), obj("doctor", 0.1, 1, 1), obj("doctor", 29.9, 1, 1),
            obj("ambulance", 0.2, 1, 1)]
    r = run(c, full)
    for rid in ("tr-depart-medecin", "tr-depart-infirmier", "tr-arrivee-medecin", "tr-arrivee-infirmiere",
                "tr-arrivee-secouristes", "tr-ambulances"):
        assert r[rid].status == "ok", (rid, r[rid].detail)
    r = run(ctx(event_type="trail", runners=3500, length_m=30_000), full)
    assert r["tr-ambulances"].status == "alert"                       # il en faut 2 au-delà de 3 000
    r = run(ctx(event_type="trail", runners=1500, length_m=30_000), [obj("ambulance", 5, 1, 0)])
    assert r["tr-ambulances"].status == "alert"                       # une ambulance sans transport ne compte pas
    r = run(ctx(event_type="trail", runners=800, length_m=30_000))
    assert "tr-depart-secouristes" in r and "tr-depart-medecin" not in r
    assert "tr-arrivee-medecin" in run(ctx(event_type="trail", runners=501, length_m=30_000))
    assert "tr-arrivee-medecin" not in run(ctx(event_type="trail", runners=500, length_m=30_000))
    assert all(k not in run(ctx()) for k in ("tr-delai-30", "tr-pcc", "tr-fermeture"))


def test_trail_km_effort_rules():
    assert "tr-barrieres" not in run(ctx(event_type="trail", length_m=35_000, d_plus_m=400))     # 39 km-effort
    assert "tr-barrieres" in run(ctx(event_type="trail", length_m=35_000, d_plus_m=600))        # 41 km-effort
    assert "tr-pcc" in run(ctx(event_type="trail", runners=1000)) and "tr-pcc" not in run(ctx(event_type="trail", runners=999))


class FakeMed:
    def __init__(self, cov_s):
        import numpy as np
        self.coverage_s = np.asarray(cov_s, dtype=float)
        self.coverage_km = np.arange(len(cov_s)) * 0.5
        self.n_reps, self.response_p95_s, self.response_max_s = 200, 900.0, 1500.0


def test_response_time_rule():
    c = ctx(event_type="trail", length_m=30_000)
    assert run(c)["tr-delai-30"].status == "alert"                                       # aucun poste
    posts = [obj("medical", 1, 2)]
    assert run(c, posts)["tr-delai-30"].status == "check"                                # pas encore simulé
    assert run(c, posts, {"med": FakeMed([600, 1200, 1500])})["tr-delai-30"].status == "ok"
    bad = run(c, posts, {"med": FakeMed([600, 1200, 2400, 2500, 900])})["tr-delai-30"]
    assert bad.status == "alert" and "km 1,0–1,5" in bad.detail


# ---------------------------------------------------------------------------- manuel et informations
def test_manual_rules_follow_confirmation():
    r = run(ctx())
    assert r["rav-depart-arrivee"].status == "check" and r["rav-depart-arrivee"].manual
    r = run(ctx(), manual={"rav-depart-arrivee": True})
    assert r["rav-depart-arrivee"].status == "ok" and r["rav-depart-arrivee"].confirmed


def test_info_rules():
    r = run(ctx(length_m=12_000))
    assert "Cadets" in r["parc-categories"].detail and "Non ouvert" in r["parc-categories"].detail
    r = run(ctx(length_m=3_000))
    assert "Benjamins" in r["parc-categories"].detail and "Non ouvert" not in r["parc-categories"].detail
    assert run(ctx(length_m=42_195))["parc-distance-officielle"].status == "info"
    assert run(ctx(length_m=42_070))["parc-distance-officielle"].status == "check"
    assert run(ctx(length_m=30_000))["parc-distance-officielle"].status == "info"
    assert "parc-distance-officielle" not in run(ctx(event_type="trail"))
    r = run(ctx(event_type="trail", runners=2000, length_m=30_000, d_plus_m=1000))
    assert "30 m²" in r["tr-locaux"].detail
    assert "40,0 km" in r["parc-categories"].detail or "40" in r["parc-categories"].detail


def test_summary_counts():
    f = rl.evaluate(DATA, ctx(), [])
    s = rl.summarize(f)
    assert sum(s.values()) == len(f) and s["alert"] > 0


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok    {name}")
            except Exception as exc:     # noqa: BLE001
                failed += 1
                print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failed else 0)
