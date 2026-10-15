# qc

Project manager and analysis tool for quantum-chemical calculations.

- **Projects**: one registry of calculation projects; inside a project folder qc
  knows which project you are in.
- **Shortcuts**: per-project command templates (`commands.json`), e.g. your ORCA
  input generator with fixed settings.
- **Bookmarks**: jump between calculation folders with `qc go <name>`.
- **Thermochemistry**: absolute E/U/H/G and reaction energies assembled from the
  CSV summaries of your calculations (electronic energy, thermal correction,
  solvation, conformer ensemble, standard state), with consistency checks.
- **PES scans**: relaxed scans as table, CSV and publication-ready plots.

Everything lives in the single file `qc.py`.

## Installation

Requirements: Python ≥ 3.8, `pandas` and `PyYAML` (everything that reads data),
`matplotlib` (plots). Optional: `ruamel.yaml`, which keeps the comments in
`data.yaml` when qc edits it.

```bash
pip install --user pandas pyyaml matplotlib ruamel.yaml
chmod +x ~/qc/qc.py
ln -s ~/qc/qc.py ~/bin/qc          # any directory in your $PATH
```

Shell integration (`cd` for `qc go`, tab completion for commands, shortcuts,
bookmarks, species, scans and options); add to `~/.bashrc` or `~/.zshrc`:

```bash
eval "$(qc shellrc)"
```

## Quick start

```bash
qc init nibr                       # folder skeleton, commands.json, data.yaml
cd nibr/1_optfreq
qc geomopt mol.xyz                 # run a shortcut, args are appended
qc -p sp mol.xyz                   # only print the command
qc mark add opt                    # bookmark this folder; later: qc go opt
qc go root                         # back to the project root

qc data find                       # CSV files in the project and their guessed role
qc data add thermo 1_optfreq/outorca.csv
qc data add sp 2_sp/pwpb95/outorca.csv -f pwpb95
qc data check                      # convergence, duplicates, coverage, charges

qc species                         # which data exist for which species
qc g ni_h -f pwpb95 -v             # G with all contributions
qc rxn "ni_h -> ni + h" -f pwpb95 -s thf --sc
qc scan ni_br B 1 2 --plot ni_br.py
```

## Commands

`qc` prints an overview, `qc <command> -h` the details of a command.

| Command | Purpose |
|---|---|
| `init <name>` | create a project folder with skeleton and register it |
| `project [ls\|add\|rm\|switch]` | manage the registered projects |
| `status` | current project, shortcuts, bookmarks, data sources |
| `<shortcut> [args…]` | run a shortcut; `qc -p <shortcut>` only prints it |
| `cmd [ls\|add\|rm\|edit]` | manage the shortcuts |
| `history [n]` | last shortcut runs |
| `mark [ls\|add\|rm]` | manage bookmarked directories |
| `go <mark>\|root` | cd to a bookmark or the project root |
| `data [ls\|init\|add\|rm\|find\|check\|edit\|archive]` | manage the CSV sources |
| `species [pattern]` | species × (source, functional, solvent) coverage table |
| `e\|u\|h\|g <species>…` | absolute energies (default unit: Eh) |
| `rxn "<a + 2 b -> c>"\|<name>` | reaction energy |
| `rxn [ls\|add\|rm]` | manage named reactions |
| `scan [<prefix> <coordinate>]` | evaluate a relaxed PES scan |
| `shellrc` | print the shell integration |

Outside of a project folder, qc uses the *active* project (the one last created,
added or selected with `qc project switch`).

## Data: `data.yaml`

Every project has a `data.yaml` that lists the CSV files (`;`-separated, one row
per calculation) and the settings for thermochemistry. `qc init` and `qc data init`
write a commented template; `qc data add` / `qc data rm` / `qc data find --write`
edit it.

| Role | Required columns | Content |
|---|---|---|
| `thermo` | `NAME`, `ENERGY`, `dU`, `dH`, `dG` | frequency jobs (e.g. optfreq) |
| `sp` | `NAME`, `ENERGY` | single points; several functionals allowed |
| `solv` | `NAME`, `Gsolv(Eh)` | solvation free energies; several solvents allowed |
| `boltz` | `NAME`, `G_LOW`, `G_AV` | conformer ensembles (CENSO) |
| `bfreq` | `NAME`, `dG(Eh)` | single-point (biased) Hessian thermal correction |

Optional columns: `CHRG`/`CHARGE`, `M`/`MULTIPLICITY`, `METHOD`, `SOLVENT`,
`SCFCONV`, `CPSCF/GEOCONV`, `CHAR`, `IMAGS`, and for `boltz` `dG_AV`, `STATUS`,
`FAILED_CALC_COUNT`. All energies are in Hartree.

The species name is `NAME` without the parts listed in `strip_suffixes`
(`a.oet2.optfreq` → `a.oet2`). `name@M` selects a multiplicity.

Charge and multiplicity belong to the species name: rows without `CHRG`/`M`
(typically the `solv` and `bfreq` files) take them from the other sources of the
same name. `qc data check` reports names that appear with different charges or
multiplicities.

`qc data archive` collects all data of a project per role in
`~/.qc_data/<project>_main_<role>.csv` (also updated on every `qc data add`); rows
whose source CSV has been deleted are kept.

## How energies are assembled

```
X = E + thermal + Gsolv + ensemble + standard state        (X = E, U, H or G)
```

| Term | Source | Included |
|---|---|---|
| `E` | `ENERGY` of `sp` (if configured), else of `thermo` | always |
| thermal | `dU`/`dH`/`dG` of `thermo`, or `dG(Eh)` of `bfreq` | for U, H, G; `bfreq` with `--bfreq` |
| `Gsolv` | `solv` | `--solv` or `-s <solvent>` |
| ensemble | `G_AV − G_LOW` of `boltz` | `--boltz` |
| standard state | RT ln(c°RT/p°), 1 atm → 1 mol/L (+1.89 kcal/mol at 298.15 K) | G only, `--sc` |

Defaults for all switches can be set in the `defaults:` section of `data.yaml`.
`-v` shows every contribution; for reactions it also shows the reaction energy
split by contribution.

Consistency checks:

- In reactions and scans all electronic energies must come from one functional
  and all solvation energies from one solvent; otherwise qc stops and asks for
  `-f` / `-s`. Different functionals for the thermal correction only give a warning.
- Reactions are checked for charge balance and, if geometries are found, atom balance.
- On reading: SCF convergence, and the number of imaginary modes against `CHAR`
  (MIN: 0, TS: 1).
- `defaults: temperature` is only used for the standard-state correction. It has
  to match the temperature of your frequency calculations; qc does not rescale
  dU/dH/dG.

## PES scans

Scan points are the species named `<prefix>.<number>` (e.g. `ni_br.1 … ni_br.25`).
The scan coordinate is computed from the geometries (`geometries:` in `data.yaml`,
then a search of the project for `<name>.xyz`):

```bash
qc scan                                  # list the scans in the project
qc scan ni_br B 1 2                      # bond length / Å
qc scan ni_br A 1 2 3                    # angle / °, atom 2 is the apex
qc scan rot D 1 2 3 4                    # dihedral / °, IUPAC sign convention
```

Atom numbers start at 1 (Avogadro, GaussView, Molden); use `-0` for ORCA's
0-based numbering. Dihedral scans across ±180° are unwrapped so that the curve
is continuous.

By default the table shows ΔE and ΔG next to each other, each relative to its
own minimum along the scan. ΔG uses the `bfreq` correction when it covers all
points (single-point Hessians are meaningful at the non-stationary points of a
constrained scan) and falls back to the `thermo` correction otherwise; one
source is used for the whole curve. `--bfreq` / `--no-bfreq` force a source,
`-q E` shows ΔE only.

```bash
qc scan ni_br B 1 2 --csv ni_br.csv                     # export the table
qc scan ni_br B 1 2 --plot ni_br.py                     # editable matplotlib script
qc scan ni_br B 1 2 --plot ni_br.pdf --plot ni_br.png   # render directly
qc scan pd_br B 1 2 --plot ni_br.py --append --label Pd # second curve in the same plot
```

The plot script contains the absolute energies and a settings block (unit,
reference point, curves, smoothing, labels, output file); edit it and run
`python3 ni_br.py`. The curves are drawn with monotone cubic interpolation
(PCHIP), which never invents minima or maxima between the computed points.

## Changes from version 1

| Version 1 | Version 2 |
|---|---|
| `qc add <name>` / `qc rm <name>` / `qc switch` / `qc list` | `qc project add\|rm\|switch\|ls` |
| `qc add name="cmd"` / `qc rmcmd` / `qc cmds` / `qc edit` | `qc cmd add name "cmd"` / `qc cmd rm` / `qc cmd` / `qc cmd edit` |
| `qc mark <name>` / `qc unmark` / `qc marks` | `qc mark add <name>` / `qc mark rm` / `qc mark` |
| `qc data scan [-w]` | `qc data find [--write]` |
| `qc data update [-a]` / `qc data main` | `qc data archive [--all]` / `qc data archive --list` |
| `qc data check` (sources + checks) | `qc data` (sources), `qc data check` (sources + checks) |
| `-solv`, `-gas`, `-lm <solvent>` | `--solv`, `--gas`, `-s/--solvent <solvent>` (implies `--solv`) |
| `-boltz`, `-bfreq`/`--sph`, `-sc` | `--boltz`, `--bfreq`, `--sc` |
| `-ft`, `-fsolv`, `-fboltz` | `--ft`, `--fsolv`, `--fboltz`, new: `--fbfreq` |
| `qc scan … -q G` (default G) | E and G side by side; G from bfreq if available |

Existing `commands.json`, `data.yaml` and `~/.qc_manager_config.json` files keep working.

## Development

```bash
pip install pytest
python3 -m pytest
```
