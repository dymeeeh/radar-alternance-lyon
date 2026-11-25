"""Petites fonctions texte partagées (normalisation sans accents, recherche par mots)."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache


def fold(text: object) -> str:
    """Minuscules + suppression des accents + espaces compactés."""
    if text is None:
        return ""
    s = str(text)
    if s.lower() == "nan":
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("’", "'")
    return re.sub(r"\s+", " ", s.lower()).strip()


@lru_cache(maxsize=None)
def _pattern(keyword: str) -> re.Pattern[str]:
    kw = re.escape(fold(keyword)).replace(r"\ ", r"[\s\-]+")
    return re.compile(rf"(?<![a-z0-9]){kw}(?![a-z0-9])")


def has_word(text_folded: str, keyword: str) -> bool:
    """Recherche du mot-clé en respectant les frontières de mots (texte déjà passé dans fold)."""
    return bool(_pattern(keyword).search(text_folded))


def matches(text_folded: str, keywords) -> list[str]:
    """Retourne les mots-clés (forme d'origine) trouvés dans le texte."""
    return [k for k in keywords if has_word(text_folded, k)]


def strip_html(text: object) -> str:
    if not text:
        return ""
    s = re.sub(r"<(br|/p|/li|/div)[^>]*>", "\n", str(text), flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
         .replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"[ \t]+", " ", s).strip()
