"""Radar à alternances Lyon — point d'entrée.

Usage :
    python main.py                 # run normal (72 h au premier lancement, puis 24 h)
    python main.py --hours-old 48  # forcer la fenêtre jobspy
    python main.py --no-dedup      # ignorer data/seen_jobs.json (test) sans le modifier
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src import collectors
from src.processor import SeenStore, process, to_public
from src.scorer import score_all

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TARGETS = ROOT / "config" / "targets.json"
SEEN = DATA / "seen_jobs.json"
OUT_JSON = DATA / "offres_du_jour.json"
OUT_MD = DATA / "offres_du_jour.md"

log = logging.getLogger("radar")


def run_collectors(hours_old: int) -> tuple[list[dict], dict]:
    plan = [
        ("LinkedIn + Indeed (jobspy)", lambda: collectors.collect_jobspy(hours_old=hours_old)),
        ("Welcome to the Jungle", collectors.collect_wttj),
        ("La Bonne Alternance", collectors.collect_lba),
        ("ATS entreprises cibles", lambda: collectors.collect_ats(TARGETS)),
    ]
    raw: list[dict] = []
    report: dict[str, str] = {}
    for name, fn in plan:
        t0 = time.monotonic()
        try:
            jobs = fn()
            if jobs is None:
                report[name] = "⏭ ignoré (clé API absente)"
                continue
            raw.extend(jobs)
            report[name] = f"✅ {len(jobs)} offres brutes ({time.monotonic() - t0:.0f}s)"
        except Exception as exc:  # noqa: BLE001 — un collecteur ne doit jamais faire tomber le radar
            log.exception("Collecteur « %s » en échec", name)
            report[name] = f"❌ {type(exc).__name__}: {str(exc)[:120]}"
    return raw, report


def md_escape(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def write_markdown(jobs: list[dict], report: dict, stats: dict, now: datetime) -> None:
    lines = [
        f"# 🎯 Radar Alternance Lyon — {now:%d/%m/%Y}",
        "",
        f"**{len(jobs)} nouvelle(s) offre(s)** Cloud / DevSecOps / SysOps / Réseau / Cyber "
        f"dans un rayon de 30 km autour de Lyon. Mise à jour : {now:%d/%m/%Y %H:%M} (Paris).",
        "",
        "| Score | Poste | Entreprise | Lieu | Source | Publiée | Postuler |",
        "|:-----:|-------|------------|------|--------|---------|:--------:|",
    ]
    for j in jobs:
        badge = "🟢" if j["score"] >= 60 else "🟡" if j["score"] >= 35 else "⚪"
        tags = ", ".join(j["tags"][:5])
        poste = md_escape(j["titre"]) + (f"<br><sub>{md_escape(tags)}</sub>" if tags else "")
        lines.append(
            f"| {badge} **{j['score']}** | {poste} | {md_escape(j['entreprise'])} | "
            f"{md_escape(j['lieu'])} | {md_escape(j['source'])} | {j['date_publication']} | "
            f"[👉 Postuler]({j['url']}) |"
        )
    lines += ["", "<details><summary>📊 État des collecteurs et filtrage</summary>", ""]
    lines += [f"- **{name}** : {status}" for name, status in report.items()]
    rej = ", ".join(f"{k} : {v}" for k, v in sorted(stats["rejetees"].items(), key=lambda kv: -kv[1]))
    lines += [
        "",
        f"- Offres brutes : {stats['brutes']} · rejetées : {sum(stats['rejetees'].values())}"
        f" ({rej or '—'}) · déjà vues : {stats['deja_vues']} · doublons : {stats['doublons_run']}",
        "", "</details>", "",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


def set_github_output(**values) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for k, v in values.items():
                fh.write(f"{k}={v}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Radar à alternances Lyon")
    parser.add_argument("--hours-old", type=int, default=None,
                        help="Fenêtre de fraîcheur jobspy (défaut : 72 au 1er run, puis 24)")
    parser.add_argument("--no-dedup", action="store_true",
                        help="Ignore seen_jobs.json (test) et ne le met pas à jour")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("JobSpy", "jobspy", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    DATA.mkdir(exist_ok=True)
    store = SeenStore(SEEN)
    env_hours = os.environ.get("HOURS_OLD")
    hours_old = args.hours_old or (int(env_hours) if env_hours else (72 if store.is_empty else 24))
    log.info("Démarrage — fenêtre jobspy : %d h — %d empreintes déjà vues", hours_old, len(store.seen))

    raw, report = run_collectors(hours_old)
    jobs, stats = process(raw, store, use_dedup=not args.no_dedup)
    jobs = score_all(jobs)

    for name, status in report.items():
        log.info("%-28s %s", name, status)
    log.info("Stats : %s", json.dumps(stats, ensure_ascii=False))

    set_github_output(new_count=len(jobs))
    if not jobs:
        log.info("Aucune nouvelle offre aujourd'hui — fichiers inchangés.")
        return 0

    now = datetime.now(ZoneInfo("Europe/Paris"))
    OUT_JSON.write_text(json.dumps([to_public(j) for j in jobs], ensure_ascii=False, indent=2),
                        encoding="utf-8")
    write_markdown(jobs, report, stats, now)
    if not args.no_dedup:
        for j in jobs:
            store.add(j["_keys"])
        store.save()
    log.info("✅ %d nouvelles offres → %s et %s", len(jobs), OUT_JSON.name, OUT_MD.name)
    for j in jobs[:10]:
        log.info("  %3d  %s — %s (%s)", j["score"], j["titre"][:60], j["entreprise"][:25], j["source"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
