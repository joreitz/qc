#!/usr/bin/env python3
"""
qc - project manager and analysis tool for quantum-chemical calculations.

  * projects, command shortcuts (commands.json) and directory bookmarks
  * thermochemistry (E/U/H/G, reaction energies) from the CSV summaries of the
    calculations, configured per project in data.yaml
  * relaxed PES scans: table, CSV export and publication-ready plots

Run `qc` for an overview and `qc <command> -h` for details.

Shell integration (cd for `qc go`, tab completion) - once in ~/.bashrc or ~/.zshrc:
    eval "$(qc shellrc)"

Requirements: Python >= 3.8; pandas + PyYAML for everything that reads data;
matplotlib for plots; ruamel.yaml (optional) keeps comments in data.yaml.
"""

import argparse
import ast
import csv
import difflib
import fnmatch
import json
import math
import os
import re
import shlex
import subprocess
import sys
import textwrap
from collections import Counter
from datetime import datetime
from pathlib import Path

__version__ = "2.0"


# ════════════════════════════════════════════════════════════════════════════
#  Constants
# ════════════════════════════════════════════════════════════════════════════

REGISTRY_FILE = Path.home() / ".qc_manager_config.json"
ARCHIVE_DIR = Path.home() / ".qc_data"
COMMANDS_FILE = "commands.json"
DATA_FILE = "data.yaml"
HISTORY_FILE = ".qc_history"

PROJECT_FOLDERS = ["scratch", "0_confsampling", "1_optfreq", "2_sp", "3_solv", "xyz"]

DEFAULT_SHORTCUTS = {
    "geomopt": "inporca -j optfreq -f wb97x-3c -dp no -fn -optt tight",
    "sp": "inporca -j sp -f r2scan0 -bs def2-QZVPPD -dp d4 -fn",
    "freq": "inporca -j freq -f wb97x-3c -dp no -fn",
}

# Data roles: which kind of CSV a source is
ROLES = ("thermo", "sp", "solv", "boltz", "bfreq")
ROLE_INFO = {
    "thermo": "ENERGY + thermal corrections dU/dH/dG (e.g. optfreq jobs)",
    "sp": "ENERGY of single points (several functionals allowed)",
    "solv": "solvation free energies Gsolv(Eh) (several solvents allowed)",
    "boltz": "conformer ensembles (CENSO): G_LOW, G_AV",
    "bfreq": "single-point (biased) Hessian thermal correction dG(Eh)",
}

# Physical constants (CODATA 2018)
HARTREE_TO_KCAL = 627.5094740631
HARTREE_TO_KJ = 2625.4996394799
HARTREE_TO_EV = 27.211386245988
R_KCAL = 1.98720425864083e-3   # kcal/(mol K)
R_LATM = 0.0820573660809596    # L atm/(mol K)

# key: (factor from Eh, display label, decimals)
UNITS = {
    "eh": (1.0, "Eh", 8),
    "hartree": (1.0, "Eh", 8),
    "kcal/mol": (HARTREE_TO_KCAL, "kcal/mol", 3),
    "kj/mol": (HARTREE_TO_KJ, "kJ/mol", 3),
    "ev": (HARTREE_TO_EV, "eV", 3),
}

QUANTITY_COLUMN = {"U": "dU", "H": "dH", "G": "dG"}
COEFF_RE = re.compile(r"^(\d+(?:\.\d+)?)\s+(.+)$")

DEFAULT_STRIP_SUFFIXES = ["optfreq", "opt", "freq", "sp", "solv", "dock",
                          "censo", "crest", "conf", "boltz", "bfreq", "sph"]
DEFAULT_SETTINGS = {
    "unit": "kcal/mol",
    "temperature": 298.15,
    "solvation": False,
    "solvent": None,
    "boltzmann": False,
    "bfreq": False,          # bfreq instead of thermo for G (e/u/h/g, rxn)
    "scan_bfreq": "auto",    # auto: bfreq if it covers the scan, else thermo
    "state_correction": False,
    "method_sp": None,
    "method_thermo": None,
    "method_solv": None,
    "method_boltz": None,
    "method_bfreq": None,
}
DEFAULT_CHECKS = {"scf": True, "geoconv": False, "character": True}
DEFAULT_GEOMETRIES = ["xyz/{name}.xyz", "1_optfreq/{name}.xyz", "{name}.xyz"]


# ════════════════════════════════════════════════════════════════════════════
#  Console output
# ════════════════════════════════════════════════════════════════════════════

class QCError(Exception):
    """A user-facing error: printed as 'error: ...', exit status 1."""


_WARNINGS_SEEN = set()


def _color(stream):
    return (hasattr(stream, "isatty") and stream.isatty()
            and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")


def style(text, code, stream=None):
    return f"\033[{code}m{text}\033[0m" if _color(stream or sys.stdout) else text


def bold(text):
    return style(text, "1")


def dim(text, stream=None):
    return style(text, "2", stream)


def warn(msg):
    """Print a warning to stderr (each distinct message only once per run)."""
    if msg in _WARNINGS_SEEN:
        return
    _WARNINGS_SEEN.add(msg)
    print(f"{style('warning:', '33;1', sys.stderr)} {msg}", file=sys.stderr)


def note(msg):
    print(f"{style('note:', '36;1', sys.stderr)} {msg}", file=sys.stderr)


def did_you_mean(word, candidates):
    hits = difflib.get_close_matches(word, list(candidates), n=3, cutoff=0.5)
    return f"\nDid you mean: {', '.join(hits)}?" if hits else ""


def print_table(rows, header=None, align=None, indent="  "):
    """Print rows as aligned columns; align is a string of 'l'/'r' per column."""
    rows = [[str(c) for c in r] for r in rows]
    every = ([list(header)] if header else []) + rows
    if not every:
        return
    ncol = max(len(r) for r in every)
    widths = [max((len(r[i]) for r in every if i < len(r)), default=0) for i in range(ncol)]
    align = (align or "").ljust(ncol, "l")

    def line(r):
        cells = [c.rjust(widths[i]) if align[i] == "r" else c.ljust(widths[i])
                 for i, c in enumerate(r)]
        return (indent + "  ".join(cells)).rstrip()

    if header:
        print(bold(line(header)))
    for r in rows:
        print(line(r))


def heading(text):
    print(bold(text))


# ════════════════════════════════════════════════════════════════════════════
#  Files
# ════════════════════════════════════════════════════════════════════════════

def write_text_atomic(path, text):
    """Write via a temporary file so that an interrupted write never truncates."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path, data):
    write_text_atomic(path, json.dumps(data, indent=4, ensure_ascii=False) + "\n")


def open_in_editor(path):
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    return subprocess.run(shlex.split(editor) + [str(path)]).returncode


# ════════════════════════════════════════════════════════════════════════════
#  Projects
# ════════════════════════════════════════════════════════════════════════════

def load_registry():
    if not REGISTRY_FILE.exists():
        return {"projects": {}, "active_project": None}
    try:
        reg = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise QCError(f"cannot read the project registry {REGISTRY_FILE}: {e}")
    reg.setdefault("projects", {})
    reg.setdefault("active_project", None)
    return reg


def save_registry(reg):
    write_json(REGISTRY_FILE, reg)


class Project:
    """A registered project. `here` is True if the cwd lies inside it."""

    def __init__(self, name, root, here):
        self.name = name
        self.root = Path(root)
        self.here = here

    @property
    def commands_file(self):
        return self.root / COMMANDS_FILE

    @property
    def data_file(self):
        return self.root / DATA_FILE

    def load_commands(self):
        """commands.json: {"commands": {...}, "paths": {...}} (paths = bookmarks)."""
        f = self.commands_file
        if not f.exists():
            return {"commands": {}, "paths": {}}
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise QCError(f"{f} is not valid JSON: {e}")
        if "commands" not in data and "paths" not in data:   # very old flat format
            data = {"commands": data, "paths": {}}
        data.setdefault("commands", {})
        data.setdefault("paths", {})
        return data

    def save_commands(self, data):
        write_json(self.commands_file, data)


def find_project(reg=None):
    """The project containing the cwd, else the active project, else None."""
    reg = reg or load_registry()
    cwd = Path.cwd().resolve()
    # longest path first, so that nested projects win
    for name, path in sorted(reg["projects"].items(), key=lambda kv: len(kv[1]), reverse=True):
        root = Path(path).resolve()
        if cwd == root or root in cwd.parents:
            return Project(name, root, here=True)
    active = reg.get("active_project")
    if active in reg["projects"]:
        return Project(active, reg["projects"][active], here=False)
    return None


def current_project():
    project = find_project()
    if project is None:
        raise QCError("no project here and no active project.\n"
                      "Create one with 'qc init <name>' or register this folder with "
                      "'qc project add'.")
    return project


def require_registered(reg, name):
    if name not in reg["projects"]:
        raise QCError(f"unknown project '{name}'." + did_you_mean(name, reg["projects"]))


def cmd_init(args):
    reg = load_registry()
    root = (Path.cwd() / args.name).resolve()
    known = reg["projects"].get(args.name)
    if known and Path(known).resolve() != root:
        raise QCError(f"a project '{args.name}' is already registered at {known}.\n"
                      f"Pick another name or remove it first with 'qc project rm {args.name}'.")
    try:
        root.mkdir(parents=True, exist_ok=True)
        for folder in PROJECT_FOLDERS:
            (root / folder).mkdir(exist_ok=True)
    except OSError as e:
        raise QCError(f"cannot create {root}: {e}")
    project = Project(args.name, root, here=False)
    if not project.commands_file.exists():
        project.save_commands({"commands": dict(DEFAULT_SHORTCUTS), "paths": {}})
    if not project.data_file.exists():
        project.data_file.write_text(DATA_TEMPLATE, encoding="utf-8")
    reg["projects"][args.name] = str(root)
    reg["active_project"] = args.name
    save_registry(reg)
    print(f"Created project '{args.name}' in {root}")
    print(f"  folders: {', '.join(PROJECT_FOLDERS)}")
    print(f"  files:   {COMMANDS_FILE}, {DATA_FILE}")
    print("It is now the active project.")


def cmd_project_ls(args):
    reg = load_registry()
    if not reg["projects"]:
        print("No projects registered. Create one with 'qc init <name>'.")
        return
    here = find_project(reg)
    rows = []
    for name, path in sorted(reg["projects"].items()):
        tags = []
        if name == reg.get("active_project"):
            tags.append("active")
        if here and here.here and name == here.name:
            tags.append("current")
        if not Path(path).is_dir():
            tags.append("missing!")
        rows.append(["*" if "current" in tags else " ", name, path, ", ".join(tags)])
    print_table(rows, header=["", "project", "path", ""], indent=" ")


def cmd_project_add(args):
    reg = load_registry()
    root = Path(args.path).expanduser().resolve() if args.path else Path.cwd().resolve()
    if not root.is_dir():
        raise QCError(f"not a directory: {root}")
    name = args.name or root.name
    known = reg["projects"].get(name)
    if known and Path(known).resolve() != root:
        raise QCError(f"a project '{name}' is already registered at {known}.\n"
                      f"Pick another name or remove it first with 'qc project rm {name}'.")
    for other, path in reg["projects"].items():
        if other != name and Path(path).resolve() == root:
            warn(f"this folder is also registered as project '{other}'.")
    reg["projects"][name] = str(root)
    reg["active_project"] = name
    save_registry(reg)
    print(f"Registered project '{name}' at {root} (now active).")


def cmd_project_rm(args):
    reg = load_registry()
    require_registered(reg, args.name)
    del reg["projects"][args.name]
    if reg.get("active_project") == args.name:
        reg["active_project"] = None
    save_registry(reg)
    print(f"Unregistered project '{args.name}' (no files were deleted).")


def cmd_project_switch(args):
    reg = load_registry()
    require_registered(reg, args.name)
    reg["active_project"] = args.name
    save_registry(reg)
    print(f"Active project: {args.name}")


def cmd_status(args):
    project = find_project()
    if project is None:
        print("No project here and no active project.")
        return
    mode = "current directory" if project.here else "active project, you are outside of it"
    cmds = project.load_commands()
    rows = [["project", f"{project.name}  ({mode})"],
            ["root", str(project.root)],
            ["shortcuts", ", ".join(cmds["commands"]) or "-"],
            ["bookmarks", ", ".join(cmds["paths"]) or "-"]]
    if project.data_file.exists():
        try:
            cfg = load_data_config(project)
            roles = [r for r in ROLES if source_entries(cfg["sources"].get(r))]
            rows.append(["data", f"{DATA_FILE}: {', '.join(roles) or 'no sources yet'}"])
            rows.append(["reactions", ", ".join(cfg["reactions"]) or "-"])
        except QCError as e:
            rows.append(["data", f"{DATA_FILE}: {e}"])
    else:
        rows.append(["data", f"no {DATA_FILE} (create one with 'qc data init')"])
    print_table(rows, indent="")


# ════════════════════════════════════════════════════════════════════════════
#  Shortcuts
# ════════════════════════════════════════════════════════════════════════════

def cmd_cmd_ls(args):
    project = current_project()
    cmds = project.load_commands()["commands"]
    heading(f"Shortcuts of '{project.name}'")
    if not cmds:
        print("  (none) - add one with: qc cmd add <name> \"<command>\"")
        return
    print_table(sorted(cmds.items()))


def cmd_cmd_add(args):
    project = current_project()
    if args.name in RESERVED:
        raise QCError(f"'{args.name}' is a qc command and cannot be a shortcut name.")
    if args.name.startswith("-"):
        raise QCError("shortcut names must not start with '-'.")
    if not args.command:
        raise QCError(f"no command given: qc cmd add {args.name} \"<command>\"")
    # one quoted word is taken verbatim; several words are re-quoted for the shell
    command = args.command[0] if len(args.command) == 1 else shlex.join(args.command)
    data = project.load_commands()
    action = "Updated" if args.name in data["commands"] else "Added"
    data["commands"][args.name] = command
    project.save_commands(data)
    print(f"{action} shortcut '{args.name}' in '{project.name}':\n  {command}")


def cmd_cmd_rm(args):
    project = current_project()
    data = project.load_commands()
    if args.name not in data["commands"]:
        raise QCError(f"no shortcut '{args.name}' in project '{project.name}'."
                      + did_you_mean(args.name, data["commands"]))
    del data["commands"][args.name]
    project.save_commands(data)
    print(f"Removed shortcut '{args.name}'.")


def cmd_cmd_edit(args):
    project = current_project()
    if not project.commands_file.exists():
        project.save_commands({"commands": {}, "paths": {}})
    return open_in_editor(project.commands_file)


def run_shortcut(name, extra, print_only=False):
    project = find_project()
    shortcuts = project.load_commands()["commands"] if project else {}
    if name not in shortcuts:
        msg = f"unknown command '{name}'"
        if project:
            msg += f" (and no shortcut of that name in project '{project.name}')"
        msg += "." + did_you_mean(name, list(COMMAND_NAMES) + list(shortcuts))
        msg += "\nRun 'qc' for the list of commands" + (", 'qc cmd' for the shortcuts." if project
                                                       else ".")
        raise QCError(msg)
    command = shortcuts[name] + (" " + shlex.join(extra) if extra else "")
    if print_only:
        print(command)
        return 0
    if not project.here:
        note(f"outside of any project - using the shortcuts of the active project "
             f"'{project.name}'.")
    print(dim(f"$ {command}", sys.stderr), file=sys.stderr)
    rc = subprocess.run(command, shell=True).returncode
    log_history(project, command, rc)
    return rc


def log_history(project, command, rc):
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        status = f"  [exit {rc}]" if rc else ""
        with open(project.root / HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"{stamp}  {os.getcwd()}  {command}{status}\n")
    except OSError:
        pass   # the history is a convenience and must never break a run


def cmd_history(args):
    project = current_project()
    hist = project.root / HISTORY_FILE
    lines = hist.read_text(encoding="utf-8").splitlines() if hist.exists() else []
    if not lines:
        print(f"No shortcuts run yet in '{project.name}'.")
        return
    lines = lines[-args.n:]
    heading(f"Last {len(lines)} shortcut runs in '{project.name}'")
    for line in lines:
        print("  " + line)


# ════════════════════════════════════════════════════════════════════════════
#  Bookmarks
# ════════════════════════════════════════════════════════════════════════════

def cmd_mark_ls(args):
    project = current_project()
    marks = project.load_commands()["paths"]
    heading(f"Bookmarks of '{project.name}'")
    if not marks:
        print("  (none) - bookmark a directory with: qc mark add <name> [dir]")
        return
    rows = [[k, v + ("" if Path(v).is_dir() else "   [missing]")] for k, v in sorted(marks.items())]
    print_table(rows)


def cmd_mark_add(args):
    project = current_project()
    if args.name in ("root",):
        raise QCError("'root' is reserved (qc go root jumps to the project root).")
    target = Path(args.dir).expanduser().resolve() if args.dir else Path.cwd().resolve()
    if not target.is_dir():
        raise QCError(f"not a directory: {target}")
    data = project.load_commands()
    action = "Updated" if args.name in data["paths"] else "Added"
    data["paths"][args.name] = str(target)
    project.save_commands(data)
    print(f"{action} bookmark '{args.name}' -> {target}")


def cmd_mark_rm(args):
    project = current_project()
    data = project.load_commands()
    if args.name not in data["paths"]:
        raise QCError(f"no bookmark '{args.name}' in project '{project.name}'."
                      + did_you_mean(args.name, data["paths"]))
    del data["paths"][args.name]
    project.save_commands(data)
    print(f"Removed bookmark '{args.name}'.")


def cmd_go(args):
    """Print the target directory on stdout (the shell function does the cd)."""
    if not args.name:
        return cmd_mark_ls(args)
    project = current_project()
    if args.name == "root":
        target = project.root
    else:
        marks = project.load_commands()["paths"]
        if args.name not in marks:
            raise QCError(f"no bookmark '{args.name}' in project '{project.name}'."
                          + did_you_mean(args.name, list(marks) + ["root"]))
        target = Path(marks[args.name])
        if not target.is_dir():
            raise QCError(f"bookmarked directory no longer exists: {target}")
    print(target)
    if not os.environ.get("QC_SHELL") and sys.stdout.isatty():
        note('to cd there directly, add  eval "$(qc shellrc)"  to your ~/.bashrc')


# ════════════════════════════════════════════════════════════════════════════
#  data.yaml
# ════════════════════════════════════════════════════════════════════════════

DATA_TEMPLATE = """\
# qc data.yaml - data sources and reactions for thermochemistry
#
# Paths are relative to the project root (or absolute). Each role takes one
# file or a list of files; 'method:' / 'solvent:' override the METHOD / SOLVENT
# column of that file (useful when it says None).
# Add sources with:  qc data add <role> <file.csv> [-f <functional>] [-s <solvent>]
#
#   thermo  ENERGY + thermal corrections dU, dH, dG (Eh), e.g. optfreq jobs
#   sp      ENERGY (Eh) of single points; several functionals allowed
#   solv    Gsolv(Eh); several solvents allowed
#   boltz   conformer ensembles (CENSO): G_LOW, G_AV
#   bfreq   single-point (biased) Hessian thermal correction dG(Eh)
sources:
  thermo: null
  sp: null
  solv: null
  boltz: null
  bfreq: null
# Example after two 'qc data add sp ...':
#  sp:
#    - {path: 2_sp/r2scan0/outorca.csv, method: r2scan0}
#    - {path: 2_sp/pwpb95/outorca.csv, method: pwpb95}

# NAME parts that are dropped to get the species name: '.'-separated parts
# anywhere, and trailing '_<part>'. 'a.oet2.optfreq' and 'a.oet2_censo' -> 'a.oet2'
strip_suffixes: [optfreq, opt, freq, sp, solv, dock, censo, crest, conf, boltz, bfreq, sph]

defaults:
  unit: kcal/mol          # unit of reaction energies and scans
  temperature: 298.15     # K, only used for the standard-state correction. Must match
                          # the temperature of the frequency jobs: qc does not
                          # rescale dU/dH/dG.
  solvation: false        # true: always add Gsolv (otherwise only with --solv)
  solvent: null           # default solvent, e.g. 1,2-dichloroethane
  boltzmann: false        # true: always add the ensemble correction (otherwise --boltz)
  bfreq: false            # true: G from the bfreq correction (otherwise --bfreq)
  scan_bfreq: auto        # scans: auto = bfreq if it covers all points, else thermo
  state_correction: false # true: always add 1 atm -> 1 M to G (otherwise --sc)
  method_sp: null         # functional of the electronic energy (otherwise -f)
  method_thermo: null     # functional of the thermal correction (otherwise --ft)
  method_solv: null       # (otherwise --fsolv)
  method_boltz: null      # (otherwise --fboltz)
  method_bfreq: null      # (otherwise --fbfreq)

# Sanity checks when reading the data
checks:
  scf: true         # SCFCONV must be True
  geoconv: false    # CPSCF/GEOCONV must be True (often False for OptFreq jobs
                    # with a converged geometry - CHAR/IMAGS is more reliable)
  character: true   # CHAR must match IMAGS (MIN: 0, TS: 1 imaginary mode)

# Where the geometries are ({name} = species name). Tried in this order;
# afterwards qc searches the whole project for <name>.xyz.
geometries:
  - xyz/{name}.xyz
  - 1_optfreq/{name}.xyz
  - "{name}.xyz"

# Column of the bfreq source (thermal correction to G, Eh)
bfreq_column: dG(Eh)

# Columns of the boltz source
boltzmann_columns:
  low: G_LOW            # G of the lowest conformer (Eh)
  av: G_AV              # Boltzmann-averaged G (Eh); correction = av - low
  check: dG_AV          # cross-check in kcal/mol (warning on mismatch)
#  value: dG_boltz      # alternatively: the finished correction in Eh (low/av ignored)

# Unify solvent names (CENSO and COSMOtherm often spell them differently)
solvent_aliases: {}
#  dichloroethane: 1,2-dichloroethane

# Named reactions - manage with 'qc rxn add|rm|ls'
reactions: {}
#  deprot: "ni_h -> ni + h"
#  dimer: "2 mono -> dimer"
"""


def _roundtrip_yaml():
    """ruamel.yaml keeps comments when data.yaml is rewritten; optional."""
    try:
        from ruamel.yaml import YAML
    except ImportError:
        return None
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 100
    y.representer.add_representer(
        type(None), lambda r, _: r.represent_scalar("tag:yaml.org,2002:null", "null"))
    return y


def _pyyaml():
    try:
        import yaml
    except ImportError:
        raise QCError("PyYAML is required to read data.yaml (pip install pyyaml).")
    return yaml


def read_data_raw(project):
    """data.yaml exactly as on disk (no defaults filled in)."""
    f = project.data_file
    if not f.exists():
        raise QCError(f"no {DATA_FILE} in {project.root}.\n"
                      f"Create a template with 'qc data init'.")
    text = f.read_text(encoding="utf-8")
    try:
        rt = _roundtrip_yaml()
        raw = rt.load(text) if rt else _pyyaml().safe_load(text)
    except Exception as e:   # both YAML libraries have their own error hierarchies
        raise QCError(f"{f} is not valid YAML:\n{e}")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise QCError(f"{f} must contain a mapping at the top level.")
    return raw


def write_data_raw(project, raw):
    rt = _roundtrip_yaml()
    if rt:
        from io import StringIO
        buf = StringIO()
        rt.dump(raw, buf)
        text = buf.getvalue()
    else:   # PyYAML loses the comments
        text = _pyyaml().safe_dump(_plain(raw), sort_keys=False, allow_unicode=True,
                                   default_flow_style=False)
    write_text_atomic(project.data_file, text)


def _plain(obj):
    """ruamel/PyYAML containers -> plain dicts/lists."""
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def effective_config(raw):
    """Copy of data.yaml with all defaults filled in (never written back)."""
    raw = _plain(raw)
    cfg = {
        "sources": dict(raw.get("sources") or {}),
        "strip_suffixes": [str(s).lower() for s in
                           (raw.get("strip_suffixes") or DEFAULT_STRIP_SUFFIXES)],
        "defaults": dict(DEFAULT_SETTINGS, **(raw.get("defaults") or {})),
        "checks": dict(DEFAULT_CHECKS, **(raw.get("checks") or {})),
        "geometries": list(raw.get("geometries") or DEFAULT_GEOMETRIES),
        "bfreq_column": raw.get("bfreq_column") or "dG(Eh)",
        "boltzmann_columns": dict({"low": "G_LOW", "av": "G_AV", "check": "dG_AV"},
                                  **(raw.get("boltzmann_columns") or {})),
        "solvent_aliases": {str(k).strip().lower(): str(v).strip().lower()
                            for k, v in (raw.get("solvent_aliases") or {}).items()},
        "reactions": {str(k): str(v) for k, v in (raw.get("reactions") or {}).items()},
    }
    return cfg


def load_data_config(project):
    return effective_config(read_data_raw(project))


def source_entries(value):
    """Normalize a sources entry: 'path', ['a', 'b'], {path, method, solvent}, or mixed lists."""
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        if isinstance(item, dict):
            if "path" not in item:
                raise QCError(f"source entry without 'path' in {DATA_FILE}: {dict(item)}")
            out.append({"path": str(item["path"]), "method": item.get("method"),
                        "solvent": item.get("solvent")})
        else:
            out.append({"path": str(item), "method": None, "solvent": None})
    return out


def _dump_entries(entries):
    def one(e):
        extra = {k: e[k] for k in ("method", "solvent") if e.get(k)}
        return dict(path=e["path"], **extra) if extra else e["path"]
    if not entries:
        return None
    return one(entries[0]) if len(entries) == 1 else [one(e) for e in entries]


# ════════════════════════════════════════════════════════════════════════════
#  Loading the CSV data
# ════════════════════════════════════════════════════════════════════════════

def make_basename(name, strip):
    """
    Species name from a NAME entry: known suffixes are removed anywhere, both
    '.'-separated and '_'-separated ('a.oet2.optfreq', 'a.oet2_censo' -> 'a.oet2').
    """
    def clean(part):
        while True:
            head, sep, tail = part.rpartition("_")
            if sep and tail.lower() in strip:
                part = head
            else:
                return part

    parts = [p for p in str(name).strip().split(".") if p.lower() not in strip]
    if not parts:
        return str(name).strip()
    if len(parts) == 1:
        return clean(parts[0])
    return ".".join([parts[0]] + [clean(p) for p in parts[1:]])


def norm_method(value):
    s = str(value).strip().lower()
    return None if s in ("", "nan", "none", "null") else s


def norm_solvent(value, aliases=None):
    s = str(value).strip().lower()
    if s in ("", "nan", "none", "null"):
        return None
    return (aliases or {}).get(s, s)


def resolve_path(project, path):
    p = Path(path).expanduser()
    return p if p.is_absolute() else project.root / p


def read_csv(path):
    try:
        import pandas as pd
    except ImportError:
        raise QCError("pandas is required to read the data (pip install pandas).")
    try:
        df = pd.read_csv(path, sep=";")
    except Exception as e:
        raise QCError(f"cannot read {path}: {e}")
    df.columns = [str(c).strip() for c in df.columns]
    if "NAME" not in df.columns:
        raise QCError(f"{path} has no NAME column (expected ';'-separated CSV).")
    return df.rename(columns={"CHARGE": "CHRG", "MULTIPLICITY": "M"})


def load_dataset(project, cfg, require=True):
    """
    {role: DataFrame} with the added columns BASENAME, METHOD, SOLVENT, SOURCEFILE.
    CHRG and M are always present: rows without them take them from the other
    sources of the same species name.
    """
    import pandas as pd   # read_csv has already checked the import

    strip = set(cfg["strip_suffixes"])
    aliases = cfg["solvent_aliases"]
    data = {}
    for role in ROLES:
        frames = []
        for entry in source_entries(cfg["sources"].get(role)):
            path = resolve_path(project, entry["path"])
            if not path.exists():
                warn(f"{role} source not found: {path}")
                continue
            df = read_csv(path)
            df["BASENAME"] = df["NAME"].map(lambda n: make_basename(n, strip))
            df["METHOD"] = df["METHOD"].map(norm_method) if "METHOD" in df.columns else None
            if entry["method"]:
                df["METHOD"] = norm_method(entry["method"])
            if "SOLVENT" in df.columns:
                df["SOLVENT"] = df["SOLVENT"].map(lambda v: norm_solvent(v, aliases))
            else:
                df["SOLVENT"] = None
            if entry["solvent"]:
                df["SOLVENT"] = norm_solvent(entry["solvent"], aliases)
            df["SOURCEFILE"] = str(path)
            frames.append(df)
        if frames:
            data[role] = pd.concat(frames, ignore_index=True)
    known = charge_mult_by_name(data.values())
    for df in data.values():
        fill_charge_mult(df, known)
    if require and "thermo" not in data and "sp" not in data:
        raise QCError(f"no usable 'thermo' or 'sp' source in {DATA_FILE}.\n"
                      f"Add one with 'qc data add <role> <file.csv>' "
                      f"(or find candidates with 'qc data find').")
    return data


def _integers(series):
    """Numeric column as nullable integers (empty cells stay empty)."""
    import pandas as pd
    values = pd.to_numeric(series, errors="coerce")
    present = values.dropna()
    if (present == present.round()).all():
        return values.round().astype("Int64")
    return values


def charge_mult_by_name(frames):
    """
    {"CHRG": {name: q}, "M": {name: M}} from all rows that give them. The species
    name determines charge and multiplicity, so a name with conflicting values
    is left out (and reported by 'qc data check').
    """
    seen = {"CHRG": {}, "M": {}}
    for df in frames:
        for col, values in seen.items():
            if col in df.columns:
                numbers = _integers(df[col])
                given = numbers.notna()
                for base, v in zip(df["BASENAME"][given], numbers[given]):
                    values.setdefault(base, set()).add(int(v))
    return {col: {b: next(iter(v)) for b, v in values.items() if len(v) == 1}
            for col, values in seen.items()}


def fill_charge_mult(df, known):
    """Fill missing CHRG/M of a source (e.g. solv, bfreq) by species name."""
    import pandas as pd
    for col in ("CHRG", "M"):
        values = (pd.to_numeric(df[col], errors="coerce").astype("float") if col in df.columns
                  else pd.Series(float("nan"), index=df.index))
        df[col] = _integers(values.fillna(df["BASENAME"].map(known[col])))


def elec_role(data):
    """Role that provides the electronic energy: sp if present, else thermo."""
    return "sp" if "sp" in data else "thermo"


def available_methods(df):
    vals = list(df["METHOD"].unique())
    named = sorted(m for m in vals if m is not None)
    return named + ([None] if any(m is None for m in vals) else [])


def available_solvents(df):
    return sorted(v for v in df["SOLVENT"].unique() if v is not None)


def resolve_method(data, role, explicit, default, flag, strict, setting=None):
    """
    Functional for one role: CLI flag > data.yaml default > the only one present.
    strict=True (reactions, scans): several present -> error, since all species
    must use the same functional. strict=False (single species): None, resolved
    per species.
    """
    if role not in data:
        return None
    methods = available_methods(data[role])
    chosen = norm_method(explicit) if explicit else norm_method(default) if default else None
    if chosen is not None:
        if chosen not in methods:
            raise QCError(f"functional '{chosen}' not in the {role} source "
                          f"(available: {', '.join(str(m) for m in methods)}).")
        return chosen
    if len(methods) > 1:
        if not strict:
            return None
        raise QCError(f"the {role} source holds several functionals "
                      f"({', '.join(str(m) for m in methods)}).\n"
                      f"Relative energies need one functional - choose with {flag} or set "
                      f"'defaults: {setting or 'method_' + role}' in {DATA_FILE}.")
    return methods[0] if methods else None


def resolve_solvent(data, explicit, default, strict, aliases):
    if "solv" not in data:
        if explicit:
            raise QCError(f"--solvent given, but there is no solv source in {DATA_FILE}.")
        return None
    solvents = available_solvents(data["solv"])
    chosen = norm_solvent(explicit, aliases) if explicit else (
        norm_solvent(default, aliases) if default else None)
    if chosen is not None:
        if chosen not in solvents:
            raise QCError(f"solvent '{chosen}' not in the solv source "
                          f"(available: {', '.join(solvents) or '-'}).")
        return chosen
    if len(solvents) > 1:
        if not strict:
            return None
        raise QCError(f"the solv source holds several solvents ({', '.join(solvents)}).\n"
                      f"Compare species only within one solvent - choose with -s/--solvent "
                      f"or set 'defaults: solvent' in {DATA_FILE}.")
    return solvents[0] if solvents else None


def parse_spec(spec):
    """'name@3' -> ('name', 3); 'name' -> ('name', None)."""
    name, sep, m = spec.rpartition("@")
    if sep and m.isdigit():
        return name, int(m)
    return spec, None


FUNCTIONAL_FLAG = {"sp": "-f", "thermo": "--ft", "solv": "--fsolv", "boltz": "--fboltz",
                   "bfreq": "--fbfreq"}


def pick_row(df, base, m, role, method=None, solvent=None, flag=None):
    hits = df[df["BASENAME"] == base]
    if m is not None and "M" in hits.columns:
        mult = hits["M"].astype(float)
        hits = hits[mult.isna() | (mult == float(m))]   # rows without M match any
    if method is not None:
        hits = hits[hits["METHOD"] == method]
    if solvent is not None and hits["SOLVENT"].notna().any():
        # sources without solvent information (e.g. CENSO without SOLVENT) are not filtered
        hits = hits[hits["SOLVENT"] == solvent]
    elif solvent is not None:
        solvent = None
    if len(hits) == 0:
        desc = f"'{base}'" + (f" (M={m})" if m is not None else "")
        if method is not None:
            desc += f" with functional '{method}'"
        if solvent is not None:
            desc += f" in '{solvent}'"
        msg = f"{desc} not found in the {role} source."
        pool = df[df["BASENAME"] == base]
        if method is not None and len(pool):
            msg += f"\nAvailable for '{base}': {', '.join(str(x) for x in available_methods(pool))}"
        else:
            msg += did_you_mean(base, df["BASENAME"].unique())
        raise LookupError(msg)
    if len(hits) > 1:
        if "M" in hits.columns and hits["M"].nunique() > 1:
            ms = sorted({int(x) for x in hits["M"]})
            raise LookupError(f"'{base}' is ambiguous in the {role} source (M = {ms}).\n"
                              f"Choose the multiplicity with '{base}@M' or -m.")
        if hits["METHOD"].nunique() > 1:
            raise LookupError(f"'{base}' has several functionals in the {role} source "
                              f"({', '.join(str(x) for x in available_methods(hits))}). "
                              f"Choose one with {flag or FUNCTIONAL_FLAG[role]}.")
        if hits["SOLVENT"].nunique() > 1:
            raise LookupError(f"'{base}' has several solvents in the {role} source "
                              f"({', '.join(available_solvents(hits))}). Choose one with -s.")
        warn(f"'{base}' occurs {len(hits)}x in the {role} source - using the first row.")
    return hits.iloc[0]


def _is_false(value):
    return str(value).strip().lower() == "false"


def row_warnings(row, base, role, checks):
    """Plausibility of one row (SCF convergence, character vs. imaginary modes)."""
    if checks.get("scf", True) and _is_false(row.get("SCFCONV")):
        warn(f"{base} ({role}): SCF not converged!")
    if checks.get("geoconv", False) and _is_false(row.get("CPSCF/GEOCONV")):
        warn(f"{base} ({role}): CPSCF/GEOCONV = False.")
    if not checks.get("character", True):
        return
    char = str(row.get("CHAR", "")).strip().upper()
    if char in ("", "NAN", "NONE"):
        return
    try:
        n_imag = int(float(str(row.get("IMAGS")).strip()))
    except (TypeError, ValueError):
        n_imag = 0   # empty column = no imaginary modes
    if char == "MIN" and n_imag > 0:
        warn(f"{base} ({role}): listed as MIN but has {n_imag} imaginary mode(s)!")
    elif char == "TS" and n_imag != 1:
        warn(f"{base} ({role}): listed as TS but has {n_imag} imaginary mode(s)!")
    elif char not in ("MIN", "TS"):
        warn(f"{base} ({role}): unknown character '{char}'.")


def boltz_warnings(row, base):
    status = str(row.get("STATUS", "")).strip().upper()
    if status not in ("", "NAN", "NONE", "OK"):
        warn(f"{base} (boltz): STATUS = {status}")
    try:
        failed = float(row.get("FAILED_CALC_COUNT", 0) or 0)
    except (TypeError, ValueError):
        return
    if failed > 0:
        rel = row.get("FAILED_CALC_COUNT_REL")
        try:
            extra = f" ({float(rel) * 100:.1f}%)"
        except (TypeError, ValueError):
            extra = ""
        warn(f"{base} (boltz): {int(failed)} failed calculation(s){extra}")


# ════════════════════════════════════════════════════════════════════════════
#  Thermochemistry
# ════════════════════════════════════════════════════════════════════════════

def unit_key(unit):
    key = str(unit).strip().lower()
    if key not in UNITS:
        raise QCError(f"unknown unit '{unit}' (valid: Eh, kcal/mol, kJ/mol, eV).")
    return key


def fmt_energy(value_eh, unit, signed=False):
    factor, label, decimals = UNITS[unit]
    return f"{value_eh * factor:{'+' if signed else ''}.{decimals}f} {label}"


def fmt_charge(q):
    return f"{q:+d}" if q else "0"


def state_correction_eh(temperature):
    """Standard-state correction 1 atm (ideal gas) -> 1 mol/L: RT ln(R'T), in Eh."""
    return R_KCAL * temperature * math.log(R_LATM * temperature) / HARTREE_TO_KCAL


class Settings:
    """Everything that decides how a species energy is assembled."""

    def __init__(self, **kw):
        self.mult = kw.get("mult")
        self.methods = kw.get("methods", {})
        self.solvent = kw.get("solvent")
        self.solv = kw.get("solv", False)
        self.boltz = kw.get("boltz", False)
        self.bfreq = kw.get("bfreq", False)
        self.sc = kw.get("sc", False)
        self.temperature = kw.get("temperature", 298.15)


def resolve_settings(data, cfg, args, strict):
    d = cfg["defaults"]
    if args.solv is False and args.solvent:
        raise QCError("--gas and --solvent contradict each other.")
    solv = args.solv if args.solv is not None else (True if args.solvent
                                                    else bool(d["solvation"]))
    er = elec_role(data)
    methods = {er: resolve_method(data, er, args.functional, d["method_sp"],
                                  "-f/--functional", strict, setting="method_sp")}
    for role, flag in (("thermo", "ft"), ("solv", "fsolv"), ("boltz", "fboltz"),
                       ("bfreq", "fbfreq")):
        if role != er:
            methods[role] = resolve_method(data, role, getattr(args, flag),
                                           d[f"method_{role}"], f"--{flag}", strict)
    solvent = resolve_solvent(data, args.solvent, d["solvent"], strict and solv,
                              cfg["solvent_aliases"])
    return Settings(
        mult=args.mult, methods=methods, solvent=solvent, solv=solv,
        boltz=args.boltz if args.boltz is not None else bool(d["boltzmann"]),
        bfreq=args.bfreq if args.bfreq is not None else bool(d["bfreq"]),
        sc=args.sc if args.sc is not None else bool(d["state_correction"]),
        temperature=float(d["temperature"]))


def species_value(data, cfg, st, spec, quantity, bfreq=None):
    """
    Energy of one species ('name' or 'name@M') as a breakdown dict; 'total' in Eh.
      total = E + thermal (dU/dH/dG or bfreq dG) + Gsolv + ensemble + standard state
    Raises LookupError if a required entry is missing.
    """
    base, m = parse_spec(spec)
    if m is None:
        m = st.mult
    use_bfreq = bool(st.bfreq if bfreq is None else bfreq)
    bd = {"name": base, "M": m, "CHRG": None, "quantity": quantity}

    # electronic energy: sp source if present, else ENERGY of thermo
    er = elec_role(data)
    row_e = pick_row(data[er], base, m, er, method=st.methods.get(er), flag="-f")
    row_warnings(row_e, base, er, cfg["checks"])
    bd.update(E=float(row_e["ENERGY"]), elec_role=er, method_E=row_e["METHOD"])
    try:
        if bd["M"] is None:
            bd["M"] = int(float(row_e["M"]))
        bd["CHRG"] = int(float(row_e["CHRG"]))
    except (KeyError, TypeError, ValueError):
        pass

    # thermal correction - may come from another functional than E
    bd.update(thermal=0.0, thermal_role=None, method_thermal=None)
    if quantity in QUANTITY_COLUMN and use_bfreq:
        if "bfreq" not in data:
            raise LookupError(f"bfreq correction requested, but no bfreq source in {DATA_FILE}.")
        if quantity != "G":
            raise LookupError(f"the bfreq source only provides dG - {quantity} needs the "
                              f"thermo source (use --no-bfreq).")
        col = cfg["bfreq_column"]
        row_t = pick_row(data["bfreq"], base, m, "bfreq", method=st.methods.get("bfreq"))
        if col not in row_t.index:
            raise LookupError(f"column '{col}' missing in the bfreq source "
                              f"(set 'bfreq_column' in {DATA_FILE}).")
        bd.update(thermal=float(row_t[col]), thermal_role="bfreq", method_thermal=row_t["METHOD"])
    elif quantity in QUANTITY_COLUMN:
        if "thermo" not in data:
            raise LookupError(f"{quantity} needs a thermo source in {DATA_FILE}.")
        if er == "thermo":
            row_t = row_e
        else:
            row_t = pick_row(data["thermo"], base, m, "thermo", method=st.methods.get("thermo"))
            row_warnings(row_t, base, "thermo", cfg["checks"])
        bd.update(thermal=float(row_t[QUANTITY_COLUMN[quantity]]), thermal_role="thermo",
                  method_thermal=row_t["METHOD"])

    # solvation
    bd.update(solv=0.0, solvent=None, method_solv=None, gas=not st.solv)
    if st.solv:
        if "solv" not in data:
            raise LookupError(f"solvation requested, but no solv source in {DATA_FILE}.")
        row_s = pick_row(data["solv"], base, m, "solv", method=st.methods.get("solv"),
                         solvent=st.solvent)
        col = "Gsolv(Eh)" if "Gsolv(Eh)" in row_s.index else "Gsolv"
        if col not in row_s.index:
            raise LookupError("no 'Gsolv(Eh)' column in the solv source.")
        bd.update(solv=float(row_s[col]), solvent=row_s["SOLVENT"], method_solv=row_s["METHOD"])

    # conformer-ensemble correction (CENSO)
    bd.update(boltz=0.0, method_boltz=None)
    if st.boltz:
        if "boltz" not in data:
            raise LookupError(f"ensemble correction requested, but no boltz source in {DATA_FILE}.")
        bc = cfg["boltzmann_columns"]
        row_b = pick_row(data["boltz"], base, m, "boltz", method=st.methods.get("boltz"),
                         solvent=st.solvent if st.solv else None)
        boltz_warnings(row_b, base)
        if bc.get("value") and bc["value"] in row_b.index:
            boltz = float(row_b[bc["value"]])
        else:
            for c in (bc["low"], bc["av"]):
                if c not in row_b.index:
                    raise LookupError(f"column '{c}' missing in the boltz source "
                                      f"(set 'boltzmann_columns' in {DATA_FILE}).")
            boltz = float(row_b[bc["av"]]) - float(row_b[bc["low"]])
            cross = bc.get("check")
            if cross and cross in row_b.index:
                try:
                    ref = float(row_b[cross])
                except (TypeError, ValueError):
                    ref = None
                if ref is not None and abs(boltz * HARTREE_TO_KCAL - ref) > 0.01:
                    warn(f"{base} (boltz): {bc['av']} - {bc['low']} = "
                         f"{boltz * HARTREE_TO_KCAL:.4f} kcal/mol does not match "
                         f"{cross} = {ref:.4f}.")
        bd.update(boltz=boltz, method_boltz=row_b["METHOD"])

    # standard-state correction (only for G)
    bd["state"] = state_correction_eh(st.temperature) if (quantity == "G" and st.sc) else 0.0
    bd["temperature"] = st.temperature
    bd["total"] = bd["E"] + bd["thermal"] + bd["solv"] + bd["boltz"] + bd["state"]
    return bd


def components(bd):
    """[(key, label)] of the non-trivial contributions of a breakdown."""
    q = bd["quantity"]
    out = [("E", f"E ({bd['method_E'] or bd['elec_role']})")]
    if bd["thermal_role"]:
        src = "bfreq dG" if bd["thermal_role"] == "bfreq" else QUANTITY_COLUMN[q]
        meth = f", {bd['method_thermal']}" if bd["method_thermal"] else ""
        out.append(("thermal", f"thermal correction ({src}{meth})"))
    if not bd["gas"]:
        parts = ", ".join(p for p in (bd["solvent"], bd["method_solv"]) if p)
        out.append(("solv", "solvation (Gsolv" + (f", {parts}" if parts else "") + ")"))
    if bd["boltz"]:
        out.append(("boltz", "ensemble correction (G_AV - G_LOW)"))
    if bd["state"]:
        out.append(("state", f"standard state 1 atm -> 1 M ({bd['temperature']:g} K)"))
    return out


def describe(bd):
    """Short provenance tag, e.g. 'E: pwpb95, thermo: wb97x-3c, solv: thf'."""
    tags = []
    if bd["method_E"]:
        tags.append(f"E: {bd['method_E']}")
    if bd["thermal_role"]:
        meth = bd["method_thermal"] or "?"
        tags.append(f"{'bfreq' if bd['thermal_role'] == 'bfreq' else 'thermo'}: {meth}")
    tags.append("gas phase" if bd["gas"] else f"solv: {bd['solvent'] or bd['method_solv'] or '?'}")
    if bd["boltz"]:
        tags.append("ensemble")
    if bd["state"]:
        tags.append("1 M")
    return ", ".join(tags)


def print_breakdown(bd):
    m = f"M = {bd['M']}" if bd["M"] is not None else ""
    q = f"q = {fmt_charge(bd['CHRG'])}" if bd["CHRG"] is not None else ""
    heading(f"{bd['quantity']} of {bd['name']}" + (f"  ({', '.join(p for p in (m, q) if p)})"
                                                    if m or q else ""))
    rows = [[label, f"{bd[key]:.8f} Eh", f"{bd[key] * HARTREE_TO_KCAL:.3f} kcal/mol"]
            for key, label in components(bd)]
    rows.append([bd["quantity"], f"{bd['total']:.8f} Eh",
                 f"{bd['total'] * HARTREE_TO_KCAL:.3f} kcal/mol"])
    width = max(len(r[0]) for r in rows)
    for i, r in enumerate(rows):
        if i == len(rows) - 1:
            print("  " + "-" * (width + 40))
        print(f"  {r[0]:<{width}}  {r[1]:>20}  {r[2]:>20}")
    print()


def cmd_species(args):
    project = current_project()
    cfg = load_data_config(project)
    data = load_dataset(project, cfg)

    # one column per (role, functional, solvent), one row per (species, charge, multiplicity)
    columns, cover = set(), {}
    for role in ROLES:
        if role not in data:
            continue
        df = data[role]
        for _, row in df.iterrows():
            col = (role, row["METHOD"] or "-", row["SOLVENT"] or "")
            columns.add(col)
            try:
                q = int(float(row["CHRG"]))
            except (KeyError, TypeError, ValueError):
                q = None
            try:
                m = int(float(row["M"]))
            except (KeyError, TypeError, ValueError):
                m = None
            cover.setdefault((row["BASENAME"], q, m), set()).add(col)
    if args.pattern:
        pat = args.pattern if any(c in args.pattern for c in "*?[") else f"*{args.pattern}*"
        cover = {k: v for k, v in cover.items() if fnmatch.fnmatchcase(k[0], pat)}
    if not cover:
        print("No species found" + (f" matching '{args.pattern}'." if args.pattern else "."))
        return
    columns = sorted(columns, key=lambda c: (ROLES.index(c[0]), c[1], c[2]))
    has_solvent_row = any(c[2] for c in columns)

    def num(v):
        return "?" if v is None else str(v)

    header_rows = [["species", "q", "M"] + [c[0] if i == 0 or columns[i - 1][0] != c[0] else ""
                                            for i, c in enumerate(columns)],
                   ["", "", ""] + [c[1] for c in columns]]
    if has_solvent_row:
        header_rows.append(["", "", ""] + [c[2] for c in columns])
    body = []
    for key in sorted(cover, key=lambda k: (k[0], k[1] if k[1] is not None else 0,
                                            k[2] if k[2] is not None else 0)):
        base, q, m = key
        body.append([base, num(q), num(m)] + ["x" if c in cover[key] else "." for c in columns])
    widths = [max(len(r[i]) for r in header_rows + body) for i in range(len(body[0]))]

    def line(r, center=True):
        cells = [r[0].ljust(widths[0]), r[1].rjust(widths[1]), r[2].rjust(widths[2])]
        cells += [c.center(w) if center else c.ljust(w) for c, w in zip(r[3:], widths[3:])]
        return ("  " + "  ".join(cells)).rstrip()

    heading(f"Species of '{project.name}' ({len(body)})")
    for r in header_rows:
        print(bold(line(r)))
    for r in body:
        print(line(r))


def add_energy_args(p):
    p.add_argument("species", nargs="+", metavar="SPECIES",
                   help="species name; 'name@M' selects the multiplicity")


def cmd_energy(args):
    quantity = args.command.upper()
    project = current_project()
    cfg = load_data_config(project)
    data = load_dataset(project, cfg)
    unit = unit_key(args.unit or "eh")
    st = resolve_settings(data, cfg, args, strict=False)
    results = []
    for spec in args.species:
        try:
            bd = species_value(data, cfg, st, spec, quantity)
        except LookupError as e:
            raise QCError(str(e))
        results.append(bd)
    if args.verbose:
        for bd in results:
            print_breakdown(bd)
        return
    width = max(len(f"{quantity}({bd['name']})") for bd in results)
    for bd in results:
        label = f"{quantity}({bd['name']})"
        print(f"{label:<{width}} = {fmt_energy(bd['total'], unit):>22}   [{describe(bd)}]")


# ── reactions ─────────────────────────────────────────────────────────────────

def parse_reaction(eq):
    """'a + 2 b -> c' -> ([(1.0, 'a'), (2.0, 'b')], [(1.0, 'c')])"""
    for arrow in ("->", "=>", "→", "="):
        if arrow in eq:
            left, right = eq.split(arrow, 1)
            break
    else:
        raise QCError(f"not a reaction equation (no '->'): {eq}")

    def side(s):
        # ' + ' with spaces separates species if present, so names may contain '+'
        parts = re.split(r"\s\+\s", s) if re.search(r"\s\+\s", s) else s.split("+")
        terms = []
        for t in (p.strip() for p in parts):
            if t:
                mm = COEFF_RE.match(t)
                terms.append((float(mm.group(1)), mm.group(2).strip()) if mm else (1.0, t))
        return terms

    reactants, products = side(left), side(right)
    if not reactants or not products:
        raise QCError(f"incomplete reaction equation: {eq}")
    return reactants, products


def element_counts(atoms):
    return Counter(a[0].capitalize() if a[0].isalpha() else a[0] for a in atoms)


def check_atom_balance(project, cfg, details):
    """Warn if the geometries show different atoms on both sides (skipped if any is missing)."""
    finder = GeometryFinder(project.root, cfg)
    total = Counter()
    for nu, bd in details:
        path = finder.find(bd["name"])
        if path is None:
            return None
        try:
            counts = element_counts(read_xyz(path))
        except (OSError, ValueError):
            return None
        for el, n in counts.items():
            total[el] += nu * n
    off = {el: n for el, n in total.items() if abs(n) > 1e-9}
    if off:
        diff = ", ".join(f"{el} {n:+g}" for el, n in sorted(off.items()))
        warn(f"atoms are not balanced (products - reactants: {diff}) - check the stoichiometry.")
        return False
    return True


def cmd_rxn(args):
    words = args.words
    if not words or words[0] == "ls":
        return rxn_ls()
    if words[0] == "add":
        if len(words) < 3:
            raise QCError('usage: qc rxn add <name> "<a + 2 b -> c>"')
        return rxn_add(words[1], " ".join(words[2:]))
    if words[0] == "rm":
        if len(words) != 2:
            raise QCError("usage: qc rxn rm <name>")
        return rxn_rm(words[1])
    return rxn_calc(args, " ".join(words))


def rxn_ls():
    project = current_project()
    cfg = load_data_config(project)
    heading(f"Reactions of '{project.name}'")
    if not cfg["reactions"]:
        print('  (none) - add one with: qc rxn add <name> "a + b -> c"')
        return
    print_table(sorted(cfg["reactions"].items()))


def rxn_add(name, equation):
    project = current_project()
    if name in ("add", "rm", "ls"):
        raise QCError(f"'{name}' is reserved and cannot be a reaction name.")
    reactants, products = parse_reaction(equation)
    try:   # check the species against the data, but only warn
        cfg = load_data_config(project)
        data = load_dataset(project, cfg)
        known = set(data[elec_role(data)]["BASENAME"])
        for _, spec in reactants + products:
            base, _ = parse_spec(spec)
            if base not in known:
                warn(f"'{base}' is not (yet) in the {elec_role(data)} source.")
    except QCError:
        pass
    raw = read_data_raw(project)
    if not isinstance(raw.get("reactions"), dict):
        raw["reactions"] = {}
    action = "Updated" if name in raw["reactions"] else "Added"
    raw["reactions"][name] = equation
    write_data_raw(project, raw)
    print(f"{action} reaction '{name}': {equation}")


def rxn_rm(name):
    project = current_project()
    raw = read_data_raw(project)
    reactions = raw.get("reactions") or {}
    if name not in reactions:
        raise QCError(f"no reaction '{name}'." + did_you_mean(name, reactions))
    del reactions[name]
    write_data_raw(project, raw)
    print(f"Removed reaction '{name}'.")


def rxn_calc(args, key):
    project = current_project()
    cfg = load_data_config(project)
    data = load_dataset(project, cfg)
    quantity = args.quantity
    unit = unit_key(args.unit or cfg["defaults"]["unit"])
    st = resolve_settings(data, cfg, args, strict=True)

    name = key if key in cfg["reactions"] else None
    eq = cfg["reactions"].get(key, key)
    try:
        reactants, products = parse_reaction(eq)
    except QCError as e:
        hint = f"\nNamed reactions: {', '.join(cfg['reactions'])}" if cfg["reactions"] else ""
        raise QCError(f"'{key}' is neither a named reaction nor an equation.{hint}"
                      + did_you_mean(key, cfg["reactions"])) from e

    details = []   # (signed stoichiometric coefficient, breakdown)
    for sign, terms in ((-1, reactants), (+1, products)):
        for coeff, spec in terms:
            try:
                details.append((sign * coeff, species_value(data, cfg, st, spec, quantity)))
            except LookupError as e:
                raise QCError(str(e))

    # consistency: same functional for E, one solvent, balanced charge and atoms
    used_e = {bd["method_E"] for _, bd in details}
    if len(used_e) > 1:
        raise QCError(f"electronic energies from different functionals "
                      f"({', '.join(sorted(map(str, used_e)))}) - relative energies would be "
                      f"meaningless. Choose one with -f.")
    used_solvents = {bd["solvent"] for _, bd in details if not bd["gas"]}
    if len(used_solvents) > 1:
        raise QCError(f"species in different solvents ({', '.join(sorted(map(str, used_solvents)))})"
                      f" - choose one with -s.")
    used_thermal = {(bd["thermal_role"], bd["method_thermal"]) for _, bd in details}
    if len(used_thermal) > 1:
        warn("thermal corrections from different functionals: "
             + ", ".join(sorted(str(m) for _, m in used_thermal)))
    charges = [(nu, bd["CHRG"]) for nu, bd in details]
    if all(q is not None for _, q in charges):
        dq = sum(nu * q for nu, q in charges)
        if abs(dq) > 1e-9:
            warn(f"charge is not balanced (products - reactants: {dq:+g}).")
    check_atom_balance(project, cfg, details)

    total = sum(nu * bd["total"] for nu, bd in details)
    title = f"{name}: {eq}" if name else eq
    if args.verbose:
        print_reaction_breakdown(title, details, unit)
    print(f"Δ{quantity} = {fmt_energy(total, unit)}   {title}   [{describe(details[0][1])}]")


def print_reaction_breakdown(title, details, unit):
    first = details[0][1]
    keys = components(first)
    heading(f"Reaction {title}")
    short = {"E": "E", "thermal": "thermal", "solv": "Gsolv", "boltz": "ensemble",
             "state": "std. state"}
    header = ["ν", "species", "M", "q"] + [short[k] for k, _ in keys] + [first["quantity"]]
    rows = []
    for nu, bd in details:
        rows.append([f"{nu:+g}", bd["name"], bd["M"] if bd["M"] is not None else "?",
                     fmt_charge(bd["CHRG"]) if bd["CHRG"] is not None else "?"]
                    + [f"{bd[k]:.8f}" for k, _ in keys] + [f"{bd['total']:.8f}"])
    print_table(rows, header=header, align="rlrr" + "r" * (len(keys) + 1))
    print(dim("  energies in Eh; ν < 0: reactants, ν > 0: products"))
    print()
    heading(f"Contributions ({UNITS[unit][1]})")
    rows = []
    for key, label in keys:
        delta = sum(nu * bd[key] for nu, bd in details)
        rows.append([f"Δ {label}", f"{delta * UNITS[unit][0]:+.{UNITS[unit][2]}f}"])
    print_table(rows, align="lr")
    print()


# ════════════════════════════════════════════════════════════════════════════
#  Geometries & internal coordinates
# ════════════════════════════════════════════════════════════════════════════

def read_xyz(path):
    """First frame of an XYZ file -> [(symbol, x, y, z), ...] in Angstrom."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    try:
        n = int(lines[0].split()[0])
    except (IndexError, ValueError):
        raise ValueError(f"not a valid XYZ file: {path}")
    atoms = []
    for line in lines[2:2 + n]:
        parts = line.split()
        if len(parts) >= 4:
            atoms.append((parts[0], float(parts[1]), float(parts[2]), float(parts[3])))
    if len(atoms) != n:
        raise ValueError(f"{path}: read {len(atoms)} atoms, expected {n}.")
    return atoms


class GeometryFinder:
    """Geometry of a species: the data.yaml patterns first, then a search of the project."""

    def __init__(self, root, cfg):
        self.root = Path(root)
        self.patterns = cfg["geometries"]
        self.strip = set(cfg["strip_suffixes"])
        self._index = None

    def find(self, base):
        for pattern in self.patterns:
            try:
                cand = self.root / str(pattern).format(name=base)
            except (KeyError, IndexError, ValueError):
                continue
            if cand.is_file():
                return cand
        hits = self.index().get(base)
        return hits[0] if hits else None

    def index(self):
        """{name: [paths]} of all .xyz files: exact file stems first, then stripped names."""
        if self._index is None:
            exact, stripped = {}, {}
            for p in sorted(self.root.rglob("*.xyz")):
                rel = p.relative_to(self.root)
                if any(part.startswith(".") for part in rel.parts) or p.stem.endswith("_trj"):
                    continue
                exact.setdefault(p.stem, []).append(p)
                stripped.setdefault(make_basename(p.stem, self.strip), []).append(p)
            self._index = {k: exact.get(k, []) + [p for p in v if p not in exact.get(k, [])]
                           for k, v in stripped.items()}
            for k, v in exact.items():
                self._index.setdefault(k, v)
        return self._index


def _sub(a, b):
    return (b[1] - a[1], b[2] - a[2], b[3] - a[3])


def _dot(u, v):
    return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]


def _cross(u, v):
    return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])


def _norm(u):
    return math.sqrt(_dot(u, u))


COORD_KINDS = {  # kind: (number of atoms, name, symbol, unit)
    "B": (2, "bond length", "r", "Å"),
    "A": (3, "angle", "θ", "°"),
    "D": (4, "dihedral angle", "φ", "°"),
}


def parse_coordinate(tokens):
    """['B', '1', '2'], ['B,1,2'] or ['B 1 2'] -> ('B', [1, 2])"""
    flat = []
    for t in tokens:
        flat.extend(str(t).replace(",", " ").split())
    if not flat:
        raise QCError("no coordinate given (e.g. B 1 2, A 1 2 3, D 1 2 3 4).")
    kind = flat[0].upper()
    if kind not in COORD_KINDS:
        raise QCError(f"unknown coordinate type '{flat[0]}' (B = bond, A = angle, D = dihedral).")
    try:
        idx = [int(x) for x in flat[1:]]
    except ValueError:
        raise QCError(f"atom numbers must be integers: {' '.join(flat[1:])}")
    if len(idx) != COORD_KINDS[kind][0]:
        raise QCError(f"{kind} needs {COORD_KINDS[kind][0]} atom numbers, got {len(idx)}.")
    return kind, idx


def internal_coordinate(atoms, kind, idx, one_based=True):
    """
    B i j      bond length in Angstrom
    A i j k    angle in degrees (j = apex)
    D i j k l  dihedral in degrees, -180..180 (IUPAC sign convention, as ORCA/Avogadro)
    """
    kind = kind.upper()
    pos = []
    for i in idx:
        j = i - 1 if one_based else i
        if not 0 <= j < len(atoms):
            raise ValueError(f"atom number {i} out of range ({len(atoms)} atoms, "
                             f"{'1' if one_based else '0'}-based).")
        pos.append(atoms[j])
    if kind == "B":
        return _norm(_sub(pos[0], pos[1]))
    if kind == "A":
        u, v = _sub(pos[1], pos[0]), _sub(pos[1], pos[2])
        c = _dot(u, v) / (_norm(u) * _norm(v))
        return math.degrees(math.acos(max(-1.0, min(1.0, c))))
    # phi = atan2(((b1 x b2) x (b2 x b3)) . b2/|b2|, (b1 x b2) . (b2 x b3))
    b1, b2, b3 = _sub(pos[0], pos[1]), _sub(pos[1], pos[2]), _sub(pos[2], pos[3])
    n1, n2 = _cross(b1, b2), _cross(b2, b3)
    if _norm(b2) < 1e-10 or _norm(n1) < 1e-10 or _norm(n2) < 1e-10:
        raise ValueError("dihedral angle undefined (collinear atoms).")
    return math.degrees(math.atan2(_dot(_cross(n1, n2), b2) / _norm(b2), _dot(n1, n2)))


def coordinate_label(kind, idx, atoms=None, one_based=True, math_text=False):
    """'r(Ni1–Br2) / Å' (atoms numbered as the user typed them)."""
    def atom(i):
        j = i - 1 if one_based else i
        sym = atoms[j][0].capitalize() if atoms and 0 <= j < len(atoms) else ""
        return f"{sym}{i}"
    _, _, symbol, unit = COORD_KINDS[kind]
    if math_text:
        symbol = {"r": "$r$", "θ": r"$\theta$", "φ": r"$\varphi$"}[symbol]
    return f"{symbol}({'–'.join(atom(i) for i in idx)}) / {unit}"


# ════════════════════════════════════════════════════════════════════════════
#  PES scans
# ════════════════════════════════════════════════════════════════════════════

def scan_groups(data):
    """{prefix: [(point, basename), ...]} for all species named <prefix>.<number>."""
    groups = {}
    names = set()
    for df in data.values():
        names.update(df["BASENAME"].tolist())
    for b in names:
        prefix, sep, tail = b.rpartition(".")
        if sep and tail.isdigit():
            groups.setdefault(prefix, []).append((int(tail), b))
    for members in groups.values():
        members.sort()
    return groups


def list_scans(project, data):
    groups = scan_groups(data)
    heading(f"Scans in '{project.name}'")
    if not groups:
        print("  (none) - scan points must be named <prefix>.<number>, e.g. mol_scan.1")
        return
    rows = [[p, len(m), f"{m[0][0]}–{m[-1][0]}"] for p, m in sorted(groups.items())]
    print_table(rows, header=["prefix", "points", "numbers"], align="lrr")
    print(dim("\n  qc scan <prefix> <B i j | A i j k | D i j k l>"))


def _scan_thermal(data, cfg, st, rows, quantity, explicit_bfreq):
    """
    Thermal quantity (G/H/U) of every scan point. One source for the whole curve:
    bfreq is preferred for G (its frequencies are meaningful at non-stationary
    points); thermo is the fallback. Returns (role, {point: breakdown}).
    """
    if quantity == "E":
        return None, {}
    if st.bfreq is True and quantity != "G":
        raise QCError(f"the bfreq source only provides G - use -q G or --no-bfreq for {quantity}.")
    if st.bfreq is True:
        options = [True]
    elif st.bfreq is False or quantity != "G":
        options = [False]
    else:
        options = [True, False]
    results = {}
    for use_bfreq in options:
        role = "bfreq" if use_bfreq else "thermo"
        if role not in data:
            if explicit_bfreq and use_bfreq:
                raise QCError(f"--bfreq given, but there is no bfreq source in {DATA_FILE}.")
            continue
        vals, errors = {}, {}
        for r in rows:
            try:
                vals[r["point"]] = species_value(data, cfg, st, r["name"], quantity,
                                                 bfreq=use_bfreq)
            except LookupError as e:
                errors[r["name"]] = str(e).splitlines()[0]
        results[role] = (vals, errors)
    if not results:
        warn(f"no thermal data for {quantity} - showing E only.")
        return None, {}

    n = len(rows)
    # complete coverage first, then more points; bfreq wins ties
    order = sorted(results, key=lambda r: (len(results[r][0]) == n, len(results[r][0]),
                                           r == "bfreq"), reverse=True)
    role = order[0]
    vals, errors = results[role]
    if not vals:
        warn(f"{quantity} not available for any scan point - showing E only.")
        return None, {}
    if role == "thermo" and "bfreq" in results:
        note(f"bfreq covers only {len(results['bfreq'][0])}/{n} points - "
             f"using the thermo correction for {quantity}.")
    for name, why in sorted(errors.items()):
        warn(f"{name}: no {quantity} ({why})")
    return role, vals


def _unwrap_dihedrals(rows):
    """Make dihedral angles continuous along the scan (e.g. 170, 180, 190 not -170)."""
    prev = None
    for r in sorted(rows, key=lambda r: r["point"]):
        if prev is not None:
            while r["x"] - prev > 180.0:
                r["x"] -= 360.0
            while r["x"] - prev < -180.0:
                r["x"] += 360.0
        prev = r["x"]


def _bar(value, vmax, width=16):
    if vmax <= 0 or value is None:
        return ""
    eighths = int(round(max(0.0, value) / vmax * width * 8))
    return "█" * (eighths // 8) + ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")[eighths % 8]


def cmd_scan(args):
    project = current_project()
    cfg = load_data_config(project)
    data = load_dataset(project, cfg)
    if not args.prefix:
        return list_scans(project, data)
    groups = scan_groups(data)
    if args.prefix not in groups:
        raise QCError(f"no scan points named '{args.prefix}.<number>'."
                      + did_you_mean(args.prefix, groups)
                      + ("\nList the scans with 'qc scan'." if groups else ""))
    if not args.coordinate:
        raise QCError(f"give the scan coordinate, e.g. 'qc scan {args.prefix} B 1 2'.")
    kind, idx = parse_coordinate(args.coordinate)
    one_based = not args.zero_based
    unit = unit_key(args.unit or cfg["defaults"]["unit"])
    st = resolve_settings(data, cfg, args, strict=True)
    if args.bfreq is None:   # scans have their own default: None = auto
        auto = str(cfg["defaults"]["scan_bfreq"]).strip().lower()
        st.bfreq = None if auto in ("auto", "none", "null") else auto in ("true", "yes", "1")
    members = groups[args.prefix]

    finder = GeometryFinder(project.root, cfg)
    rows, atoms0 = [], None
    for point, name in members:
        geo = finder.find(name)
        if geo is None:
            warn(f"{name}: no geometry found (see 'geometries' in {DATA_FILE}) - skipped.")
            continue
        try:
            atoms = read_xyz(geo)
            x = internal_coordinate(atoms, kind, idx, one_based)
            bd = species_value(data, cfg, st, name, "E", bfreq=False)
        except (LookupError, ValueError, OSError) as e:
            warn(f"{name} skipped: {str(e).splitlines()[0]}")
            continue
        atoms0 = atoms0 or atoms
        rows.append({"point": point, "name": name, "x": x, "geometry": geo,
                     "E": bd["total"], "E_bd": bd})
    if not rows:
        raise QCError("not a single scan point could be evaluated.")
    if len(rows) < len(members):
        warn(f"{len(members) - len(rows)} of {len(members)} points missing - the curve has gaps.")

    q = args.quantity
    role, thermal = _scan_thermal(data, cfg, st, rows, q, explicit_bfreq=args.bfreq is True)
    quantities = ["E"] + ([q] if role else [])
    if role:
        for r in rows:
            r[q] = thermal[r["point"]]["total"] if r["point"] in thermal else None
    if kind == "D":
        _unwrap_dihedrals(rows)
    rows.sort(key=lambda r: (r["x"], r["point"]))

    factor = UNITS[unit][0]
    for qq in quantities:
        vals = [r[qq] for r in rows if r[qq] is not None]
        lo = min(vals)
        for r in rows:
            r["d" + qq] = None if r[qq] is None else (r[qq] - lo) * factor

    # provenance
    bd0 = rows[0]["E_bd"]
    info = [f"E: {bd0['method_E'] or bd0['elec_role']}"]
    if role:
        bdt = next(iter(thermal.values()))
        src = "bfreq dG" if role == "bfreq" else QUANTITY_COLUMN[q]
        info.append(f"{q} = E + {src} ({bdt['method_thermal'] or role})")
    info.append("gas phase" if st.solv is False else f"solvation: {bd0['solvent'] or '?'}")
    if st.boltz:
        info.append("ensemble correction")
    if st.sc and role and q == "G":
        info.append("standard state 1 M")
    xlabel = coordinate_label(kind, idx, atoms0, one_based)
    dec_x = 4 if kind == "B" else 2
    x_unit = " Å" if kind == "B" else "°"

    heading(f"Scan {args.prefix}: {len(rows)} points along {xlabel}")
    print(dim("  " + " · ".join(info)))
    main_q = quantities[-1]
    vmax = max(r["d" + main_q] for r in rows if r["d" + main_q] is not None)
    unit_label = UNITS[unit][1]
    header = ["point", xlabel] + [f"Δ{qq} / {unit_label}" for qq in quantities] + ["species", ""]
    table = []
    for r in rows:
        cells = [r["point"], f"{r['x']:.{dec_x}f}"]
        cells += ["-" if r["d" + qq] is None else f"{r['d' + qq]:.2f}" for qq in quantities]
        cells += [r["name"], _bar(r["d" + main_q], vmax)]
        table.append(cells)
    print_table(table, header=header, align="rr" + "r" * len(quantities) + "ll")
    print()
    for qq in quantities:
        avail = [r for r in rows if r["d" + qq] is not None]
        top = max(avail, key=lambda r: r["d" + qq])
        print(f"  max Δ{qq} = {top['d' + qq]:.2f} {unit_label} at {top['x']:.{dec_x}f}{x_unit} "
              f"(point {top['point']}, relative to the scan minimum)")
    if args.verbose:
        print()
        heading("Geometries")
        print_table([[r["point"], r["geometry"]] for r in sorted(rows, key=lambda r: r["point"])],
                    align="rl")

    if args.csv:
        write_scan_csv(args.csv, rows, quantities, unit)
        print(f"Wrote {args.csv}")

    if args.plot:
        dataset = {
            "label": args.label or args.prefix,
            "coordinate": xlabel,
            "info": " · ".join(info),
            "points": [r["point"] for r in rows],
            "x": [round(r["x"], 6) for r in rows],
        }
        for qq in quantities:
            dataset[qq] = [None if r[qq] is None else round(r[qq], 8) for r in rows]
        provenance = [f"Project:    {project.name}",
                      f"Scan:       {args.prefix} ({len(rows)} points)",
                      f"Coordinate: {xlabel} (atom numbers {'1' if one_based else '0'}-based)",
                      f"Energies:   {' · '.join(info)}"]
        settings = {"title": args.title or "",
                    "xlabel": coordinate_label(kind, idx, atoms0, one_based, math_text=True),
                    "unit": UNITS[unit][1] if unit not in ("eh", "hartree") else "Eh",
                    "curves": list(reversed(quantities))}
        for target in args.plot:
            write_plot(Path(target), dataset, settings, provenance, append=args.append)


def write_scan_csv(path, rows, quantities, unit):
    tag = re.sub(r"\W+", "_", UNITS[unit][1]).upper()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["POINT", "NAME", "X"] + [f"{q}_EH" for q in quantities]
                   + [f"D{q}_{tag}" for q in quantities])
        for r in rows:
            w.writerow([r["point"], r["name"], f"{r['x']:.6f}"]
                       + ["" if r[q] is None else f"{r[q]:.8f}" for q in quantities]
                       + ["" if r["d" + q] is None else f"{r['d' + q]:.6f}" for q in quantities])


# ── plot script ──────────────────────────────────────────────────────────────

PLOT_DATA_BEGIN = "# >>> qc data (rewritten by 'qc scan --append'; keep this line)"
PLOT_DATA_END = "# <<< qc data"
PLOT_IMAGE_TYPES = (".png", ".pdf", ".svg", ".eps", ".jpg", ".jpeg", ".tif", ".tiff")

PLOT_TEMPLATE = r'''#!/usr/bin/env python3
"""
Relaxed PES scan - generated by qc @VERSION@ on @STAMP@.

@PROVENANCE@

Edit the settings below and run:   python3 @FILENAME@
Add another scan to this figure:   qc scan <prefix> <coordinate> --plot @FILENAME@ --append
"""
from bisect import bisect_right
from pathlib import Path

import matplotlib.pyplot as plt

# -- Settings -----------------------------------------------------------------
@SETTINGS@

# One color per dataset, in this order (colorblind-safe palette)
COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
          "#e87ba4", "#008300", "#6250d6", "#e34948"]

# -- Data ---------------------------------------------------------------------
# Absolute energies in Eh; None: not available for that point.
@DATA@

# -- Plotting -----------------------------------------------------------------
UNITS = {  # factor from Eh, axis label, decimals of the maximum label
    "kcal/mol": (627.5094740631, "kcal mol$^{-1}$", 1),
    "kj/mol": (2625.4996394799, "kJ mol$^{-1}$", 1),
    "ev": (27.211386245988, "eV", 2),
    "eh": (1.0, r"$E_\mathrm{h}$", 4),
}
SYMBOLS = {"E": r"$\Delta E$", "H": r"$\Delta H$", "U": r"$\Delta U$", "G": r"$\Delta G$"}
LINESTYLES = ["-", (0, (4, 2)), (0, (1, 1.6))]
INK, INK_MUTED, GRID = "#1f1f1f", "#6b6b6b", "#e6e6e6"
STYLE = {
    "font.size": 10, "axes.labelsize": 10, "axes.titlesize": 10,
    "axes.edgecolor": "#9a9a9a", "axes.linewidth": 0.8, "axes.labelcolor": INK,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.6,
    "xtick.color": INK_MUTED, "ytick.color": INK_MUTED, "text.color": INK,
    "xtick.major.width": 0.8, "ytick.major.width": 0.8,
    "legend.frameon": False, "legend.fontsize": 9,
    "savefig.dpi": 300, "savefig.bbox": "tight", "savefig.facecolor": "white",
}


def curve(ds, quantity):
    """(x, y) of one curve: sorted by x, relative to REFERENCE, in UNIT."""
    factor = UNITS[UNIT.lower()][0]
    pts = [(p, x, e) for p, x, e in zip(ds["points"], ds["x"], ds[quantity]) if e is not None]
    if not pts:
        return [], []
    if REFERENCE == "min":
        ref = min(e for _, _, e in pts)
    else:
        numbers = sorted(p for p, _, _ in pts)
        want = {"first": numbers[0], "last": numbers[-1]}.get(REFERENCE, REFERENCE)
        hits = [e for p, _, e in pts if p == want]
        if not hits:
            raise SystemExit(f"REFERENCE: point {want!r} has no {quantity} in '{ds['label']}'.")
        ref = hits[0]
    pts.sort(key=lambda t: t[1])
    return [x for _, x, _ in pts], [(e - ref) * factor for _, _, e in pts]


def pchip(x, y, n=400):
    """
    Monotone piecewise cubic Hermite interpolation (Fritsch-Carlson, as in SciPy).
    Unlike a cubic spline it never overshoots: it adds no minima or maxima that
    the computed points do not have.
    """
    h = [b - a for a, b in zip(x, x[1:])]
    if len(x) < 3 or min(h) <= 0:
        return x, y
    d = [(y[i + 1] - y[i]) / h[i] for i in range(len(h))]
    m = [0.0] * len(x)
    for i in range(1, len(x) - 1):
        if d[i - 1] * d[i] > 0:
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    m[0] = end_slope(h[0], h[1], d[0], d[1])
    m[-1] = end_slope(h[-1], h[-2], d[-1], d[-2])
    xs, ys = [], []
    for k in range(n):
        t = x[0] + (x[-1] - x[0]) * k / (n - 1)
        i = min(max(bisect_right(x, t) - 1, 0), len(h) - 1)
        s = (t - x[i]) / h[i]
        xs.append(t)
        ys.append((2 * s**3 - 3 * s**2 + 1) * y[i] + (s**3 - 2 * s**2 + s) * h[i] * m[i]
                  + (3 * s**2 - 2 * s**3) * y[i + 1] + (s**3 - s**2) * h[i] * m[i + 1])
    return xs, ys


def end_slope(h0, h1, d0, d1):
    """Three-point end slope, limited so that the curve stays monotone."""
    m = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
    if m * d0 <= 0:
        return 0.0
    if d0 * d1 <= 0 and abs(m) > abs(3 * d0):
        return 3 * d0
    return m


def main():
    _, unit_label, decimals = UNITS[UNIT.lower()]
    shown = [q for q in CURVES if any(q in ds for ds in DATASETS)]
    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=FIGSIZE)
        ax.set_axisbelow(True)
        n_series = 0
        for k, ds in enumerate(DATASETS):
            color = COLORS[k % len(COLORS)]
            for j, q in enumerate(shown):
                x, y = curve(ds, q) if q in ds else ([], [])
                if not x:
                    continue
                if len(DATASETS) == 1:
                    label = SYMBOLS[q]
                elif len(shown) == 1:
                    label = ds["label"]
                else:
                    label = f"{ds['label']}, {SYMBOLS[q]}"
                sx, sy = pchip(x, y) if SMOOTH else (x, y)
                ax.plot(sx, sy, color=color, lw=1.6, ls=LINESTYLES[min(j, 2)], label=label,
                        solid_capstyle="round", dash_capstyle="round", zorder=2)
                primary = j == 0
                ax.plot(x, y, ls="none", marker="o", ms=5.5 if primary else 4.5,
                        mfc=color if primary else "white", mec="white" if primary else color,
                        mew=1.0 if primary else 1.2, zorder=3)
                n_series += 1
                if ANNOTATE_MAX and primary:
                    i = max(range(len(y)), key=y.__getitem__)
                    ax.annotate(f"{y[i]:.{decimals}f}", (x[i], y[i]), xytext=(0, 7),
                                textcoords="offset points", ha="center", va="bottom",
                                fontsize=8.5, color=INK_MUTED)
        if len(shown) == 1:
            ax.set_ylabel(f"{SYMBOLS[shown[0]]} / {unit_label}")
        else:
            ax.set_ylabel(f"Relative energy / {unit_label}")
        ax.set_xlabel(XLABEL)
        if TITLE:
            ax.set_title(TITLE, loc="left")
        ax.margins(x=0.04, y=0.14)
        ax.tick_params(length=3.5)
        if n_series > 1:
            ax.legend(loc="best", handlelength=2.6)
        fig.tight_layout()
        if OUTPUT:
            here = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
            out = Path(OUTPUT) if Path(OUTPUT).is_absolute() else here / OUTPUT
            fig.savefig(out)
            print(f"Saved {out}")
        if SHOW:
            plt.show()
        plt.close(fig)


if __name__ == "__main__":
    main()
'''


def _pystr(value):
    """Python literal for the generated script: double quotes, raw string for LaTeX."""
    if not isinstance(value, str):
        return repr(value)
    if "\\" in value and '"' not in value and not value.endswith("\\") and "\n" not in value:
        return f'r"{value}"'
    if '"' not in value and "\\" not in value and "\n" not in value:
        return f'"{value}"'
    return repr(value)


def _settings_block(settings, output):
    entries = [
        ("TITLE", _pystr(settings["title"]), "plot title ('': none)"),
        ("XLABEL", _pystr(settings["xlabel"]), ""),
        ("UNIT", _pystr(settings["unit"]), '"kcal/mol", "kJ/mol", "eV" or "Eh"'),
        ("CURVES", "[" + ", ".join(_pystr(c) for c in settings["curves"]) + "]",
         "quantities to draw: 1st solid, 2nd dashed, 3rd dotted"),
        ("REFERENCE", '"min"', '"min": every curve relative to its own minimum'),
        ("", "", '"first" / "last": relative to the first / last scan point'),
        ("", "", "12: relative to scan point 12"),
        ("SMOOTH", "True", "monotone cubic (PCHIP) curve through the points"),
        ("ANNOTATE_MAX", "True", "label the maximum of the first curve"),
        ("FIGSIZE", "(5.0, 3.5)", "width, height in inches"),
        ("OUTPUT", _pystr(output), ".png / .pdf / .svg next to this script; None: don't save"),
        ("SHOW", "True", "open a window"),
    ]
    lines = []
    for key, val, comment in entries:
        left = f"{key} = {val}" if key else ""
        left = left.ljust(30) if len(left) < 30 else left + "  "
        lines.append((left + (f"# {comment}" if comment else "")).rstrip())
    return "\n".join(lines)


def _format_datasets(datasets):
    lines = [PLOT_DATA_BEGIN, "DATASETS = ["]
    for ds in datasets:
        lines.append("    {")
        for key, val in ds.items():
            if not isinstance(val, list):
                lines.append(f"        {_pystr(key)}: {_pystr(val)},")
                continue
            items = ", ".join(_pystr(v) for v in val)
            if len(items) <= 72:
                lines.append(f"        {_pystr(key)}: [{items}],")
            else:
                lines.append(f"        {_pystr(key)}: [")
                lines.extend(textwrap.wrap(items + ",", width=96, initial_indent=" " * 12,
                                           subsequent_indent=" " * 12, break_on_hyphens=False,
                                           break_long_words=False))
                lines.append("        ],")
        lines.append("    },")
    lines += ["]", PLOT_DATA_END]
    return "\n".join(lines)


def read_plot_datasets(path):
    text = Path(path).read_text(encoding="utf-8")
    begin, end = text.find(PLOT_DATA_BEGIN), text.find(PLOT_DATA_END)
    if begin < 0 or end < begin:
        raise QCError(f"{path} has no qc data block - can only append to scripts "
                      f"written by 'qc scan --plot'.")
    block = text[begin + len(PLOT_DATA_BEGIN):end]
    try:
        datasets = ast.literal_eval(block.split("=", 1)[1].strip())
    except (ValueError, SyntaxError, IndexError) as e:
        raise QCError(f"cannot read the DATASETS block of {path}: {e}")
    return text, begin, end + len(PLOT_DATA_END), datasets


def plot_script(name, datasets, settings, provenance, output):
    fill = {
        "@VERSION@": __version__,
        "@STAMP@": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "@PROVENANCE@": "\n".join(provenance),
        "@FILENAME@": name,
        "@SETTINGS@": _settings_block(settings, output),
        "@DATA@": _format_datasets(datasets),
    }
    text = PLOT_TEMPLATE
    for key, val in fill.items():
        text = text.replace(key, val)
    return text


def render_script(source, output):
    """Run a plot script in-process (no window) and save the figure to 'output'."""
    try:
        import matplotlib
        matplotlib.use("Agg")
    except ImportError:
        raise QCError("matplotlib is required to render plots (pip install matplotlib).\n"
                      "Alternatively write a script with --plot scan.py and run it elsewhere.")
    namespace = {"__name__": "qc_plot"}
    exec(compile(source, "<qc plot>", "exec"), namespace)
    namespace["OUTPUT"] = str(Path(output).resolve())
    namespace["SHOW"] = False
    namespace["main"]()


def write_plot(target, dataset, settings, provenance, append=False):
    suffix = target.suffix.lower()
    if suffix in PLOT_IMAGE_TYPES:
        if append:
            raise QCError("--append works with plot scripts (.py), not with images.")
        source = plot_script(target.with_suffix(".py").name, [dataset], settings, provenance,
                             target.name)
        render_script(source, target)
        return
    if suffix != ".py":
        raise QCError(f"--plot: unknown file type '{target.suffix}' "
                      f"(use .py for a script or {', '.join(PLOT_IMAGE_TYPES[:3])} for an image).")
    if append and target.exists():
        text, start, stop, datasets = read_plot_datasets(target)
        coords = {ds.get("coordinate") for ds in datasets}
        if dataset["coordinate"] not in coords:
            warn(f"{target} plots {', '.join(sorted(map(str, coords)))}; the new scan is along "
                 f"{dataset['coordinate']} - check XLABEL.")
        labels = [ds.get("label") for ds in datasets]
        if dataset["label"] in labels:
            datasets[labels.index(dataset["label"])] = dataset
            action = "Replaced"
        else:
            datasets.append(dataset)
            action = "Added"
        write_text_atomic(target, text[:start] + _format_datasets(datasets) + text[stop:])
        print(f"{action} dataset '{dataset['label']}' in {target} ({len(datasets)} datasets)")
        return
    if append:
        note(f"{target} does not exist yet - creating it.")
    source = plot_script(target.name, [dataset], settings, provenance,
                         target.with_suffix(".png").name)
    write_text_atomic(target, source)
    print(f"Wrote {target}  (run it with: python3 {target})")


# ════════════════════════════════════════════════════════════════════════════
#  Data commands
# ════════════════════════════════════════════════════════════════════════════

def classify_csv(path):
    """Guess the role of a CSV from its header."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
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


def cmd_data_init(args):
    project = current_project()
    if project.data_file.exists():
        print(f"{project.data_file} already exists - nothing overwritten.")
        return
    project.data_file.write_text(DATA_TEMPLATE, encoding="utf-8")
    print(f"Created {project.data_file}.\n"
          f"Add sources with 'qc data add <role> <file.csv>' or 'qc data find'.")


def cmd_data_ls(args):
    project = current_project()
    cfg = load_data_config(project)
    data = load_dataset(project, cfg, require=False)
    heading(f"Data sources of '{project.name}' ({DATA_FILE})")
    rows = []
    for role in ROLES:
        entries = source_entries(cfg["sources"].get(role))
        files = []
        for e in entries:
            extra = ", ".join(x for x in (e["method"], e["solvent"]) if x)
            mark = "" if resolve_path(project, e["path"]).exists() else "  [missing]"
            files.append(e["path"] + (f" [{extra}]" if extra else "") + mark)
        if role in data:
            df = data[role]
            rows.append([role, len(df), ", ".join(str(m) for m in available_methods(df)),
                         ", ".join(available_solvents(df)) or "-", "; ".join(files)])
        else:
            rows.append([role, "-", "-", "-", "; ".join(files) or "(not configured)"])
    print_table(rows, header=["role", "rows", "functionals", "solvents", "files"],
                align="lrlll")
    if "thermo" not in data and "sp" not in data:
        print("\nNo thermo/sp data yet - add a source with 'qc data add' or 'qc data find'.")


def cmd_data_add(args):
    project = current_project()
    if args.role not in ROLES:
        raise QCError(f"unknown role '{args.role}' (valid: {', '.join(ROLES)}).")
    path = Path(args.file).expanduser()
    path = path if path.is_absolute() else (Path.cwd() / path)
    path = path.resolve()
    if not path.is_file():
        raise QCError(f"no such file: {path}")
    try:
        stored = str(path.relative_to(project.root.resolve()))
    except ValueError:
        stored = str(path)   # outside the project: store the absolute path
    guess = classify_csv(path)
    if guess and guess != args.role:
        warn(f"the header looks like a '{guess}' source rather than '{args.role}'.")

    raw = read_data_raw(project)
    if not isinstance(raw.get("sources"), dict):
        raw["sources"] = {}
    entries = [e for e in source_entries(raw["sources"].get(args.role)) if e["path"] != stored]
    entries.append({"path": stored, "method": args.functional, "solvent": args.solvent})
    raw["sources"][args.role] = _dump_entries(entries)
    write_data_raw(project, raw)
    extra = ", ".join(x for x in (args.functional, args.solvent) if x)
    print(f"Added {args.role} source {stored}" + (f" [{extra}]" if extra else ""))
    try:
        archive_project(project, load_data_config(project), roles=[args.role], quiet=True)
    except QCError:
        pass


def cmd_data_rm(args):
    project = current_project()
    if args.role not in ROLES:
        raise QCError(f"unknown role '{args.role}' (valid: {', '.join(ROLES)}).")
    raw = read_data_raw(project)
    sources = raw.get("sources") or {}
    entries = source_entries(sources.get(args.role))
    if not entries:
        raise QCError(f"no {args.role} source configured.")
    if args.file is None:
        sources[args.role] = None
        write_data_raw(project, raw)
        print(f"Removed all {args.role} sources.")
        return
    keep = [e for e in entries if e["path"] != args.file and Path(e["path"]).name != args.file]
    if len(keep) == len(entries):
        raise QCError(f"'{args.file}' is not a {args.role} source.\n"
                      f"Configured: {', '.join(e['path'] for e in entries)}")
    sources[args.role] = _dump_entries(keep)
    write_data_raw(project, raw)
    print(f"Removed {args.file} from the {args.role} sources.")


def cmd_data_find(args):
    project = current_project()
    root = project.root.resolve()
    cfg = load_data_config(project) if project.data_file.exists() else effective_config({})
    registered = {str(resolve_path(project, e["path"]).resolve())
                  for role in ROLES for e in source_entries(cfg["sources"].get(role))}
    found = []
    for path in sorted(root.rglob("*.csv")):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        role = classify_csv(path)
        if role:
            found.append((role, str(rel), str(path.resolve()) in registered))
    heading(f"Candidate CSV files in '{project.name}'")
    if not found:
        print("  (none) - create them first, e.g. with outorca in the calculation folders.")
        return
    found.sort(key=lambda t: (ROLES.index(t[0]), t[1]))
    print_table([[role, rel, "registered" if reg else "new"] for role, rel, reg in found])
    new = [(role, rel) for role, rel, reg in found if not reg]
    if not args.write:
        if new:
            print("\nAdd one with 'qc data add <role> <file>' or all new ones with "
                  "'qc data find --write'.")
        return
    if not new:
        print("\nNothing new to add.")
        return
    if not project.data_file.exists():
        project.data_file.write_text(DATA_TEMPLATE, encoding="utf-8")
    raw = read_data_raw(project)
    if not isinstance(raw.get("sources"), dict):
        raw["sources"] = {}
    for role, rel in new:
        entries = source_entries(raw["sources"].get(role))
        entries.append({"path": rel, "method": None, "solvent": None})
        raw["sources"][role] = _dump_entries(entries)
    write_data_raw(project, raw)
    print(f"\nAdded {len(new)} source(s) to {DATA_FILE}.")


def cmd_data_check(args):
    project = current_project()
    cfg = load_data_config(project)
    cmd_data_ls(args)
    data = load_dataset(project, cfg)
    print()
    heading("Checks")
    before = len(_WARNINGS_SEEN)

    # convergence and character of every row
    for role, df in data.items():
        for _, row in df.iterrows():
            row_warnings(row, row["BASENAME"], role, cfg["checks"])
            if role == "boltz":
                boltz_warnings(row, row["BASENAME"])

    # duplicates: same species, multiplicity, functional and solvent
    for role, df in data.items():
        keys = ["BASENAME", "METHOD", "SOLVENT"] + (["M"] if "M" in df.columns else [])
        dup = df[df.duplicated(subset=keys, keep=False)]
        for base in sorted(dup["BASENAME"].unique()):
            warn(f"duplicate entries in {role}: {base}")

    # coverage of thermo and of every solvent
    er = elec_role(data)
    species = set(data[er]["BASENAME"])

    def report(what, missing):
        if missing:
            names = sorted(missing)
            more = f" (+{len(names) - 8} more)" if len(names) > 8 else ""
            warn(f"{what} missing for {len(names)} species: {', '.join(names[:8])}{more}")

    if er == "sp" and "thermo" in data:
        report("thermo", species - set(data["thermo"]["BASENAME"]))
    if "solv" in data:
        df = data["solv"]
        for solvent in available_solvents(df) or [None]:
            sub = df if solvent is None else df[df["SOLVENT"] == solvent]
            report(f"Gsolv ({solvent})" if solvent else "Gsolv",
                   species - set(sub["BASENAME"]))

    # solvent names of boltz that the solv source does not know (spelling differs?)
    if "boltz" in data and "solv" in data:
        known = set(available_solvents(data["solv"]))
        for solvent in available_solvents(data["boltz"]):
            if solvent not in known:
                warn(f"boltz solvent '{solvent}' does not occur in the solv source "
                     f"({', '.join(sorted(known)) or '-'}) - add it to 'solvent_aliases'?")

    # the species name determines charge and multiplicity
    for col, what in (("CHRG", "charges"), ("M", "multiplicities")):
        values = {}
        for df in data.values():
            given = df[col].notna()
            for base, v in zip(df["BASENAME"][given], df[col][given]):
                values.setdefault(base, set()).add(int(v))
        for base, vs in sorted(values.items()):
            if len(vs) > 1:
                warn(f"{base} appears with different {what} ({', '.join(map(str, sorted(vs)))})"
                     f" - rows without {col} cannot be assigned.")

    n = len(_WARNINGS_SEEN) - before
    print(f"  {n} warning(s)." if n else "  All checks passed.")


def cmd_data_edit(args):
    project = current_project()
    if not project.data_file.exists():
        project.data_file.write_text(DATA_TEMPLATE, encoding="utf-8")
    return open_in_editor(project.data_file)


# ── archive (~/.qc_data) ──────────────────────────────────────────────────────

ARCHIVE_KEYS = ["PROJECT", "BASENAME", "M", "CHRG", "METHOD", "SOLVENT"]


def archive_path(project_name, role):
    """~/.qc_data/<project>_main_<role>.csv"""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(project_name))
    return ARCHIVE_DIR / f"{safe}_main_{role}.csv"


def _merge_into(target, df, subset):
    """Merge df into the CSV 'target'; for identical keys the new row wins."""
    import pandas as pd
    merged = df
    if target.exists():
        try:
            old = pd.read_csv(target, sep=";")
            old.columns = [str(c).strip() for c in old.columns]
            if "BASENAME" in old.columns:   # rows archived before CHRG/M were filled by name
                fill_charge_mult(old, charge_mult_by_name([df]))
            merged = pd.concat([old, df], ignore_index=True)
            merged = merged.drop_duplicates(subset=subset, keep="last")
        except Exception as e:
            warn(f"{target} is unreadable ({e}) - rewriting it.")
    sort_cols = [c for c in ("PROJECT", "BASENAME", "METHOD", "SOLVENT") if c in merged.columns]
    if sort_cols:
        merged = merged.sort_values(by=sort_cols)
    merged.to_csv(target, sep=";", index=False)
    return len(merged)


def archive_project(project, cfg, roles=None, quiet=False):
    """
    Collect all data of a project per role into ~/.qc_data/<project>_main_<role>.csv.
    Key: BASENAME + M + CHRG + METHOD + SOLVENT - every combination appears once,
    re-reading overwrites the old values; entries whose source CSV is gone are kept.
    """
    data = load_dataset(project, cfg)
    ARCHIVE_DIR.mkdir(exist_ok=True)
    for role in roles or ROLES:
        if role not in data:
            continue
        df = data[role].copy()
        df.insert(0, "PROJECT", project.name)
        for col in ARCHIVE_KEYS:
            if col not in df.columns:
                df[col] = None
        target = archive_path(project.name, role)
        n = _merge_into(target, df, [k for k in ARCHIVE_KEYS if k != "PROJECT"])
        if not quiet:
            print(f"  {role:<7} {target}  ({n} rows)")


def cmd_data_archive(args):
    if args.list:
        return archive_list()
    if args.all:
        reg = load_registry()
        for name, path in sorted(reg["projects"].items()):
            project = Project(name, path, here=False)
            if not project.data_file.exists():
                continue
            heading(name)
            try:
                archive_project(project, load_data_config(project))
            except QCError as e:
                warn(f"{name} skipped: {str(e).splitlines()[0]}")
        return
    project = current_project()
    heading(f"Archiving '{project.name}' to {ARCHIVE_DIR}")
    archive_project(project, load_data_config(project))


def archive_list():
    files = sorted(ARCHIVE_DIR.glob("*_main_*.csv")) if ARCHIVE_DIR.exists() else []
    if not files:
        print(f"No archive files in {ARCHIVE_DIR} yet ('qc data archive' creates them).")
        return
    try:
        import pandas as pd
    except ImportError:
        pd = None
    heading(f"Archive {ARCHIVE_DIR}")
    rows = []
    for f in files:
        project, _, role = f.stem.rpartition("_main_")
        if role not in ROLES:
            continue
        if pd is None:
            rows.append([project, role, "?", "", ""])
            continue
        try:
            df = pd.read_csv(f, sep=";")
        except Exception:
            rows.append([project, role, "unreadable", "", ""])
            continue
        meths = sorted({str(x) for x in df.get("METHOD", []) if str(x) != "nan"})
        solvents = sorted({str(x) for x in df.get("SOLVENT", []) if str(x) != "nan"})
        rows.append([project, role, len(df), ", ".join(meths) or "-", ", ".join(solvents) or "-"])
    print_table(rows, header=["project", "role", "rows", "functionals", "solvents"], align="llr")


# ════════════════════════════════════════════════════════════════════════════
#  Shell integration & completion
# ════════════════════════════════════════════════════════════════════════════

SHELL_TEMPLATE = r'''# qc shell integration (bash/zsh): cd for 'qc go', tab completion
unalias qc 2>/dev/null
_qc_exec() { @EXEC@ "$@"; }
qc() {
    if [ "$1" = "go" ] && [ "$#" -ge 2 ] && [ "${2#-}" = "$2" ]; then
        local _qc_dir
        _qc_dir="$(QC_SHELL=1 _qc_exec go "$2")" && cd -- "$_qc_dir"
    else
        _qc_exec "$@"
    fi
}
_qc_complete() {
    local IFS=$'\n'
    COMPREPLY=($(_qc_exec __complete "$COMP_CWORD" "${COMP_WORDS[@]}" 2>/dev/null))
}
if [ -n "$ZSH_VERSION" ]; then
    autoload -U +X bashcompinit 2>/dev/null && bashcompinit
fi
complete -o default -F _qc_complete qc
'''


def cmd_shellrc(args):
    exe = f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}"
    print(SHELL_TEMPLATE.replace("@EXEC@", exe).rstrip())


def _complete_species(include_scans=False):
    """Species names from the data sources, read cheaply without pandas."""
    project = find_project()
    if not project or not project.data_file.exists():
        return []
    cfg = load_data_config(project)
    strip = set(cfg["strip_suffixes"])
    names = set()
    for role in ROLES:
        for e in source_entries(cfg["sources"].get(role)):
            path = resolve_path(project, e["path"])
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for row in csv.DictReader(f, delimiter=";"):
                        n = (row.get("NAME") or "").strip()
                        if n:
                            names.add(make_basename(n, strip))
            except OSError:
                continue
    if include_scans:
        return sorted({n.rpartition(".")[0] for n in names
                       if n.rpartition(".")[2].isdigit() and "." in n})
    return sorted(names)


def complete_words(cword, words):
    """Candidates for word number cword of the command line 'words' (bash COMP_WORDS)."""
    words = list(words[1:])
    i = cword - 1
    cur = words[i] if 0 <= i < len(words) else ""
    prev = words[:max(i, 0)]
    print_only = False
    while prev and prev[0] in ("-p", "--print"):
        prev, print_only = prev[1:], True
    project = find_project()
    cmds = project.load_commands() if project else {"commands": {}, "paths": {}}

    def flags(command):
        parser = build_parser()
        sub = parser._qc_subparsers.get(command)
        return sorted(o for o in (sub._option_string_actions if sub else {}) if o.startswith("--")
                      or len(o) == 2)

    if not prev:
        cands = list(cmds["commands"]) + ([] if print_only else list(COMMAND_NAMES))
        if cur.startswith("-"):
            cands = ["-p", "--print", "-h", "--help", "--version"]
    else:
        command, args = prev[0], prev[1:]
        positional = [a for a in args if not a.startswith("-")]
        if command in cmds["commands"] and command not in COMMAND_NAMES:
            return []
        if cur.startswith("-") and command in COMMAND_NAMES:
            return [c for c in flags(command) if c.startswith(cur)]
        cands = []
        if command == "help" and not args:
            cands = list(COMMAND_NAMES)
        elif command in GROUP_VERBS and not positional:
            cands = list(GROUP_VERBS[command])
            if command == "rxn" and project and project.data_file.exists():
                cands += list(load_data_config(project)["reactions"])
        elif command == "go" and not positional:
            cands = list(cmds["paths"]) + ["root"]
        elif command == "mark" and positional == ["rm"]:
            cands = list(cmds["paths"])
        elif command == "cmd" and positional == ["rm"]:
            cands = list(cmds["commands"])
        elif command == "project" and positional in (["rm"], ["switch"]):
            cands = list(load_registry()["projects"])
        elif command == "data" and positional in (["add"], ["rm"]):
            cands = list(ROLES)
        elif command == "rxn" and positional == ["rm"] and project:
            cands = list(load_data_config(project)["reactions"])
        elif command in ("e", "u", "h", "g"):
            cands = _complete_species()
        elif command == "scan":
            cands = _complete_species(include_scans=True) if not positional else (
                ["B", "A", "D"] if len(positional) == 1 else [])
    return sorted({c for c in cands if c.startswith(cur)})


def cmd_complete(args):
    try:
        for c in complete_words(int(args.cword), args.words):
            print(c)
    except Exception:
        pass   # completion must never print errors


# ════════════════════════════════════════════════════════════════════════════
#  Command line
# ════════════════════════════════════════════════════════════════════════════

OVERVIEW = f"""\
qc {__version__} - project manager and analysis tool for quantum-chemical calculations

usage: qc <command> [options]          details: qc <command> -h
       qc [-p] <shortcut> [args...]

Projects
  init <name>                    create a project folder with skeleton and register it
  project [ls|add|rm|switch]     manage the registered projects
  status                         show the current project

Shortcuts (commands.json)
  <shortcut> [args...]           run a shortcut, args are appended (-p: only print it)
  cmd [ls|add|rm|edit]           manage the shortcuts
  history [n]                    last shortcut runs

Bookmarks
  mark [ls|add|rm]               manage bookmarked directories
  go <mark>|root                 cd to a bookmark (needs the shell integration)

Data (data.yaml)
  data [ls|init|add|rm|find|check|edit|archive]
                                 manage the CSV sources

Thermochemistry
  species [pattern]              species and their data coverage
  e|u|h|g <species>...           absolute E, U, H or G
  rxn "<a + 2 b -> c>"|<name>    reaction energy
  rxn [ls|add|rm]                manage named reactions

PES scans
  scan [<prefix> <B i j | A i j k | D i j k l>]
                                 relaxed scan: table, CSV, plot

Setup
  shellrc                        shell integration; put  eval "$(qc shellrc)"  in ~/.bashrc
"""

THERMO_EPILOG = """\
energies:
  E    electronic energy (sp source if configured, else ENERGY of thermo)
  U/H  E + dU/dH of the thermo source
  G    E + dG of the thermo source, or of the bfreq source with --bfreq
  optional terms: + Gsolv (--solv), + G_AV - G_LOW (--boltz),
                  + RT ln(c°RT/p°) for 1 atm -> 1 mol/L (--sc, G only)
"""


class _Formatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=30, width=96)


def add_thermo_args(p, quantity_default=None, scan=False):
    g = p.add_argument_group("thermochemistry options")
    if quantity_default:
        g.add_argument("-q", "--quantity", type=str.upper, choices=["E", "U", "H", "G"],
                       default=quantity_default, metavar="Q",
                       help=("shown next to E: G (default), H or U; E: only E" if scan
                             else f"E, U, H or G (default: {quantity_default})"))
    g.add_argument("-u", "--unit", metavar="UNIT",
                   help="Eh, kcal/mol, kJ/mol or eV (default: "
                        + ("Eh" if not quantity_default else f"'unit' in {DATA_FILE}") + ")")
    g.add_argument("-m", "--mult", type=int, metavar="M",
                   help="multiplicity (or per species: name@M)")
    g.add_argument("-f", "--functional", metavar="F", help="functional of the electronic energy")
    g.add_argument("--ft", metavar="F", help="functional of the thermal correction")
    g.add_argument("--fsolv", metavar="F", help="method of the solvation data")
    g.add_argument("--fboltz", metavar="F", help="method of the ensemble data")
    g.add_argument("--fbfreq", metavar="F", help="method of the bfreq data")
    solv = g.add_mutually_exclusive_group()
    solv.add_argument("--solv", dest="solv", action="store_const", const=True, default=None,
                      help="add the solvation free energy Gsolv")
    solv.add_argument("--gas", dest="solv", action="store_const", const=False,
                      help="gas phase (overrides 'solvation: true')")
    g.add_argument("-s", "--solvent", metavar="NAME", help="solvent (implies --solv)")
    boltz = g.add_mutually_exclusive_group()
    boltz.add_argument("--boltz", dest="boltz", action="store_const", const=True, default=None,
                       help="add the conformer-ensemble correction G_AV - G_LOW")
    boltz.add_argument("--no-boltz", dest="boltz", action="store_const", const=False,
                       help=argparse.SUPPRESS)
    bfreq = g.add_mutually_exclusive_group()
    bfreq.add_argument("--bfreq", dest="bfreq", action="store_const", const=True, default=None,
                       help="thermal correction to G from the bfreq source"
                            + (" (default for scans if available)" if scan else ""))
    bfreq.add_argument("--no-bfreq", dest="bfreq", action="store_const", const=False,
                       help="thermal correction from the thermo source")
    sc = g.add_mutually_exclusive_group()
    sc.add_argument("--sc", dest="sc", action="store_const", const=True, default=None,
                    help="standard-state correction 1 atm -> 1 mol/L (G only)")
    sc.add_argument("--no-sc", dest="sc", action="store_const", const=False,
                    help=argparse.SUPPRESS)
    g.add_argument("-v", "--verbose", action="store_true", help="show all contributions")


def build_parser():
    parser = argparse.ArgumentParser(prog="qc", add_help=False, allow_abbrev=False,
                                     formatter_class=_Formatter)
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    subs = {}

    def command(name, help_, description=None, epilog=None, **kw):
        p = sub.add_parser(name, help=help_, description=description or help_[:1].upper()
                           + help_[1:] + ".", epilog=epilog, formatter_class=_Formatter,
                           allow_abbrev=False, **kw)
        subs[name] = p
        return p

    def group(p, title):
        verbs = p.add_subparsers(dest="verb", metavar="<action>", title=title)
        return lambda name, help_: verbs.add_parser(name, help=help_, description=help_[:1].upper()
                                                    + help_[1:] + ".", formatter_class=_Formatter,
                                                    allow_abbrev=False)

    # projects
    p = command("init", "create a project folder with skeleton and register it",
                epilog=f"Creates <name>/ with {', '.join(PROJECT_FOLDERS)},\n"
                       f"{COMMANDS_FILE} (default shortcuts) and {DATA_FILE} (template).")
    p.add_argument("name")
    p.set_defaults(func=cmd_init)

    p = command("project", "manage the registered projects",
                description="Manage the registered projects. Without an action: list them.\n"
                            "Inside a project folder qc uses that project, elsewhere the "
                            "active one.")
    p.set_defaults(group_default=cmd_project_ls)
    verb = group(p, "actions")
    verb("ls", "list the projects").set_defaults(func=cmd_project_ls)
    v = verb("add", "register an existing folder as project (and make it active)")
    v.add_argument("name", nargs="?", help="project name (default: folder name)")
    v.add_argument("--path", metavar="DIR", help="folder (default: current directory)")
    v.set_defaults(func=cmd_project_add)
    v = verb("rm", "unregister a project (its files are kept)")
    v.add_argument("name")
    v.set_defaults(func=cmd_project_rm)
    v = verb("switch", "set the active project (used outside of project folders)")
    v.add_argument("name")
    v.set_defaults(func=cmd_project_switch)

    command("status", "show the current project").set_defaults(func=cmd_status)

    # shortcuts
    p = command("cmd", "manage the shortcuts of the project",
                description=f"Manage the command shortcuts in {COMMANDS_FILE}. Without an "
                            f"action: list them.\nRun one with 'qc <shortcut> [args...]'; the "
                            f"args are appended.\n'qc -p <shortcut> [args...]' only prints the "
                            f"command.")
    p.set_defaults(group_default=cmd_cmd_ls)
    verb = group(p, "actions")
    verb("ls", "list the shortcuts").set_defaults(func=cmd_cmd_ls)
    v = verb("add", "add or replace a shortcut")
    v.add_argument("name")
    v.add_argument("command", nargs=argparse.REMAINDER, help="the command (quoted or not)")
    v.epilog = 'example: qc cmd add sp "inporca -j sp -f r2scan0 -bs def2-QZVPPD"'
    v.set_defaults(func=cmd_cmd_add)
    v = verb("rm", "remove a shortcut")
    v.add_argument("name")
    v.set_defaults(func=cmd_cmd_rm)
    verb("edit", f"open {COMMANDS_FILE} in $EDITOR").set_defaults(func=cmd_cmd_edit)

    p = command("history", "show the last shortcut runs")
    p.add_argument("n", nargs="?", type=int, default=15, help="number of entries (default: 15)")
    p.set_defaults(func=cmd_history)

    # bookmarks
    p = command("mark", "manage bookmarked directories",
                description="Bookmark directories of the project. Without an action: list "
                            "them.\nJump to one with 'qc go <name>'.")
    p.set_defaults(group_default=cmd_mark_ls)
    verb = group(p, "actions")
    verb("ls", "list the bookmarks").set_defaults(func=cmd_mark_ls)
    v = verb("add", "bookmark a directory")
    v.add_argument("name")
    v.add_argument("dir", nargs="?", help="directory (default: current directory)")
    v.set_defaults(func=cmd_mark_add)
    v = verb("rm", "remove a bookmark")
    v.add_argument("name")
    v.set_defaults(func=cmd_mark_rm)

    p = command("go", "cd to a bookmark or to the project root",
                description="cd to a bookmark ('root': the project root). Without the shell "
                            "integration\nthis only prints the path; set it up with:\n"
                            '  eval "$(qc shellrc)"   (in ~/.bashrc or ~/.zshrc)')
    p.add_argument("name", nargs="?", help="bookmark or 'root' (none: list the bookmarks)")
    p.set_defaults(func=cmd_go)

    # data
    p = command("data", "manage the CSV data sources",
                description=f"Manage the data sources in {DATA_FILE}. Without an action: "
                            f"show them.",
                epilog="roles:\n" + "\n".join(f"  {r:<7} {ROLE_INFO[r]}" for r in ROLES))
    p.set_defaults(group_default=cmd_data_ls)
    verb = group(p, "actions")
    verb("ls", "show the data sources").set_defaults(func=cmd_data_ls)
    verb("init", f"create a {DATA_FILE} template").set_defaults(func=cmd_data_init)
    v = verb("add", "add a CSV file as source of a role")
    v.add_argument("role", help=", ".join(ROLES))
    v.add_argument("file")
    v.add_argument("-f", "--functional", metavar="F",
                   help="functional of the file (overrides its METHOD column)")
    v.add_argument("-s", "--solvent", metavar="NAME",
                   help="solvent of the file (overrides its SOLVENT column)")
    v.epilog = "roles:\n" + "\n".join(f"  {r:<7} {ROLE_INFO[r]}" for r in ROLES)
    v.set_defaults(func=cmd_data_add)
    v = verb("rm", "remove a source (or all sources of a role)")
    v.add_argument("role", help=", ".join(ROLES))
    v.add_argument("file", nargs="?", help="path or file name (default: all of the role)")
    v.set_defaults(func=cmd_data_rm)
    v = verb("find", "find CSV files in the project and guess their role")
    v.add_argument("-w", "--write", action="store_true",
                   help=f"add all new files to {DATA_FILE}")
    v.set_defaults(func=cmd_data_find)
    verb("check", "check convergence, duplicates, coverage, charges and multiplicities"
         ).set_defaults(func=cmd_data_check)
    verb("edit", f"open {DATA_FILE} in $EDITOR").set_defaults(func=cmd_data_edit)
    v = verb("archive", "collect the data in ~/.qc_data/<project>_main_<role>.csv")
    v.description = ("Collect all data of the project per role in\n"
                     "~/.qc_data/<project>_main_<role>.csv (updated on every 'qc data add').\n"
                     "Rows are keyed by species, M, charge, functional and solvent; new values "
                     "replace old ones,\nrows whose source is gone are kept.")
    v.add_argument("-a", "--all", action="store_true", help="archive all registered projects")
    v.add_argument("-l", "--list", action="store_true", help="summarize the archive files")
    v.set_defaults(func=cmd_data_archive)

    # thermochemistry
    p = command("species", "list the species and their data coverage",
                description="List the species with one column per source, functional and "
                            "solvent\n(x: data available).")
    p.add_argument("pattern", nargs="?", help="filter, e.g. 'ni*' (plain text: substring)")
    p.set_defaults(func=cmd_species)

    for q, name in (("e", "electronic energy E"), ("u", "internal energy U"),
                    ("h", "enthalpy H"), ("g", "free energy G")):
        p = command(q, f"absolute {name} of species", epilog=THERMO_EPILOG)
        add_energy_args(p)
        add_thermo_args(p)
        p.set_defaults(func=cmd_energy)

    p = command("rxn", "reaction energies and named reactions",
                usage='qc rxn "<a + 2 b -> c>" | <name>  [options]\n'
                      '       qc rxn [ls | add <name> "<equation>" | rm <name>]',
                description="Reaction energy from an equation or a named reaction "
                            f"(stored in {DATA_FILE}).\nWithout arguments: list the named "
                            f"reactions. Species are separated by '+', coefficients\nprecede "
                            f"them ('2 b'), 'name@M' selects a multiplicity.",
                epilog=THERMO_EPILOG + "\nexamples:\n"
                       '  qc rxn "ni_h -> ni + h" -s thf --sc -v\n'
                       '  qc rxn add deprot "ni_h -> ni + h"\n'
                       "  qc rxn deprot -q H")
    p.add_argument("words", nargs="*", help=argparse.SUPPRESS)
    add_thermo_args(p, quantity_default="G")
    p.set_defaults(func=cmd_rxn)

    p = command("scan", "evaluate a relaxed PES scan",
                usage="qc scan [<prefix> <B i j | A i j k | D i j k l>] [options]",
                description="Relaxed PES scan over the species <prefix>.<number>. The scan "
                            "coordinate is computed from\nthe geometries ('geometries' in "
                            f"{DATA_FILE}). Without a prefix: list the scans.\n\n"
                            "By default E and G are shown; G uses the bfreq (single-point "
                            "Hessian) correction\nif it covers the scan, otherwise the thermo "
                            "correction. Energies are relative to\nthe minimum of each curve.",
                epilog="plots:\n"
                       "  --plot scan.py    editable matplotlib script with the data embedded\n"
                       "  --plot scan.pdf   render directly (.png, .pdf, .svg)\n"
                       "  --plot scan.py --append   add this scan to an existing script\n\n"
                       "examples:\n"
                       "  qc scan ni_br B 1 2 --plot ni_br.py\n"
                       "  qc scan pd_br B 1 2 --plot ni_br.py --append --label Pd\n"
                       "  qc scan rot D 1 2 3 4 -q E --csv rot.csv")
    p.add_argument("prefix", nargs="?", help=argparse.SUPPRESS)
    p.add_argument("coordinate", nargs="*", help=argparse.SUPPRESS)
    g = p.add_argument_group("scan options")
    g.add_argument("-0", "--zero-based", action="store_true",
                   help="atom numbers start at 0 (as in ORCA input); default: 1")
    g.add_argument("--plot", action="append", metavar="FILE",
                   help="write a plot script (.py) or image (.png/.pdf/.svg); repeatable")
    g.add_argument("--append", action="store_true", help="add to an existing plot script")
    g.add_argument("--label", metavar="TEXT", help="name of this scan in the plot (default: prefix)")
    g.add_argument("--title", metavar="TEXT", help="plot title")
    g.add_argument("--csv", metavar="FILE", help="export the table as ';'-separated CSV")
    add_thermo_args(p, quantity_default="G", scan=True)
    p.set_defaults(func=cmd_scan)

    # setup
    command("shellrc", "print the shell integration (cd for 'qc go', completion)",
            description='Print the shell integration for bash and zsh. Add to ~/.bashrc or '
                        '~/.zshrc:\n  eval "$(qc shellrc)"').set_defaults(func=cmd_shellrc)
    p = command("__complete", argparse.SUPPRESS)
    p.add_argument("cword")
    p.add_argument("words", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_complete)

    parser._qc_subparsers = subs
    return parser


COMMAND_NAMES = ("init", "project", "status", "cmd", "history", "mark", "go", "data",
                 "species", "e", "u", "h", "g", "rxn", "scan", "shellrc", "help")
GROUP_VERBS = {
    "project": ("ls", "add", "rm", "switch"),
    "cmd": ("ls", "add", "rm", "edit"),
    "mark": ("ls", "add", "rm"),
    "data": ("ls", "init", "add", "rm", "find", "check", "edit", "archive"),
    "rxn": ("ls", "add", "rm"),
}
RESERVED = set(COMMAND_NAMES) | {"__complete"}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    print_only = False
    while argv and argv[0] in ("-p", "--print"):
        print_only = True
        argv.pop(0)
    if not argv or argv[0] in ("-h", "--help"):
        print(OVERVIEW.rstrip())
        return 0
    if argv[0] == "--version":
        print(f"qc {__version__}")
        return 0
    if argv[0] == "help":
        if len(argv) > 1 and argv[1] in COMMAND_NAMES and argv[1] != "help":
            argv = [argv[1], "-h"]
        else:
            print(OVERVIEW.rstrip())
            return 0

    name = argv[0]
    if name not in RESERVED:
        if name.startswith("-"):
            raise QCError(f"unknown option '{name}'. Run 'qc -h' for help.")
        return run_shortcut(name, argv[1:], print_only)
    if print_only:
        raise QCError("-p only applies to shortcuts: qc -p <shortcut> [args...]")

    parser = build_parser()
    if name in GROUP_VERBS and name != "rxn" or name == "__complete":
        args = parser.parse_args(argv)
    else:   # options may come anywhere, e.g. qc g a -f pwpb95 b
        args = parser._qc_subparsers[name].parse_intermixed_args(argv[1:])
        args.command = name
    func = getattr(args, "func", None) or getattr(args, "group_default")
    return func(args) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except QCError as e:
        print(f"{style('error:', '31;1', sys.stderr)} {e}", file=sys.stderr)
        sys.exit(1)
    except BrokenPipeError:   # e.g. 'qc species | head'
        try:
            sys.stdout.close()
        finally:
            os._exit(0)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
