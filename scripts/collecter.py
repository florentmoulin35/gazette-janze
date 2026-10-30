#!/usr/bin/env python3
"""
Collecte de chaque nuit pour La Gazette de Janzé.

Lit les sources en ligne, garde ce qui concerne Janzé et écrit donnees/auto.js,
que la page index.html charge au démarrage. Aucune dépendance : bibliothèque
standard Python uniquement.

Sources lues :
- janze.fr : liste des actualités, puis chaque nouvel article (date et chapeau)
- Presse : Google Actualités, recherche « Janzé » (Ouest-France, Journal de Vitré…)
- VigiEau : niveau de restriction d'eau en vigueur à Janzé
- Roche aux Fées Communauté : flux RSS des actus (collectivité, mobilité, urbanisme…)

Une source en panne ne bloque pas les autres : on garde ce qu'elle avait donné
la veille. Le script échoue (code 1) seulement si toutes les sources échouent.
"""

from __future__ import annotations

import email.utils
import hashlib
import html
import json
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
SORTIE = RACINE / "donnees" / "auto.js"

MOT_CLE = "janze"            # mot-clé normalisé (sans accent, minuscules)
CODE_INSEE = "35136"
GARDER_JOURS = 400           # on oublie les infos de plus d'un an
MAX_ITEMS = 200
MAX_ARTICLES_NOUVEAUX = 15   # articles janze.fr à ouvrir par passage

UA = "Mozilla/5.0 (compatible; GazetteJanze/1.0; +https://florentmoulin35.github.io/gazette-janze/)"

JANZE_LISTE = "https://www.janze.fr/actualites"
PRESSE_RSS = "https://news.google.com/rss/search?q=Janz%C3%A9&hl=fr&gl=FR&ceid=FR:fr"
VIGIEAU = f"https://api.vigieau.gouv.fr/api/zones?commune={CODE_INSEE}&profil=particulier&zoneType=SUP"
# Flux « Actu » de rafcom.bzh : on garde tout pour les thèmes qui touchent la
# vie de la commune, et seulement ce qui cite Janzé pour les autres.
RAFCOM = {
    "https://www.rafcom.bzh/taxonomy/term/1%2C69/feed": True,    # Collectivité
    "https://www.rafcom.bzh/taxonomy/term/1%2C257/feed": True,   # Mobilité
    "https://www.rafcom.bzh/taxonomy/term/1%2C143/feed": True,   # Urbanisme / Habitat
    "https://www.rafcom.bzh/taxonomy/term/1%2C502/feed": False,  # Territoire
    "https://www.rafcom.bzh/taxonomy/term/1%2C4/feed": False,    # Environnement - Energie
}

# Rubrique devinée à partir du titre et du résumé (premier motif trouvé).
RUBRIQUES = [
    ("alerte", r"secheresse|crise|vigilance|alerte|tempete|inondation|catastrophe naturelle|canicule|coupure d.eau|orage|intemperie"),
    ("conseil", r"conseil municipal|conseil communautaire|deliberation|budget|election|\belus?\b|le maire|compte.rendu"),
    ("urba", r"\bplu\b|urbanisme|permis de (construire|demolir|amenager)|declaration prealable|lotissement|\bzac\b|habitat|logement|haies?\b|cadastre"),
    ("travaux", r"travaux|chantier|circulation|deviation|\broute|stationnement|voirie|parking|fermee?s? a la circulation|reseau d.eau|assainissement"),
]
NIVEAUX_EAU = {"alerte": "alerte", "alerte_renforcee": "alerte renforcée", "crise": "crise"}


def normaliser(texte: str) -> str:
    texte = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", texte.lower()).strip()


def cite_janze(*textes: str) -> bool:
    return any(MOT_CLE in normaliser(t) for t in textes)


def rubrique(titre: str, resume: str = "") -> str:
    texte = normaliser(f"{titre} {resume}")
    for cat, motif in RUBRIQUES:
        if re.search(motif, texte):
            return cat
    return "vie"


def telecharger(url: str) -> str:
    requete = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr"})
    with urllib.request.urlopen(requete, timeout=30) as reponse:  # noqa: S310
        return reponse.read().decode(reponse.headers.get_content_charset() or "utf-8", "replace")


def ident(prefixe: str, url: str) -> str:
    return f"{prefixe}-{hashlib.sha1(url.encode()).hexdigest()[:10]}"


def titre_propre(titre: str) -> str:
    """Remet en minuscules un titre écrit tout en capitales (« SÉCHERESSE : LE … »)."""
    titre = re.sub(r"\s+", " ", html.unescape(titre or "")).strip()
    if len(titre) > 12 and titre == titre.upper():
        titre = titre[0] + titre[1:].lower()
    return titre


def cle_titre(titre: str) -> str:
    return normaliser(re.sub(r"\W+", " ", titre))[:70]


def couper(texte: str, n: int = 280) -> str:
    texte = re.sub(r"\s+", " ", html.unescape(texte or "")).strip()
    return texte if len(texte) <= n else texte[: n - 1].rsplit(" ", 1)[0] + "…"


def date_rss(valeur: str | None) -> str | None:
    if not valeur:
        return None
    try:
        return email.utils.parsedate_to_datetime(valeur).date().isoformat()
    except (TypeError, ValueError):
        return None


def _reparer_xml(xml: str) -> str:
    """Corrige les flux mal formés : & isolés, entités HTML (&nbsp;…), caractères de contrôle."""
    xml = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml)
    xml = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)([a-zA-Z]+;)?",
                 lambda m: html.unescape(m.group(0)).replace("&", "&amp;").replace("<", "&lt;") if m.group(2) else "&amp;",
                 xml)
    return xml


def lire_rss(xml: str) -> list[dict]:
    """Retourne les <item> d'un flux RSS 2.0 sous forme de dictionnaires."""
    try:
        racine = ET.fromstring(xml.encode())
    except ET.ParseError:
        racine = ET.fromstring(_reparer_xml(xml).encode())
    items = []
    for it in racine.iter("item"):
        source = it.find("source")
        items.append({
            "titre": (it.findtext("title") or "").strip(),
            "lien": (it.findtext("link") or "").strip(),
            "date": date_rss(it.findtext("pubDate")),
            "resume": re.sub(r"<[^>]+>", " ", it.findtext("description") or ""),
            "source": (source.text or "").strip() if source is not None else "",
        })
    return items


# --- janze.fr ---------------------------------------------------------------

class _Liens(HTMLParser):
    """Récupère les liens /actualites/<slug> d'une page de liste."""

    def __init__(self):
        super().__init__()
        self.liens: list[str] = []

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href") or ""
        if tag == "a" and re.match(r"^(https://www\.janze\.fr)?/actualites/[a-z0-9-]+/?$", href):
            url = urllib.parse.urljoin("https://www.janze.fr", href).rstrip("/")
            if url not in self.liens:
                self.liens.append(url)


class _Article(HTMLParser):
    """Lit le titre, la date de publication et les paragraphes d'un article Drupal."""

    def __init__(self):
        super().__init__()
        self.titre = ""
        self.date = None
        self.paragraphes: list[str] = []
        self._dans = None
        self._tampon = ""
        self._apres_h1 = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("property") == "article:published_time":
            self.date = (a.get("content") or "")[:10] or None
        elif tag == "meta" and a.get("property") == "og:title" and not self.titre:
            self.titre = a.get("content") or ""
        elif tag in ("h1", "p") and self._dans is None:
            self._dans, self._tampon = tag, ""

    def handle_endtag(self, tag):
        if tag != self._dans:
            return
        texte = re.sub(r"\s+", " ", self._tampon).strip()
        if tag == "h1":
            self._apres_h1 = True
            self.titre = self.titre or texte
        elif self._apres_h1 and len(texte) >= 40:
            self.paragraphes.append(texte)
        self._dans = None

    def handle_data(self, data):
        if self._dans:
            self._tampon += data


def collecter_janze(deja: dict[str, dict]) -> list[dict]:
    liens = []
    for page in (JANZE_LISTE, JANZE_LISTE + "?page=1"):
        p = _Liens()
        p.feed(telecharger(page))
        liens += [l for l in p.liens if l not in liens]
    if not liens:
        raise RuntimeError("aucun lien d'actualité trouvé sur janze.fr (la page a peut-être changé)")

    items, ouverts = [], 0
    for url in liens:
        if url in deja:            # déjà lu un autre soir : on garde la version connue
            items.append(deja[url])
            continue
        if ouverts >= MAX_ARTICLES_NOUVEAUX:
            break
        ouverts += 1
        a = _Article()
        a.feed(telecharger(url))
        titre = re.sub(r"\s*\|\s*Ville de Janz.*$", "", a.titre).strip()
        if not titre:
            continue
        resume = couper(" ".join(a.paragraphes[:2]))
        items.append({
            "id": ident("janze", url), "d": a.date or date.today().isoformat(),
            "cat": rubrique(titre, resume), "t": titre, "p": resume,
            "src": "janze.fr", "url": url, "a": "auto", "k": "janze",
        })
    return items


# --- Presse -----------------------------------------------------------------

def collecter_presse() -> list[dict]:
    items, vus = [], set()
    # Ouest-France avant ses reprises (maville.com…), pour garder la source d'origine.
    flux = sorted(lire_rss(telecharger(PRESSE_RSS)), key=lambda i: "maville" in i["source"].lower())
    for it in flux:
        source = it["source"] or "Presse"
        titre = re.sub(rf"\s+-\s+{re.escape(source)}$", "", it["titre"]) if source else it["titre"]
        # Détection du mot-clé : on ne garde que les articles dont le titre cite Janzé,
        # et pas le site de la mairie, déjà lu directement.
        if not cite_janze(titre) or "janze.fr" in source.lower() or not it["date"]:
            continue
        # Les feuilles de match (« HB Janzé vs … ») ne sont pas des articles.
        if re.search(r"\bvs\b", normaliser(titre)):
            continue
        # Un même article repris par plusieurs sites (Ouest-France et maville.com…) : on le garde une fois.
        cle = cle_titre(titre)
        if cle in vus:
            continue
        vus.add(cle)
        items.append({
            "id": ident("presse", it["lien"]), "d": it["date"], "cat": "presse", "press": True,
            "t": titre, "p": f"Article paru dans {source}.", "src": source,
            "url": it["lien"], "a": "auto", "k": "presse",
        })
    return items


# --- VigiEau ----------------------------------------------------------------

def collecter_vigieau() -> list[dict]:
    zones = json.loads(telecharger(VIGIEAU))
    items = []
    for z in zones if isinstance(zones, list) else [zones]:
        niveau = NIVEAUX_EAU.get(z.get("niveauGravite") or "")
        arrete = z.get("arrete") or {}
        if not niveau:
            continue
        usages = [u for u in z.get("usages", []) if u.get("concerneParticulier")]
        url = arrete.get("cheminFichier") or "https://vigieau.gouv.fr"
        items.append({
            "id": f"vigieau-{arrete.get('id', 'x')}-{z.get('niveauGravite')}",
            "d": (arrete.get("dateDebutValidite") or date.today().isoformat())[:10],
            "cat": "alerte", "t": f"Restrictions d'eau : Janzé en {niveau}",
            "p": f"Un arrêté préfectoral place Janzé au niveau « {niveau} ». Voici ce qui change pour les particuliers.",
            "li": [couper(f"{u['nom']} : {u['description']}", 160) for u in usages[:6]],
            "src": "VigiEau", "url": url, "a": "auto", "k": "vigieau",
        })
    return items


# --- Roche aux Fées Communauté ------------------------------------------------

def collecter_rafcom() -> list[dict]:
    items, vus = [], set()
    for flux, tout_garder in RAFCOM.items():
        for it in lire_rss(telecharger(flux)):
            if it["lien"] in vus or not it["date"]:
                continue
            if not tout_garder and not cite_janze(it["titre"], it["resume"]):
                continue
            vus.add(it["lien"])
            resume = couper(it["resume"])
            items.append({
                "id": ident("rafcom", it["lien"]), "d": it["date"],
                "cat": rubrique(it["titre"], resume), "t": titre_propre(it["titre"]), "p": resume,
                "src": "Roche aux Fées Communauté", "url": it["lien"], "a": "auto", "k": "rafcom",
            })
    return items


# --- Assemblage ---------------------------------------------------------------

COLLECTEURS = {"janze": collecter_janze, "presse": collecter_presse,
               "vigieau": collecter_vigieau, "rafcom": collecter_rafcom}


def lire_precedent() -> dict:
    if not SORTIE.exists():
        return {"items": [], "sources": {}}
    texte = SORTIE.read_text(encoding="utf-8")
    return json.loads(texte[texte.index("{"): texte.rindex("}") + 1])


def main() -> int:
    avant = lire_precedent()
    par_url = {i["url"]: i for i in avant.get("items", [])}
    maintenant = datetime.now(timezone.utc).isoformat(timespec="minutes")
    sources = dict(avant.get("sources", {}))
    nouveaux: dict[str, dict] = {}
    echecs = 0

    for cle, fonction in COLLECTEURS.items():
        try:
            argument = ({u: i for u, i in par_url.items() if i.get("k") == "janze"},) if cle == "janze" else ()
            trouves = fonction(*argument)
            for i in trouves:
                nouveaux[i["url"]] = i
            sources[cle] = {"etat": "ok", "le": maintenant, "n": len(trouves)}
            print(f"{cle:8} ok      {len(trouves)} infos")
        except Exception as erreur:  # noqa: BLE001 : une source en panne ne doit pas tout arrêter
            echecs += 1
            sources[cle] = {"etat": "erreur", "le": maintenant, "msg": str(erreur)[:200],
                            "dernier_ok": (avant.get("sources", {}).get(cle) or {}).get("le")}
            print(f"{cle:8} ERREUR  {erreur}", file=sys.stderr)

    # On cumule avec l'historique, sans doublon, en oubliant ce qui est trop ancien.
    limite = (date.today() - timedelta(days=GARDER_JOURS)).isoformat()
    tous = {**par_url, **nouveaux}
    items, titres_presse = [], set()
    for i in sorted(tous.values(), key=lambda i: (i["d"], "maville" not in i["src"].lower()), reverse=True):
        if i["d"] < limite:
            continue
        if i["k"] == "presse":   # même article repris par plusieurs journaux, d'un soir à l'autre
            if cle_titre(i["t"]) in titres_presse:
                continue
            titres_presse.add(cle_titre(i["t"]))
        items.append(i)
    items = items[:MAX_ITEMS]

    donnees = {"maj": maintenant, "sources": sources, "items": items}
    SORTIE.parent.mkdir(exist_ok=True)
    SORTIE.write_text(
        "// Fichier écrit chaque nuit par scripts/collecter.py : ne pas modifier à la main.\n"
        "window.GAZETTE_AUTO = " + json.dumps(donnees, ensure_ascii=False, indent=1) + ";\n",
        encoding="utf-8",
    )
    print(f"-> {SORTIE.relative_to(RACINE)} : {len(items)} infos")
    return 1 if echecs == len(COLLECTEURS) else 0


if __name__ == "__main__":
    sys.exit(main())
