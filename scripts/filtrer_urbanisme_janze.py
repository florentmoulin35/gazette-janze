#!/usr/bin/env python3
"""
Filtre les fichiers nationaux d'urbanisme de data.gouv.fr pour ne garder que
les lignes de Janzé, et les enregistre dans des CSV légers.

Sources (vérifiées sur data.gouv.fr le 31/10/2026) :
- Sitadel, « Liste des permis de construire et autres autorisations
  d'urbanisme » (SDES) : https://www.data.gouv.fr/datasets/689c42fa521ccf80ce954f83/
  4 CSV servis par l'API DiDo du ministère (logements, locaux, permis
  d'aménager, permis de démolir), jusqu'à ~360 Mo chacun.
- DVF géolocalisées, « Demandes de valeurs foncières » (ventes immobilières) :
  https://www.data.gouv.fr/datasets/demandes-de-valeurs-foncieres-geolocalisees/
  un seul fichier dvf.csv.gz pour toute la France sur 5 ans.

Ces fichiers sont trop gros pour la mémoire : on les lit en flux, par morceaux
(chunks) de quelques centaines de milliers de lignes, et on n'écrit que les
lignes de Janzé au fur et à mesure. Rien n'est stocké en entier, ni en mémoire
ni sur le disque (les .gz sont décompressés à la volée).

Exemples
--------
# 1. Télécharger et filtrer directement les 4 fichiers Sitadel (rien à installer
#    à part pandas) :
python filtrer_urbanisme_janze.py --sitadel

# 2. Ventes immobilières (DVF) à Janzé, 5 dernières années :
python filtrer_urbanisme_janze.py --dvf

# 3. Filtrer un fichier déjà téléchargé (CSV ou CSV.gz) :
python filtrer_urbanisme_janze.py --entree permis_logements.csv --sortie janze_logements.csv

# 4. Filtrer sur le nom de commune plutôt que sur le code INSEE :
python filtrer_urbanisme_janze.py --entree fichier.csv --colonne commune --valeur "Janzé"
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

import pandas as pd

# Code INSEE de Janzé. Plus fiable que le nom : pas de souci d'accents, de
# majuscules ou d'homonymes.
CODE_INSEE_JANZE = "35136"
NOM_JANZE = "Janzé"

# Les 4 fichiers CSV de la base Sitadel (identifiants DiDo relevés sur
# data.gouv.fr le 31/10/2026). Sans millésime, l'API renvoie le plus récent.
DIDO = "https://data.statistiques.developpement-durable.gouv.fr/dido/api/v1/datafiles/{rid}/csv"
SITADEL = {
    "logements": "8b35affb-55fc-4c1f-915b-7750f974446a",
    "locaux_non_residentiels": "f8f0700f-806c-40a7-83b1-f21cf507e7c4",
    "permis_amenager": "96883f50-538b-41f9-a059-c6eb97e6a23a",
    "permis_demolir": "1a9a2f0c-56fe-4e69-84a7-fbbda2121f02",
}

# DVF géolocalisées : lien stable data.gouv.fr, qui redirige vers la dernière
# version du fichier unique (dvf.csv.gz, séparateur « , », colonne code_commune).
DVF = "https://www.data.gouv.fr/api/1/datasets/r/d7933994-2c66-4131-a4da-cf7cd18040a4"

# Noms de colonne possibles pour la commune, essayés dans cet ordre si
# --colonne n'est pas donné. Sitadel utilise « COMM », DVF « code_commune ».
COLONNES_CODE = ["COMM", "code_commune", "CODE_COMMUNE", "CODE_INSEE", "CODGEO", "INSEE_COM", "insee"]
COLONNES_NOM = ["commune", "COMMUNE", "Commune", "nom_commune", "LIB_COMM", "NOM_COMMUNE",
                "ADR_LOCALITE_TER"]

TAILLE_CHUNK = 200_000  # lignes par morceau : ~100-300 Mo de RAM selon le fichier


def normaliser(texte: pd.Series) -> pd.Series:
    """Met en minuscules et retire les accents, pour que « JANZE » = « Janzé »."""
    # Optimisation : une colonne commune ne compte que quelques milliers de valeurs
    # distinctes pour des centaines de milliers de lignes. On normalise donc chaque
    # valeur distincte une seule fois, puis on reporte le résultat sur la colonne.
    texte = texte.fillna("")
    distinctes = texte.unique()
    correspondance = {
        v: unicodedata.normalize("NFKD", v.strip().lower()).encode("ascii", "ignore").decode()
        for v in distinctes
    }
    return texte.map(correspondance)


def ouvrir(source: str) -> io.BufferedReader:
    """Ouvre un fichier local ou une URL en flux (le fichier n'est jamais stocké en entier)."""
    if source.startswith(("http://", "https://")):
        requete = urllib.request.Request(source, headers={"User-Agent": "veille-janze/1.0"})
        return urllib.request.urlopen(requete, timeout=120)  # noqa: S310
    return open(source, "rb")


def detecter_format(entete: bytes) -> tuple[str, str]:
    """Devine l'encodage et le séparateur à partir des premiers octets du fichier."""
    try:
        encodage, texte = "utf-8-sig", entete.decode("utf-8-sig")
    except UnicodeDecodeError as erreur:
        # Une erreur dans les 3 derniers octets vient juste d'un caractère accentué
        # coupé par la lecture des 64 Ko : le fichier est bien en UTF-8.
        if erreur.start >= len(entete) - 3:
            encodage, texte = "utf-8-sig", entete[: erreur.start].decode("utf-8-sig")
        else:
            encodage, texte = "latin-1", entete.decode("latin-1")
    premiere_ligne = texte.splitlines()[0] if texte else ""
    try:
        separateur = csv.Sniffer().sniff(premiere_ligne, delimiters=";,\t|").delimiter
    except csv.Error:
        separateur = ";"  # format le plus courant dans les données publiques françaises
    return encodage, separateur


def choisir_colonne(colonnes: list[str], demandee: str | None, valeur: str) -> str:
    """Retourne la colonne de commune à utiliser, ou explique pourquoi on n'en trouve pas."""
    if demandee:
        if demandee not in colonnes:
            sys.exit(f"Colonne « {demandee} » introuvable. Colonnes disponibles : {', '.join(colonnes)}")
        return demandee
    candidates = COLONNES_CODE if valeur.isdigit() else COLONNES_NOM
    for nom in candidates:
        if nom in colonnes:
            return nom
    sys.exit(
        "Impossible de deviner la colonne de commune. Précisez-la avec --colonne. "
        f"Colonnes disponibles : {', '.join(colonnes)}"
    )


def filtrer(source: str, sortie: Path, colonne: str | None, valeur: str) -> int:
    """Lit `source` par morceaux, garde les lignes de la commune et écrit `sortie`.

    Retourne le nombre de lignes conservées.
    """
    debut = time.time()
    with ouvrir(source) as flux:
        # On lit 64 Ko pour détecter le format, puis on les « remet » devant le flux.
        entete = flux.read(65_536)
        flux_complet = io.BufferedReader(_Recolle(entete, flux), buffer_size=1 << 20)
        if entete[:2] == b"\x1f\x8b":
            # Fichier compressé (.gz, comme DVF) : décompression à la volée,
            # puis on relit le début décompressé pour détecter le format.
            flux_complet = io.BufferedReader(gzip.GzipFile(fileobj=flux_complet), buffer_size=1 << 20)
            entete = flux_complet.peek(65_536)[:65_536]
        encodage, separateur = detecter_format(entete)

        lecteur = pd.read_csv(
            flux_complet,
            sep=separateur,
            encoding=encodage,
            dtype=str,             # pas d'inférence de types : plus rapide, et garde les zéros (« 01 »)
            keep_default_na=False,  # une case vide reste vide, pas de NaN partout
            chunksize=TAILLE_CHUNK,
            engine="c",
            on_bad_lines="warn",
        )

        conservees = lues = 0
        premier = True
        cible = valeur if valeur.isdigit() else normaliser(pd.Series([valeur]))[0]

        for morceau in lecteur:
            if premier:
                colonne = choisir_colonne(list(morceau.columns), colonne, valeur)
                print(f"  séparateur « {separateur} », encodage {encodage}, filtre sur la colonne {colonne}")

            if valeur.isdigit():
                # Code INSEE : comparaison directe, en complétant à 5 chiffres (« 5136 » -> « 05136 »)
                masque = morceau[colonne].str.strip().str.zfill(5) == cible
            else:
                masque = normaliser(morceau[colonne]) == cible

            extrait = morceau.loc[masque]
            # Écriture au fil de l'eau : en-tête au premier morceau seulement, puis ajout.
            extrait.to_csv(sortie, mode="w" if premier else "a", header=premier,
                           index=False, sep=";", encoding="utf-8")
            premier = False

            lues += len(morceau)
            conservees += len(extrait)
            print(f"\r  {lues:>12,} lignes lues · {conservees:,} pour {valeur}".replace(",", " "),
                  end="", flush=True)

    duree = time.time() - debut
    print(f"\n  -> {sortie} ({conservees} lignes, {sortie.stat().st_size / 1024:.0f} Ko, {duree:.0f} s)")
    return conservees


class _Recolle(io.RawIOBase):
    """Flux qui relit d'abord les octets déjà consommés, puis la suite de la source."""

    def __init__(self, debut: bytes, suite):
        self._debut = io.BytesIO(debut)
        self._suite = suite

    def readable(self) -> bool:
        return True

    def readinto(self, tampon) -> int:
        n = self._debut.readinto(tampon)
        if n:
            return n
        donnees = self._suite.read(len(tampon))
        tampon[: len(donnees)] = donnees
        return len(donnees)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extrait les données d'urbanisme de Janzé d'un CSV national.")
    parser.add_argument("--entree", help="Fichier CSV (ou URL) à filtrer")
    parser.add_argument("--sortie", help="Fichier CSV de sortie (défaut : <entree>_janze.csv)")
    parser.add_argument("--sitadel", action="store_true",
                        help="Télécharge et filtre les 4 fichiers Sitadel de data.gouv.fr")
    parser.add_argument("--dvf", action="store_true",
                        help="Télécharge et filtre le fichier national DVF (ventes immobilières)")
    parser.add_argument("--colonne", help="Nom de la colonne commune (deviné si absent)")
    parser.add_argument("--valeur", default=CODE_INSEE_JANZE,
                        help=f"Code INSEE ou nom de commune (défaut : {CODE_INSEE_JANZE} = {NOM_JANZE})")
    parser.add_argument("--dossier", default="donnees_janze", help="Dossier de sortie pour --sitadel / --dvf")
    args = parser.parse_args()

    if args.sitadel or args.dvf:
        dossier = Path(args.dossier)
        dossier.mkdir(parents=True, exist_ok=True)
        if args.sitadel:
            for nom, rid in SITADEL.items():
                print(f"Sitadel · {nom}")
                filtrer(DIDO.format(rid=rid), dossier / f"sitadel_{nom}_janze.csv", args.colonne, args.valeur)
        if args.dvf:
            print("DVF · ventes immobilières")
            filtrer(DVF, dossier / "dvf_janze.csv", args.colonne, args.valeur)
    elif args.entree:
        sortie = Path(args.sortie or f"{Path(args.entree).stem}_janze.csv")
        print(f"Fichier · {args.entree}")
        filtrer(args.entree, sortie, args.colonne, args.valeur)
    else:
        parser.error("indiquez --entree <fichier.csv>, --sitadel ou --dvf")


if __name__ == "__main__":
    main()
