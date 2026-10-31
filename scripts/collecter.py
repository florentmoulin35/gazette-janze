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
import urllib.error
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
BUDGET_PRESSE = 180          # secondes au plus pour chercher l'aperçu des nouveaux articles
MAX_APERCUS = 25             # aperçus de presse cherchés par passage
PARIS = ZoneInfo("Europe/Paris")

UA = "Mozilla/5.0 (compatible; GazetteJanze/1.0; +https://florentmoulin35.github.io/gazette-janze/)"
# Certains journaux (Actu.fr) refusent le robot mais acceptent un navigateur :
# pour l'aperçu seulement, on réessaie en se présentant comme un navigateur.
UA_NAVIGATEUR = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
# Sites qui refusent même un navigateur depuis les serveurs de GitHub (testé le 30/10/2026) :
# inutile d'y perdre le budget, l'article garde juste son lien direct.
# (agence-api.ouest-france.fr, elle, répond : ses articles ont un aperçu.)
SITES_FERMES = r"www\.ouest-france\.fr|letelegramme\.fr|helloasso\.com|auto-infos\.fr|maville\.com"

JANZE_LISTE = "https://www.janze.fr/actualites"
PRESSE_RSS = "https://news.google.com/rss/search?q=Janz%C3%A9&hl=fr&gl=FR&ceid=FR:fr"
GN_DECODE = "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je"
# Sites trouvés par Google Actualités qui ne sont pas des journaux mais annoncent
# la vie locale (bals, spectacles, courses, matchs) : rangés en Vie locale.
SOURCES_LOCALES = r"helloasso|kidiklik|velopresse|federation francaise|score.n.co|agirc"
# Pages sans rapport avec l'actualité : écartées.
SOURCES_EXCLUES = r"iqair"
VIGIEAU = f"https://api.vigieau.gouv.fr/api/zones?commune={CODE_INSEE}&profil=particulier&zoneType=SUP"
CATNAT = f"https://georisques.gouv.fr/api/v1/gaspar/catnat?code_insee={CODE_INSEE}&page=1&page_size=100"
EAU = f"https://hubeau.eaufrance.fr/api/v1/qualite_eau_potable/resultats_dis?code_commune={CODE_INSEE}&size=100&sort=desc"
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


def telecharger(url: str, essais: int = 2, donnees: dict | None = None, ua: str = UA) -> str:
    corps = urllib.parse.urlencode(donnees).encode() if donnees else None
    requete = urllib.request.Request(url, data=corps, headers={
        "User-Agent": ua, "Accept-Language": "fr-FR,fr",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
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

def url_reelle(lien: str) -> str | None:
    """Adresse de l'article derrière un lien news.google.com/rss/articles/…
    (Google la masque : on la demande comme le fait sa page de redirection)."""
    if "news.google.com" not in lien:
        return lien
    code = urllib.parse.urlsplit(lien).path.rstrip("/").rsplit("/", 1)[-1]
    page = telecharger(f"https://news.google.com/articles/{code}", essais=1)
    sig = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sig and ts):
        return None
    demande = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "FR:fr", None, 1, None, None, None, None, None, 0, 1],
                              "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], code, int(ts.group(1)), sig.group(1)]
    reponse = telecharger(GN_DECODE, essais=1, donnees={"f.req": json.dumps([[["Fbv4je", json.dumps(demande), None, "generic"]]])})
    trouve = re.search(r'\\"garturlres\\",\\"(https?://[^"\\]+)', reponse)
    return trouve.group(1) if trouve and lien_valide(trouve.group(1)) else None


def apercu(url: str) -> str:
    """Chapeau public d'un article (balise og:description, celle des aperçus de partage)."""
    if re.search(SITES_FERMES, url):
        return ""
    try:
        page = telecharger(url, essais=1)
    except urllib.error.HTTPError as erreur:
        if erreur.code != 403:
            raise
        page = telecharger(url, essais=1, ua=UA_NAVIGATEUR)
    for motif in (r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"',
                  r'<meta[^>]+content="([^"]*)"[^>]+property="og:description"',
                  r'<meta[^>]+name="description"[^>]+content="([^"]*)"'):
        trouve = re.search(motif, page, re.I)
        if trouve and len(trouve.group(1).strip()) >= 40:
            return couper(trouve.group(1))
    return ""


def completer_presse(item: dict, connu: dict | None, debut: float, cherches: list) -> dict:
    """Reprend l'adresse et l'aperçu déjà trouvés, sinon les cherche (dans la limite du budget)."""
    budget_ok = len(cherches) < MAX_APERCUS and time.monotonic() - debut <= BUDGET_PRESSE
    if connu and connu.get("resolu"):
        repris = {**item, "url": connu["url"], "p": connu.get("p", ""), "resolu": True, "ap": connu.get("ap")}
        # Adresse connue mais pas d'aperçu : un seul nouvel essai avec le repli navigateur.
        if repris["p"] or repris["ap"] == 2 or not budget_ok:
            return repris
        cherches.append(item["id"])
        try:
            repris["p"] = apercu(repris["url"])
        except Exception as erreur:  # noqa: BLE001
            print(f"presse   aperçu impossible : {item['t'][:50]} ({erreur})", file=sys.stderr)
        repris["ap"] = 2
        return repris
    if not budget_ok:
        return item
    cherches.append(item["id"])
    try:
        url = url_reelle(item["url"])
    except Exception as erreur:  # noqa: BLE001 : on réessaiera la nuit suivante
        print(f"presse   adresse introuvable : {item['t'][:50]} ({erreur})", file=sys.stderr)
        return item
    if not url:
        return item
    try:
        resume = apercu(url)
    except Exception as erreur:  # noqa: BLE001 : certains journaux refusent les robots (403)
        print(f"presse   aperçu impossible : {item['t'][:50]} ({erreur})", file=sys.stderr)
        resume = ""
    # Même sans aperçu, on garde le lien direct vers l'article et on ne recherche plus.
    return {**item, "url": url, "p": resume, "resolu": True, "ap": 2}


def collecter_presse(deja: dict[str, dict] | None = None) -> list[dict]:
    deja = deja or {}
    items, vus = [], set()
    debut, cherches = time.monotonic(), []
    # Ouest-France avant ses reprises (maville.com…), pour garder la source d'origine.
    flux = sorted(lire_rss(telecharger(PRESSE_RSS)), key=lambda i: "maville" in i["source"].lower())
    for it in flux:
        source = it["source"] or "Presse"
        titre = re.sub(rf"\s+-\s+{re.escape(source)}$", "", it["titre"]) if source else it["titre"]
        # Détection du mot-clé : on ne garde que les articles dont le titre cite Janzé,
        # et pas le site de la mairie, déjà lu directement.
        if not cite_janze(titre) or "janze.fr" in source.lower() or not it["date"] or not lien_valide(it["lien"]):
            continue
        if re.search(SOURCES_EXCLUES, normaliser(source)):
            continue
        # Un même article repris par plusieurs sites (Ouest-France et maville.com…) : on le garde une fois.
        cle = cle_titre(titre)
        if cle in vus:
            continue
        vus.add(cle)
        item = {
            "id": ident("presse", it["lien"]), "d": it["date"], "cat": "presse", "press": True,
            "t": titre, "p": "", "src": source, "url": it["lien"], "a": "auto", "k": "presse",
        }
        items.append(classer(completer_presse(item, deja.get(item["id"]), debut, cherches)))
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


# --- Géorisques : arrêtés de catastrophe naturelle -----------------------------

def date_fr(texte: str | None) -> str | None:
    """« 21/09/2026 » -> « 2026-09-21 »."""
    try:
        return datetime.strptime((texte or "").strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def jour_fr(iso: str) -> str:
    mois = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
            "septembre", "octobre", "novembre", "décembre"]
    d = date_de(iso)
    return f"{'1er' if d.day == 1 else d.day} {mois[d.month - 1]} {d.year}"


def collecter_catnat() -> list[dict]:
    reponse = json.loads(telecharger(CATNAT))
    arretes: dict[str, dict] = {}
    for a in reponse.get("data") or []:
        jo = date_fr(a.get("date_publication_jo"))
        if not jo or not a.get("code_national_catnat"):
            continue
        arrete = arretes.setdefault(a["code_national_catnat"], {**a, "jo": jo, "risques": []})
        risque = (a.get("libelle_risque_jo") or "").strip()
        if risque and risque not in arrete["risques"]:
            arrete["risques"].append(risque)
    items = []
    for code, a in arretes.items():
        debut, fin = date_fr(a.get("date_debut_evt")), date_fr(a.get("date_fin_evt"))
        quand = "" if not debut else f" du {jour_fr(debut)}" if fin in (None, debut) else f" du {jour_fr(debut)} au {jour_fr(fin)}"
        risques = " ; ".join(a["risques"]).lower() or "événement naturel"
        # Le délai de déclaration n'est rappelé que tant qu'il court (30 jours après le JO).
        delai = (" Les sinistrés ont 30 jours après cette date pour déclarer leurs dommages à leur assureur."
                 if (date_de(aujourdhui()) - date_de(a["jo"])).days <= 30 else "")
        items.append({
            "id": f"catnat-{code}", "d": a["jo"], "cat": "alerte",
            "t": f"Catastrophe naturelle reconnue : {risques}",
            "p": f"L'État reconnaît l'état de catastrophe naturelle à Janzé pour l'événement{quand} ({risques}). "
                 f"L'arrêté est paru au Journal officiel le {jour_fr(a['jo'])}." + delai,
            "src": "Géorisques", "url": "https://www.georisques.gouv.fr/", "a": "auto", "k": "catnat",
        })
    return items


# --- Hub'Eau : qualité de l'eau du robinet -------------------------------------

# Rempli par collecter_eau, affiché dans la page Sources.
INFOS_SOURCES: dict[str, str] = {}


def collecter_eau() -> list[dict]:
    """Ne crée une alerte que si le dernier prélèvement dépasse une limite de qualité."""
    lignes = json.loads(telecharger(EAU)).get("data") or []
    if not lignes:
        return []
    dernier = lignes[0]["code_prelevement"]
    analyses = [l for l in lignes if l["code_prelevement"] == dernier]
    p = analyses[0]
    jour = p["date_prelevement"][:10]
    hors_limite = [l for l in analyses if "N" in (l.get("conformite_limites_bact_prelevement"),
                                                   l.get("conformite_limites_pc_prelevement"))]
    INFOS_SOURCES["eau"] = f"Dernier prélèvement le {jour_fr(jour)} : {'non conforme' if hors_limite else 'conforme'}"
    if not hors_limite:
        return []
    return [{
        "id": f"eau-{dernier}", "d": jour, "cat": "alerte",
        "t": "Eau du robinet : prélèvement non conforme",
        "p": couper(p.get("conclusion_conformite_prelevement") or "Le dernier contrôle sanitaire dépasse une limite de qualité.", 600),
        "src": "Hub'Eau (ARS)", "url": "https://orobnat.sante.gouv.fr/orobnat/",
        "a": "auto", "k": "eau",
    }]


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
        if i.get("p", "").startswith("Article paru dans"):   # ancien résumé générique
            i["p"] = ""
        if re.search(SOURCES_LOCALES, normaliser(i["src"])):
            i["cat"], i["press"] = "vie", False
            return i
        i["cat"], i["press"] = "presse", True
        cat = rubrique(i["t"])
        if cat != "vie":
            i["cat2"] = cat
    elif i["k"] in ("janze", "rafcom"):
        i["cat"] = rubrique(i["t"], i.get("p", ""))
    return i


COLLECTEURS = {"janze": collecter_janze, "presse": collecter_presse,
               "vigieau": collecter_vigieau, "rafcom": collecter_rafcom,
               "catnat": collecter_catnat, "eau": collecter_eau}


def lire_precedent() -> dict:
    if not SORTIE.exists():
        return {"items": [], "sources": {}}
    texte = SORTIE.read_text(encoding="utf-8")
    return json.loads(texte[texte.index("{"): texte.rindex("}") + 1])


def main() -> int:
    avant = lire_precedent()
    par_id = {i["id"]: i for i in avant.get("items", [])}
    maintenant = datetime.now(timezone.utc).isoformat(timespec="minutes")
    sources = dict(avant.get("sources", {}))
    nouveaux: dict[str, dict] = {}
    echecs = 0
    reussies = set()

    for cle, fonction in COLLECTEURS.items():
        try:
            if cle == "janze":
                argument = ({i["url"]: i for i in par_id.values() if i.get("k") == "janze"},)
            elif cle == "presse":
                argument = ({n: i for n, i in par_id.items() if i.get("k") == "presse"},)
            else:
                argument = ()
            trouves = fonction(*argument)
            for i in trouves:
                nouveaux[i["id"]] = i
            reussies.add(cle)
            sources[cle] = {"etat": "ok", "le": maintenant, "n": len(trouves),
                            **({"info": INFOS_SOURCES[cle]} if cle in INFOS_SOURCES else {})}
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
    # VigiEau et Hub'Eau disent l'état du jour : s'ils ont répondu, leurs anciennes alertes sont remplacées.
    for cle in ("vigieau", "eau"):
        if cle in reussies:
            par_id = {n: i for n, i in par_id.items() if i.get("k") != cle}
    tous = {**par_id, **nouveaux}
    items, titres_presse = [], set()
    for i in sorted(tous.values(), key=lambda i: (i["d"], "maville" not in i["src"].lower()), reverse=True):
        if i["d"] < limite or not lien_valide(i.get("url", "")):
            continue
        if i.get("fin") and i["fin"] < aujourdhui():   # arrêté VigiEau expiré
            continue
        if i["k"] == "rafcom" and autre_commune(i["t"], i.get("p", "")):
            continue
        if i["k"] == "presse" and re.search(SOURCES_EXCLUES, normaliser(i["src"])):
            continue
        i = classer(i)   # l'historique profite des règles de classement à jour
        if i["k"] == "presse":   # même article repris par plusieurs journaux, d'un soir à l'autre
            if cle_titre(i["t"]) in titres_presse:
                continue
            titres_presse.add(cle_titre(i["t"]))
        items.append(i)
    items = regrouper(items)[:MAX_ITEMS]

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


MOTS_VIDES = {"janze", "dans", "pour", "avec", "sont", "plus", "apres", "avant", "entre", "leur", "leurs",
              "cette", "tout", "tous", "fait", "font", "sera", "etre", "avoir", "35150", "voici", "quoi"}


def mots(titre: str) -> set[str]:
    return {m for m in re.findall(r"[a-z0-9]+", normaliser(titre)) if len(m) >= 4 and m not in MOTS_VIDES}


def regrouper(items: list[dict]) -> list[dict]:
    """Une même info racontée par plusieurs sources (mots du titre en commun,
    à 3 jours d'écart au plus) devient une seule carte : on garde la version la
    plus complète, et les autres restent accessibles par « Aussi dans … »."""
    def qualite(i):
        return (bool(i.get("p")), i["k"] != "presse", i["src"] == "Ouest-France")

    gardes: list[dict] = []
    for i in items:
        if i["k"] == "vigieau":
            gardes.append(i)
            continue
        mi = mots(i["t"])
        proche = None
        for g in gardes:
            if g["k"] == "vigieau" or abs((date_de(g["d"]) - date_de(i["d"])).days) > 3:
                continue
            mg = mots(g["t"])
            ni, ng = set(re.findall(r"\d+", i["t"])), set(re.findall(r"\d+", g["t"]))
            if ni and ng and ni != ng:   # « Division 2 » et « Division 3 » : deux matchs différents
                continue
            if mi and mg and len(mi & mg) / len(mi | mg) >= 0.4:
                proche = g
                break
        if proche is None:
            gardes.append(i)
            continue
        tete, autre = (i, proche) if qualite(i) > qualite(proche) else (proche, i)
        aussi = {a["url"]: a for a in proche.get("aussi", []) + i.get("aussi", [])}
        aussi[autre["url"]] = {"src": autre["src"], "url": autre["url"], "t": autre["t"]}
        aussi.pop(tete["url"], None)
        fusion = {**tete, "aussi": list(aussi.values())}
        gardes[gardes.index(proche)] = fusion
    return gardes


def date_de(texte: str):
    return datetime.fromisoformat(texte[:10]).date()


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
