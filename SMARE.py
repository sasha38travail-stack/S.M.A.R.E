r"""
SMARE : réception du CO2 envoyé par le drone, enregistrement et publication sur GitHub.

Branchez l'Arduino récepteur : le programme le détecte tout seul, enregistre chaque
mesure et publie le fichier sur GitHub. Aucune installation de Git n'est nécessaire.

Les données et les réglages sont dans le dossier  C:\Users\VOTRE_NOM\SMARE
"""
import base64
import bisect
import collections
import hashlib
import json
import math
import os
import queue
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import serial
import serial.tools.list_ports

# ---------- Emplacements et constantes ----------
DOSSIER = os.path.join(os.path.expanduser("~"), "SMARE")
os.makedirs(DOSSIER, exist_ok=True)
FICHIER_CONFIG = os.path.join(DOSSIER, "config.json")
FICHIER_A_ENVOYER = os.path.join(DOSSIER, "a_envoyer.flag")   # présent = des mesures ne sont pas encore sur GitHub
ENTETE = "id,CO2_ppm,latitude,longitude,altitude,temperature,humidite,pression\n"
ENTETE_ANCIEN = "id,CO2_ppm,latitude,longitude,altitude\n"
RAYON_DOUBLON_M = 10   # une mesure importée à moins de 10 m d'un point existant est ignorée

API_GITHUB = "https://api.github.com"
URL_JETON = "https://github.com/settings/tokens/new?scopes=public_repo&description=SMARE"
CONFIG_DEFAUT = {
    "utilisateur": "sasha38travail-stack",
    "depot": "S.M.A.R.E",
    "branche": "main",
    "fichier": "data.csv",
    "jeton": "",
}
# Puces USB courantes des Arduino et de leurs clones (pour détecter le récepteur)
VID_ARDUINO = {0x2341, 0x2A03, 0x1B4F, 0x1A86, 0x10C4, 0x0403, 0x239A}


def ports_arduino():
    """Ports série qui ressemblent à un Arduino."""
    trouves = []
    for p in serial.tools.list_ports.comports():
        if p.vid in VID_ARDUINO or "arduino" in (p.description or "").lower():
            trouves.append(p.device)
    return trouves


# ---------- Réglages ----------
def charger_config():
    cfg = dict(CONFIG_DEFAUT)
    try:
        with open(FICHIER_CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def enregistrer_config(cfg):
    with open(FICHIER_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------- Connexion à GitHub (sans Git) ----------
MESSAGES_HTTP = {
    401: "Jeton refusé : incorrect ou expiré. Créez-en un nouveau dans les réglages.",
    403: "Accès refusé : le jeton n'a pas le droit d'écrire, ou trop de requêtes. Réessayez plus tard.",
    404: "Dépôt ou fichier introuvable : vérifiez le compte et le nom du dépôt.",
    409: "Conflit avec GitHub : nouvel essai au prochain envoi.",
    422: "GitHub a refusé le fichier (conflit) : nouvel essai au prochain envoi.",
}


class ErreurGitHub(Exception):
    def __init__(self, code, texte):
        super().__init__(texte)
        self.code = code
        self.texte = texte

    def __str__(self):
        return self.texte


def _requete(cfg, methode, chemin, donnees=None, accept="application/vnd.github+json"):
    corps = json.dumps(donnees).encode("utf-8") if donnees is not None else None
    req = urllib.request.Request(API_GITHUB + "/" + chemin, data=corps, method=methode)
    req.add_header("Authorization", "Bearer " + cfg["jeton"].strip())
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "SMARE-station-CO2")
    if corps is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        raise ErreurGitHub(e.code, MESSAGES_HTTP.get(e.code, f"Erreur GitHub {e.code}")) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise ErreurGitHub(0, "Pas de connexion internet (ou GitHub injoignable).") from None


def _chemin_depot(cfg):
    return f"repos/{urllib.parse.quote(cfg['utilisateur'])}/{urllib.parse.quote(cfg['depot'])}"


def _chemin_fichier(cfg):
    return _chemin_depot(cfg) + "/contents/" + urllib.parse.quote(cfg["fichier"])


def sha_git(contenu):
    """Empreinte que Git associe à un fichier (permet de savoir s'il a changé)."""
    return hashlib.sha1(b"blob %d\0" % len(contenu) + contenu).hexdigest()


def github_tester(cfg):
    """Vérifie le jeton, le dépôt et le droit d'écriture."""
    _, corps = _requete(cfg, "GET", _chemin_depot(cfg))
    info = json.loads(corps)
    if not (info.get("permissions") or {}).get("push"):
        raise ErreurGitHub(403, "Le jeton n'a pas le droit d'écrire dans ce dépôt.")
    return info


def github_sha(cfg):
    """Empreinte du fichier distant, ou None s'il n'existe pas encore."""
    try:
        _, corps = _requete(cfg, "GET", _chemin_fichier(cfg) + "?ref=" + urllib.parse.quote(cfg["branche"]))
    except ErreurGitHub as e:
        if e.code == 404:
            return None
        raise
    return json.loads(corps)["sha"]


def github_telecharger(cfg):
    """Contenu du fichier distant (octets), ou None s'il n'existe pas."""
    try:
        _, corps = _requete(cfg, "GET", _chemin_fichier(cfg) + "?ref=" + urllib.parse.quote(cfg["branche"]),
                            accept="application/vnd.github.raw+json")
    except ErreurGitHub as e:
        if e.code == 404:
            return None
        raise
    return corps


def github_envoyer(cfg, contenu, message="Nouvelles mesures"):
    """Envoie le fichier. Renvoie True si envoyé, False si GitHub a déjà exactement ce contenu."""
    sha = github_sha(cfg)
    if sha == sha_git(contenu):
        return False
    donnees = {"message": message, "branch": cfg["branche"],
               "content": base64.b64encode(contenu).decode("ascii")}
    if sha:
        donnees["sha"] = sha
    _requete(cfg, "PUT", _chemin_fichier(cfg), donnees)
    return True


def couleur_co2(ppm):
    if ppm < 600:
        return "#2e9e5b"
    if ppm < 800:
        return "#d4a900"
    if ppm < 1000:
        return "#ef8a1f"
    return "#cf3a2f"


def valeur_env(texte, mini, maxi):
    """Température, humidité ou pression : None si absente ou aberrante."""
    try:
        v = float(texte)
    except (ValueError, TypeError):
        return None
    return v if mini <= v <= maxi else None


def fmt(valeur, decimales=1):
    return "" if valeur is None else f"{valeur:.{decimales}f}"


def ligne_valide(champs):
    """Renvoie (co2, lat, lon, alt, temp, hum, pres) ou None.
    Accepte 4 valeurs (sans BME680) ou 7 valeurs. Une température, humidité ou
    pression aberrante devient None : la mesure de CO2 est conservée."""
    try:
        if len(champs) not in (4, 7):
            return None
        co2, lat, lon, alt = int(champs[0]), float(champs[1]), float(champs[2]), int(float(champs[3]))
        if not (0 < co2 < 10000) or abs(lat) > 90 or abs(lon) > 180:
            return None
        if lat == 0 and lon == 0:
            return None
        env = [None, None, None]
        if len(champs) == 7:
            env = [valeur_env(champs[4], -60, 80), valeur_env(champs[5], 0, 100),
                   valeur_env(champs[6], 300, 1100)]
        return (co2, lat, lon, alt, *env)
    except ValueError:
        return None


def ligne_csv(numero, m):
    co2, lat, lon, alt, temp, hum, pres = m
    return f"{numero},{co2},{lat:.6f},{lon:.6f},{alt},{fmt(temp)},{fmt(hum)},{fmt(pres)}\n"


def lire_csv_importe(chemin):
    """Lit un CSV externe. Renvoie (mesures valides, nombre de lignes ignorées).
    Séparateur , ou ; ; décimale point ou virgule ; avec ou sans en-tête ;
    avec ou sans colonnes température / humidité / pression."""
    with open(chemin, encoding="utf-8-sig", errors="ignore") as f:
        lignes = [l.strip() for l in f if l.strip()]
    if not lignes:
        return [], 0
    sep = ";" if lignes[0].count(";") > lignes[0].count(",") else ","
    premiere = [c.strip().lower() for c in lignes[0].split(sep)]
    if {"co2_ppm", "latitude", "longitude"} <= set(premiere):
        noms = ("co2_ppm", "latitude", "longitude", "altitude", "temperature", "humidite", "pression")
        pos = [premiere.index(n) if n in premiere else None for n in noms]
        corps = lignes[1:]

        def extraire(c):
            v = [c[p] if p is not None and p < len(c) else "" for p in pos]
            if not v[3].strip():
                v[3] = "0"
            return v
    else:
        corps = lignes if premiere[0].replace("-", "").isdigit() else lignes[1:]

        def extraire(c):
            if len(c) in (5, 8):      # première colonne = id
                c = c[1:]
            return c[:7]

    mesures, ignorees = [], 0
    for ligne in corps:
        try:
            champs = [x.strip().replace(",", ".") for x in extraire(ligne.split(sep))]
            champs[0] = str(int(float(champs[0])))
            mesure = ligne_valide(champs)
        except (IndexError, ValueError):
            mesure = None
        if mesure:
            mesures.append(mesure)
        else:
            ignorees += 1
    return mesures, ignorees


def distance_m(lat1, lon1, lat2, lon2):
    """Distance en mètres entre deux points GPS (formule de haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def filtrer_proches(nouvelles, existantes, rayon=RAYON_DOUBLON_M):
    """Renvoie (à ajouter, ignorées). Une nouvelle mesure est ignorée si un point
    EXISTANT se trouve à moins de `rayon` mètres. Les nouvelles mesures ne sont pas
    comparées entre elles (un drone lent produit des points rapprochés)."""
    pts = sorted((m[1], m[2]) for m in existantes)
    lats = [p[0] for p in pts]
    marge_lat = rayon / 100000.0              # environ `rayon` en degrés de latitude, avec marge
    a_ajouter, ignorees = [], []
    for m in nouvelles:
        lat, lon = m[1], m[2]
        marge_lon = marge_lat / max(math.cos(math.radians(lat)), 0.01)
        debut = bisect.bisect_left(lats, lat - marge_lat)
        fin = bisect.bisect_right(lats, lat + marge_lat)
        proche = any(min(abs(lo - lon), 360 - abs(lo - lon)) <= marge_lon and distance_m(lat, lon, la, lo) <= rayon
                     for la, lo in pts[debut:fin])
        (ignorees if proche else a_ajouter).append(m)
    return a_ajouter, ignorees


def fusionner(existantes, importees, rayon=RAYON_DOUBLON_M):
    """La mesure importée (la plus récente) l'emporte : toute ancienne mesure située à moins de
    `rayon` mètres d'une mesure importée est remplacée par elle. Les autres sont conservées.
    Renvoie (liste finale, conservées, remplacées, importées nouvelles, importées remplaçantes, changement)."""
    conservees, remplacees = filtrer_proches(existantes, importees, rayon)
    nouvelles, remplacantes = filtrer_proches(importees, existantes, rayon)
    changement = collections.Counter(remplacees) != collections.Counter(importees)
    return conservees + list(importees), conservees, remplacees, nouvelles, remplacantes, changement


class Application(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SMARE : station CO₂")
        self.geometry("900x660")
        self.minsize(780, 560)

        self.cfg = charger_config()
        self.file_msg = queue.Queue()
        self.ser = None
        self.thread_lecture = None
        self.en_marche = False
        self.deconnexion_manuelle = False
        self.synchro_faite = False
        self.compteur = 0
        self.dernier_push = time.time()
        self.verrou_fichier = threading.RLock()
        self.verrou_reseau = threading.Lock()
        self.fenetre_reglages = None

        self.construire()
        self.preparer_csv()
        self.actualiser_ports()
        self.after(150, self.traiter_messages)
        self.after(1000, self.tick_push)
        self.after(1500, self.tick_auto)
        self.protocol("WM_DELETE_WINDOW", self.fermer)
        if self.cfg["jeton"].strip():
            self.synchroniser_async()
        else:
            self.var_git.set("Réglages à faire : cliquez sur « Réglages… »")
            self.after(500, self.ouvrir_reglages)

    # ---------- Fenêtre ----------
    def construire(self):
        pad = {"padx": 10, "pady": 5}

        c = ttk.LabelFrame(self, text="1. Récepteur Arduino")
        c.pack(fill="x", **pad)
        self.var_port = tk.StringVar()
        self.liste_ports = ttk.Combobox(c, textvariable=self.var_port, width=34, state="readonly")
        self.liste_ports.grid(row=0, column=0, padx=8, pady=8)
        ttk.Button(c, text="Actualiser", command=self.actualiser_ports).grid(row=0, column=1, padx=4)
        self.bouton_connexion = ttk.Button(c, text="Connecter", command=self.basculer_connexion)
        self.bouton_connexion.grid(row=0, column=2, padx=4)
        self.var_auto_port = tk.BooleanVar(value=True)
        ttk.Checkbutton(c, text="Connexion automatique", variable=self.var_auto_port).grid(row=0, column=3, padx=8)
        self.var_etat = tk.StringVar(value="Branchez le récepteur Arduino")
        ttk.Label(c, textvariable=self.var_etat).grid(row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 8))

        d = ttk.LabelFrame(self, text="2. Données")
        d.pack(fill="x", **pad)
        self.var_compte = tk.StringVar()
        ttk.Label(d, textvariable=self.var_compte).grid(row=0, column=0, padx=8, pady=8, sticky="w")
        ttk.Button(d, text="Importer un CSV (carte SD)…", command=self.importer_csv).grid(row=0, column=1, padx=8)
        ttk.Button(d, text="Ouvrir le dossier", command=self.ouvrir_dossier).grid(row=0, column=2)

        g = ttk.LabelFrame(self, text="3. Publication sur GitHub")
        g.pack(fill="x", **pad)
        self.var_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(g, text="Envoi automatique toutes les", variable=self.var_auto).grid(row=0, column=0, padx=8, pady=8)
        self.var_intervalle = tk.IntVar(value=60)
        ttk.Spinbox(g, from_=30, to=3600, increment=30, width=6, textvariable=self.var_intervalle).grid(row=0, column=1)
        ttk.Label(g, text="secondes").grid(row=0, column=2, padx=4)
        ttk.Button(g, text="Envoyer maintenant", command=self.pousser_async).grid(row=0, column=3, padx=10)
        ttk.Button(g, text="Réglages…", command=self.ouvrir_reglages).grid(row=0, column=4)
        self.var_git = tk.StringVar(value="")
        ttk.Label(g, textvariable=self.var_git, wraplength=820).grid(row=1, column=0, columnspan=5, sticky="w", padx=8, pady=(0, 8))

        m = ttk.LabelFrame(self, text="Dernière mesure")
        m.pack(fill="x", **pad)
        self.label_co2 = tk.Label(m, text="--- ppm", font=("Segoe UI", 28, "bold"))
        self.label_co2.pack(side="left", padx=16, pady=8)
        self.var_pos = tk.StringVar(value="En attente de données…")
        ttk.Label(m, textvariable=self.var_pos).pack(side="left", padx=8)

        h = ttk.LabelFrame(self, text="Dernières mesures reçues")
        h.pack(fill="both", expand=True, **pad)
        colonnes = ("id", "heure", "co2", "lat", "lon", "alt", "temp", "hum", "pres")
        titres = ("N°", "Heure", "CO₂ (ppm)", "Latitude", "Longitude", "Alt. (m)",
                  "Temp. (°C)", "Hum. (%)", "Pression (hPa)")
        self.tableau = ttk.Treeview(h, columns=colonnes, show="headings", height=8)
        for col, titre in zip(colonnes, titres):
            self.tableau.heading(col, text=titre)
            self.tableau.column(col, width=82, anchor="center")
        self.tableau.pack(fill="both", expand=True, padx=6, pady=6)

    def report_callback_exception(self, exc, val, tb):
        """Écrit les erreurs imprévues dans un fichier (l'exe n'a pas de console)."""
        with open(os.path.join(DOSSIER, "erreurs.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "".join(traceback.format_exception(exc, val, tb)) + "\n")
        self.var_git.set("Erreur interne : voir erreurs.log dans le dossier SMARE")

    def ouvrir_dossier(self):
        if hasattr(os, "startfile"):
            os.startfile(DOSSIER)
        else:
            webbrowser.open(DOSSIER)

    # ---------- Réglages GitHub ----------
    def ouvrir_reglages(self):
        if self.fenetre_reglages is not None and self.fenetre_reglages.winfo_exists():
            self.fenetre_reglages.lift()
            return
        w = tk.Toplevel(self)
        self.fenetre_reglages = w
        w.title("Réglages GitHub")
        w.transient(self)
        w.columnconfigure(1, weight=1)
        ttk.Label(w, text="Connexion à votre dépôt GitHub", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=2, padx=12, pady=(12, 6), sticky="w")
        self.vars_cfg = {}
        lignes = (("Compte GitHub", "utilisateur"), ("Dépôt", "depot"), ("Branche", "branche"),
                  ("Fichier de données", "fichier"), ("Jeton (mot de passe)", "jeton"))
        for i, (titre, cle) in enumerate(lignes, start=1):
            ttk.Label(w, text=titre).grid(row=i, column=0, padx=12, pady=4, sticky="w")
            var = tk.StringVar(value=self.cfg.get(cle, ""))
            self.vars_cfg[cle] = var
            ttk.Entry(w, textvariable=var, width=44, show="•" if cle == "jeton" else "").grid(
                row=i, column=1, padx=12, pady=4, sticky="ew")
        ttk.Button(w, text="1. Créer un jeton sur GitHub", command=lambda: webbrowser.open(URL_JETON)).grid(
            row=6, column=0, columnspan=2, padx=12, pady=(10, 2), sticky="w")
        ttk.Label(w, wraplength=480, justify="left", text=(
            "Sur la page qui s'ouvre : choisissez une durée (Expiration), descendez et cliquez sur "
            "« Generate token », copiez le jeton (il commence par ghp_) puis collez-le dans la case "
            "« Jeton » ci-dessus.")).grid(row=7, column=0, columnspan=2, padx=12, pady=2, sticky="w")
        ttk.Button(w, text="2. Tester et enregistrer", command=self.tester_reglages).grid(
            row=8, column=0, columnspan=2, padx=12, pady=(10, 2), sticky="w")
        self.var_test = tk.StringVar()
        ttk.Label(w, textvariable=self.var_test, wraplength=480).grid(
            row=9, column=0, columnspan=2, padx=12, pady=(2, 12), sticky="w")

    def tester_reglages(self):
        cfg = dict(self.cfg)
        for cle, var in self.vars_cfg.items():
            cfg[cle] = var.get().strip()
        if not cfg["jeton"]:
            self.var_test.set("Collez d'abord le jeton (étape 1).")
            return
        self.var_test.set("Test en cours…")
        threading.Thread(target=self._test_reglages, args=(cfg,), daemon=True).start()

    def _test_reglages(self, cfg):
        try:
            github_tester(cfg)
            self.file_msg.put(("test", (True, cfg, "Connexion réussie.")))
        except ErreurGitHub as e:
            self.file_msg.put(("test", (False, cfg, str(e))))
        except Exception as e:
            self.file_msg.put(("test", (False, cfg, "Erreur : " + str(e))))

    def fin_test(self, ok, cfg, texte):
        try:
            self.var_test.set(texte)
        except (AttributeError, tk.TclError):
            pass
        if ok:
            self.cfg = cfg
            enregistrer_config(cfg)
            self.var_git.set("Connexion à GitHub réussie.")
            self.after(900, self._fermer_reglages)
            self.synchro_faite = False
            self.synchroniser_async()

    def _fermer_reglages(self):
        if self.fenetre_reglages is not None and self.fenetre_reglages.winfo_exists():
            self.fenetre_reglages.destroy()

    # ---------- Connexion série ----------
    def actualiser_ports(self):
        ports = [p.device + " : " + (p.description or "") for p in serial.tools.list_ports.comports()]
        self.liste_ports["values"] = ports
        if self.en_marche:
            return
        if not ports:
            self.var_port.set("")
        elif self.var_port.get() not in ports:
            arduino = ports_arduino()
            choix = next((p for p in ports if arduino and p.split(" : ")[0] == arduino[0]), ports[0])
            self.var_port.set(choix)

    def tick_auto(self):
        self.actualiser_ports()
        if self.var_auto_port.get() and not self.en_marche and not self.deconnexion_manuelle and ports_arduino():
            self.connecter()
        self.after(3000, self.tick_auto)

    def basculer_connexion(self):
        if self.en_marche:
            self.deconnexion_manuelle = True
            self.deconnecter()
        else:
            self.deconnexion_manuelle = False
            self.connecter()

    def connecter(self):
        if not self.cfg["jeton"].strip():
            self.var_etat.set("Faites d'abord les réglages GitHub (bouton « Réglages… »).")
            return
        if not self.synchro_faite:
            self.var_etat.set("Synchronisation avec GitHub en cours, patientez quelques secondes…")
            return
        if not self.var_port.get():
            self.var_etat.set("Aucun port détecté : branchez le récepteur Arduino.")
            return
        port = self.var_port.get().split(" : ")[0]
        try:
            self.ser = serial.Serial(port, 115200, timeout=1)
        except (serial.SerialException, OSError) as e:
            self.var_etat.set("Port inaccessible (fermez le Moniteur série d'Arduino) : " + str(e)[:50])
            return
        self.preparer_csv()
        self.en_marche = True
        self.thread_lecture = threading.Thread(target=self.lire_serie, daemon=True)
        self.thread_lecture.start()
        self.bouton_connexion.config(text="Déconnecter")
        self.var_etat.set(f"Connecté sur {port} : en attente de mesures")

    def deconnecter(self):
        self.en_marche = False
        if self.thread_lecture:
            self.thread_lecture.join(timeout=2)
        if self.ser:
            try:
                self.ser.close()
            except OSError:
                pass
        self.bouton_connexion.config(text="Connecter")
        self.var_etat.set("Déconnecté")

    def lire_serie(self):
        while self.en_marche:
            try:
                texte = self.ser.readline().decode("utf-8", errors="ignore").strip()
            except (serial.SerialException, OSError):
                self.file_msg.put(("perdu", None))
                return
            if texte:
                self.file_msg.put(("ligne", texte))

    # ---------- Fichier CSV ----------
    def chemin_csv(self):
        return os.path.join(DOSSIER, os.path.basename(self.cfg.get("fichier") or "data.csv"))

    def marquer_a_envoyer(self):
        open(FICHIER_A_ENVOYER, "w").close()

    @staticmethod
    def migrer_ancien_format(chemin):
        """Ajoute les colonnes température/humidité/pression à un ancien CSV."""
        with open(chemin, encoding="utf-8") as f:
            lignes = f.read().splitlines()
        if lignes and lignes[0].strip().lower() == ENTETE_ANCIEN.strip().lower():
            nouvelles = [ENTETE.strip()] + [l + ",,," for l in lignes[1:] if l.strip()]
            with open(chemin, "w", encoding="utf-8", newline="") as f:
                f.write("\n".join(nouvelles) + "\n")

    def preparer_csv(self):
        chemin = self.chemin_csv()
        with self.verrou_fichier:
            if not os.path.exists(chemin) or os.path.getsize(chemin) == 0:
                with open(chemin, "w", encoding="utf-8", newline="") as f:
                    f.write(ENTETE)
            else:
                self.migrer_ancien_format(chemin)
                with open(chemin, "rb") as f:
                    contenu = f.read()
                if not contenu.endswith(b"\n"):          # le fichier doit finir par un saut de ligne
                    with open(chemin, "ab") as f:
                        f.write(b"\n")
            self.compteur = 0
            with open(chemin, encoding="utf-8-sig") as f:
                lignes = [l for l in f.read().splitlines() if l.strip()]
            if len(lignes) > 1:
                try:
                    self.compteur = int(lignes[-1].split(",")[0])
                except ValueError:
                    self.compteur = len(lignes) - 1
        self.var_compte.set(f"{self.compteur} mesure(s) enregistrée(s)")

    def enregistrer(self, mesure):
        co2, lat, lon, alt, temp, hum, pres = mesure
        with self.verrou_fichier:
            self.compteur += 1
            with open(self.chemin_csv(), "a", encoding="utf-8", newline="") as f:
                f.write(ligne_csv(self.compteur, mesure))
            self.marquer_a_envoyer()
        self.var_compte.set(f"{self.compteur} mesure(s) enregistrée(s)")
        self.label_co2.config(text=f"{co2} ppm", fg=couleur_co2(co2))
        ambiance = "   ".join(x for x in (
            f"{temp:.1f} °C" if temp is not None else "",
            f"{hum:.0f} %" if hum is not None else "",
            f"{pres:.0f} hPa" if pres is not None else "") if x)
        self.var_pos.set(f"Lat {lat:.5f}   Lon {lon:.5f}   Alt {alt} m\n{ambiance}")
        self.tableau.insert("", 0, values=(self.compteur, time.strftime("%H:%M:%S"), co2, f"{lat:.5f}",
                                           f"{lon:.5f}", alt, fmt(temp) or "-", fmt(hum) or "-", fmt(pres) or "-"))
        for ancien in self.tableau.get_children()[30:]:
            self.tableau.delete(ancien)

    def importer_csv(self):
        if not self.cfg["jeton"].strip() or not self.synchro_faite:
            messagebox.showinfo("Importer un CSV",
                                "Terminez d'abord les réglages GitHub et attendez la synchronisation.")
            return
        chemin = filedialog.askopenfilename(
            title="Choisir un fichier CSV (carte SD)",
            filetypes=[("Fichiers CSV", "*.csv"), ("Tous les fichiers", "*.*")])
        if not chemin:
            return
        try:
            mesures, ignorees = lire_csv_importe(chemin)
        except OSError as e:
            messagebox.showerror("Import", "Lecture impossible : " + str(e))
            return
        if not mesures:
            messagebox.showerror("Import", "Aucune ligne valide trouvée dans ce fichier.")
            return

        with self.verrou_fichier:
            self.preparer_csv()
            existantes, _ = lire_csv_importe(self.chemin_csv())
            finales, conservees, remplacees, nouvelles, remplacantes, changement = fusionner(existantes, mesures)
            if not changement:
                messagebox.showinfo("Importer un CSV",
                                    "Rien à mettre à jour : ces mesures sont déjà présentes.")
                return
            if not messagebox.askokcancel(
                    "Importer un CSV",
                    f"{len(mesures)} mesure(s) valide(s), {ignorees} ligne(s) invalide(s) ignorée(s).\n\n"
                    f"Nouvelles (aucun point à moins de {RAYON_DOUBLON_M} m) : {len(nouvelles)}\n"
                    f"Plus récentes que des mesures existantes (< {RAYON_DOUBLON_M} m) : {len(remplacantes)}\n"
                    f"    -> anciennes mesures remplacées : {len(remplacees)}\n"
                    f"Mesures existantes conservées : {len(conservees)}\n\n"
                    "Continuer ?"):
                return
            chemin = self.chemin_csv()
            with open(chemin + ".tmp", "w", encoding="utf-8", newline="") as f:
                f.write(ENTETE)
                for i, m in enumerate(finales, start=1):
                    f.write(ligne_csv(i, m))
            os.replace(chemin + ".tmp", chemin)
            self.marquer_a_envoyer()
            self.preparer_csv()
        self.var_git.set(f"Import terminé : {len(nouvelles)} nouvelle(s), "
                         f"{len(remplacees)} ancienne(s) remplacée(s) (< {RAYON_DOUBLON_M} m), envoi en cours…")
        self.pousser_async()

    # ---------- Messages venant des threads ----------
    def traiter_messages(self):
        try:
            while True:
                genre, contenu = self.file_msg.get_nowait()
                if genre == "ligne":
                    mesure = ligne_valide(contenu.split(","))
                    if mesure:
                        self.enregistrer(mesure)
                elif genre == "perdu":
                    self.deconnecter()
                    self.var_etat.set("Câble débranché : reconnexion automatique dès son retour")
                elif genre == "statut":
                    self.var_git.set(contenu)
                elif genre == "rafraichir":
                    self.preparer_csv()
                elif genre == "test":
                    self.fin_test(*contenu)
        except queue.Empty:
            pass
        self.after(150, self.traiter_messages)

    # ---------- Publication sur GitHub ----------
    def tick_push(self):
        if self.var_auto.get() and self.synchro_faite and os.path.exists(FICHIER_A_ENVOYER):
            try:
                delai = max(30, int(self.var_intervalle.get()))
            except (tk.TclError, ValueError):
                delai = 60
            if time.time() - self.dernier_push >= delai:
                self.pousser_async()
        self.after(1000, self.tick_push)

    def _envoyer_fichier(self):
        """Envoie le CSV local. Renvoie True si GitHub a reçu du nouveau."""
        with self.verrou_fichier:
            with open(self.chemin_csv(), "rb") as f:
                contenu = f.read()
            if os.path.exists(FICHIER_A_ENVOYER):
                os.remove(FICHIER_A_ENVOYER)      # remis plus bas si l'envoi échoue
        try:
            return github_envoyer(self.cfg, contenu)
        except Exception:
            self.marquer_a_envoyer()
            raise

    def pousser_async(self):
        self.dernier_push = time.time()
        threading.Thread(target=self.pousser, daemon=True).start()

    def pousser(self):
        if not self.cfg["jeton"].strip():
            self.file_msg.put(("statut", "Réglages à faire : jeton GitHub manquant."))
            return
        if not self.verrou_reseau.acquire(blocking=False):
            return                                 # un envoi est déjà en cours
        try:
            envoye = self._envoyer_fichier()
            texte = "envoyé sur GitHub" if envoye else "rien de nouveau à envoyer"
            self.file_msg.put(("statut", f"{time.strftime('%H:%M:%S')} : {texte}"))
        except ErreurGitHub as e:
            self.file_msg.put(("statut", "Échec de l'envoi : " + str(e)))
        except Exception as e:
            self.file_msg.put(("statut", "Erreur : " + str(e)))
        finally:
            self.verrou_reseau.release()

    def synchroniser_async(self):
        threading.Thread(target=self.synchroniser, daemon=True).start()

    def synchroniser(self):
        """Au démarrage : récupère le fichier de GitHub (ou envoie les mesures restées en attente)."""
        with self.verrou_reseau:
            try:
                if os.path.exists(FICHIER_A_ENVOYER):
                    self._envoyer_fichier()
                    texte = "Mesures de la session précédente envoyées sur GitHub."
                else:
                    contenu = github_telecharger(self.cfg)
                    with self.verrou_fichier:
                        if contenu is not None:
                            with open(self.chemin_csv(), "wb") as f:
                                f.write(contenu)
                            texte = "Données récupérées depuis GitHub."
                        else:
                            self.marquer_a_envoyer()
                            texte = "Fichier absent sur GitHub : il sera créé au premier envoi."
                self.file_msg.put(("statut", texte))
            except ErreurGitHub as e:
                self.file_msg.put(("statut", "Synchronisation impossible : " + str(e)))
            except Exception as e:
                self.file_msg.put(("statut", "Erreur de synchronisation : " + str(e)))
            finally:
                self.synchro_faite = True
                self.file_msg.put(("rafraichir", None))

    def fermer(self):
        self.deconnecter()
        if self.cfg["jeton"].strip() and os.path.exists(FICHIER_A_ENVOYER):
            self.var_git.set("Envoi final sur GitHub…")
            self.update_idletasks()
            self.pousser()
        self.destroy()


if __name__ == "__main__":
    try:                                    # texte net sur les écrans haute résolution
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError, ImportError):
        pass
    Application().mainloop()
