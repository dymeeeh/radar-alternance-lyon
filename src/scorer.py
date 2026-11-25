"""Couche 3 : scoring sur 100 adapté à un profil BUT R&T (Cloud / DevSecOps / SysOps / Réseau / Cyber)."""
from __future__ import annotations

import re
from datetime import date, datetime

from .collectors import in_lyon_area
from .processor import ALTERNANCE_WORDS, FALSE_FRIENDS, HELPDESK_WORDS
from .textutils import fold, matches

# (mot-clé recherché, libellé de tag)
CORE = [("aws", "AWS"), ("cloud", "Cloud"), ("terraform", "Terraform"), ("devsecops", "DevSecOps"),
        ("devops", "DevOps"), ("linux", "Linux"), ("docker", "Docker"), ("kubernetes", "Kubernetes"),
        ("k8s", "Kubernetes"), ("azure", "Azure"), ("gcp", "GCP"), ("ansible", "Ansible"),
        ("ci/cd", "CI/CD"), ("gitlab", "GitLab"), ("sysops", "SysOps"), ("sre", "SRE")]
CYBER_NET = [("soc", "SOC"), ("splunk", "Splunk"), ("siem", "SIEM"), ("firewall", "Firewall"),
             ("pare-feu", "Firewall"), ("vlan", "VLAN"), ("cisco", "Cisco"), ("securite", "Sécurité"),
             ("cybersecurite", "Cybersécurité"), ("cyber", "Cybersécurité"), ("pentest", "Pentest"),
             ("fortinet", "Fortinet"), ("palo alto", "Palo Alto"), ("reseau", "Réseau"),
             ("reseaux", "Réseau"), ("routage", "Routage"), ("iso 27001", "ISO 27001"), ("edr", "EDR")]
PRIME_LOCATIONS = [("lyon", "Lyon"), ("part-dieu", "Part-Dieu"), ("confluence", "Confluence"),
                   ("presqu'ile", "Presqu'île"), ("villeurbanne", "Villeurbanne"), ("limonest", "Limonest")]
# « BUT » est testé à part, en majuscules, pour ne pas confondre avec « le but de ce poste »
BUT_RE = re.compile(r"(?<![A-Za-z])(BUT|B\.U\.T\.?)(?![A-Za-z])")
CONTRACT = [("24 mois", "24 mois"), ("2 ans", "24 mois"), ("deux ans", "24 mois"),
            ("bachelor", "Bachelor"), ("licence", "Licence"), ("licence pro", "Licence"),
            ("bac+3", "Bac+3"), ("bac +3", "Bac+3"), ("r&t", "R&T"),
            ("reseaux et telecommunications", "R&T"), ("reseaux et telecoms", "R&T")]
RENTREE_2027 = ["2027", "septembre 2027", "rentree 2027"]
# Villes proches acceptables (ni bonus, ni malus)
NEAR = ["vienne", "villefranche", "bourgoin", "isle-d'abeau", "isle d'abeau", "saint-quentin-fallavier",
        "miribel", "montluel", "beynost", "trevoux", "brignais", "chaponost", "anse"]
NEUTRAL_LOCATIONS = {"", "france", "auvergne-rhone-alpes", "rhone", "teletravail", "remote", "hybride"}

W_CORE, W_CYBER, W_LOC, W_CONTRACT, W_TITLE_ALT, W_2027, W_FRESH = 25, 20, 15, 15, 10, 10, 5
PENALTY = 30


def _labels(text: str, table) -> list[str]:
    found = matches(text, [k for k, _ in table])
    labels: list[str] = []
    for k, lab in table:
        if k in found and lab not in labels:
            labels.append(lab)
    return labels


def _days_old(iso: str) -> int | None:
    try:
        return (date.today() - datetime.strptime(iso[:10], "%Y-%m-%d").date()).days
    except (ValueError, TypeError):
        return None


def score_job(job: dict) -> dict:
    text = FALSE_FRIENDS.sub(" ", job["_full_text"])
    title = FALSE_FRIENDS.sub(" ", job["_title"])
    lieu = fold(job["lieu"])
    loc_text = f"{lieu} {title}"  # pas la description : « Confluence » y désigne souvent l'outil Atlassian
    score = 0
    tags: list[str] = []

    core = _labels(text, CORE)
    if core:
        score += W_CORE
        tags += core
    cyber = _labels(text, CYBER_NET)
    if cyber:
        score += W_CYBER
        tags += [t for t in cyber if t not in tags]
    locs = _labels(loc_text, PRIME_LOCATIONS)
    if locs:
        score += W_LOC
        tags.append(locs[0])
    contract = _labels(text, CONTRACT)
    if BUT_RE.search(f"{job['titre']} {job['description']}"):
        contract.insert(0, "BUT")
    if contract:
        score += W_CONTRACT
        tags += [t for t in contract if t not in tags]
    if matches(title, ALTERNANCE_WORDS):
        score += W_TITLE_ALT
    if matches(text, RENTREE_2027):
        score += W_2027
        tags.append("Rentrée 2027")
    age = _days_old(job["date_publication"])
    if age is not None and age <= 3:
        score += W_FRESH
        tags.append("Nouveau")

    too_far = (lieu not in NEUTRAL_LOCATIONS and not in_lyon_area(lieu)
               and not matches(lieu, NEAR))
    helpdesk_only = bool(matches(title, HELPDESK_WORDS)) and not core and not cyber
    if too_far:
        score -= PENALTY
        tags.append("⚠ Éloigné")
    if helpdesk_only:
        score -= PENALTY
        tags.append("⚠ Helpdesk")

    job["score"] = max(0, min(100, score))
    job["tags"] = tags
    return job


def score_all(jobs: list[dict]) -> list[dict]:
    for j in jobs:
        score_job(j)
    return sorted(jobs, key=lambda j: (j["score"], j["date_publication"] or ""), reverse=True)
