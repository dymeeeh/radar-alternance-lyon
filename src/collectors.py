"""Couche 1 : collecte multi-sources.

Chaque collecteur renvoie une liste d'offres « brutes » au format commun minimal
(titre, entreprise, lieu, source, date_publication, url, description).
Chaque collecteur — et chaque requête à l'intérieur — est isolé dans un try/except :
un site qui bloque ou change d'API ne fait jamais tomber les autres.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from .textutils import fold, strip_html

log = logging.getLogger("radar.collectors")

LYON_LAT, LYON_LON = 45.7640, 4.8357
RADIUS_KM = 30
HTTP_TIMEOUT = 25
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

JOBSPY_QUERIES = [
    "alternance cloud",
    "alternance devops",
    "alternance cybersécurité",
    "alternance système réseau",
    "alternance devsecops",
]

# Villes de l'agglomération lyonnaise (sert à filtrer les ATS qui renvoient toute la France)
LYON_AREA = [
    "lyon", "villeurbanne", "limonest", "venissieux", "vaulx-en-velin", "bron",
    "caluire", "ecully", "dardilly", "saint-priest", "st priest", "champagne-au-mont-d'or",
    "tassin", "oullins", "rillieux", "decines", "meyzieu", "chassieu", "genas",
    "saint-fons", "pierre-benite", "francheville", "sainte-foy-les-lyon", "craponne",
    "givors", "corbas", "mions", "neuville-sur-saone", "part-dieu", "confluence",
    "gerland", "techlid", "saint-genis-laval", "irigny",
]


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    return s


def _clean(v):
    """Remplace NaN/None par une chaîne vide (DataFrame pandas → dict)."""
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    return v


def _iso_date(v) -> str:
    v = _clean(v)
    if not v:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)):
        ts = v / 1000 if v > 1e11 else v
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    s = str(v)
    return s[:10]


def in_lyon_area(location: str) -> bool:
    loc = fold(location)
    if re.search(r"\b69\d{3}\b", loc):  # code postal du Rhône
        return True
    return any(city in loc for city in LYON_AREA)


# --------------------------------------------------------------------------- #
# 1. LinkedIn + Indeed via python-jobspy
# --------------------------------------------------------------------------- #
def collect_jobspy(hours_old: int = 24, results_wanted: int = 40) -> list[dict]:
    from jobspy import scrape_jobs  # import local : si jobspy casse, seul ce collecteur tombe

    out: list[dict] = []
    distance_miles = round(RADIUS_KM / 1.609)  # jobspy attend des miles
    for site in ("linkedin", "indeed"):
        for i, query in enumerate(JOBSPY_QUERIES):
            if i:
                time.sleep(4 if site == "linkedin" else 1)  # LinkedIn renvoie vite des 429
            try:
                df = scrape_jobs(
                    site_name=[site],
                    search_term=query,
                    location="Lyon, France",
                    distance=distance_miles,
                    hours_old=hours_old,
                    results_wanted=results_wanted,
                    country_indeed="France",
                    linkedin_fetch_description=False,
                    verbose=0,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("jobspy %s « %s » : %s", site, query, exc)
                continue
            n = 0 if df is None else len(df)
            log.info("jobspy %-8s « %s » → %d", site, query, n)
            if not n:
                continue
            for row in df.to_dict(orient="records"):
                out.append({
                    "titre": _clean(row.get("title")),
                    "entreprise": _clean(row.get("company")),
                    "lieu": _clean(row.get("location")),
                    "source": "LinkedIn" if site == "linkedin" else "Indeed",
                    "date_publication": _iso_date(row.get("date_posted")),
                    "url": _clean(row.get("job_url")) or _clean(row.get("job_url_direct")),
                    "description": _clean(row.get("description")),
                    "contrat": " ".join(str(_clean(row.get(k))) for k in ("job_type",)),
                })
    return out


# --------------------------------------------------------------------------- #
# 2. Welcome to the Jungle (index Algolia public utilisé par le site)
# --------------------------------------------------------------------------- #
WTTJ_APP_ID = "CSEKHVMS53"
WTTJ_API_KEY = "4bd8f6215d0cc52b26430765769e65a0"  # clé « search-only » publique du site
WTTJ_INDEX = "wttj_jobs_production_fr"


def collect_wttj() -> list[dict]:
    url = f"https://{WTTJ_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
    headers = {
        "x-algolia-application-id": WTTJ_APP_ID,
        "x-algolia-api-key": WTTJ_API_KEY,
        "Referer": "https://www.welcometothejungle.com/",
        "Origin": "https://www.welcometothejungle.com",
        "Content-Type": "application/json",
    }
    s = _session()
    out: list[dict] = []
    page, nb_pages = 0, 1
    while page < nb_pages and page < 10:
        params = (
            f"query=&hitsPerPage=100&page={page}"
            f"&filters=contract_type:apprenticeship"
            f"&aroundLatLng={LYON_LAT},{LYON_LON}&aroundRadius={RADIUS_KM * 1000}"
        )
        r = s.post(url, headers=headers, timeout=HTTP_TIMEOUT,
                   json={"requests": [{"indexName": WTTJ_INDEX, "params": params}]})
        r.raise_for_status()
        res = r.json()["results"][0]
        nb_pages = res.get("nbPages", 1)
        for h in res.get("hits", []):
            org = h.get("organization") or {}
            offices = h.get("offices") or [{}]
            lieu = ", ".join(sorted({o.get("city", "") for o in offices if o.get("city")})) or "Lyon"
            desc_parts = [h.get("summary") or "", h.get("profile") or ""]
            desc_parts += h.get("key_missions") or []
            prof = h.get("new_profession") or {}
            desc_parts.append(prof.get("sub_category_name") or "")
            dur = h.get("contract_duration_maximum") or h.get("contract_duration_minimum")
            if dur:
                desc_parts.append(f"Durée du contrat : {dur} mois")
            out.append({
                "titre": h.get("name", ""),
                "entreprise": org.get("name", ""),
                "lieu": lieu,
                "source": "Welcome to the Jungle",
                "date_publication": _iso_date(h.get("published_at")),
                "url": f"https://www.welcometothejungle.com/fr/companies/{org.get('slug')}/jobs/{h.get('slug')}",
                "description": "\n".join(p for p in desc_parts if p),
                "contrat": "alternance",
            })
        page += 1
    log.info("WTTJ → %d offres d'alternance dans un rayon de %d km", len(out), RADIUS_KM)
    return out


# --------------------------------------------------------------------------- #
# 3. La Bonne Alternance (API officielle apprentissage.beta.gouv.fr)
# --------------------------------------------------------------------------- #
LBA_URL = "https://api.apprentissage.beta.gouv.fr/api/job/v1/search"
LBA_ROMES = ["M1801", "M1802", "M1805", "M1810"]


def _dig(d, *keys, default=""):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return default if d is None else d


def collect_lba() -> list[dict] | None:
    """Nécessite une clé gratuite (variable d'env LBA_API_KEY).

    Inscription : https://api.apprentissage.beta.gouv.fr → « Mon compte » → « Jetons d'accès ».
    Sans clé, le collecteur est simplement ignoré.
    """
    token = os.environ.get("LBA_API_KEY", "").strip()
    if not token:
        log.info("La Bonne Alternance ignorée : pas de LBA_API_KEY (clé gratuite, voir README)")
        return None
    s = _session()
    r = s.get(
        LBA_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={"romes": ",".join(LBA_ROMES), "latitude": LYON_LAT,
                "longitude": LYON_LON, "radius": RADIUS_KM},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    out: list[dict] = []
    for job in data.get("jobs", []) or []:
        offer = job.get("offer") or {}
        wp = job.get("workplace") or {}
        loc = _dig(wp, "location", "address")
        contract = job.get("contract") or {}
        desc = "\n".join(str(x) for x in (
            offer.get("description"), offer.get("desired_skills"), offer.get("to_be_acquired_skills"),
            f"Durée du contrat : {contract.get('duration')} mois" if contract.get("duration") else "",
        ) if x)
        apply_url = _dig(job, "apply", "url") or _dig(job, "identifier", "url")
        jid = _dig(job, "identifier", "id")
        if not apply_url and jid:
            apply_url = f"https://labonnealternance.apprentissage.beta.gouv.fr/emploi/offres_emploi_lba/{jid}"
        out.append({
            "titre": offer.get("title", ""),
            "entreprise": wp.get("brand") or wp.get("name") or wp.get("legal_name") or "",
            "lieu": loc,
            "source": "La Bonne Alternance",
            "date_publication": _iso_date(_dig(offer, "publication", "creation")),
            "url": apply_url,
            "description": desc,
            "contrat": "alternance " + " ".join(contract.get("type") or []),
        })
    log.info("La Bonne Alternance → %d offres", len(out))
    return out


# --------------------------------------------------------------------------- #
# 4. ATS publics d'entreprises cibles (Greenhouse / Lever / SmartRecruiters)
# --------------------------------------------------------------------------- #
def _greenhouse(s, company: dict) -> list[dict]:
    r = s.get(f"https://boards-api.greenhouse.io/v1/boards/{company['id']}/jobs",
              params={"content": "true"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "titre": j.get("title", ""),
            "entreprise": company["name"],
            "lieu": _dig(j, "location", "name"),
            "source": f"ATS Greenhouse ({company['name']})",
            "date_publication": _iso_date(j.get("first_published") or j.get("updated_at")),
            "url": j.get("absolute_url", ""),
            "description": strip_html(_unescape(j.get("content", ""))),
            "contrat": "",
        })
    return out


def _unescape(s: str) -> str:
    import html
    return html.unescape(s or "")


def _lever(s, company: dict) -> list[dict]:
    r = s.get(f"https://api.lever.co/v0/postings/{company['id']}",
              params={"mode": "json"}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        lieu = cat.get("location") or ", ".join(cat.get("allLocations") or [])
        out.append({
            "titre": j.get("text", ""),
            "entreprise": company["name"],
            "lieu": lieu,
            "source": f"ATS Lever ({company['name']})",
            "date_publication": _iso_date(j.get("createdAt")),
            "url": j.get("hostedUrl", ""),
            "description": j.get("descriptionPlain") or strip_html(j.get("description")),
            "contrat": cat.get("commitment") or "",
        })
    return out


def _smartrecruiters(s, company: dict) -> list[dict]:
    out, offset, total = [], 0, 1
    while offset < total and offset < 1000:
        r = s.get(f"https://api.smartrecruiters.com/v1/companies/{company['id']}/postings",
                  params={"limit": 100, "offset": offset, "country": "fr"}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        total = d.get("totalFound", 0)
        content = d.get("content", [])
        if not content:
            break
        for j in content:
            loc = j.get("location") or {}
            lieu = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)
            type_emploi = _dig(j, "typeOfEmployment", "label")
            out.append({
                "titre": j.get("name", ""),
                "entreprise": _dig(j, "company", "name") or company["name"],
                "lieu": lieu,
                "source": f"ATS SmartRecruiters ({company['name']})",
                "date_publication": _iso_date(j.get("releasedDate")),
                "url": f"https://jobs.smartrecruiters.com/{company['id']}/{j.get('id')}",
                "description": " ".join(x for x in (
                    _dig(j, "function", "label"), _dig(j, "experienceLevel", "label"), type_emploi) if x),
                "contrat": type_emploi,
            })
        offset += len(content)
    return out


ATS_HANDLERS = {"greenhouse": _greenhouse, "lever": _lever, "smartrecruiters": _smartrecruiters}


def collect_ats(targets_path: Path) -> list[dict]:
    targets = json.loads(Path(targets_path).read_text(encoding="utf-8"))
    s = _session()
    out: list[dict] = []
    for ats, companies in targets.items():
        handler = ATS_HANDLERS.get(ats)
        if handler is None or not isinstance(companies, list):
            continue
        for company in companies:
            try:
                jobs = handler(s, company)
            except Exception as exc:  # noqa: BLE001
                log.warning("ATS %s / %s : %s", ats, company.get("name"), exc)
                continue
            local = [j for j in jobs if in_lyon_area(j["lieu"])]
            log.info("ATS %-15s %-22s → %4d offres FR, %3d à Lyon", ats, company["name"], len(jobs), len(local))
            out.extend(local)
    return out
