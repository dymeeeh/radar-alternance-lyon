# 🎯 Radar Alternance Lyon

Radar 100 % gratuit qui collecte chaque matin les offres d'**alternance Cloud Ops / DevSecOps / SysOps / Admin Système-Réseau / Cybersécurité** dans un rayon de 30 km autour de Lyon, les filtre, les score sur 100 et publie :

- [`data/offres_du_jour.md`](data/offres_du_jour.md) — tableau cliquable (lisible sur GitHub, PC ou téléphone)
- [`data/offres_du_jour.json`](data/offres_du_jour.json) — même contenu, trié par score, à glisser dans l'artefact Claude

Schéma JSON : `id, titre, entreprise, lieu, source, date_publication, url, description, score, tags`.

## Sources

| Collecteur | Méthode | Remarque |
|---|---|---|
| LinkedIn + Indeed | `python-jobspy` (5 requêtes « alternance … ») | LinkedIn renvoie souvent des 429 depuis des IP de datacenter : les autres sources continuent quand même |
| Welcome to the Jungle | Index Algolia public du site, `contract_type:apprenticeship`, géo 30 km | |
| La Bonne Alternance | API officielle `api.apprentissage.beta.gouv.fr` (ROME M1801/M1802/M1805/M1810) | **Clé gratuite requise** (voir ci-dessous) ; ignorée sans clé |
| ATS entreprises | Greenhouse / Lever / SmartRecruiters (`config/targets.json`) | Seules les offres dans l'agglo lyonnaise sont gardées |

### Activer La Bonne Alternance (gratuit)
1. Créer un compte sur https://api.apprentissage.beta.gouv.fr et générer un jeton d'accès.
2. Sur GitHub : *Settings → Secrets and variables → Actions → New repository secret* : `LBA_API_KEY`.
3. En local : `$env:LBA_API_KEY = "..."` avant `python main.py`.

## Filtrage & scoring

- **Rejet** : pas d'alternance (CDI/CDD/freelance), stage, senior/lead/confirmé/expert, commercial/business developer, métiers hors IT (RH, marketing, BTP, électrotechnique…), support N1 purement bureautique.
- **Dédup** : empreintes `entreprise+titre` et `url` dans `data/seen_jobs.json` (conservées 180 jours).
- **Score /100** : cœur Cloud/DevOps +25 · Cyber/Réseau +20 · Lyon intra-muros/Villeurbanne/Limonest +15 · 24 mois/BUT/Licence/Bachelor +15 · « alternance » dans le titre +10 · mention 2027 +10 · publiée < 3 jours +5 · **−30** si éloigné ou helpdesk basique.

## Lancer en local

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py              # 72 h au 1er run, puis 24 h
.venv\Scripts\python main.py --no-dedup   # test sans toucher à seen_jobs.json
```

## Automatisation

`.github/workflows/radar.yml` tourne tous les jours à 06:00 UTC (07:00 Paris en hiver) et via *Actions → Run workflow*. Il ne committe dans `data/` que s'il y a de nouvelles offres.
