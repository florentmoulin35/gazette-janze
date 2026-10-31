#!/usr/bin/env python3
"""
Résume les CSV d'urbanisme de Janzé (donnees/urbanisme/*.csv, produits par
filtrer_urbanisme_janze.py) en un petit fichier donnees/urbanisme.js que la
page index.html affiche dans la rubrique Urbanisme.

On ne publie que des chiffres agrégés et quelques lignes récentes : la page
reste légère (quelques Ko). Le nom du demandeur est affiché quand Sitadel le
publie, c'est-à-dire pour les personnes morales (entreprises, SCI, collectivités) :
la base nationale ne contient pas le nom des particuliers.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

DOSSIER = Path(__file__).resolve().parent.parent / "donnees" / "urbanisme"
SORTIE = DOSSIER.parent / "urbanisme.js"

# Codes de la base Sitadel (dictionnaire des variables du SDES).
ETAT_ANNULE = "4"
ETATS = {"2": "Autorisé", "5": "Chantier commencé", "6": "Terminé"}
NATURE = {"1": "Construction neuve", "2": "Travaux sur un bâtiment existant"}


def lire(nom: str) -> pd.DataFrame:
    """Charge un CSV filtré (tout en texte, cases vides = chaîne vide)."""
    chemin = DOSSIER / nom
    if not chemin.exists():
        return pd.DataFrame()
    return pd.read_csv(chemin, sep=";", dtype=str, keep_default_na=False)


def reparer(texte: str) -> str:
    """Corrige les accents abîmés de la source (« CAÃÂVENNES » -> « CÉVENNES »).

    Dans la base Sitadel, les lettres accentuées ont été encodées plusieurs fois
    puis passées en majuscules : « É » devient « AÃ\\x82Â\\x89 ». On retrouve
    l'octet d'origine derrière chaque « AÃ\\x82Â », puis on redécode en UTF-8.
    """
    texte = re.sub("AÃ\x82Â([\x80-\xbf©])", lambda m: "Ã" + m.group(1), texte)
    for _ in range(2):
        try:
            texte = re.sub("Ã([\x80-\xbf©])", lambda m: bytes([0xC3, ord(m.group(1))]).decode("utf-8"), texte)
        except UnicodeDecodeError:
            break
    return texte.replace("Ã", "")


def adresse(ligne: pd.Series) -> str:
    """Rue du terrain, en minuscules lisibles (« RUE DES CÉVENNES » -> « Rue des Cévennes »)."""
    voie = reparer(ligne.get("ADR_LIBVOIE_TER", "")).strip()
    voie = re.sub(r"^\d+\s*", "", voie)  # numéro parfois collé à la voie
    if not voie:
        voie = reparer(ligne.get("ADR_LIEUDIT_TER", "")).strip()
    petits = {"de", "des", "du", "la", "le", "les", "l", "d", "et", "aux", "au", "a"}
    mots = re.split(r"(\s+|-|')", voie.lower())
    return "".join(m if (m in petits and i) else m.capitalize() for i, m in enumerate(mots)) or "Adresse non précisée"


def entier(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(0).astype(int)


def societe(ligne: pd.Series) -> str:
    """Nom du demandeur quand c'est une personne morale (entreprise, SCI, collectivité).

    Sitadel ne publie pas le nom des particuliers : la case est alors vide.
    """
    return reparer(ligne.get("DENOM_DEM", "")).strip()


# Quartier suivi : la rue des Hirondelles et les rues qui la touchent (repérées
# avec les coordonnées des ventes DVF : toutes à moins de 110 m). Les variantes
# d'écriture de la source sont couvertes (« RUE RENE DE CHATEAUBRIANT »,
# « RUE FRANCOIS-RENE CHATEAUB »…). La ligne de train et la route de
# Châteaubriant, plus loin, sont exclues.
QUARTIER = re.compile(r"HIRONDELLE|ALOUETTE|MESANGE|CHATEAUBRIAN?[DT]?\b|CHATEAUB$")
HORS_QUARTIER = re.compile(r"LIGNE|ROUTE|RTE")


def dans_quartier(voie: str) -> bool:
    voie = voie.upper()
    return bool(QUARTIER.search(voie)) and not HORS_QUARTIER.search(voie)


# Nom affiché pour chaque rue du quartier, quelle que soit l'écriture de la source.
RUES_QUARTIER = [("HIRONDELLE", "Rue des Hirondelles"), ("ALOUETTE", "Rue des Alouettes"),
                 ("MESANGE", "Rue des Mésanges"), ("CHATEAUB", "Rue Chateaubriand")]


def rue_quartier(voie: str) -> str:
    voie = voie.upper()
    nom = next((n for cle, n in RUES_QUARTIER if cle in voie), voie.title())
    lot = re.search(r"LOT\D*(\d+)", voie)
    return f"{nom} (lot {lot.group(1)})" if lot else nom


def numero(ligne: pd.Series, colonne: str) -> str:
    """Numéro dans la rue, sauf quand la source y a mis un numéro de lot ou de parcelle."""
    n = str(ligne.get(colonne, "")).strip()
    return n if re.fullmatch(r"\d{1,4}\s?(BIS|TER|[A-E])?", n, re.I) else ""


def resumer_logements() -> dict:
    df = lire("sitadel_logements_janze.csv")
    if df.empty:
        return {}
    df = df[df.ETAT_DAU != ETAT_ANNULE].copy()
    df["an"] = df.DATE_REELLE_AUTORISATION.str[:4]
    df["lgt"] = entier(df.NB_LGT_TOT_CREES)
    df["ind"] = entier(df.NB_LGT_IND_CREES)
    df["col"] = entier(df.NB_LGT_COL_CREES)
    par_an = (df.groupby("an").agg(permis=("lgt", "size"), logements=("lgt", "sum"),
                                   individuels=("ind", "sum"), collectifs=("col", "sum"))
              .reset_index().sort_values("an"))
    recents = df.sort_values("DATE_REELLE_AUTORISATION", ascending=False).head(12)
    return {
        "par_an": par_an.to_dict("records"),
        "recents": [{
            "d": l.DATE_REELLE_AUTORISATION,
            "voie": adresse(l),
            "lgt": int(l.lgt),
            "nature": NATURE.get(l.NATURE_PROJET_DECLAREE, ""),
            "etat": ETATS.get(l.ETAT_DAU, ""),
            "societe": societe(l),
        } for _, l in recents.iterrows()],
    }


def resumer_simples(nom: str, surface: str | None = None) -> dict:
    """Locaux, permis d'aménager, permis de démolir : nombre par an et derniers dossiers."""
    df = lire(nom)
    if df.empty:
        return {}
    # La colonne d'état s'appelle ETAT_DAU, ETAT_PA (aménager) ou ETAT_PD (démolir).
    etat = next(c for c in df.columns if c.startswith("ETAT_"))
    df = df[df[etat] != ETAT_ANNULE].copy()
    df["an"] = df.DATE_REELLE_AUTORISATION.str[:4]
    recents = df.sort_values("DATE_REELLE_AUTORISATION", ascending=False).head(6)
    sortie = {
        "total": len(df),
        "depuis": df.an.min(),
        "par_an": df.groupby("an").size().to_dict(),
        "recents": [{"d": l.DATE_REELLE_AUTORISATION, "voie": adresse(l), "societe": societe(l),
                     **({"m2": int(entier(pd.Series([l[surface]]))[0])} if surface else {})}
                    for _, l in recents.iterrows()],
    }
    return sortie


def resumer_ventes() -> dict:
    df = lire("dvf_janze.csv")
    if df.empty:
        return {}
    df = df[df.nature_mutation == "Vente"]
    # Une vente (id_mutation) a une ligne par bien et par parcelle. On ne garde, pour
    # les prix, que les ventes d'un seul logement, sinon le prix couvre plusieurs biens.
    locaux = df[df.type_local.isin(["Maison", "Appartement"])]
    nb = locaux.groupby("id_mutation").type_local.agg(["size", "first"])
    simples = nb[nb["size"] == 1].index
    v = locaux[locaux.id_mutation.isin(simples)].copy()
    v["prix"] = pd.to_numeric(v.valeur_fonciere, errors="coerce")
    v["surf"] = pd.to_numeric(v.surface_reelle_bati, errors="coerce")
    v = v[(v.prix > 10_000) & (v.surf > 10)]
    v["m2"] = v.prix / v.surf
    v["an"] = v.date_mutation.str[:4]
    lignes = []
    for an, g in v.groupby("an"):
        maisons = g[g.type_local == "Maison"]
        apparts = g[g.type_local == "Appartement"]
        lignes.append({
            "an": an,
            "maisons": len(maisons),
            "prix_maison": int(round(maisons.prix.median(), -3)) if len(maisons) else None,
            "m2_maison": int(round(maisons.m2.median(), -1)) if len(maisons) else None,
            "apparts": len(apparts),
            "m2_appart": int(round(apparts.m2.median(), -1)) if len(apparts) >= 5 else None,
        })
    return {
        "ventes": int(df.id_mutation.nunique()),
        "du": df.date_mutation.min(),
        "au": df.date_mutation.max(),
        "par_an": lignes,
    }


def resumer_quartier() -> dict:
    """Tous les permis et toutes les ventes de la rue des Hirondelles et des rues voisines."""
    permis = []
    fichiers = [("sitadel_logements_janze.csv", "Logement"), ("sitadel_locaux_non_residentiels_janze.csv", "Local"),
                ("sitadel_permis_amenager_janze.csv", "Aménagement"), ("sitadel_permis_demolir_janze.csv", "Démolition")]
    for nom, genre in fichiers:
        df = lire(nom)
        if df.empty:
            continue
        etat = next(c for c in df.columns if c.startswith("ETAT_"))
        for _, l in df[df[etat] != ETAT_ANNULE].iterrows():
            if not dans_quartier(f"{l.ADR_LIBVOIE_TER} {l.ADR_LIEUDIT_TER}"):
                continue
            lgt = int(entier(pd.Series([l.get("NB_LGT_TOT_CREES", "0")]))[0])
            quoi = (f"{lgt} logement{'s' if lgt > 1 else ''}" if genre == "Logement" and lgt
                    else "Travaux sur une maison" if genre == "Logement" else genre)
            permis.append({"d": l.DATE_REELLE_AUTORISATION, "n": numero(l, "ADR_NUM_TER"),
                           "voie": rue_quartier(f"{l.ADR_LIBVOIE_TER} {l.ADR_LIEUDIT_TER}"),
                           "quoi": quoi, "nature": NATURE.get(l.get("NATURE_PROJET_DECLAREE", ""), ""),
                           "etat": ETATS.get(l[etat], ""), "societe": societe(l)})
    ventes = []
    df = lire("dvf_janze.csv")
    if not df.empty:
        df = df[df.adresse_nom_voie.map(dans_quartier)]
        for mid, g in df.groupby("id_mutation"):
            locaux = g[g.type_local.isin(["Maison", "Appartement", "Local industriel. commercial ou assimilé"])]
            l = (locaux if len(locaux) else g).iloc[0]
            surf = pd.to_numeric(locaux.surface_reelle_bati, errors="coerce").sum()
            prix = pd.to_numeric(l.valeur_fonciere, errors="coerce")
            type_bien = {"Local industriel. commercial ou assimilé": "Local commercial"}.get(l.type_local, l.type_local)
            ventes.append({"d": l.date_mutation, "n": l.adresse_numero, "voie": rue_quartier(l.adresse_nom_voie),
                           "type": type_bien or ("Terrain" if l.nature_culture else "Dépendance"),
                           "m2": int(surf) if surf else None, "prix": int(prix) if prix == prix else None,
                           "nature": l.nature_mutation,
                           # Surface du terrain : une fois par parcelle (une parcelle peut avoir plusieurs lignes).
                           "terrain": int(pd.to_numeric(g.drop_duplicates("id_parcelle").surface_terrain, errors="coerce").sum()) or None,
                           # Position de la parcelle, pour l'ouvrir dans Google Earth.
                           **({"lat": round(float(pos.latitude.iloc[0]), 6), "lon": round(float(pos.longitude.iloc[0]), 6)}
                              if len(pos := g[g.latitude != ""]) else {})})
    return {"permis": sorted(permis, key=lambda x: x["d"], reverse=True),
            "ventes": sorted(ventes, key=lambda x: x["d"], reverse=True)}


def main() -> None:
    if not DOSSIER.exists():
        sys.exit(f"Dossier introuvable : {DOSSIER}. Lancez d'abord filtrer_urbanisme_janze.py.")
    resume = {
        "maj": date.today().isoformat(),
        "logements": resumer_logements(),
        "locaux": resumer_simples("sitadel_locaux_non_residentiels_janze.csv", "SURF_LOC_CREEE"),
        "amenager": resumer_simples("sitadel_permis_amenager_janze.csv"),
        "demolir": resumer_simples("sitadel_permis_demolir_janze.csv"),
        "ventes": resumer_ventes(),
        "quartier": resumer_quartier(),
    }
    texte = json.dumps(resume, ensure_ascii=False, separators=(",", ":"), default=int)
    SORTIE.write_text(
        "// Fichier écrit par scripts/resumer_urbanisme.py, ne pas modifier à la main.\n"
        f"window.URBA = {texte};\n", encoding="utf-8")
    print(f"{SORTIE} ({SORTIE.stat().st_size / 1024:.1f} Ko)")


if __name__ == "__main__":
    main()
