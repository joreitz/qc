#!/usr/bin/env python3
"""
qc — Projektmanager für QC-Rechnungen
=====================================
Projekte registrieren, Shortcuts aus commands.json ausführen,
Pfad-Bookmarks pro Projekt, Print-Modus, History.

Für 'qc go <mark>' (echtes cd) einmalig in die ~/.bashrc:
    eval "$(qc shellrc)"
"""

import difflib
import json
import math
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# --- KONFIGURATION ---
GLOBAL_CONFIG_FILE = Path.home() / ".qc_manager_config.json"
PROJECT_CONFIG_NAME = "commands.json"
HISTORY_NAME = ".qc_history"
PROJECT_FOLDERS = [
    "scratch",
    "0_confsampling",
    "1_optfreq",
    "2_sp",
    "3_solv",
    "xyz",
]
RESERVED = {
    "init", "add", "rm", "switch", "list", "cmds", "rmcmd",
    "mark", "marks", "unmark", "go", "status", "edit",
    "history", "shellrc", "help", "-h", "--help",
    "data", "species", "e", "u", "h", "g", "rxn", "scan",
}

DEFAULT_COMMANDS = {
    "geomopt": "inporca -j optfreq -f wb97x-3c -dp no -fn -optt tight",
    "sp": "inporca -j sp -f r2scan0 -bs def2-QZVPPD -dp d4 -fn",
    "freq": "inporca -j freq -f wb97x-3c -dp no -fn",
}


# --- HILFSFUNKTIONEN: GLOBALE CONFIG ---

def load_global_config():
    if not GLOBAL_CONFIG_FILE.exists():
        return {"projects": {}, "active_project": None}
    try:
        with open(GLOBAL_CONFIG_FILE) as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"[WARN] {GLOBAL_CONFIG_FILE} ist korrupt — starte mit leerer Config.",
              file=sys.stderr)
        return {"projects": {}, "active_project": None}


def save_global_config(config):
    with open(GLOBAL_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)


# --- HILFSFUNKTIONEN: PROJEKT-CONFIG ---

def project_config_path(project_path):
    return Path(project_path) / PROJECT_CONFIG_NAME


def load_project_config(project_path):
    """
    Lädt commands.json. Altes Format (flaches Dict aus Shortcuts) wird
    automatisch ins neue Format {"commands": {...}, "paths": {...}} migriert.
    """
    cfg_file = project_config_path(project_path)
    if not cfg_file.exists():
        print(f"Fehler: Keine {PROJECT_CONFIG_NAME} im Projekt-Root gefunden:\n{project_path}",
              file=sys.stderr)
        sys.exit(1)
    with open(cfg_file) as f:
        data = json.load(f)

    # Migration: altes flaches Format erkennen
    if "commands" not in data and "paths" not in data:
        data = {"commands": data, "paths": {}}
    data.setdefault("commands", {})
    data.setdefault("paths", {})
    return data


def save_project_config(project_path, data):
    with open(project_config_path(project_path), "w") as f:
        json.dump(data, f, indent=4)


def detect_current_project(config):
    """
    Prüft, ob der aktuelle Ordner zu einem registrierten Projekt gehört.
    Rückgabe: (name, Path, is_local) — is_local=True bei automatischer Erkennung,
    sonst Fallback auf das global aktive Projekt.
    """
    current_path = Path.cwd().resolve()

    # Längste Pfade zuerst, damit verschachtelte Projekte korrekt greifen
    sorted_projects = sorted(config["projects"].items(),
                             key=lambda x: len(str(x[1])), reverse=True)
    for name, p_path_str in sorted_projects:
        p_path = Path(p_path_str).resolve()
        if current_path == p_path or p_path in current_path.parents:
            return name, p_path, True

    active = config.get("active_project")
    if active and active in config["projects"]:
        return active, Path(config["projects"][active]), False

    return None, None, False


def require_project(config):
    name, path, is_local = detect_current_project(config)
    if not name:
        print("Fehler: Kein Projekt gefunden. Nutze 'qc init <name>' oder geh in einen Projektordner.",
              file=sys.stderr)
        sys.exit(1)
    return name, path, is_local


def suggest(word, candidates):
    matches = difflib.get_close_matches(word, candidates, n=3, cutoff=0.5)
    if matches:
        print(f"Meintest du: {', '.join(matches)}?", file=sys.stderr)


# --- PROJEKTE ---

def init_project(project_name):
    root_path = Path.cwd() / project_name
    print(f"--- Initialisiere Projekt: {project_name} ---")

    if root_path.exists():
        print(f"[INFO] Ordner '{project_name}' existiert bereits.")
    else:
        try:
            root_path.mkdir()
            print(f"[OK] Hauptordner erstellt: {root_path}")
        except OSError as e:
            print(f"[ERROR] Konnte Ordner nicht erstellen: {e}", file=sys.stderr)
            sys.exit(1)

    for folder in PROJECT_FOLDERS:
        (root_path / folder).mkdir(exist_ok=True)

    cfg_file = project_config_path(root_path)
    if not cfg_file.exists():
        save_project_config(root_path, {"commands": dict(DEFAULT_COMMANDS), "paths": {}})
        print(f"[OK] {PROJECT_CONFIG_NAME} erstellt.")

    register_project(project_name, str(root_path))


def register_project(name, path=None):
    config = load_global_config()
    target_path = str(Path(path).resolve()) if path else str(Path.cwd().resolve())
    config["projects"][name] = target_path
    config["active_project"] = name
    save_global_config(config)
    print(f"[SUCCESS] Projekt '{name}' registriert.")
    print(f"          Pfad: {target_path}")


def remove_project(name):
    config = load_global_config()
    if name not in config["projects"]:
        print(f"Fehler: Projekt '{name}' ist unbekannt.", file=sys.stderr)
        suggest(name, config["projects"].keys())
        sys.exit(1)
    del config["projects"][name]
    if config.get("active_project") == name:
        config["active_project"] = None
    save_global_config(config)
    print(f"[OK] Projekt '{name}' entfernt (Ordner und Dateien bleiben unberührt).")


def switch_project(name):
    config = load_global_config()
    if name not in config["projects"]:
        print(f"Fehler: Projekt '{name}' ist unbekannt.", file=sys.stderr)
        suggest(name, config["projects"].keys())
        sys.exit(1)
    config["active_project"] = name
    save_global_config(config)
    print(f"--> Globales aktives Projekt: {name}")


def list_projects():
    config = load_global_config()
    detected_name, _, is_local = detect_current_project(config)
    global_active = config.get("active_project")

    if not config["projects"]:
        print("Keine Projekte registriert. Nutze 'qc init <name>'.")
        return

    print("--- Projekte ---")
    for name, path in config["projects"].items():
        markers = []
        if name == global_active:
            markers.append("(global active)")
        if name == detected_name and is_local:
            markers.append("<-- DU BIST HIER")
        marker_str = " " + " ".join(markers) if markers else ""
        print(f" [{name}] {path}{marker_str}")


# --- COMMANDS ---

def add_command(assignment):
    """qc add name="befehl ..." — fügt einen Shortcut zur commands.json hinzu."""
    config = load_global_config()
    project_name, project_path, _ = require_project(config)

    name, _, cmd = assignment.partition("=")
    name = name.strip().strip('"').strip("'")
    cmd = cmd.strip().strip('"').strip("'")

    if not name or not cmd:
        print('Fehler: Format ist  qc add name="befehl"', file=sys.stderr)
        sys.exit(1)
    if name in RESERVED:
        print(f"Fehler: '{name}' ist ein reservierter qc-Befehl.", file=sys.stderr)
        sys.exit(1)

    pcfg = load_project_config(project_path)
    action = "aktualisiert" if name in pcfg["commands"] else "hinzugefügt"
    pcfg["commands"][name] = cmd
    save_project_config(project_path, pcfg)
    print(f"[OK] Shortcut '{name}' in Projekt '{project_name}' {action}:")
    print(f"     {cmd}")


def remove_command(name):
    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    pcfg = load_project_config(project_path)
    if name not in pcfg["commands"]:
        print(f"Fehler: Shortcut '{name}' nicht in Projekt '{project_name}'.", file=sys.stderr)
        suggest(name, pcfg["commands"].keys())
        sys.exit(1)
    del pcfg["commands"][name]
    save_project_config(project_path, pcfg)
    print(f"[OK] Shortcut '{name}' entfernt.")


def list_commands():
    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    pcfg = load_project_config(project_path)
    print(f"--- Shortcuts in '{project_name}' ---")
    if not pcfg["commands"]:
        print(" (keine)")
        return
    width = max(len(k) for k in pcfg["commands"])
    for k, v in pcfg["commands"].items():
        print(f" {k:<{width}}  {v}")


# --- BOOKMARKS (temporäre Pfade) ---

def mark_path(name, path=None):
    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    target = Path(path).resolve() if path else Path.cwd().resolve()
    if not target.is_dir():
        print(f"Fehler: Ordner existiert nicht: {target}", file=sys.stderr)
        sys.exit(1)
    pcfg = load_project_config(project_path)
    pcfg["paths"][name] = str(target)
    save_project_config(project_path, pcfg)
    print(f"[OK] Bookmark '{name}' -> {target}  (Projekt: {project_name})")


def unmark_path(name):
    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    pcfg = load_project_config(project_path)
    if name not in pcfg["paths"]:
        print(f"Fehler: Bookmark '{name}' nicht in Projekt '{project_name}'.", file=sys.stderr)
        suggest(name, pcfg["paths"].keys())
        sys.exit(1)
    del pcfg["paths"][name]
    save_project_config(project_path, pcfg)
    print(f"[OK] Bookmark '{name}' entfernt.")


def list_marks():
    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    pcfg = load_project_config(project_path)
    print(f"--- Bookmarks in '{project_name}' ---")
    if not pcfg["paths"]:
        print(" (keine) — setze eins mit 'qc mark <name>' im Zielordner")
        return
    width = max(len(k) for k in pcfg["paths"])
    for k, v in pcfg["paths"].items():
        missing = "" if Path(v).is_dir() else "  [FEHLT!]"
        print(f" {k:<{width}}  {v}{missing}")


def go_mark(name):
    """
    Gibt NUR den Pfad auf stdout aus (alles andere auf stderr),
    damit der Shell-Wrapper 'cd $(qc go name)' machen kann.
    Sonderfall: 'qc go .' bzw. 'qc go root' -> Projekt-Root.
    """
    config = load_global_config()
    project_name, project_path, _ = require_project(config)

    if name in (".", "root"):
        print(project_path)
        return

    pcfg = load_project_config(project_path)
    if name not in pcfg["paths"]:
        print(f"Fehler: Bookmark '{name}' nicht in Projekt '{project_name}'.", file=sys.stderr)
        suggest(name, list(pcfg["paths"].keys()) + ["root"])
        sys.exit(1)

    target = Path(pcfg["paths"][name])
    if not target.is_dir():
        print(f"Fehler: Ordner existiert nicht mehr: {target}", file=sys.stderr)
        sys.exit(1)
    print(target)


# --- DATEN / THERMOCHEMIE (data.yaml) ---

DATA_CONFIG_NAME = "data.yaml"
MAIN_DATA_DIR = Path.home() / ".qc_data"
ROLES = ("thermo", "sp", "solv", "boltz", "bfreq")

HARTREE_TO_KCAL = 627.5094740631
UNIT_CONVERSION = {
    "hartree": 1.0,
    "eh": 1.0,
    "kcal/mol": HARTREE_TO_KCAL,
    "kj/mol": 2625.4996394799,
    "ev": 27.211386245988,
}
R_KCAL = 1.98720425864083e-3  # kcal/(mol K)

DEFAULT_STRIP_SUFFIXES = ["optfreq", "opt", "freq", "sp", "solv", "dock",
                          "censo", "crest", "conf", "boltz", "bfreq", "sph"]

DATA_TEMPLATE = """\
# qc data.yaml — Datenquellen & Reaktionen für Thermochemie
# Pfade relativ zum Projekt-Root oder absolut.
# Pro Rolle ist eine Datei ODER eine Liste von Dateien erlaubt.
# 'method:' überschreibt die METHOD-Spalte (nützlich, wenn dort None steht).

# Quellen am besten mit 'qc data add <rolle> <csv> [-f <funktional>]' eintragen,
# damit Zwischenergebnis-CSVs draußen bleiben. Rollen: thermo, sp, solv.
sources:
  thermo: null                     # dU/dH/dG (+ ENERGY als Fallback-Elektronik)
  sp: null                         # elektronische Energien (mehrere Funktionale erlaubt)
  solv: null                       # Gsolv(Eh), mehrere Lösemittel erlaubt
  boltz: null                      # CENSO-Ensembles (G_LOW/G_AV)
  bfreq: null                      # Single-Point-Hessian (dG(Eh))
# Beispiel nach zwei 'qc data add sp ...':
#  sp:
#    - {path: 2_sp/r2scan0/outorca.csv, method: r2scan0}
#    - {path: 2_sp/pwpb95/outorca.csv, method: pwpb95}

# NAME-Bestandteile (durch '.' getrennt), die bei der Basename-Bildung
# an beliebiger Position entfernt werden. Tags wie .oet2 bleiben erhalten.
strip_suffixes: [optfreq, opt, freq, sp, solv, dock, censo, crest, conf, boltz, bfreq, sph]

defaults:
  unit: kcal/mol        # Einheit für Reaktionsenergien
  temperature: 298.15   # K, für die Standard-State-Correction
  solvation: false      # true = Gsolv immer mitnehmen (sonst nur mit -solv)
  solvent: null         # Standard-Lösemittel, z.B. 1,2-dichloroethane (sonst -lm)
  boltzmann: false      # true = Ensemble-Korrektur immer mitnehmen (sonst nur mit -boltz)
  state_correction: false
  method_sp: null       # Funktional der Hauptenergie, z.B. pwpb95 (sonst -f)
  method_thermo: null   # Funktional der Thermokorrektur (darf abweichen!)
  method_solv: null

# Plausibilitätsprüfungen beim Einlesen
checks:
  scf: true         # SCFCONV muss True sein
  geoconv: false    # CPSCF/GEOCONV — bei OptFreq oft False trotz sauberer Geometrie
  character: true   # CHAR passend zu IMAGS (MIN: 0, TS: 1)

# Wo liegen die Geometrien? {name} = Basename der Spezies.
# Wird der Reihe nach probiert; danach sucht qc rekursiv nach <name>.xyz.
geometries:
  - xyz/{name}.xyz
  - 1_optfreq/{name}.xyz
  - "{name}.xyz"

# Spalte der Single-Point-Hessian-Quelle (thermische Korrektur zu G)
bfreq_column: dG(Eh)

# Spalten der CENSO-/Boltzmann-Quelle
boltzmann_columns:
  low: G_LOW            # G des tiefsten Konformers (Eh)
  av: G_AV              # Boltzmann-gemitteltes G (Eh); Korrektur = av - low
  check: dG_AV          # Gegenprobe in kcal/mol (nur Warnung bei Abweichung)
#  value: dG_boltz      # alternativ: fertige Korrektur in Eh, dann low/av ignoriert

# Lösemittelnamen vereinheitlichen (CENSO schreibt oft anders als COSMOtherm)
solvent_aliases: {}
#  dichloroethane: 1,2-dichloroethane
#  dcm: dichloromethane

reactions: {}
#  deprot: "ni_h -> ni + h"
#  dimer: "2 mono -> dimer"
"""

QUANT_COLUMN = {"U": "dU", "H": "dH", "G": "dG"}
COEFF_RE = re.compile(r"^(\d+(?:\.\d+)?)\s+(.+)$")


def data_config_path(project_path):
    return Path(project_path) / DATA_CONFIG_NAME


def data_init():
    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    target = data_config_path(project_path)
    if target.exists():
        print(f"[INFO] {target} existiert bereits — nichts überschrieben.")
        return
    target.write_text(DATA_TEMPLATE)
    print(f"[OK] {target} erstellt (Projekt: {project_name}). Quellen eintragen!")


def load_data_config(project_path):
    try:
        import yaml
    except ImportError:
        print("Fehler: PyYAML nicht installiert (pip install pyyaml).", file=sys.stderr)
        sys.exit(1)
    cfg_file = data_config_path(project_path)
    if not cfg_file.exists():
        print(f"Fehler: Keine {DATA_CONFIG_NAME} im Projekt-Root gefunden.\n"
              f"Erzeuge eine Vorlage mit 'qc data init'.", file=sys.stderr)
        sys.exit(1)
    with open(cfg_file) as f:
        cfg = yaml.safe_load(f) or {}
    cfg["sources"] = cfg.get("sources") or {}
    cfg["strip_suffixes"] = cfg.get("strip_suffixes") or list(DEFAULT_STRIP_SUFFIXES)
    cfg["defaults"] = cfg.get("defaults") or {}
    cfg["defaults"].setdefault("unit", "kcal/mol")
    cfg["defaults"].setdefault("temperature", 298.15)
    cfg["defaults"].setdefault("state_correction", False)
    cfg["defaults"].setdefault("solvation", False)
    cfg["defaults"].setdefault("method_sp", None)
    cfg["defaults"].setdefault("method_thermo", None)
    cfg["defaults"].setdefault("method_solv", None)
    cfg["defaults"].setdefault("solvent", None)
    cfg["defaults"].setdefault("boltzmann", False)
    cfg["defaults"].setdefault("method_boltz", None)
    cfg["defaults"].setdefault("bfreq", False)
    cfg.setdefault("bfreq_column", "dG(Eh)")
    cfg["geometries"] = cfg.get("geometries") or ["xyz/{name}.xyz", "1_optfreq/{name}.xyz",
                                                  "{name}.xyz"]
    bc = cfg.get("boltzmann_columns") or {}
    bc.setdefault("low", "G_LOW")
    bc.setdefault("av", "G_AV")
    cfg["boltzmann_columns"] = bc
    cfg["solvent_aliases"] = cfg.get("solvent_aliases") or {}
    cfg["reactions"] = cfg.get("reactions") or {}
    cfg["checks"] = cfg.get("checks") or {}
    cfg["checks"].setdefault("scf", True)
    cfg["checks"].setdefault("geoconv", False)
    cfg["checks"].setdefault("character", True)
    return cfg


def make_basename(name, strip):
    """
    Bekannte Suffixe an beliebiger Stelle entfernen — sowohl punkt- als auch
    unterstrich-getrennt ('a.oet2.optfreq' und 'a.oet2_censo' -> 'a.oet2').
    """
    def clean(part):
        while True:
            head, sep, tail = part.rpartition("_")
            if sep and tail.lower() in strip:
                part = head
            else:
                return part

    parts = [q for q in str(name).strip().split(".") if q.lower() not in strip]
    if not parts:
        return str(name).strip()
    return ".".join([parts[0]] + [clean(q) for q in parts[1:]]) if len(parts) > 1 else clean(parts[0])


def norm_method(value):
    """'None'/leer -> None, sonst kleingeschriebener String."""
    s = str(value).strip().lower()
    return None if s in ("", "nan", "none", "null") else s


def normalize_source_entries(value):
    """Erlaubt: 'pfad', ['a','b'], {'path':..,'method':..}, [ {..}, 'pfad' ]."""
    if not value:
        return []
    if isinstance(value, (str, Path)):
        return [{"path": str(value), "method": None, "solvent": None}]
    if isinstance(value, dict):
        return [{"path": str(value["path"]), "method": value.get("method"),
                 "solvent": value.get("solvent")}]
    entries = []
    for item in value:
        if isinstance(item, dict):
            entries.append({"path": str(item["path"]), "method": item.get("method"),
                            "solvent": item.get("solvent")})
        else:
            entries.append({"path": str(item), "method": None, "solvent": None})
    return entries


def load_dataset(project_path, cfg):
    try:
        import pandas as pd
    except ImportError:
        print("Fehler: pandas nicht installiert.", file=sys.stderr)
        sys.exit(1)
    strip = {str(x).lower() for x in cfg["strip_suffixes"]}
    aliases = {str(k).strip().lower(): str(v).strip().lower()
               for k, v in (cfg.get("solvent_aliases") or {}).items()}
    data = {}
    for role in ROLES:
        frames = []
        for entry in normalize_source_entries(cfg["sources"].get(role)):
            path = Path(entry["path"])
            if not path.is_absolute():
                path = Path(project_path) / path
            if not path.exists():
                print(f"[WARN] Quelle '{role}' nicht gefunden: {path}", file=sys.stderr)
                continue
            df = pd.read_csv(path, sep=";")
            df.columns = [str(c).strip() for c in df.columns]
            df = df.rename(columns={"CHARGE": "CHRG", "MULTIPLICITY": "M"})
            df["BASENAME"] = df["NAME"].map(lambda n: make_basename(n, strip))
            # METHOD: Spalte normalisieren, Eintrag aus der data.yaml hat Vorrang
            if "METHOD" in df.columns:
                df["METHOD"] = df["METHOD"].map(norm_method)
            else:
                df["METHOD"] = None
            if entry["method"]:
                df["METHOD"] = norm_method(entry["method"])
            # SOLVENT: nur bei Solvatationsdaten relevant
            if "SOLVENT" in df.columns:
                df["SOLVENT"] = df["SOLVENT"].map(lambda v: norm_solvent(v, aliases))
            else:
                df["SOLVENT"] = None
            if entry.get("solvent"):
                df["SOLVENT"] = norm_solvent(entry["solvent"], aliases)
            df["SOURCEFILE"] = str(path)
            frames.append(df)
        if frames:
            data[role] = pd.concat(frames, ignore_index=True)
    if "thermo" not in data and "sp" not in data:
        print("Fehler: Keine nutzbare Quelle (thermo/sp) in data.yaml konfiguriert/gefunden.",
              file=sys.stderr)
        sys.exit(1)
    return data


def norm_solvent(value, aliases=None):
    """Lösemittelname normalisieren: klein, ohne Randleerzeichen, Aliase aufgelöst."""
    sv = str(value).strip().lower()
    if sv in ("", "nan", "none", "null"):
        return None
    if aliases:
        sv = aliases.get(sv, sv)
    return sv


def available_solvents(df):
    vals = [v for v in df["SOLVENT"].unique() if v is not None]
    return sorted(vals)


def resolve_solvent(data, explicit, cfg_default, strict=True, aliases=None):
    """Analog zu resolve_method, aber für die SOLVENT-Spalte der solv-Quelle."""
    if "solv" not in data:
        return None
    solvents = available_solvents(data["solv"])
    chosen = norm_solvent(explicit, aliases) if explicit else (
        norm_solvent(cfg_default, aliases) if cfg_default else None)
    if chosen is not None:
        if chosen not in solvents:
            avail = ", ".join(solvents) or "(keine)"
            print(f"Fehler: Lösemittel '{chosen}' nicht in der solv-Quelle vorhanden. "
                  f"Verfügbar: {avail}", file=sys.stderr)
            sys.exit(1)
        return chosen
    if len(solvents) > 1:
        if not strict:
            return None
        avail = ", ".join(solvents)
        print(f"Fehler: Mehrere Lösemittel in der solv-Quelle vorhanden ({avail}).\n"
              f"Reaktionen nur innerhalb eines Lösemittels — wähle mit -lm "
              f"oder setze 'defaults: solvent:' in der data.yaml.", file=sys.stderr)
        sys.exit(1)
    return solvents[0] if solvents else None


def available_methods(df):
    """Sortierte Liste der in einer Quelle vorhandenen Methoden (None möglich)."""
    vals = list(df["METHOD"].unique())
    named = sorted(m for m in vals if m is not None)
    return named + ([None] if any(m is None for m in vals) else [])


def resolve_method(data, role, explicit, cfg_default, label, strict=True):
    """
    Legt die zu verwendende Methode für eine Rolle fest.
    Reihenfolge: CLI-Flag > data.yaml > eindeutig vorhandene Methode.
    strict=True  (Reaktionen): mehrere Methoden -> Fehler, denn alle Spezies
                 müssen zwingend dasselbe Funktional benutzen.
    strict=False (Einzelabfragen): mehrere Methoden -> None, die Auflösung
                 passiert dann pro Spezies (eine Spezies hat oft nur eine).
    """
    if role not in data:
        return None
    methods = available_methods(data[role])
    chosen = norm_method(explicit) if explicit else (norm_method(cfg_default) if cfg_default else None)
    if chosen is not None:
        if chosen not in methods:
            avail = ", ".join(str(m) for m in methods)
            print(f"Fehler: Methode '{chosen}' nicht in Quelle '{role}' vorhanden. "
                  f"Verfügbar: {avail}", file=sys.stderr)
            sys.exit(1)
        return chosen
    if len(methods) > 1:
        if not strict:
            return None
        avail = ", ".join(str(m) for m in methods)
        print(f"Fehler: Mehrere Funktionale in Quelle '{role}' vorhanden ({avail}).\n"
              f"Relative Energien nur bei gleichem Funktional — wähle mit {label} "
              f"oder setze 'defaults' in der data.yaml.", file=sys.stderr)
        sys.exit(1)
    return methods[0] if methods else None


def parse_spec(spec):
    """'name@3' -> ('name', 3); 'name' -> ('name', None)"""
    if "@" in spec:
        name, _, m = spec.rpartition("@")
        if m.isdigit():
            return name, int(m)
    return spec, None


def pick_row(df, base, m, role, method=None, solvent=None):
    hits = df[df["BASENAME"] == base]
    if m is not None and "M" in hits.columns:
        hits = hits[hits["M"].astype(float) == float(m)]
    if method is not None:
        hits = hits[hits["METHOD"] == method]
    if solvent is not None and hits["SOLVENT"].notna().any():
        # Quellen ohne Lösemittelangabe (z.B. CENSO ohne SOLVENT-Spalte) nicht filtern
        hits = hits[hits["SOLVENT"] == solvent]
    elif solvent is not None:
        solvent = None
    if len(hits) == 0:
        desc = f"'{base}'"
        if m is not None:
            desc += f" (M={m})"
        if method is not None:
            desc += f" mit Methode '{method}'"
        if solvent is not None:
            desc += f" in '{solvent}'"
        msg = f"{desc} nicht in Quelle '{role}' gefunden."
        pool = df[df["BASENAME"] == base]
        if method is not None and len(pool):
            have = ", ".join(str(x) for x in available_methods(pool))
            msg += f"\nFür '{base}' vorhanden: {have}"
        else:
            matches = difflib.get_close_matches(base, list(df["BASENAME"].unique()), n=3, cutoff=0.5)
            if matches:
                msg += f"\nMeintest du: {', '.join(matches)}?"
        raise LookupError(msg)
    if len(hits) > 1:
        if "M" in hits.columns and hits["M"].nunique() > 1:
            ms = sorted(set(int(x) for x in hits["M"]))
            raise LookupError(f"'{base}' ist mehrdeutig in Quelle '{role}' (M = {ms}).\n"
                              f"Wähle die Multiplizität mit '{base}@M' oder -m.")
        if hits["METHOD"].nunique() > 1:
            ms = ", ".join(str(x) for x in available_methods(hits))
            raise LookupError(f"'{base}' liegt in Quelle '{role}' mit mehreren Funktionalen "
                              f"vor ({ms}). Wähle eines mit -f.")
        if hits["SOLVENT"].nunique() > 1:
            svs = ", ".join(available_solvents(hits))
            raise LookupError(f"'{base}' liegt in Quelle '{role}' in mehreren Lösemitteln "
                              f"vor ({svs}). Wähle eines mit -lm.")
        print(f"[WARN] '{base}' kommt {len(hits)}x in Quelle '{role}' vor — nehme die erste Zeile.",
              file=sys.stderr)
    return hits.iloc[0]


def row_sanity_warnings(row, base, role, checks=None):
    """
    Plausibilitätsprüfung einer Zeile. 'checks' kommt aus data.yaml:
      scf:       SCFCONV muss True sein
      geoconv:   CPSCF/GEOCONV muss True sein (Default AUS — die Spalte ist bei
                 OptFreq oft False, obwohl die Geometrie sauber konvergiert ist;
                 CHAR/IMAGS ist der verlässlichere Indikator)
      character: CHAR und IMAGS müssen zueinander passen
    """
    checks = checks or {}

    def is_false(v):
        return str(v).strip().lower() == "false"

    if checks.get("scf", True) and is_false(row.get("SCFCONV")):
        print(f"[WARN] {base} ({role}): SCF nicht konvergiert!", file=sys.stderr)

    if checks.get("geoconv", False) and is_false(row.get("CPSCF/GEOCONV")):
        print(f"[WARN] {base} ({role}): CPSCF/GEOCONV = False.", file=sys.stderr)

    if not checks.get("character", True):
        return

    char = str(row.get("CHAR", "")).strip().upper()
    if char in ("", "NAN", "NONE"):
        return

    imags = row.get("IMAGS")
    s = str(imags).strip()
    n_imag = None
    if s not in ("", "nan", "None"):
        try:
            n_imag = int(float(s))
        except (TypeError, ValueError):
            n_imag = None
    if n_imag is None:
        n_imag = 0  # leere Spalte = keine imaginären Moden

    if char == "MIN" and n_imag > 0:
        print(f"[WARN] {base} ({role}): als MIN geführt, aber {n_imag} imaginäre Mode(n)!",
              file=sys.stderr)
    elif char == "TS" and n_imag != 1:
        print(f"[WARN] {base} ({role}): als TS geführt, aber {n_imag} imaginäre Mode(n)!",
              file=sys.stderr)
    elif char not in ("MIN", "TS"):
        print(f"[WARN] {base} ({role}): unbekannter Charakter '{char}'.", file=sys.stderr)


def boltz_sanity_warnings(row, base):
    """CENSO-spezifische Plausibilitätsprüfungen."""
    status = str(row.get("STATUS", "")).strip().upper()
    if status not in ("", "NAN", "NONE", "OK"):
        print(f"[WARN] {base} (boltz): STATUS = {status}", file=sys.stderr)
    try:
        failed = float(row.get("FAILED_CALC_COUNT", 0) or 0)
        if failed > 0:
            rel = row.get("FAILED_CALC_COUNT_REL")
            extra = f" ({float(rel) * 100:.1f}%)" if rel not in (None, "", "nan") else ""
            print(f"[WARN] {base} (boltz): {int(failed)} fehlgeschlagene Rechnungen{extra}",
                  file=sys.stderr)
    except (TypeError, ValueError):
        pass


def state_correction_eh(temperature):
    """Standard-State-Correction 1 atm (ideales Gas) -> 1 mol/L: RT ln(R'T)."""
    return R_KCAL * temperature * math.log(0.0820573660809596 * temperature) / HARTREE_TO_KCAL


def species_value(data, cfg, spec, quantity, m_flag=None, sc=None, use_solv=None,
                  methods=None, solvent=None, use_boltz=None, use_bfreq=None):
    """Liefert (Wert in Eh, Breakdown-Dict) für eine Spezies wie 'name' oder 'name@M'."""
    base, m = parse_spec(spec)
    if m is None:
        m = m_flag
    methods = methods or {}
    defaults = cfg["defaults"]
    if sc is None:
        sc = bool(defaults.get("state_correction", False))
    if use_solv is None:
        use_solv = bool(defaults.get("solvation", False))
    if use_boltz is None:
        use_boltz = bool(defaults.get("boltzmann", False))
    if use_bfreq is None:
        use_bfreq = bool(defaults.get("bfreq", False))
    T = float(defaults.get("temperature", 298.15))

    bd = {"name": base, "M": m, "CHRG": None, "thermo_role": None}

    # Elektronische Energie: sp-Quelle bevorzugt, sonst ENERGY aus thermo
    elec_role = "sp" if "sp" in data else "thermo"
    m_elec = methods.get(elec_role)
    row_e = pick_row(data[elec_role], base, m, elec_role, method=m_elec)
    row_sanity_warnings(row_e, base, elec_role, cfg["checks"])
    E = float(row_e["ENERGY"])
    bd["E"], bd["elec_role"], bd["method_sp"] = E, elec_role, row_e["METHOD"]
    try:
        if bd["M"] is None:
            bd["M"] = int(float(row_e["M"]))
        bd["CHRG"] = int(float(row_e["CHRG"]))
    except (KeyError, TypeError, ValueError):
        pass

    # Thermische Korrektur (dU/dH/dG) — darf ein anderes Funktional sein
    thermal = 0.0
    bd["method_thermo"] = None
    if quantity in QUANT_COLUMN and use_bfreq:
        if "bfreq" not in data:
            raise LookupError("SPH-Korrektur angefordert (-bfreq), aber keine 'bfreq'-Quelle "
                              "in der data.yaml eingetragen.")
        if quantity != "G":
            raise LookupError(f"Die bfreq-Quelle liefert nur dG — {quantity} ist damit "
                              f"nicht berechenbar (nutze -q G oder lass -bfreq weg).")
        col = cfg["bfreq_column"]
        row_t = pick_row(data["bfreq"], base, m, "bfreq", method=methods.get("bfreq"))
        if col not in row_t.index:
            raise LookupError(f"Spalte '{col}' fehlt in der bfreq-Quelle. "
                              f"Passe 'bfreq_column' in der data.yaml an.")
        thermal = float(row_t[col])
        bd["method_thermo"] = row_t["METHOD"]
        bd["thermo_role"] = "bfreq"
    elif quantity in QUANT_COLUMN:
        if "thermo" not in data:
            raise LookupError(f"{quantity} benötigt eine 'thermo'-Quelle in der data.yaml.")
        if elec_role == "thermo":
            row_t = row_e
        else:
            row_t = pick_row(data["thermo"], base, m, "thermo", method=methods.get("thermo"))
            row_sanity_warnings(row_t, base, "thermo", cfg["checks"])
        thermal = float(row_t[QUANT_COLUMN[quantity]])
        bd["method_thermo"] = row_t["METHOD"]
        bd["thermo_role"] = "thermo"
    bd["thermal"] = thermal

    # Solvatation — nur wenn ausdrücklich gewünscht (-solv bzw. defaults.solvation)
    solv = 0.0
    bd["method_solv"] = None
    if use_solv:
        if "solv" not in data:
            raise LookupError("Solvatation angefordert (-solv), aber keine 'solv'-Quelle "
                              "in der data.yaml eingetragen.")
        row_s = pick_row(data["solv"], base, m, "solv", method=methods.get("solv"),
                         solvent=solvent)
        col = "Gsolv(Eh)" if "Gsolv(Eh)" in row_s.index else "Gsolv"
        if col not in row_s.index:
            raise LookupError("Keine 'Gsolv(Eh)'-Spalte in der solv-Quelle gefunden.")
        solv = float(row_s[col])
        bd["method_solv"] = row_s["METHOD"]
        bd["solvent"] = row_s["SOLVENT"]
    bd["solv"] = solv
    bd["gas"] = not use_solv
    bd.setdefault("solvent", None)

    # Boltzmann-/Ensemble-Korrektur (CENSO) — nur mit -boltz
    boltz = 0.0
    if use_boltz:
        if "boltz" not in data:
            raise LookupError("Ensemble-Korrektur angefordert (-boltz), aber keine "
                              "'boltz'-Quelle in der data.yaml eingetragen.")
        bc = cfg["boltzmann_columns"]
        row_b = pick_row(data["boltz"], base, m, "boltz", method=methods.get("boltz"),
                         solvent=solvent if use_solv else None)
        boltz_sanity_warnings(row_b, base)
        if bc.get("value") and bc["value"] in row_b.index:
            boltz = float(row_b[bc["value"]])
        else:
            for c in (bc["low"], bc["av"]):
                if c not in row_b.index:
                    raise LookupError(f"Spalte '{c}' fehlt in der boltz-Quelle. "
                                      f"Passe 'boltzmann_columns' in der data.yaml an.")
            boltz = float(row_b[bc["av"]]) - float(row_b[bc["low"]])
            # Gegenprobe gegen eine mitgelieferte kcal/mol-Spalte (z.B. dG_AV)
            cross = bc.get("check", "dG_AV")
            if cross in row_b.index:
                try:
                    ref = float(row_b[cross])
                    if abs(boltz * HARTREE_TO_KCAL - ref) > 0.01:
                        print(f"[WARN] {base} (boltz): {bc['av']}-{bc['low']} = "
                              f"{boltz * HARTREE_TO_KCAL:.4f} kcal/mol passt nicht zu "
                              f"{cross} = {ref:.4f}.", file=sys.stderr)
                except (TypeError, ValueError):
                    pass
    bd["boltz"] = boltz

    # Standard-State-Correction (nur für G)
    state = state_correction_eh(T) if (quantity == "G" and sc) else 0.0
    bd["state"] = state

    bd["total"] = E + thermal + solv + state + bd["boltz"]
    return bd["total"], bd


def print_breakdown(bd, quantity):
    w = 34

    def line(label, val):
        print(f" {label:<{w}}{val:18.8f} Eh  {val * HARTREE_TO_KCAL:14.3f} kcal/mol")

    mstr = f", M={bd['M']}" if bd["M"] is not None else ""
    cstr = f", q={bd['CHRG']:+d}" if bd["CHRG"] is not None else ""
    print(f"\n{quantity} für '{bd['name']}'{mstr}{cstr}:")
    lab_e = f"Elektronische Energie ({bd['method_sp'] or bd['elec_role']}):"
    line(lab_e, bd["E"])
    if quantity in QUANT_COLUMN:
        m_t = bd["method_thermo"]
        src = "SPH" if bd.get("thermo_role") == "bfreq" else QUANT_COLUMN[quantity]
        lab_t = f"Thermokorrektur {src}" + (f" ({m_t})" if m_t else "") + ":"
        line(lab_t, bd["thermal"])
    if not bd["gas"]:
        parts = [p for p in (bd.get("solvent"), bd["method_solv"]) if p]
        line("Solvatation (Gsolv" + (", " + ", ".join(parts) if parts else "") + "):", bd["solv"])
    if bd.get("boltz"):
        line("Ensemble-Korrektur (Boltzmann):", bd["boltz"])
    if bd["state"]:
        line("Standard-State (1 atm -> 1 M):", bd["state"])
    print(" " + "-" * (w + 40))
    line("Summe:", bd["total"])


def format_value(val_eh, unit):
    conv = UNIT_CONVERSION[unit]
    fmt = ".8f" if conv == 1.0 else ".3f"
    label = "Eh" if conv == 1.0 else unit
    return f"{val_eh * conv:{fmt}} {label}"


def check_unit(unit):
    unit = unit.lower()
    if unit not in UNIT_CONVERSION:
        valid = ", ".join(UNIT_CONVERSION)
        print(f"Fehler: Unbekannte Einheit '{unit}'. Gültig: {valid}", file=sys.stderr)
        sys.exit(1)
    return unit


def parse_data_flags(rest):
    opts = {"m": None, "sc": None, "v": False, "unit": None, "q": "G", "solv": None,
            "f": None, "ft": None, "fsolv": None, "fboltz": None,
            "boltz": None, "bfreq": None, "lm": None}
    pos = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("-sc", "--state-correction"):
            opts["sc"] = True
        elif a in ("-solv", "--solvation"):
            opts["solv"] = True
        elif a in ("-gas", "--gas"):
            opts["solv"] = False
        elif a in ("-boltz", "--boltzmann"):
            opts["boltz"] = True
        elif a in ("-bfreq", "--sph"):
            opts["bfreq"] = True
        elif a in ("-lm", "--solvent") and i + 1 < len(rest):
            opts["lm"] = rest[i + 1]; i += 1
        elif a in ("-v", "--verbose"):
            opts["v"] = True
        elif a == "-m" and i + 1 < len(rest):
            opts["m"] = int(rest[i + 1]); i += 1
        elif a in ("-f", "--functional") and i + 1 < len(rest):
            opts["f"] = rest[i + 1]; i += 1
        elif a in ("-ft", "--functional-thermo") and i + 1 < len(rest):
            opts["ft"] = rest[i + 1]; i += 1
        elif a in ("-fsolv", "--functional-solv") and i + 1 < len(rest):
            opts["fsolv"] = rest[i + 1]; i += 1
        elif a in ("-fboltz", "--functional-boltz") and i + 1 < len(rest):
            opts["fboltz"] = rest[i + 1]; i += 1
        elif a in ("-u", "--unit") and i + 1 < len(rest):
            opts["unit"] = rest[i + 1]; i += 1
        elif a == "-q" and i + 1 < len(rest):
            opts["q"] = rest[i + 1].upper(); i += 1
        else:
            pos.append(a)
        i += 1
    return pos, opts


def resolve_all_methods(data, cfg, opts, strict=True):
    """
    Legt die Funktionale für alle Rollen fest. Bei strict=True (Reaktionen) ist
    damit garantiert, dass alle Hauptenergien vom selben Funktional stammen;
    bei strict=False bleibt die Wahl offen und wird pro Spezies aufgelöst.
    """
    d = cfg["defaults"]
    elec_role = "sp" if "sp" in data else "thermo"
    methods = {elec_role: resolve_method(data, elec_role, opts["f"], d.get("method_sp"),
                                         "-f", strict)}
    if "thermo" in data and elec_role != "thermo":
        methods["thermo"] = resolve_method(data, "thermo", opts["ft"], d.get("method_thermo"),
                                           "-ft", strict)
    if "solv" in data:
        methods["solv"] = resolve_method(data, "solv", opts["fsolv"], d.get("method_solv"),
                                         "-fsolv", strict)
    if "boltz" in data:
        methods["boltz"] = resolve_method(data, "boltz", opts["fboltz"],
                                          d.get("method_boltz"), "-fboltz", strict)
    if "bfreq" in data:
        methods["bfreq"] = resolve_method(data, "bfreq", None, None, "-f", strict)
    return methods


def resolve_context(data, cfg, opts, strict=True):
    """Funktionale + Lösemittel in einem Rutsch festlegen."""
    methods = resolve_all_methods(data, cfg, opts, strict)
    need_solv = opts["solv"] if opts["solv"] is not None else bool(cfg["defaults"].get("solvation"))
    solvent = resolve_solvent(data, opts["lm"], cfg["defaults"].get("solvent"),
                              strict and bool(need_solv),
                              aliases={str(k).strip().lower(): str(v).strip().lower()
                                       for k, v in (cfg.get("solvent_aliases") or {}).items()})
    return methods, solvent


def parse_reaction(eq):
    for arrow in ("->", "=>", "→", "="):
        if arrow in eq:
            left, right = eq.split(arrow, 1)
            break
    else:
        raise ValueError(f"Keine Reaktionsgleichung erkennbar (kein '->'): {eq}")

    def side(s):
        terms = []
        for t in s.split("+"):
            t = t.strip()
            if not t:
                continue
            mm = COEFF_RE.match(t)
            terms.append((float(mm.group(1)), mm.group(2).strip()) if mm else (1.0, t))
        return terms

    reactants, products = side(left), side(right)
    if not reactants or not products:
        raise ValueError(f"Unvollständige Reaktionsgleichung: {eq}")
    return reactants, products


def cmd_energy(quantity, rest):
    pos, opts = parse_data_flags(rest)
    if not pos:
        print(f"Verwendung: qc {quantity.lower()} <name> [name2 ...] [-m M] [-solv] [-sc] [-u unit] [-v]",
              file=sys.stderr)
        sys.exit(1)
    config = load_global_config()
    _, project_path, _ = require_project(config)
    cfg = load_data_config(project_path)
    data = load_dataset(project_path, cfg)
    unit = check_unit(opts["unit"] or "hartree")
    methods, solvent = resolve_context(data, cfg, opts, strict=False)

    for spec in pos:
        try:
            val, bd = species_value(data, cfg, spec, quantity, m_flag=opts["m"],
                                    sc=opts["sc"], use_solv=opts["solv"], methods=methods,
                                    solvent=solvent, use_boltz=opts["boltz"],
                                    use_bfreq=opts["bfreq"])
        except (LookupError, ValueError) as e:
            print(f"Fehler: {e}", file=sys.stderr)
            sys.exit(1)
        if opts["v"]:
            print_breakdown(bd, quantity)
        else:
            tag = "" if bd["gas"] else f"(solv{', ' + bd['solvent'] if bd.get('solvent') else ''})"
            extras = [x for x in (bd["method_sp"], "boltz" if bd.get("boltz") else None) if x]
            mtag = f" [{', '.join(extras)}]" if extras else ""
            print(f"{quantity}{tag}({bd['name']}) = {format_value(val, unit)}{mtag}")


def rxn_add(name, equation):
    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    root = Path(project_path)
    cfg = load_data_config(root)

    if name in ("add", "rm", "list"):
        print(f"Fehler: '{name}' ist ein reservierter Unterbefehl von 'qc rxn'.", file=sys.stderr)
        sys.exit(1)
    try:
        reactants, products = parse_reaction(equation)
    except ValueError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        sys.exit(1)

    # Spezies gegen die Daten prüfen (nur warnen, nicht blockieren)
    try:
        data = load_dataset(root, cfg)
        elec_role = "sp" if "sp" in data else "thermo"
        known = set(data[elec_role]["BASENAME"])
        for _, spec in reactants + products:
            base, _m = parse_spec(spec)
            if base not in known:
                print(f"[WARN] '{base}' ist in Quelle '{elec_role}' (noch) nicht vorhanden.",
                      file=sys.stderr)
    except SystemExit:
        pass

    action = "aktualisiert" if name in cfg["reactions"] else "hinzugefügt"
    cfg["reactions"][name] = equation
    write_data_config(root, cfg)
    print(f"[OK] Reaktion '{name}' {action} (Projekt: {project_name}):")
    print(f"     {equation}")


def rxn_rm(name):
    config = load_global_config()
    _, project_path, _ = require_project(config)
    root = Path(project_path)
    cfg = load_data_config(root)
    if name not in cfg["reactions"]:
        print(f"Fehler: Reaktion '{name}' ist nicht definiert.", file=sys.stderr)
        suggest(name, cfg["reactions"].keys())
        sys.exit(1)
    del cfg["reactions"][name]
    write_data_config(root, cfg)
    print(f"[OK] Reaktion '{name}' entfernt.")


def rxn_list():
    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    cfg = load_data_config(project_path)
    print(f"--- Reaktionen in '{project_name}' ---")
    if not cfg["reactions"]:
        print(' (keine) — anlegen mit: qc rxn add <name> "a + b -> c"')
        return
    width = max(len(k) for k in cfg["reactions"])
    for k, v in cfg["reactions"].items():
        print(f" {k:<{width}}  {v}")


def cmd_reaction(rest):
    # Unterbefehle zur Verwaltung
    if rest and rest[0] == "add":
        args = rest[1:]
        if len(args) < 2:
            print('Verwendung: qc rxn add <name> "a + 2 b -> c"', file=sys.stderr)
            sys.exit(1)
        rxn_add(args[0], " ".join(args[1:]))
        return
    if rest and rest[0] == "rm":
        if len(rest) < 2:
            print("Verwendung: qc rxn rm <name>", file=sys.stderr)
            sys.exit(1)
        rxn_rm(rest[1])
        return
    if rest and rest[0] == "list":
        rxn_list()
        return

    pos, opts = parse_data_flags(rest)
    if not pos:
        print('Verwendung: qc rxn "<a + b -> c>" | <definierter_name>  [-q E|U|H|G] [-solv] [-sc] [-u unit] [-v]',
              file=sys.stderr)
        sys.exit(1)
    quantity = opts["q"]
    if quantity not in ("E", "U", "H", "G"):
        print(f"Fehler: -q muss E, U, H oder G sein (nicht '{quantity}').", file=sys.stderr)
        sys.exit(1)

    config = load_global_config()
    _, project_path, _ = require_project(config)
    cfg = load_data_config(project_path)
    data = load_dataset(project_path, cfg)
    unit = check_unit(opts["unit"] or cfg["defaults"]["unit"])
    methods, solvent = resolve_context(data, cfg, opts, strict=True)

    key = " ".join(pos)
    eq = cfg["reactions"].get(key, key)
    try:
        reactants, products = parse_reaction(eq)
    except ValueError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        if cfg["reactions"]:
            print("Definierte Reaktionen:", ", ".join(cfg["reactions"]), file=sys.stderr)
        sys.exit(1)

    total = 0.0
    charge = {-1: 0.0, +1: 0.0}
    charge_ok = True
    details = []
    for sign, terms in ((-1, reactants), (+1, products)):
        for coeff, spec in terms:
            try:
                val, bd = species_value(data, cfg, spec, quantity, sc=opts["sc"],
                                        use_solv=opts["solv"], methods=methods,
                                        solvent=solvent, use_boltz=opts["boltz"],
                                    use_bfreq=opts["bfreq"])
            except (LookupError, ValueError) as e:
                print(f"Fehler: {e}", file=sys.stderr)
                sys.exit(1)
            total += sign * coeff * val
            if bd["CHRG"] is None:
                charge_ok = False
            else:
                charge[sign] += coeff * bd["CHRG"]
            details.append((sign, coeff, bd))

    if charge_ok and abs(charge[-1] - charge[+1]) > 1e-9:
        print(f"[WARN] Ladungsbilanz verletzt: links {charge[-1]:g}, rechts {charge[+1]:g}!",
              file=sys.stderr)

    # Sicherheitsnetz: Hauptenergien müssen vom selben Funktional stammen
    used_sp = {bd["method_sp"] for _, _, bd in details}
    if len(used_sp) > 1:
        print(f"Fehler: Hauptenergien stammen aus verschiedenen Funktionalen "
              f"({', '.join(str(m) for m in sorted(used_sp, key=str))}) — "
              f"relative Energien wären bedeutungslos. Wähle eines mit -f.", file=sys.stderr)
        sys.exit(1)
    used_lm = {bd.get("solvent") for _, _, bd in details if not bd["gas"]}
    if len(used_lm) > 1:
        print(f"Fehler: Spezies aus verschiedenen Lösemitteln "
              f"({', '.join(str(x) for x in sorted(used_lm, key=str))}) — "
              f"nicht vergleichbar. Wähle eines mit -lm.", file=sys.stderr)
        sys.exit(1)
    used_thermo = {bd["method_thermo"] for _, _, bd in details}
    if len(used_thermo) > 1:
        print(f"[WARN] Thermokorrekturen aus verschiedenen Funktionalen: "
              f"{', '.join(str(m) for m in sorted(used_thermo, key=str))}", file=sys.stderr)

    if opts["v"]:
        print(f"\nReaktion: {eq}")
        for sign, coeff, bd in details:
            side = "Edukt  " if sign < 0 else "Produkt"
            mstr = f"@{bd['M']}" if bd["M"] is not None else ""
            print(f" {side}  {coeff:g} x {bd['name'] + mstr:<32} "
                  f"{quantity} = {bd['total']:18.8f} Eh")
        print(" " + "-" * 78)

    lm0 = details[0][2].get("solvent")
    tag = "" if details[0][2]["gas"] else f"(solv{', ' + lm0 if lm0 else ''})"
    sc_used = any(bd["state"] for _, _, bd in details)
    notes = []
    m_sp = details[0][2]["method_sp"]
    m_th = details[0][2]["method_thermo"]
    if m_sp:
        notes.append(f"E: {m_sp}")
    if m_th and m_th != m_sp:
        notes.append(f"thermo: {m_th}")
    if any(bd.get("boltz") for _, _, bd in details):
        notes.append("Boltzmann")
    if sc_used:
        notes.append("State-Corr.")
    note_str = f"  [{', '.join(notes)}]" if notes else ""
    print(f"d{quantity}{tag} = {format_value(total, unit)}{note_str}   ({eq})")


def cmd_species():
    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    cfg = load_data_config(project_path)
    data = load_dataset(project_path, cfg)

    roles = [r for r in ROLES if r in data]
    info = {}
    for role in roles:
        for _, row in data[role].iterrows():
            entry = info.setdefault(row["BASENAME"],
                                    {"roles": set(), "M": set(), "CHRG": set(),
                                     "meth": set(), "lm": set()})
            entry["roles"].add(role)
            if row["METHOD"]:
                entry["meth"].add(row["METHOD"])
            if row.get("SOLVENT"):
                entry["lm"].add(row["SOLVENT"])
            for col, key in (("M", "M"), ("CHRG", "CHRG")):
                try:
                    entry[key].add(int(float(row[col])))
                except (KeyError, TypeError, ValueError):
                    pass

    if not info:
        print("Keine Spezies gefunden — sind die Quellen in der data.yaml korrekt?")
        return

    width = max([len(b) for b in info] + [len("SPEZIES")])
    show_lm = any(e["lm"] for e in info.values())
    header = (f" {'SPEZIES':<{width}}  {'q':>3} {'M':>3}  "
              + "  ".join(f"{r:^6}" for r in roles) + "  METHODEN")
    if show_lm:
        header += "   LÖSEMITTEL"
    print(f"--- Spezies in '{project_name}' ---")
    print(header)
    mwidth = max([len(", ".join(sorted(e["meth"]))) for e in info.values()] + [8])
    for base in sorted(info):
        e = info[base]
        q = "/".join(str(x) for x in sorted(e["CHRG"])) or "?"
        m = "/".join(str(x) for x in sorted(e["M"])) or "?"
        cells = "  ".join(f"{'x' if r in e['roles'] else '-':^6}" for r in roles)
        meth = ", ".join(sorted(e["meth"])) or "-"
        row = f" {base:<{width}}  {q:>3} {m:>3}  {cells}  {meth:<{mwidth}}"
        if show_lm:
            row += "   " + (", ".join(sorted(e["lm"])) or "-")
        print(row.rstrip())


# --- GEOMETRIEN & INTERNE KOORDINATEN ---

def read_xyz(path):
    """XYZ-Datei lesen -> [(symbol, x, y, z), ...] in Angström."""
    with open(path) as f:
        lines = f.read().splitlines()
    try:
        n = int(lines[0].split()[0])
    except (IndexError, ValueError):
        raise ValueError(f"Keine gültige XYZ-Datei: {path}")
    atoms = []
    for line in lines[2:2 + n]:
        parts = line.split()
        if len(parts) < 4:
            continue
        atoms.append((parts[0], float(parts[1]), float(parts[2]), float(parts[3])))
    if len(atoms) != n:
        raise ValueError(f"{path}: {len(atoms)} Atome gelesen, {n} erwartet.")
    return atoms


def find_geometry(project_path, base, cfg):
    """Geometriedatei zu einer Spezies suchen (Muster aus data.yaml, dann rekursiv)."""
    root = Path(project_path)
    for pattern in cfg["geometries"]:
        cand = root / pattern.format(name=base)
        if cand.exists():
            return cand
    hits = sorted(root.rglob(f"{base}.xyz"))
    if hits:
        return hits[0]
    return None


def _vec(a, b):
    return (b[1] - a[1], b[2] - a[2], b[3] - a[3])


def _dot(u, v):
    return sum(x * y for x, y in zip(u, v))


def _cross(u, v):
    return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])


def _norm(u):
    return math.sqrt(_dot(u, u))


def internal_coordinate(atoms, kind, idx, one_based=True):
    """
    Interne Koordinate berechnen.
      B i j       -> Bindungslänge in Angström
      A i j k     -> Winkel in Grad (j = Scheitel)
      D i j k l   -> Diederwinkel in Grad
    """
    kind = kind.upper()
    need = {"B": 2, "A": 3, "D": 4}
    if kind not in need:
        raise ValueError(f"Unbekannter Koordinatentyp '{kind}' (erlaubt: B, A, D).")
    if len(idx) != need[kind]:
        raise ValueError(f"{kind} braucht {need[kind]} Atomindizes, {len(idx)} angegeben.")

    pos = []
    for i in idx:
        j = i - 1 if one_based else i
        if j < 0 or j >= len(atoms):
            raise ValueError(f"Atomindex {i} liegt außerhalb (Molekül hat {len(atoms)} Atome).")
        pos.append(atoms[j])

    if kind == "B":
        return _norm(_vec(pos[0], pos[1]))

    if kind == "A":
        u, v = _vec(pos[1], pos[0]), _vec(pos[1], pos[2])
        c = _dot(u, v) / (_norm(u) * _norm(v))
        return math.degrees(math.acos(max(-1.0, min(1.0, c))))

    # Dieder nach der praxisüblichen atan2-Formulierung (vorzeichenrichtig)
        # Signierter Diederwinkel nach IUPAC/Klyne-Prelog (-180..+180),
    # gleiche Konvention wie ORCA, Molden und Avogadro:
    #   phi = atan2( ((b1 x b2) x (b2 x b3)) . b2/|b2| ,  (b1 x b2) . (b2 x b3) )
    b1 = _vec(pos[0], pos[1])
    b2 = _vec(pos[1], pos[2])
    b3 = _vec(pos[2], pos[3])
    n1, n2 = _cross(b1, b2), _cross(b2, b3)
    nb2 = _norm(b2)
    if nb2 < 1e-10 or _norm(n1) < 1e-10 or _norm(n2) < 1e-10:
        raise ValueError("Diederwinkel nicht definiert (kollineare Atome).")
    x = _dot(n1, n2)
    y = _dot(_cross(n1, n2), b2) / nb2
    return math.degrees(math.atan2(y, x))

def parse_coord_spec(tokens):
    """['B','1','2'] -> ('B', [1, 2]); auch 'B,1,2' oder 'B 1 2' als ein Token."""
    flat = []
    for t in tokens:
        flat.extend(str(t).replace(",", " ").split())
    if not flat:
        raise ValueError("Keine Koordinate angegeben (z.B. B 1 2, A 1 2 3, D 1 2 3 4).")
    kind = flat[0]
    try:
        idx = [int(x) for x in flat[1:]]
    except ValueError:
        raise ValueError(f"Atomindizes müssen ganze Zahlen sein: {' '.join(flat[1:])}")
    return kind, idx


COORD_LABEL = {"B": ("Bindungslänge", "Å"), "A": ("Winkel", "°"), "D": ("Diederwinkel", "°")}


# --- HAUPT-CSVs IM HOME-VERZEICHNIS ---

MAIN_KEYS = ["PROJECT", "BASENAME", "M", "CHRG", "METHOD", "SOLVENT"]


def project_csv_path(project_name, role):
    """Projekt-eigene Sammel-CSV, z.B. ~/.qc_data/vertiefer_demyan_main_sp.csv"""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(project_name))
    return MAIN_DATA_DIR / f"{safe}_main_{role}.csv"


def _merge_into(target, df, subset):
    """df in die CSV unter 'target' einpflegen (letzter Stand gewinnt)."""
    import pandas as pd
    if target.exists():
        try:
            old = pd.read_csv(target, sep=";")
            old.columns = [str(c).strip() for c in old.columns]
            merged = pd.concat([old, df], ignore_index=True)
            merged = merged.drop_duplicates(subset=subset, keep="last")
        except Exception as e:
            print(f"[WARN] {target} nicht lesbar ({e}) — wird neu geschrieben.", file=sys.stderr)
            merged = df
    else:
        merged = df
    sort_cols = [c for c in ("PROJECT", "BASENAME", "METHOD", "SOLVENT") if c in merged.columns]
    if sort_cols:
        merged = merged.sort_values(by=sort_cols)
    merged.to_csv(target, sep=";", index=False)
    return len(merged)


def update_main_csvs(project_name, project_path, cfg=None, roles=None, quiet=False):
    """
    Schreibt pro Rolle eine projekteigene Sammel-CSV:
    ~/.qc_data/<projekt>_main_<rolle>.csv
    Schlüssel: BASENAME + M + CHRG + METHOD + SOLVENT — jede Kombination steht
    genau einmal drin, neue Einlesungen überschreiben den alten Stand.
    """
    try:
        import pandas as pd  # noqa: F401
    except ImportError:
        return
    cfg = cfg or load_data_config(project_path)
    data = load_dataset(project_path, cfg)
    MAIN_DATA_DIR.mkdir(exist_ok=True)

    for role in (roles or ROLES):
        if role not in data:
            continue
        df = data[role].copy()
        df.insert(0, "PROJECT", project_name)
        for col in MAIN_KEYS:
            if col not in df.columns:
                df[col] = None

        target = project_csv_path(project_name, role)
        n = _merge_into(target, df, [k for k in MAIN_KEYS if k != "PROJECT"])
        if not quiet:
            print(f"[OK] {target}  ({n} Zeilen)")


def cmd_main_update(all_projects=False):
    config = load_global_config()
    if all_projects:
        if not config["projects"]:
            print("Keine Projekte registriert.")
            return
        for name, path in config["projects"].items():
            if not data_config_path(Path(path)).exists():
                continue
            print(f"--- {name} ---")
            try:
                update_main_csvs(name, Path(path))
            except SystemExit:
                print(f"[WARN] {name} übersprungen (Datenquellen unvollständig).",
                      file=sys.stderr)
        return
    name, path, _ = require_project(config)
    print(f"--- {name} ---")
    update_main_csvs(name, path)


def cmd_main_show():
    if not MAIN_DATA_DIR.exists():
        print(f"Noch keine CSVs unter {MAIN_DATA_DIR}.")
        return
    try:
        import pandas as pd
    except ImportError:
        pd = None

    def describe(f):
        if pd is None:
            return ""
        try:
            df = pd.read_csv(f, sep=";")
        except Exception:
            return "  [nicht lesbar]"
        meth = ", ".join(sorted({str(x) for x in df.get("METHOD", []) if str(x) != "nan"})) or "-"
        out = f"{len(df):>5} Zeilen | Methoden: {meth}"
        lms = sorted({str(x) for x in df.get("SOLVENT", []) if str(x) != "nan"})
        if lms:
            out += f" | Lösemittel: {', '.join(lms)}"
        return out

    files = sorted(MAIN_DATA_DIR.glob("*_main_*.csv"))
    if not files:
        print(f"Noch keine CSVs unter {MAIN_DATA_DIR}.")
        return

    print(f"--- Projekt-CSVs in {MAIN_DATA_DIR} ---")
    by_project = {}
    for f in files:
        for role in ROLES:
            if f.stem.endswith(f"_main_{role}"):
                by_project.setdefault(f.stem[: -len(f"_main_{role}")], []).append((role, f))
                break
    for proj in sorted(by_project):
        print(f" [{proj}]")
        for role, f in sorted(by_project[proj]):
            print(f"   {role:<7} {describe(f)}")


def write_data_config(project_path, cfg):
    """Schreibt die data.yaml zurück. Achtung: Kommentare gehen verloren."""
    import yaml
    with open(data_config_path(project_path), "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)


def data_add_source(role, raw_path, method=None, solvent=None):
    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    root = Path(project_path)

    if role not in ROLES:
        print(f"Fehler: Rolle muss eine von {', '.join(ROLES)} sein (nicht '{role}').",
              file=sys.stderr)
        sys.exit(1)

    # Pfad auflösen: relativ zum cwd angegeben, gespeichert relativ zum Projekt-Root
    abs_path = Path(raw_path).expanduser()
    if not abs_path.is_absolute():
        abs_path = (Path.cwd() / abs_path).resolve()
    if not abs_path.exists():
        print(f"Fehler: Datei existiert nicht: {abs_path}", file=sys.stderr)
        sys.exit(1)
    try:
        stored = str(abs_path.relative_to(root.resolve()))
    except ValueError:
        stored = str(abs_path)  # außerhalb des Projekts -> absolut speichern

    guess = classify_csv(abs_path)
    if guess and guess != role:
        print(f"[WARN] Header sieht eher nach Rolle '{guess}' aus als nach '{role}'.",
              file=sys.stderr)

    cfg = load_data_config(root)
    entries = normalize_source_entries(cfg["sources"].get(role))
    entries = [e for e in entries if e["path"] != stored]  # doppelte ersetzen
    entries.append({"path": stored, "method": method, "solvent": solvent})

    # Kompakt speichern: ohne Zusatzangaben als reiner String
    def dump(e):
        extra = {k: e.get(k) for k in ("method", "solvent") if e.get(k)}
        return e["path"] if not extra else dict(path=e["path"], **extra)

    cfg["sources"][role] = dump(entries[0]) if len(entries) == 1 else [dump(e) for e in entries]
    write_data_config(root, cfg)
    mstr = f" (method: {method})" if method else ""
    print(f"[OK] {role} += {stored}{mstr}   (Projekt: {project_name})")
    try:
        update_main_csvs(project_name, root, roles=[role], quiet=True)
    except SystemExit:
        pass


def data_rm_source(role, raw_path=None):
    config = load_global_config()
    _, project_path, _ = require_project(config)
    root = Path(project_path)
    cfg = load_data_config(root)
    entries = normalize_source_entries(cfg["sources"].get(role))

    if not entries:
        print(f"Fehler: Für '{role}' ist nichts eingetragen.", file=sys.stderr)
        sys.exit(1)

    if raw_path is None:
        cfg["sources"][role] = None
        write_data_config(root, cfg)
        print(f"[OK] Alle Quellen der Rolle '{role}' entfernt.")
        return

    target = str(raw_path)
    keep = [e for e in entries if e["path"] != target and Path(e["path"]).name != target]
    if len(keep) == len(entries):
        print(f"Fehler: '{target}' ist bei '{role}' nicht eingetragen.", file=sys.stderr)
        print("Eingetragen:", ", ".join(e["path"] for e in entries), file=sys.stderr)
        sys.exit(1)

    def dump(e):
        extra = {k: e.get(k) for k in ("method", "solvent") if e.get(k)}
        return e["path"] if not extra else dict(path=e["path"], **extra)

    cfg["sources"][role] = None if not keep else (dump(keep[0]) if len(keep) == 1
                                                  else [dump(e) for e in keep])
    write_data_config(root, cfg)
    print(f"[OK] '{target}' aus Rolle '{role}' entfernt.")


def classify_csv(path):
    """Rolle einer CSV am Header erkennen: solv > thermo > sp."""
    try:
        with open(path) as f:
            header = f.readline()
    except OSError:
        return None
    cols = {c.strip() for c in header.replace(",", ";").split(";")}
    if {"G_LOW", "G_AV"} & cols:
        return "boltz"
    if any(c.startswith("dG(") for c in cols) and "ENERGY" not in cols:
        return "bfreq"
    if any(c.startswith("Gsolv") for c in cols):
        return "solv"
    if "dG" in cols or "dH" in cols:
        return "thermo"
    if "ENERGY" in cols:
        return "sp"
    return None


def data_scan(write=False):
    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    root = Path(project_path)

    found = {r: [] for r in ROLES}
    for path in sorted(root.rglob("*.csv")):
        role = classify_csv(path)
        if role:
            found[role].append(str(path.relative_to(root)))

    print(f"--- Gefundene CSVs in '{project_name}' ---")
    if not any(found.values()):
        print(" (keine) — erzeuge sie zuerst mit outorca in den Rechenordnern.")
        return
    for role in ROLES:
        for p in found[role]:
            print(f" {role:<7} {p}")

    if not write:
        print("\nEinzeln übernehmen mit 'qc data add <rolle> <datei>' — "
              "oder alle auf einmal mit 'qc data scan -w'.")
        return

    import yaml
    cfg_file = data_config_path(root)
    cfg = load_data_config(root) if cfg_file.exists() else yaml.safe_load(DATA_TEMPLATE)
    for role in ROLES:
        paths = found[role]
        cfg["sources"][role] = None if not paths else (paths[0] if len(paths) == 1 else paths)
    write_data_config(root, cfg)
    print(f"\n[OK] sources in {cfg_file} aktualisiert.")


def cmd_data(rest):
    sub = rest[0] if rest else "check"
    if sub == "init":
        data_init()
        return
    if sub == "scan":
        data_scan(write=("-w" in rest or "--write" in rest))
        return
    if sub == "add":
        args, method, solvent = [], None, None
        i = 1
        while i < len(rest):
            if rest[i] in ("-f", "--method", "--functional") and i + 1 < len(rest):
                method = rest[i + 1]; i += 1
            elif rest[i] in ("-lm", "--solvent") and i + 1 < len(rest):
                solvent = rest[i + 1]; i += 1
            else:
                args.append(rest[i])
            i += 1
        if len(args) < 2:
            print(f'Verwendung: qc data add <{"|".join(ROLES)}> <datei.csv> '
                  f'[-f <funktional>] [-lm <lösemittel>]', file=sys.stderr)
            sys.exit(1)
        data_add_source(args[0], args[1], method, solvent)
        return
    if sub == "update":
        cmd_main_update(all_projects=("-a" in rest or "--all" in rest))
        return
    if sub == "main":
        cmd_main_show()
        return
    if sub == "rm":
        args = [a for a in rest[1:]]
        if not args:
            print("Verwendung: qc data rm <thermo|sp|solv> [datei.csv]", file=sys.stderr)
            sys.exit(1)
        data_rm_source(args[0], args[1] if len(args) > 1 else None)
        return
    if sub not in ("check", "list"):
        print("Verwendung: qc data init|add|rm|scan [-w]|check|update [-a]|main",
              file=sys.stderr)
        sys.exit(1)

    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    cfg = load_data_config(project_path)
    data = load_dataset(project_path, cfg)

    print(f"--- Datenlage in '{project_name}' ---")
    for role in ROLES:
        entries = normalize_source_entries(cfg["sources"].get(role))
        if role in data:
            meths = ", ".join(str(m) for m in available_methods(data[role]))
            files = ", ".join(e["path"] for e in entries)
            info = f"{len(data[role])} Zeilen, Methoden: {meths}"
            lms = available_solvents(data[role])
            if lms:
                info += f", Lösemittel: {', '.join(lms)}"
            print(f" {role:<7} {files}  ({info})")
        elif entries:
            print(f" {role:<7} {', '.join(e['path'] for e in entries)}  [NICHT GEFUNDEN]")
        else:
            print(f" {role:<7} (nicht konfiguriert)")

    # Sanity-Checks über alle Zeilen
    print("\nPrüfe Konvergenz/Charakter aller Einträge ...")
    for role in ("thermo", "sp"):
        if role in data:
            for _, row in data[role].iterrows():
                row_sanity_warnings(row, row["BASENAME"], role, cfg["checks"])

    # Duplikate (gleiche Spezies, gleiche M UND gleiche Methode)
    for role, df in data.items():
        keys = ["BASENAME", "METHOD"] + (["M"] if "M" in df.columns else [])
        dup = df[df.duplicated(subset=keys, keep=False)]
        for base in sorted(dup["BASENAME"].unique()):
            print(f"[WARN] Duplikat in '{role}': {base}", file=sys.stderr)

    # Solvatations-Abdeckung
    if "solv" in data:
        elec_role = "sp" if "sp" in data else "thermo"
        missing = set(data[elec_role]["BASENAME"]) - set(data["solv"]["BASENAME"])
        for base in sorted(missing):
            print(f"[WARN] Keine Solvatation für: {base}", file=sys.stderr)
    print("Fertig.")


# --- STATUS / EDIT / HISTORY ---

def show_status():
    config = load_global_config()
    name, path, is_local = detect_current_project(config)
    if not name:
        print("Kein Projekt erkannt und kein aktives Projekt gesetzt.")
        return
    mode = "automatisch erkannt (cwd)" if is_local else "global aktiv (Remote-Modus)"
    print(f"Projekt : {name}  [{mode}]")
    print(f"Root    : {path}")
    pcfg = load_project_config(path)
    print(f"Shortcuts: {', '.join(pcfg['commands']) or '(keine)'}")
    print(f"Bookmarks: {', '.join(pcfg['paths']) or '(keine)'}")


def edit_config():
    config = load_global_config()
    _, project_path, _ = require_project(config)
    editor = os.environ.get("EDITOR", "vi")
    subprocess.run([editor, str(project_config_path(project_path))])


def show_history(n=15):
    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    hist = Path(project_path) / HISTORY_NAME
    if not hist.exists():
        print(f"Noch keine History in Projekt '{project_name}'.")
        return
    lines = hist.read_text().splitlines()
    print(f"--- Letzte {min(n, len(lines))} Befehle in '{project_name}' ---")
    for line in lines[-n:]:
        print(" " + line)


def log_history(project_path, full_cmd):
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(Path(project_path) / HISTORY_NAME, "a") as f:
            f.write(f"{stamp}  [{os.getcwd()}]  {full_cmd}\n")
    except OSError:
        pass  # History ist nice-to-have, darf nie den Lauf verhindern


# --- SHELL-INTEGRATION ---

SHELL_FUNC = r'''
# qc Shell-Wrapper: macht 'qc go <mark>' zu einem echten cd
qc() {
    if [ "$1" = "go" ] && [ "$#" -ge 2 ]; then
        local _qc_target
        _qc_target="$(command qc go "$2")" && cd "$_qc_target"
    else
        command qc "$@"
    fi
}
'''


def print_shellrc():
    print(SHELL_FUNC.strip())


# --- AUSFÜHRUNG ---

def run_calc_command(shortcut, extra_args, print_only=False):
    config = load_global_config()
    project_name, project_path, is_local = require_project(config)

    if not is_local and not print_only:
        print(f"[Remote-Modus] Nutze Config von Projekt: {project_name}", file=sys.stderr)

    pcfg = load_project_config(project_path)
    commands = pcfg["commands"]

    if shortcut not in commands:
        print(f"Fehler: Shortcut '{shortcut}' nicht in Projekt '{project_name}' definiert.",
              file=sys.stderr)
        print("Verfügbare Shortcuts:", ", ".join(commands.keys()) or "(keine)", file=sys.stderr)
        suggest(shortcut, commands.keys())
        sys.exit(1)

    base_cmd = commands[shortcut]
    full_cmd = f"{base_cmd} {' '.join(extra_args)}".strip() if extra_args else base_cmd

    if print_only:
        # Nur den Befehl auf stdout — direkt kopierbar / pipebar
        print(full_cmd)
        return

    print(f"> {full_cmd}")
    try:
        subprocess.run(full_cmd, cwd=os.getcwd(), shell=True, check=True)
        log_history(project_path, full_cmd)
    except subprocess.CalledProcessError as e:
        print(f"Fehler bei der Ausführung: {e}", file=sys.stderr)
        sys.exit(e.returncode)


# --- PES-SCANS ---

PLOT_TEMPLATE = """#!/usr/bin/env python3
# PES-Scan - erzeugt von qc am {stamp}
{provenance}
#
# Weitere Kurven einfach als zusaetzlichen Eintrag in DATASETS einfuegen:
#     "Pd-Analogon": {{"x": [...], "y": [...]}},
# Die y-Werte werden beim Plotten auf ihr jeweiliges Minimum bezogen.
import matplotlib.pyplot as plt

XLABEL = "{xlabel}"
YLABEL = "{ylabel}"
TITLE = "{title}"

SMOOTH = True      # geschwungene Linie durch die Punkte (False = gerade Verbindungen)
SMOOTH_N = 300     # Stuetzstellen der geglaetteten Kurve
SHOW_POINTS = True # echte Rechenpunkte als Marker zeigen

DATASETS = {{
    "{label}": {{
        "x": {xs},
        "y": {ys},
    }},
}}

# --- ab hier ist nichts mehr anzupassen ---

def relative(y):
    lo = min(y)
    return [v - lo for v in y]


def pchip(x, y, n=SMOOTH_N):
    \"\"\"
    Monotone kubische Hermite-Interpolation (Fritsch-Carlson).
    Bewusst NICHT der klassische kubische Spline: der ueberschwingt zwischen
    eng liegenden Punkten und erfindet dann Minima/Maxima, die es im Scan
    nicht gibt. PCHIP bleibt zwischen den Datenpunkten.
    \"\"\"
    m = len(x)
    if m < 3:
        return list(x), list(y)
    h = [x[i + 1] - x[i] for i in range(m - 1)]
    d = [(y[i + 1] - y[i]) / h[i] for i in range(m - 1)]

    tang = [0.0] * m
    for i in range(1, m - 1):
        if d[i - 1] * d[i] <= 0:
            tang[i] = 0.0
        else:
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            tang[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    tang[0] = d[0]
    tang[-1] = d[-1]

    xs, ys = [], []
    for k in range(n):
        t = x[0] + (x[-1] - x[0]) * k / (n - 1)
        i = min(range(m - 1), key=lambda j: (t < x[j], t > x[j + 1], j))
        for j in range(m - 1):
            if x[j] <= t <= x[j + 1]:
                i = j
                break
        s = (t - x[i]) / h[i]
        h00 = 2 * s**3 - 3 * s**2 + 1
        h10 = s**3 - 2 * s**2 + s
        h01 = -2 * s**3 + 3 * s**2
        h11 = s**3 - s**2
        xs.append(t)
        ys.append(h00 * y[i] + h10 * h[i] * tang[i]
                  + h01 * y[i + 1] + h11 * h[i] * tang[i + 1])
    return xs, ys


def main():
    fig, ax = plt.subplots(figsize=(6.0, 4.2))

    for label, dset in DATASETS.items():
        pairs = sorted(zip(dset["x"], relative(dset["y"])))
        x = [p[0] for p in pairs]
        y = [p[1] for p in pairs]

        if SMOOTH:
            sx, sy = pchip(x, y)
            line, = ax.plot(sx, sy, linewidth=1.8, label=label)
            if SHOW_POINTS:
                ax.plot(x, y, linestyle="none", marker="o", markersize=4,
                        color=line.get_color())
        else:
            ax.plot(x, y, marker="o", markersize=4, linewidth=1.6, label=label)

        imax = max(range(len(y)), key=lambda i: y[i])
        ax.annotate("%.1f" % y[imax], (x[imax], y[imax]),
                    textcoords="offset points", xytext=(0, 8),
                    ha="center", fontsize=9)

    ax.set_xlabel(XLABEL)
    ax.set_ylabel(YLABEL)
    if TITLE:
        ax.set_title(TITLE)
    ax.axhline(0.0, color="0.7", linewidth=0.8, zorder=0)
    if len(DATASETS) > 1:
        ax.legend(frameon=False)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig("{stem}.png", dpi=300)
    plt.show()


if __name__ == "__main__":
    main()
"""


def scan_members(data, prefix, cfg):
    """
    Alle Spezies eines Scans finden: Basenames, die mit <prefix>. beginnen und
    danach nur noch eine Zahl tragen (z.B. nio_br_nio_p1_1.0, ....003, ....180).
    Rückgabe: [(punktnummer, basename), ...] numerisch sortiert.
    """
    elec_role = "sp" if "sp" in data else "thermo"
    bases = set()
    for role in data:
        bases.update(data[role]["BASENAME"].tolist())

    out = []
    for b in bases:
        if not b.startswith(prefix + "."):
            continue
        tail = b[len(prefix) + 1:]
        if tail.isdigit():
            out.append((int(tail), b))
    out.sort(key=lambda t: t[0])
    return out, elec_role


def cmd_scan(rest):
    if not rest:
        print('Verwendung: qc scan <prefix> <B i j | A i j k | D i j k l> [flags]\n'
              '            [--plot <datei.py>] [--csv <datei.csv>] [--label <text>]',
              file=sys.stderr)
        sys.exit(1)

    # eigene Optionen herausziehen, Rest geht an parse_data_flags
    plot_path = csv_path = label = title = None
    keep, i = [], 0
    while i < len(rest):
        a = rest[i]
        if a == "--plot" and i + 1 < len(rest):
            plot_path = rest[i + 1]; i += 1
        elif a == "--csv" and i + 1 < len(rest):
            csv_path = rest[i + 1]; i += 1
        elif a == "--label" and i + 1 < len(rest):
            label = rest[i + 1]; i += 1
        elif a == "--title" and i + 1 < len(rest):
            title = rest[i + 1]; i += 1
        else:
            keep.append(a)
        i += 1

    pos, opts = parse_data_flags(keep)
    if len(pos) < 2:
        print("Fehler: Prefix und Koordinate angeben, z.B. qc scan mycomplex B 1 2",
              file=sys.stderr)
        sys.exit(1)
    prefix, coord_tokens = pos[0], pos[1:]
    try:
        kind, idx = parse_coord_spec(coord_tokens)
    except ValueError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        sys.exit(1)

    config = load_global_config()
    project_name, project_path, _ = require_project(config)
    cfg = load_data_config(project_path)
    data = load_dataset(project_path, cfg)
    quantity = opts["q"]
    unit = check_unit(opts["unit"] or "kcal/mol")
    methods, solvent = resolve_context(data, cfg, opts, strict=True)

    members, _ = scan_members(data, prefix, cfg)
    if not members:
        print(f"Fehler: Keine Scanpunkte mit Prefix '{prefix}.' gefunden.", file=sys.stderr)
        cands = sorted({b.rsplit(".", 1)[0] for role in data
                        for b in data[role]["BASENAME"] if "." in b})
        if cands:
            print("Mögliche Prefixe:", ", ".join(cands[:10]), file=sys.stderr)
        sys.exit(1)

    rows, missing_geom, failed = [], [], []
    for point, base in members:
        try:
            val, bd = species_value(data, cfg, base, quantity, sc=opts["sc"],
                                    use_solv=opts["solv"], methods=methods, solvent=solvent,
                                    use_boltz=opts["boltz"], use_bfreq=opts["bfreq"])
        except (LookupError, ValueError) as e:
            failed.append((base, str(e).splitlines()[0]))
            continue
        geo = find_geometry(project_path, base, cfg)
        if geo is None:
            missing_geom.append(base)
            continue
        try:
            x = internal_coordinate(read_xyz(geo), kind, idx)
        except (ValueError, OSError) as e:
            failed.append((base, str(e)))
            continue
        rows.append({"point": point, "name": base, "x": x, "E": val})

    for base, why in failed:
        print(f"[WARN] {base} übersprungen: {why}", file=sys.stderr)
    for base in missing_geom:
        print(f"[WARN] {base}: keine Geometrie gefunden (siehe 'geometries' in data.yaml).",
              file=sys.stderr)
    if not rows:
        print("Fehler: Kein einziger Punkt auswertbar.", file=sys.stderr)
        sys.exit(1)
    if len(rows) < len(members):
        print(f"[WARN] {len(members) - len(rows)} von {len(members)} Punkten fehlen — "
              f"die Kurve hat Lücken.", file=sys.stderr)

    rows.sort(key=lambda r: r["x"])
    lo = min(r["E"] for r in rows)
    conv = UNIT_CONVERSION[unit]
    for r in rows:
        r["rel"] = (r["E"] - lo) * conv

    cname, cunit = COORD_LABEL[kind.upper()]
    xlabel = f"{cname} {kind.upper()}({', '.join(str(i) for i in idx)}) / {cunit}"
    ylabel = f"rel. {quantity} / {unit}"

    # Konsolenausgabe
    print(f"--- Scan '{prefix}' ({len(rows)} Punkte, {quantity}) ---")
    print(f" {'PUNKT':>6} {'x':>10} {'rel.':>12}   NAME")
    for r in rows:
        print(f" {r['point']:>6} {r['x']:>10.4f} {r['rel']:>12.3f}   {r['name']}")
    imax = max(range(len(rows)), key=lambda i: rows[i]["rel"])
    print(f"\n Barriere (Maximum): {rows[imax]['rel']:.2f} {unit} "
          f"bei {rows[imax]['x']:.4f} {cunit}  [{rows[imax]['name']}]")

    if csv_path:
        with open(csv_path, "w") as f:
            f.write(f"POINT;NAME;X;{quantity}_REL_{unit}\n")
            for r in rows:
                f.write(f"{r['point']};{r['name']};{r['x']:.6f};{r['rel']:.6f}\n")
        print(f"[OK] {csv_path}")

    if plot_path:
        parts = [f"Projekt: {project_name}", f"Prefix: {prefix}", f"Größe: {quantity}"]
        if methods.get("sp") or methods.get("thermo"):
            parts.append("Funktional: " + str(methods.get("sp") or methods.get("thermo")))
        if opts["solv"]:
            parts.append(f"solvatisiert{f' ({solvent})' if solvent else ''}")
        if opts["boltz"]:
            parts.append("Boltzmann-korrigiert")
        if opts["bfreq"]:
            parts.append("SPH-Thermokorrektur")
        if opts["sc"]:
            parts.append("Standard-State-Correction")
        stem = Path(plot_path).stem
        code = PLOT_TEMPLATE.format(
            stamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
            provenance="\n".join(f"# {p}" for p in parts),
            xlabel=xlabel, ylabel=ylabel, unit=unit,
            title=title or "", stem=stem,
            label=label or prefix,
            xs="[" + ", ".join(f"{r['x']:.6f}" for r in rows) + "]",
            ys="[" + ", ".join(f"{r['rel']:.6f}" for r in rows) + "]",
        )
        Path(plot_path).write_text(code)
        print(f"[OK] {plot_path}  (in VS Code öffnen und ausführen)")


# --- HILFE ---

HELP = """qc — Projektmanager & Auswertung für QC-Rechnungen

PROJEKTE
  qc init <name>              Projektordner + Struktur anlegen und registrieren
  qc add [<name>]             aktuellen Ordner als Projekt registrieren
  qc rm <name>                Projekt aus der Registry entfernen (löscht nichts)
  qc switch <name>            global aktives Projekt setzen
  qc list                     alle Projekte anzeigen
  qc status                   erkanntes Projekt, Shortcuts & Bookmarks

SHORTCUTS (commands.json)
  qc <shortcut> [args...]     Befehl ausführen, args werden angehängt
  qc -p <shortcut> [args]     Befehl nur ausgeben (kopieren & anpassen)
  qc add name="befehl"        Shortcut hinzufügen/überschreiben
  qc rmcmd <name>             Shortcut entfernen
  qc cmds                     Shortcuts auflisten
  qc edit                     commands.json im $EDITOR öffnen
  qc history [n]              zuletzt ausgeführte Befehle

BOOKMARKS (Rechenordner)
  qc mark <name> [pfad]       Ordner (Default: cwd) merken
  qc marks                    Bookmarks auflisten
  qc unmark <name>            Bookmark entfernen
  qc go <name> | root         hinspringen (braucht den Shell-Wrapper)

DATENQUELLEN (data.yaml)
  qc data init                data.yaml-Vorlage anlegen
  qc data add <rolle> <csv> [-f <funktional>] [-lm <lösemittel>]
                              Rollen: thermo | sp | solv | boltz | bfreq
  qc data rm <rolle> [csv]    Quelle (oder alle der Rolle) entfernen
  qc data scan [-w]           CSVs im Projekt finden (-w schreibt sie ein)
  qc data check               Quellen prüfen: Konvergenz, Duplikate, Abdeckung
  qc data update [-a]         Sammel-CSVs ~/.qc_data/<projekt>_main_<rolle>.csv
                              aktualisieren (-a: alle Projekte)
  qc data main                Inhalt der Sammel-CSVs zusammenfassen

THERMOCHEMIE
  qc species                  Spezies + Datenabdeckung + Funktionale
  qc e|u|h|g <name...>        E/U/H/G einer Spezies (Default: Eh, Gasphase)
  qc rxn "a + 2 b -> c"       Reaktionsenergie ad-hoc (Default: kcal/mol)
  qc rxn <name>               gespeicherte Reaktion rechnen
  qc rxn add <name> "a -> b"  Reaktion speichern
  qc rxn rm <name> | list     Reaktion löschen / alle anzeigen

PES-SCANS
  qc scan <prefix> B i j            Scan über Bindungslänge (Å)
  qc scan <prefix> A i j k          über Winkel (°, j = Scheitel)
  qc scan <prefix> D i j k l        über Diederwinkel (°)
                              Punkte = <prefix>.<nummer>, Koordinate wird aus
                              den Geometrien berechnet ('geometries' in data.yaml)
    --plot <datei.py>         matplotlib-Skript schreiben (Daten eingebettet)
    --csv <datei.csv>         Rohwerte exportieren
    --label / --title <text>  Kurvenname / Titel im Plot

  Flags:
    -q E|U|H|G                Größe (nur bei rxn; Default G)
    -m <M>  oder  name@M      Multiplizität wählen
    -solv                     mit Gsolv rechnen (Default: Gasphase)
    -lm <lösemittel>          Lösemittel wählen, z.B. -lm 1,2-dichloroethane
    -boltz                    Ensemble-/Boltzmann-Korrektur addieren (G_AV - G_LOW)
    -bfreq                    Thermokorrektur aus der SPH-Quelle statt aus thermo
    -sc                       Standard-State-Correction (1 atm -> 1 M, nur G)
    -f <funktional>           Funktional der Hauptenergie
    -ft <funktional>          Funktional der Thermokorrektur (darf abweichen)
    -fsolv / -fboltz <f>      Funktional der Solvatations-/Ensemble-Daten
    -u <einheit>              hartree | kcal/mol | kj/mol | ev
    -v                        Aufschlüsselung aller Beiträge
    -gas                      erzwingt Gasphase (falls solvation: true gesetzt)

SHELL-INTEGRATION (einmalig in ~/.bashrc)
  eval "$(qc shellrc)"
"""


# --- MAIN ---

def main():
    args = sys.argv[1:]

    # -p / --print überall erlauben und herausfiltern
    print_only = False
    for flag in ("-p", "--print"):
        while flag in args:
            args.remove(flag)
            print_only = True

    if not args:
        print(HELP)
        sys.exit(0)

    command, rest = args[0], args[1:]

    if command in ("help", "-h", "--help"):
        print(HELP)

    elif command == "init":
        if not rest:
            print("Name fehlt: qc init <name>", file=sys.stderr); sys.exit(1)
        init_project(rest[0])

    elif command == "add":
        joined = " ".join(rest)
        if "=" in joined:
            add_command(joined)          # qc add name="befehl"
        elif rest:
            register_project(rest[0])    # qc add <projektname>  (cwd)
        else:
            register_project(Path.cwd().name)  # qc add  -> cwd unter Ordnernamen

    elif command == "rm":
        if not rest:
            print("Name fehlt: qc rm <projekt>", file=sys.stderr); sys.exit(1)
        remove_project(rest[0])

    elif command == "switch":
        if not rest:
            print("Name fehlt: qc switch <projekt>", file=sys.stderr); sys.exit(1)
        switch_project(rest[0])

    elif command == "list":
        list_projects()

    elif command == "cmds":
        list_commands()

    elif command == "rmcmd":
        if not rest:
            print("Name fehlt: qc rmcmd <shortcut>", file=sys.stderr); sys.exit(1)
        remove_command(rest[0])

    elif command == "mark":
        if not rest:
            print("Name fehlt: qc mark <name> [pfad]", file=sys.stderr); sys.exit(1)
        mark_path(rest[0], rest[1] if len(rest) > 1 else None)

    elif command == "marks":
        list_marks()

    elif command == "unmark":
        if not rest:
            print("Name fehlt: qc unmark <name>", file=sys.stderr); sys.exit(1)
        unmark_path(rest[0])

    elif command == "go":
        if not rest:
            list_marks()
        else:
            go_mark(rest[0])

    elif command == "status":
        show_status()

    elif command == "edit":
        edit_config()

    elif command == "history":
        n = int(rest[0]) if rest and rest[0].isdigit() else 15
        show_history(n)

    elif command == "shellrc":
        print_shellrc()

    elif command == "data":
        cmd_data(rest)

    elif command == "species":
        cmd_species()

    elif command in ("e", "u", "h", "g"):
        cmd_energy(command.upper(), rest)

    elif command == "rxn":
        cmd_reaction(rest)

    elif command == "scan":
        cmd_scan(rest)

    else:
        # Shortcut-Ausführung: alles nach dem Shortcut wird angehängt.
        # Altes '-e "..."' funktioniert weiterhin:
        if "-e" in rest:
            idx = rest.index("-e")
            extra = rest[:idx] + rest[idx + 1:]
        else:
            extra = rest
        run_calc_command(command, extra, print_only=print_only)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # z.B. 'qc help | head' — sauber beenden statt Traceback
        try:
            sys.stdout.close()
        finally:
            os._exit(0)
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        sys.exit(130)
