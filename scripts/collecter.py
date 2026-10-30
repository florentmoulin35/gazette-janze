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
la veille. Codes de sortie :
- 0 : tout va bien (ou panne récente, de moins de 48 h) ;
- 1 : toutes les sources ont échoué ;
- 2 : auto.js est écrit, mais une source est en panne depuis plus de 48 h
  (le workflow enregistre auto.js puis passe en rouge pour prévenir).
"""

from __future__ import annotations

import email.utils
import hashlib
import html
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

RACINE = Path(__file__).resolve().parent.parent
SORTIE = RACINE / "donnees" / "auto.js"

MOT_CLE = "janze"            # mot-clé normalisé (sans accent, minuscules)
CODE_INSEE = "35136"
GARDER_JOURS = 400           # on oublie les infos de plus d'un an
MAX_ITEMS = 200
MAX_ARTICLES_NOUVEAUX = 15   # articles janze.fr à ouvrir par passage
PANNE_HEURES = 48            # au-delà, une source en panne fait échouer le workflow
BUDGET_JANZE = 300           # secondes au plus pour ouvrir les articles de janze.fr
PARIS = ZoneInfo("Europe/Paris")

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
# Autres communes de Roche aux Fées Communauté : une actu qui en cite une sans
# citer Janzé ne concerne pas la commune (chantier participatif à Marcillé-Robert…).
AUTRES_COMMUNES = r"arbrissel|boistrudan|chelun|coesmes|eance|\besse\b|forges.la.foret|theil.de.bretagne|marcille.robert|martigne.ferchaud|\bmoutiers\b|retiers|sainte.colombe|thourie"

# Rubrique devinée à partir du titre et du résumé (premier motif trouvé).
RUBRIQUES = [
    ("alerte", r"secheresse|crise|vigilance|alerte|tempete|inondation|catastrophe naturelle|canicule|coupure d.eau|orage|intemperie"),
    ("conseil", r"conseil municipal|conseil communautaire|deliberation|budget|election|\belu maire|maire elu|forum des elus|adjoints|mandat|le maire|compte.rendu"),
    ("urba", r"\bplu\b|urbanisme|permis de (construire|demolir|amenager)|declaration prealable|lotissement|\bzac\b|habitat|logement|haies?\b|cadastre"),
    # Grands équipements, seulement quand il est question de les construire ou de les déplacer.
    ("projets", r"(construction|nouveaux? locaux|extension|renovation|projet).{0,40}(gendarmerie|hopital|mediatheque|ecole|salle|complexe sportif|maison de sante)"
                r"|(gendarmerie|hopital|mediatheque|groupe scolaire|maison de sante).{0,40}(construction|nouveaux? locaux|demenage|travaux|projet)"
                r"|(futur|nouvel)(le)? (gendarmerie|hopital|mediatheque|ecole|groupe scolaire)|piste cyclable"),
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


def aujourdhui() -> str:
    """Date du jour à Janzé (le serveur de collecte est à l'heure UTC)."""
    return datetime.now(PARIS).date().isoformat()


def telecharger(url: str, essais: int = 2) -> str:
    requete = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr"})
    for essai in range(essais):
        try:
            with urllib.request.urlopen(requete, timeout=30) as reponse:  # noqa: S310
                return reponse.read().decode(reponse.headers.get_content_charset() or "utf-8", "replace")
        except Exception:  # noqa: BLE001 : une coupure passagère mérite un second essai
            if essai == essais - 1:
                raise
            time.sleep(3)
    raise AssertionError("inatteignable")


def lien_valide(url: str) -> bool:
    """N'accepte que les liens web (http/https) : pas de javascript:, data:, etc."""
    return urllib.parse.urlsplit((url or "").strip()).scheme.lower() in ("http", "https")


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
        moment = email.utils.parsedate_to_datetime(valeur)
    except (TypeError, ValueError):
        return None
    # 23 h 30 GMT le 30 octobre, c'est déjà le 31 à Janzé.
    return (moment.astimezone(PARIS) if moment.tzinfo else moment).date().isoformat()


def autre_commune(*textes: str) -> bool:
    """Cite une autre commune de la communauté sans citer Janzé."""
    texte = normaliser(" ".join(textes))
    return MOT_CLE not in texte and re.search(AUTRES_COMMUNES, texte) is not None


def date_iso(valeur: str | None) -> str | None:
    """« 2026-10-30T23:30:00+00:00 » → date à Janzé ; « 2026-10-30 » reste tel quel."""
    if not valeur:
        return None
    try:
        moment = datetime.fromisoformat(valeur.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment.astimezone(PARIS) if moment.tzinfo else moment).date().isoformat()


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
            self.date = date_iso(a.get("content")) or self.date
        elif tag == "time" and a.get("datetime") and not self.date:
            self.date = date_iso(a.get("datetime"))
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

    items, ouverts, debut = [], 0, time.monotonic()
    for url in liens:
        if not lien_valide(url):
            continue
        if url in deja:            # déjà lu un autre soir : on garde la version connue
            items.append(deja[url])
            continue
        if ouverts >= MAX_ARTICLES_NOUVEAUX or time.monotonic() - debut > BUDGET_JANZE:
            break
        ouverts += 1
        a = _Article()
        try:
            a.feed(telecharger(url))
        except Exception as erreur:  # noqa: BLE001 : un article illisible ne doit pas faire tomber les autres
            print(f"janze    article ignoré : {url} ({erreur})", file=sys.stderr)
            continue
        titre = re.sub(r"\s*\|\s*Ville de Janz.*$", "", a.titre).strip()
        if not titre:
            continue
        resume = couper(" ".join(a.paragraphes[:2]))
        items.append({
            "id": ident("janze", url), "d": a.date or aujourdhui(),
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
        if not cite_janze(titre) or "janze.fr" in source.lower() or not it["date"] or not lien_valide(it["lien"]):
            continue
        # Les feuilles de match (« HB Janzé vs … ») ne sont pas des articles.
        if re.search(r"\bvs\b", normaliser(titre)):
            continue
        # Un même article repris par plusieurs sites (Ouest-France et maville.com…) : on le garde une fois.
        cle = cle_titre(titre)
        if cle in vus:
            continue
        vus.add(cle)
        items.append(classer({
            "id": ident("presse", it["lien"]), "d": it["date"], "cat": "presse", "press": True,
            "t": titre, "p": f"Article paru dans {source}.", "src": source,
            "url": it["lien"], "a": "auto", "k": "presse",
        }))
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
        fin = (arrete.get("dateFinValidite") or "")[:10]
        if fin and fin < aujourdhui():   # arrêté expiré
            continue
        usages = [u for u in z.get("usages", []) if u.get("concerneParticulier")]
        url = arrete.get("cheminFichier") or ""
        if not lien_valide(url):
            url = "https://vigieau.gouv.fr"
        items.append({
            "id": f"vigieau-{arrete.get('id', 'x')}-{z.get('niveauGravite')}",
            "d": (arrete.get("dateDebutValidite") or aujourdhui())[:10],
            "cat": "alerte", "t": f"Restrictions d'eau : Janzé en {niveau}",
            "p": f"Un arrêté préfectoral place Janzé au niveau « {niveau} ». Voici ce qui change pour les particuliers.",
            "li": [couper(f"{u['nom']} : {u['description']}", 160) for u in usages[:6]],
            "src": "VigiEau", "url": url, "a": "auto", "k": "vigieau",
            **({"fin": fin} if fin else {}),
        })
    return items


# --- Roche aux Fées Communauté ------------------------------------------------

def collecter_rafcom() -> list[dict]:
    items, vus = [], set()
    for flux, tout_garder in RAFCOM.items():
        for it in lire_rss(telecharger(flux)):
            if it["lien"] in vus or not it["date"] or not lien_valide(it["lien"]):
                continue
            if not tout_garder and not cite_janze(it["titre"], it["resume"]):
                continue
            if autre_commune(it["titre"], it["resume"]):
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

def classer(i: dict) -> dict:
    """(Re)devine la rubrique. Un article de presse reste en Presse et peut
    apparaître aussi dans une rubrique (cat2) : Travaux, Alertes, Urbanisme…"""
    i = dict(i)
    if i["k"] == "presse":
        i.pop("cat2", None)
        cat = rubrique(i["t"])
        if cat != "vie":
            i["cat2"] = cat
    elif i["k"] in ("janze", "rafcom"):
        i["cat"] = rubrique(i["t"], i.get("p", ""))
    return i


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
    reussies = set()

    for cle, fonction in COLLECTEURS.items():
        try:
            argument = ({u: i for u, i in par_url.items() if i.get("k") == "janze"},) if cle == "janze" else ()
            trouves = fonction(*argument)
            for i in trouves:
                nouveaux[i["url"]] = i
            reussies.add(cle)
            sources[cle] = {"etat": "ok", "le": maintenant, "n": len(trouves)}
            print(f"{cle:8} ok      {len(trouves)} infos")
        except Exception as erreur:  # noqa: BLE001 : une source en panne ne doit pas tout arrêter
            echecs += 1
            ancien = avant.get("sources", {}).get(cle) or {}
            # Après une passe déjà en erreur, son « le » n'est pas une réussite.
            dernier_ok = ancien.get("dernier_ok") if ancien.get("etat") == "erreur" else ancien.get("le")
            sources[cle] = {"etat": "erreur", "le": maintenant, "msg": str(erreur)[:200],
                            "dernier_ok": dernier_ok}
            print(f"{cle:8} ERREUR  {erreur}", file=sys.stderr)

    # On cumule avec l'historique, sans doublon, en oubliant ce qui est trop ancien.
    limite = (datetime.now(PARIS).date() - timedelta(days=GARDER_JOURS)).isoformat()
    # VigiEau dit l'état du jour : s'il a répondu, ses anciennes alertes sont remplacées.
    if "vigieau" in reussies:
        par_url = {u: i for u, i in par_url.items() if i.get("k") != "vigieau"}
    tous = {**par_url, **nouveaux}
    items, titres_presse = [], set()
    for i in sorted(tous.values(), key=lambda i: (i["d"], "maville" not in i["src"].lower()), reverse=True):
        if i["d"] < limite or not lien_valide(i.get("url", "")):
            continue
        if i.get("fin") and i["fin"] < aujourdhui():   # arrêté VigiEau expiré
            continue
        if i["k"] == "rafcom" and autre_commune(i["t"], i.get("p", "")):
            continue
        i = classer(i)   # l'historique profite des règles de classement à jour
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
    if echecs == len(COLLECTEURS):
        return 1
    pannes = sources_en_panne(sources, datetime.now(timezone.utc))
    for message in pannes:
        print(message, file=sys.stderr)
    return 2 if pannes else 0


def sources_en_panne(sources: dict, maintenant: datetime) -> list[str]:
    """Messages pour les sources en erreur depuis plus de PANNE_HEURES."""
    messages = []
    for cle, etat in sources.items():
        if etat.get("etat") != "erreur":
            continue
        dernier_ok = etat.get("dernier_ok")
        if not dernier_ok:
            messages.append(f"Source {cle} en panne (aucune réussite connue) : {etat.get('msg', '')}")
            continue
        try:
            depuis = datetime.fromisoformat(dernier_ok)
        except ValueError:
            continue
        if maintenant - depuis > timedelta(hours=PANNE_HEURES):
            messages.append(f"Source {cle} en panne depuis le {dernier_ok} : {etat.get('msg', '')}")
    return messages


if __name__ == "__main__":
    sys.exit(main())
