import ast
import json
import math
import subprocess
import sys

import pytest

import qc
from conftest import ROOT, SPECIES

pd = pytest.importorskip("pandas")


# ── pure functions ────────────────────────────────────────────────────────────

STRIP = set(qc.DEFAULT_STRIP_SUFFIXES)


@pytest.mark.parametrize("name, base", [
    ("a.oet2.optfreq", "a.oet2"),
    ("a.oet2_censo", "a.oet2"),
    ("mol.sp", "mol"),
    ("mol_opt", "mol"),
    ("scan.12.sp", "scan.12"),
    ("optfreq", "optfreq"),
])
def test_make_basename(name, base):
    assert qc.make_basename(name, STRIP) == base


def test_parse_reaction():
    assert qc.parse_reaction("a + 2 b -> c") == ([(1.0, "a"), (2.0, "b")], [(1.0, "c")])
    assert qc.parse_reaction("a+b=>0.5 c") == ([(1.0, "a"), (1.0, "b")], [(0.5, "c")])
    # with ' + ' separators, species names may contain '+'
    assert qc.parse_reaction("nh4+ + cl- -> nh4cl") == ([(1.0, "nh4+"), (1.0, "cl-")],
                                                        [(1.0, "nh4cl")])
    with pytest.raises(qc.QCError):
        qc.parse_reaction("a + b")


def test_parse_coordinate():
    assert qc.parse_coordinate(["B", "1", "2"]) == ("B", [1, 2])
    assert qc.parse_coordinate(["d,1,2,3,4"]) == ("D", [1, 2, 3, 4])
    with pytest.raises(qc.QCError):
        qc.parse_coordinate(["A", "1", "2"])


def test_internal_coordinates():
    atoms = [("O", 0.0, 0.0, 0.0), ("H", 0.96, 0.0, 0.0), ("H", -0.24, 0.93, 0.0)]
    assert qc.internal_coordinate(atoms, "B", [1, 2]) == pytest.approx(0.96)
    expected = math.degrees(math.acos(-0.24 / math.hypot(0.24, 0.93)))
    assert qc.internal_coordinate(atoms, "A", [2, 1, 3]) == pytest.approx(expected)
    assert qc.internal_coordinate(atoms, "B", [0, 1], one_based=False) == pytest.approx(0.96)


@pytest.mark.parametrize("phi", [60.0, -120.0, 179.0, -179.0, 0.0])
def test_dihedral_sign_convention(phi):
    # looking along atom 2 -> 3, a clockwise rotation of the front bond is positive (IUPAC)
    r = math.radians(phi)
    atoms = [("C", 1, 0, 0), ("C", 0, 0, 0), ("C", 0, 0, 1), ("C", math.cos(r), math.sin(r), 1)]
    assert qc.internal_coordinate(atoms, "D", [1, 2, 3, 4]) == pytest.approx(phi)


def test_state_correction():
    # RT ln(24.46) at 298.15 K = 1.894 kcal/mol
    assert qc.state_correction_eh(298.15) * qc.HARTREE_TO_KCAL == pytest.approx(1.8943, abs=1e-4)


def test_unwrap_dihedrals():
    rows = [{"point": i, "x": x} for i, x in enumerate([160, 170, 180, -170, -160], 1)]
    qc._unwrap_dihedrals(rows)
    assert [r["x"] for r in rows] == [160, 170, 180, 190, 200]


def _plot_namespace():
    ns = {"__name__": "test"}
    source = qc.plot_script("t.py", [], {"title": "", "xlabel": "x", "unit": "kcal/mol",
                                         "curves": ["E"]}, ["test"], None)
    exec(compile(source, "<plot>", "exec"), ns)
    return ns


def test_pchip_is_monotone_and_interpolating():
    pytest.importorskip("matplotlib")
    pchip = _plot_namespace()["pchip"]
    x = [0.0, 1.0, 1.1, 3.0, 4.0]
    y = [0.0, 0.0, 5.0, 5.2, 9.0]
    xs, ys = pchip(x, y, n=801)
    assert xs[0] == x[0] and xs[-1] == x[-1]
    assert all(b >= a - 1e-12 for a, b in zip(ys, ys[1:]))      # no overshoot
    for xi, yi in zip(x, y):
        k = min(range(len(xs)), key=lambda j: abs(xs[j] - xi))
        if abs(xs[k] - xi) < 1e-9:
            assert ys[k] == pytest.approx(yi)


# ── projects, shortcuts, bookmarks ────────────────────────────────────────────

def test_init_and_project_commands(tmp_path, monkeypatch, run):
    monkeypatch.chdir(tmp_path)
    rc, out, _ = run("init", "demo")
    assert rc == 0
    root = tmp_path / "demo"
    assert (root / "commands.json").exists() and (root / "data.yaml").exists()
    for folder in qc.PROJECT_FOLDERS:
        assert (root / folder).is_dir()
    reg = json.loads(qc.REGISTRY_FILE.read_text())
    assert reg["active_project"] == "demo"

    (tmp_path / "other").mkdir()
    monkeypatch.chdir(tmp_path / "other")
    assert run("project", "add")[0] == 0
    assert run("project", "add", "demo")[0] == 1          # name taken by another folder
    rc, out, _ = run("project")
    assert "demo" in out and "other" in out
    assert run("project", "switch", "demo")[0] == 0
    assert run("project", "rm", "other")[0] == 0
    assert "other" not in json.loads(qc.REGISTRY_FILE.read_text())["projects"]


def test_shortcuts(project, run):
    assert run("cmd", "add", "hi", "echo", "hello world", "-p")[0] == 0
    data = json.loads((project / "commands.json").read_text())
    assert data["commands"]["hi"] == "echo 'hello world' -p"
    # options after the shortcut belong to the command (-p is not swallowed)
    rc, out, _ = run("-p", "hi", "-p", "8", "a b")
    assert out.strip() == "echo 'hello world' -p -p 8 'a b'"
    assert run("cmd", "add", "scan", "x")[0] == 1          # reserved name
    assert run("cmd", "rm", "hi")[0] == 0
    rc, _, err = run("spcies")
    assert rc == 1 and "species" in err


def test_shortcut_runs_and_logs(project, run):
    run("cmd", "add", "touchit", "touch ran.txt")
    rc, _, _ = run("touchit")
    assert rc == 0 and (project / "ran.txt").exists()
    rc, out, _ = run("history")
    assert "touch ran.txt" in out


def test_bookmarks(project, run, monkeypatch):
    monkeypatch.chdir(project / "2_sp")
    assert run("mark", "add", "sp")[0] == 0
    monkeypatch.chdir(project)
    rc, out, _ = run("go", "sp")
    assert out.strip() == str(project / "2_sp")
    rc, out, _ = run("go", "root")
    assert out.strip() == str(project)
    assert run("mark", "rm", "sp")[0] == 0
    assert run("go", "sp")[0] == 1


# ── data sources ──────────────────────────────────────────────────────────────

def test_data_add_keeps_comments(project):
    text = (project / "data.yaml").read_text()
    if qc._roundtrip_yaml():
        assert "# Sanity checks when reading the data" in text
    assert "2_sp/r2scan0.csv" in text and "method: r2scan0" in text


def test_data_add_without_ruamel(project, run, monkeypatch):
    monkeypatch.setattr(qc, "_roundtrip_yaml", lambda: None)
    assert run("data", "rm", "bfreq")[0] == 0
    assert run("data", "add", "bfreq", "sph/bfreq.csv")[0] == 0
    cfg = qc.load_data_config(qc.current_project())
    assert cfg["sources"]["bfreq"] == "sph/bfreq.csv"
    assert cfg["sources"]["sp"][0] == {"path": "2_sp/r2scan0.csv", "method": "r2scan0"}
    raw = qc.read_data_raw(qc.current_project())
    assert "checks" in raw and "geometries" in raw     # nothing lost, nothing added
    assert "scan_bfreq" in raw["defaults"]


def test_data_find(project, run):
    (project / "data.yaml").write_text(qc.DATA_TEMPLATE)
    rc, out, _ = run("data", "find")
    assert sum(line.endswith(" new") for line in out.splitlines()) == 6
    assert run("data", "find", "--write")[0] == 0
    rc, out, _ = run("data", "find")
    assert not any(line.endswith(" new") for line in out.splitlines())


def test_data_check(project, run):
    rc, out, err = run("data", "check")
    assert rc == 0
    assert "duplicate" not in err      # two solvents per species are no duplicates
    assert "All checks passed" in out
    raw = qc.read_data_raw(qc.current_project())
    raw["solvent_aliases"] = {}
    qc.write_data_raw(qc.current_project(), raw)
    rc, out, err = run("data", "check")
    assert "'dichloroethane' does not occur in the solv source" in err


def test_archive(project, run):
    assert run("data", "archive")[0] == 0
    files = sorted(p.name for p in qc.ARCHIVE_DIR.glob("*.csv"))
    assert files == [f"proj_main_{r}.csv" for r in sorted(qc.ROLES)]
    rc, out, _ = run("data", "archive", "--list")
    assert "proj" in out and "pwpb95" in out


# ── thermochemistry (reference values of qc 1.x) ──────────────────────────────

def _value(out):
    return float(out.split("=")[1].split()[0])


def test_species_energies(project, run):
    rc, out, _ = run("g", "a", "-f", "pwpb95")
    assert _value(out) == pytest.approx(-100.38)
    rc, out, _ = run("e", "a", "-f", "pwpb95", "-u", "kcal/mol")
    assert _value(out) == pytest.approx(-63001.951, abs=1e-3)
    rc, out, _ = run("g", "a", "-f", "pwpb95", "-s", "1,2-dichloroethane", "--boltz", "--sc", "-v")
    assert "-100.38598120" in out
    rc, out, _ = run("g", "scanx.3", "-f", "pwpb95", "--bfreq")
    assert _value(out) == pytest.approx(-300.28774507, abs=1e-8)
    # options may follow the species
    rc, out, _ = run("g", "a", "-f", "pwpb95", "b")
    assert rc == 0 and len(out.strip().splitlines()) == 2


def test_species_needs_functional(project, run):
    rc, _, err = run("g", "a")
    assert rc == 1 and "-f" in err


@pytest.mark.parametrize("args, value", [
    (("a + b -> c", "-f", "pwpb95"), -9.413),
    (("a + b -> c", "-f", "pwpb95", "-q", "E"), -18.825),
    (("a + b -> c", "-f", "pwpb95", "-s", "thf", "--sc"), -9.424),
    (("a + b -> c", "-f", "r2scan0", "-s", "1,2-dichloroethane", "--boltz", "-u", "kj/mol"),
     -89.267),
    (("ni_h -> ni + h", "-f", "pwpb95", "-q", "H"), 61.496),
])
def test_reactions(project, run, args, value):
    rc, out, err = run("rxn", *args)
    assert rc == 0, err
    assert _value(out.splitlines()[-1]) == pytest.approx(value, abs=1e-3)


def test_reaction_breakdown_sums_up(project, run):
    rc, out, _ = run("rxn", "a + b -> c", "-f", "pwpb95", "-s", "thf", "--sc", "-v")
    contrib = [float(line.split()[-1]) for line in out.splitlines() if line.strip().startswith("Δ ")]
    assert sum(contrib) == pytest.approx(-9.424, abs=2e-3)


def test_reaction_balance_checks(project, run):
    rc, _, err = run("rxn", "a + b -> c", "-f", "pwpb95")
    assert "not balanced" not in err
    rc, _, err = run("rxn", "2 b -> c", "-f", "pwpb95")
    assert "atoms are not balanced" in err


def test_reaction_needs_one_functional(project, run):
    rc, _, err = run("rxn", "a + b -> c")
    assert rc == 1 and "several functionals" in err


def test_named_reactions(project, run):
    assert run("rxn", "add", "bind", "a + b -> c")[0] == 0
    rc, out, _ = run("rxn")
    assert "bind" in out
    rc, out, _ = run("rxn", "bind", "-f", "pwpb95")
    assert _value(out) == pytest.approx(-9.413, abs=1e-3)
    assert run("rxn", "rm", "bind")[0] == 0
    assert run("rxn", "bind", "-f", "pwpb95")[0] == 1


def test_species_table(project, run):
    rc, out, _ = run("species", "scan")
    assert rc == 0 and "scanx.7" in out and "ni_h" not in out


# ── charge and multiplicity come with the species name ───────────────────────

def _species_rows(out, name):
    return [line.split() for line in out.splitlines() if line.split()[:1] == [name]]


def test_charge_mult_taken_by_name(project, run):
    # solv and bfreq have no CHRG/M columns: one row per species, q/M from thermo/sp
    rc, out, _ = run("species", "ni")
    rows = _species_rows(out, "ni_h")
    assert len(rows) == 1 and rows[0][1:3] == ["0", "2"]
    assert rows[0][3:8] == ["x"] * 5        # thermo, 2x sp, 2x solv in one row
    rows = _species_rows(run("species", "scanx.3")[1], "scanx.3")
    assert len(rows) == 1 and rows[0][-1] == "x"            # bfreq in the same row
    data = qc.load_dataset(qc.current_project(), qc.load_data_config(qc.current_project()))
    solv = data["solv"].set_index(["BASENAME", "SOLVENT"])
    assert solv.loc[("ni_h", "thf"), "CHRG"] == 0 and solv.loc[("ni_h", "thf"), "M"] == 2


def test_multiplicity_selection_with_sources_without_m(project, run):
    rc, out, err = run("g", "ni_h@2", "-f", "pwpb95", "-s", "thf")
    assert rc == 0, err
    assert _value(out) == pytest.approx(-200.60 + 0.001 - 0.007)
    rc, out, err = run("g", "scanx.3@1", "-f", "pwpb95", "--bfreq")
    assert rc == 0, err


def test_conflicting_multiplicity_is_reported(project, run):
    with open(project / "2_sp/pwpb95.csv", "a") as f:
        f.write("ni_h.sp;0;4;pwpb95;-200.55;True\n")
    rc, out, err = run("data", "check")
    assert "ni_h appears with different multiplicities (2, 4)" in err
    # the solv rows of ni_h stay unassigned, but still match either state
    rc, out, err = run("e", "ni_h@4", "-f", "pwpb95", "-s", "thf")
    assert rc == 0, err


def test_archive_fills_rows_written_without_charge_mult(project, run):
    assert run("data", "archive")[0] == 0
    target = qc.archive_path("proj", "solv")
    df = pd.read_csv(target, sep=";")
    df["CHRG"], df["M"] = float("nan"), float("nan")      # as written before this fix
    df.to_csv(target, sep=";", index=False)
    assert run("data", "archive")[0] == 0
    df = pd.read_csv(target, sep=";")
    assert len(df) == 2 * len(SPECIES) and df["M"].notna().all()


# ── scans ─────────────────────────────────────────────────────────────────────

def _scan_rows(out):
    return [line.split() for line in out.splitlines() if line.strip()[:1].isdigit()]


def test_scan_default_e_and_g(project, run):
    rc, out, err = run("scan", "scanx", "B", "1", "2", "-f", "pwpb95")
    assert rc == 0, err
    # bfreq misses point 7 -> the whole G curve uses the thermo correction
    assert "using the thermo correction" in err
    assert "ΔE" in out and "ΔG" in out
    rows = _scan_rows(out)
    assert [r[0] for r in rows] == [str(i) for i in range(1, 8)]
    assert float(rows[4][2]) == 0.0 and float(rows[0][2]) == pytest.approx(3.64, abs=0.01)


def test_scan_prefers_bfreq_when_complete(project, run):
    with open(project / "sph/bfreq.csv", "a") as f:
        f.write("scanx.7.bfreq;gfn2;0.017\n")
    rc, out, err = run("scan", "scanx", "B", "1", "2", "-f", "pwpb95")
    assert "bfreq dG (gfn2)" in out and "thermo correction" not in err


def test_scan_old_data_yaml_still_auto(project, run):
    # data.yaml rewritten by qc 1.x contains 'bfreq: false' - scans must stay on auto
    raw = qc.read_data_raw(qc.current_project())
    raw["defaults"]["bfreq"] = False
    del raw["defaults"]["scan_bfreq"]
    qc.write_data_raw(qc.current_project(), raw)
    rc, out, err = run("scan", "scanx", "B", "1", "2", "-f", "pwpb95")
    assert "bfreq covers only 6/7 points" in err


def test_scan_energy_only_and_explicit_bfreq(project, run):
    rc, out, _ = run("scan", "scanx", "B", "1", "2", "-f", "pwpb95", "-q", "E")
    assert rc == 0 and "ΔG" not in out
    rc, out, err = run("scan", "scanx", "B", "1", "2", "-f", "pwpb95", "--bfreq")
    rows = _scan_rows(out)
    assert rows[-1][3] == "-"       # no bfreq for point 7
    assert run("scan", "scanx", "B", "1", "2", "-f", "pwpb95", "--bfreq", "-q", "H")[0] == 1


def test_scan_dihedral_crossing_180(project, run):
    rc, out, _ = run("scan", "scanx", "D", "1", "2", "3", "4", "-f", "pwpb95", "-q", "E")
    rows = _scan_rows(out)
    assert [r[0] for r in rows] == [str(i) for i in range(1, 8)]
    assert [round(float(r[1])) for r in rows] == [160, 170, 180, 190, 200, 210, 220]


def test_scan_zero_based(project, run):
    _, a, _ = run("scan", "scanx", "B", "1", "2", "-f", "pwpb95", "-q", "E")
    _, b, _ = run("scan", "scanx", "B", "0", "1", "-0", "-f", "pwpb95", "-q", "E")
    assert _scan_rows(a) == _scan_rows(b)


def test_scan_list_and_csv(project, run):
    rc, out, _ = run("scan")
    assert "scanx" in out and "7" in out
    run("scan", "scanx", "B", "1", "2", "-f", "pwpb95", "--csv", "s.csv")
    lines = (project / "s.csv").read_text().splitlines()
    assert lines[0] == "POINT;NAME;X;E_EH;G_EH;DE_KCAL_MOL;DG_KCAL_MOL"
    assert len(lines) == 8


def test_plot_script_and_append(project, run):
    rc, out, err = run("scan", "scanx", "B", "1", "2", "-f", "pwpb95", "--plot", "s.py")
    assert rc == 0, err
    src = (project / "s.py").read_text()
    compile(src, "s.py", "exec")
    run("scan", "scanx", "B", "1", "2", "-f", "r2scan0", "--plot", "s.py", "--append",
        "--label", "r2scan0")
    run("scan", "scanx", "B", "1", "2", "-f", "r2scan0", "--plot", "s.py", "--append",
        "--label", "r2scan0")
    _, _, _, datasets = qc.read_plot_datasets(project / "s.py")
    assert [d["label"] for d in datasets] == ["scanx", "r2scan0"]
    assert datasets[0]["G"][0] == pytest.approx(-300.28610408)


def test_plot_rendering(project, run):
    pytest.importorskip("matplotlib")
    rc, _, err = run("scan", "scanx", "B", "1", "2", "-f", "pwpb95", "--plot", "s.png",
                     "--plot", "s.py")
    assert rc == 0, err
    assert (project / "s.png").stat().st_size > 10000
    # the written script runs on its own
    src = (project / "s.py").read_text().replace("SHOW = True", "SHOW = False")
    (project / "s.py").write_text(src)
    subprocess.run([sys.executable, "s.py"], cwd=project, check=True, capture_output=True)
    assert (project / "s.png").exists()


# ── command line ──────────────────────────────────────────────────────────────

def test_help(run):
    rc, out, _ = run()
    assert rc == 0 and "usage: qc" in out
    for command in qc.COMMAND_NAMES:
        if command != "help":
            assert run(command, "-h")[0] == 0


def test_completion(project, run):
    def complete(*words):
        return qc.complete_words(len(words), ["qc"] + list(words))
    assert "species" in complete("sp") and "sp" in complete("sp")    # command and shortcut
    assert complete("data", "add", "") == sorted(qc.ROLES)
    assert complete("scan", "") == ["scanx"]
    assert "scanx.3" in complete("g", "scanx")
    assert "--solvent" in complete("g", "a", "--sol")
    assert complete("-p", "")[0] in ("freq", "geomopt", "sp")


def test_shellrc_is_valid_bash(run, tmp_path):
    rc, out, _ = run("shellrc")
    script = tmp_path / "rc.sh"
    script.write_text(out)
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_script_entry_point(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    res = subprocess.run([sys.executable, str(ROOT / "qc.py"), "status"], env=env,
                         cwd=tmp_path, capture_output=True, text=True)
    assert res.returncode == 0 and "No project" in res.stdout
    res = subprocess.run([sys.executable, str(ROOT / "qc.py"), "go", "x"], env=env,
                         cwd=tmp_path, capture_output=True, text=True)
    assert res.returncode == 1 and res.stderr.startswith("error:")


def test_generated_template_is_valid_yaml():
    yaml = pytest.importorskip("yaml")
    cfg = qc.effective_config(yaml.safe_load(qc.DATA_TEMPLATE))
    assert cfg["defaults"]["scan_bfreq"] == "auto"
    assert ast.literal_eval(repr(cfg["strip_suffixes"])) == qc.DEFAULT_STRIP_SUFFIXES
