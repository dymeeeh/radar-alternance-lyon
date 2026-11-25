"""Couche 2 : normalisation, filtrage anti-bruit et déduplication persistante."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .textutils import fold, has_word, matches, strip_html

log = logging.getLogger("radar.processor")

SCHEMA_KEYS = ["id", "titre", "entreprise", "lieu", "source", "date_publication",
               "url", "description", "score", "tags"]
DESCRIPTION_MAX = 1500
SEEN_RETENTION_DAYS = 180

ALTERNANCE_WORDS = [
    "alternance", "alternant", "alternante", "alternant(e)", "apprentissage", "apprenti",
    "apprentie", "apprenti(e)", "contrat pro", "contrat de professionnalisation",
    "work-study", "work study", "apprenticeship", "apprentice", "alterne", "alternee",
]

# Mots du titre qui disqualifient d'office
TITLE_BLOCKLIST = [
    "senior", "sr", "lead", "tech lead", "confirme", "confirmee", "expert", "experte",
    "principal", "head of", "directeur", "directrice", "responsable d'equipe",
    "commercial", "commerciale", "technico-commercial", "business developer",
    "business development", "sales", "ingenieur d'affaires", "charge d'affaires",
    "account manager", "account executive", "key account",
]
STAGE_WORDS = ["stage", "stagiaire", "internship", "intern", "pfe", "stage de fin d'etudes"]

HELPDESK_WORDS = [
    "helpdesk", "help desk", "hotline", "support niveau 1", "support n1", "niveau 1",
    "support utilisateur", "support utilisateurs", "support bureautique", "technicien de proximite",
    "technicienne de proximite", "assistance utilisateurs", "service desk",
]

# Alternance « forte » dans la description (le mot « alternance » seul est souvent du bruit :
# « profil sortant d'alternance », « nos alternants »…)
STRONG_ALT_PHRASES = [
    "en alternance", "contrat d'apprentissage", "contrat de professionnalisation", "contrat pro",
    "contrat en alternance", "poste en alternance", "offre d'alternance", "rythme d'alternance",
    "rythme de l'alternance", "rythme alterne", "apprenti", "apprentie", "apprentissage",
    "work-study", "apprenticeship", "alternance de", "alternance a pourvoir", "alternance :",
]
OTHER_CONTRACTS = ["cdi", "cdd", "freelance", "interim", "independant", "portage salarial"]

# Métier : un terme FORT dans le titre suffit ; un terme FAIBLE (ambigu : « réseau de chaleur »,
# « systèmes d'acquisition »…) doit être confirmé par un ancrage IT dans le texte.
STRONG_DOMAIN = [
    "cloud", "devops", "devsecops", "sysops", "secops", "sre", "cyber", "cybersecurite",
    "securite informatique", "securite des systemes", "securite si", "securite reseau",
    "securite des si", "securite operationnelle", "linux", "unix", "windows server",
    "admin sys", "sysadmin", "adminsys", "soc", "siem", "aws", "azure", "gcp", "kubernetes",
    "k8s", "docker", "terraform", "ansible", "firewall", "pare-feu", "cisco", "telecom",
    "telecoms", "telecommunications", "r&t", "virtualisation", "vmware", "datacenter",
    "pentest", "iam", "noc", "network", "security", "platform engineer", "ops", "informatique",
    "it", "si", "dsi", "systemes d'information", "systemes et reseaux", "systeme et reseau",
    "reseaux et systemes", "reseaux et telecoms", "administrateur systeme",
    "administrateur systemes", "administrateur reseau", "administrateur reseaux",
]
WEAK_DOMAIN = ["reseau", "reseaux", "systeme", "systemes", "infrastructure", "infrastructures",
               "infra", "administrateur", "administratrice", "exploitation", "support"]
IT_ANCHORS = [
    "informatique", "linux", "windows", "cisco", "cloud", "cyber", "cybersecurite", "tcp/ip",
    "lan", "wan", "serveur", "serveurs", "active directory", "vmware", "virtualisation",
    "firewall", "sysadmin", "dsi", "systemes d'information", "aws", "azure", "docker",
    "kubernetes", "vlan", "routeur", "switch", "supervision", "sauvegarde", "office 365", "m365",
]
TITLE_OFFTOPIC = [
    "chaleur", "electrique", "electriques", "electrotechnicien", "electrotechnicienne",
    "electrotechnique", "routier", "routiers", "btp", "batiment", "cvc", "ferroviaire", "gaz",
    "hydraulique", "mecanique", "automatisme", "automaticien", "communication", "marketing",
    "rh", "ressources humaines", "recrutement", "comptable", "comptabilite", "qse", "qsse",
    "qhse", "hse", "juriste", "achats", "acheteur", "logistique", "graphiste", "community manager",
]
# « réseaux sociaux » etc. ne doivent pas compter comme du réseau informatique
FALSE_FRIENDS = re.compile(r"reseaux? (sociaux|social|de chaleur|electrique|d'agences|de distribution"
                           r"|de transport|commercial|de partenaires|d'eau|de gaz)")


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


TRACKING_PARAMS = re.compile(r"^(utm_.*|refid|trackingid|trk|from|src|position|pagenum|ref)$", re.I)


def canonical_url(url: str) -> str:
    if not url:
        return ""
    p = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if not TRACKING_PARAMS.match(k)]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urlencode(q), ""))


def job_keys(job: dict) -> tuple[str, str]:
    k_company_title = _sha(fold(job["entreprise"]) + "|" + fold(job["titre"]))
    k_url = _sha(canonical_url(job["url"])) if job["url"] else ""
    return k_company_title, k_url


def normalize(raw: dict) -> dict | None:
    titre = str(raw.get("titre") or "").strip()
    url = str(raw.get("url") or "").strip()
    if not titre or not url:
        return None
    desc = strip_html(raw.get("description") or "")
    desc = re.sub(r"\n{3,}", "\n\n", desc)
    job = {
        "id": "",
        "titre": titre,
        "entreprise": str(raw.get("entreprise") or "Non précisé").strip(),
        "lieu": str(raw.get("lieu") or "").strip(),
        "source": raw.get("source", ""),
        "date_publication": raw.get("date_publication") or date.today().isoformat(),
        "url": url,
        "description": desc[:DESCRIPTION_MAX] + ("…" if len(desc) > DESCRIPTION_MAX else ""),
        "score": 0,
        "tags": [],
        # champs internes (retirés à l'export)
        "_full_text": fold(" ".join([titre, desc, str(raw.get("contrat") or "")])),
        "_title": fold(titre),
        "_contract": fold(raw.get("contrat") or ""),
    }
    job["id"] = job_keys(job)[0][:12]
    return job


def rejection_reason(job: dict) -> str | None:
    """Renvoie la raison du rejet, ou None si l'offre est pertinente."""
    title = FALSE_FRIENDS.sub(" ", job["_title"])
    text = FALSE_FRIENDS.sub(" ", job["_full_text"])
    title_alternance = bool(matches(title, ALTERNANCE_WORDS))
    # WTTJ / LBA : le type de contrat « alternance » vient de la source elle-même
    contract_alternance = "alternance" in job["_contract"]

    if not (title_alternance or contract_alternance):
        if not matches(text, STRONG_ALT_PHRASES):
            return "pas d'alternance (CDI/CDD/freelance)"
        if matches(title + " " + job["_contract"], OTHER_CONTRACTS):
            return "pas d'alternance (CDI/CDD/freelance)"
    if matches(title, STAGE_WORDS) and not title_alternance:
        return "stage"
    bad = matches(title, TITLE_BLOCKLIST)
    if bad:
        return f"titre exclu ({bad[0]})"
    off = matches(title, TITLE_OFFTOPIC)
    if off:
        return f"hors périmètre Cloud/Réseau/Sécu ({off[0]})"
    strong = matches(title, STRONG_DOMAIN)
    weak = matches(title, WEAK_DOMAIN) and matches(text, IT_ANCHORS)
    if not (strong or weak):
        return "hors périmètre Cloud/Réseau/Sécu"
    helpdesk = matches(title, HELPDESK_WORDS)
    if helpdesk:
        tech = matches(text, ["linux", "cloud", "devops", "reseau", "reseaux", "securite", "cyber",
                              "infrastructure", "administrateur", "systeme", "systemes", "aws",
                              "azure", "docker", "cisco", "firewall"])
        if not tech:
            return "support N1 bureautique"
    return None


# --------------------------------------------------------------------------- #
# Déduplication persistante
# --------------------------------------------------------------------------- #
class SeenStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.seen: dict[str, str] = {}
        if self.path.exists():
            try:
                self.seen = json.loads(self.path.read_text(encoding="utf-8")).get("seen", {})
            except (json.JSONDecodeError, AttributeError):
                log.warning("%s illisible, on repart de zéro", self.path)

    @property
    def is_empty(self) -> bool:
        return not self.seen

    def contains(self, keys) -> bool:
        return any(k and k in self.seen for k in keys)

    def add(self, keys) -> None:
        today = date.today().isoformat()
        for k in keys:
            if k:
                self.seen.setdefault(k, today)

    def save(self) -> None:
        cutoff = (date.today() - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"version": 1, "seen": dict(sorted(self.seen.items()))},
                                        ensure_ascii=False, indent=1), encoding="utf-8")


def process(raw_jobs: list[dict], store: SeenStore, use_dedup: bool = True) -> tuple[list[dict], dict]:
    """Normalise, filtre et déduplique. Retourne (offres_nouvelles, statistiques)."""
    stats = {"brutes": len(raw_jobs), "invalides": 0, "rejetees": {}, "deja_vues": 0, "doublons_run": 0}
    kept: list[dict] = []
    run_keys: set[str] = set()
    for raw in raw_jobs:
        job = normalize(raw)
        if job is None:
            stats["invalides"] += 1
            continue
        reason = rejection_reason(job)
        if reason:
            key = reason.split(" (")[0]
            stats["rejetees"][key] = stats["rejetees"].get(key, 0) + 1
            continue
        keys = job_keys(job)
        if any(k and k in run_keys for k in keys):
            stats["doublons_run"] += 1
            continue
        run_keys.update(k for k in keys if k)
        if use_dedup and store.contains(keys):
            stats["deja_vues"] += 1
            continue
        job["_keys"] = keys
        kept.append(job)
    stats["nouvelles"] = len(kept)
    return kept, stats


def to_public(job: dict) -> dict:
    return {k: job[k] for k in SCHEMA_KEYS}
