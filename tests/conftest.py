import math
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MPLBACKEND", "Agg")

import qc  # noqa: E402


# species: (charge, M, E_thermo, dU, dH, dG, E_sp_r2scan0, E_sp_pwpb95, Gsolv_dce, Gsolv_thf)
SPECIES = {
    "a": (0, 1, -100.10, 0.050, 0.051, 0.020, -100.30, -100.40, -0.010, -0.011),
    "b": (0, 1, -50.20, 0.030, 0.031, 0.005, -50.35, -50.45, -0.004, -0.005),
    "c": (0, 1, -150.33, 0.085, 0.086, 0.040, -150.70, -150.88, -0.012, -0.013),
    "ni_h": (0, 2, -200.00, 0.020, 0.021, 0.001, -200.50, -200.60, -0.006, -0.007),
    "ni": (0, 1, -199.40, 0.015, 0.016, -0.005, -199.90, -200.00, -0.005, -0.006),
    "h": (0, 2, -0.50, 0.002, 0.003, -0.010, -0.499, -0.500, 0.000, 0.000),
}
for _i in range(1, 8):
    _d = 1.8 + 0.1 * _i
    _e = -300.0 + 0.01 * (_d - 2.2) ** 2 - 0.005 * math.exp(-((_d - 2.3) / 0.15) ** 2)
    SPECIES[f"scanx.{_i}"] = (0, 1, _e, 0.03, 0.031, 0.012 + 0.001 * _i, _e - 0.2, _e - 0.3,
                              -0.008, -0.009)


def write_fixture_files(root):
    """CSV sources and geometries of a small synthetic project."""
    for d in ("xyz", "1_optfreq", "2_sp", "3_solv", "censo", "sph"):
        (root / d).mkdir(parents=True, exist_ok=True)
    thermo = ["NAME;CHRG;M;METHOD;ENERGY;dU;dH;dG;SCFCONV;CPSCF/GEOCONV;CHAR;IMAGS"]
    sp1 = ["NAME;CHRG;M;METHOD;ENERGY;SCFCONV"]
    sp2 = ["NAME;CHRG;M;METHOD;ENERGY;SCFCONV"]
    solv = ["NAME;CHRG;M;METHOD;SOLVENT;Gsolv(Eh)"]
    bfreq = ["NAME;CHRG;M;METHOD;dG(Eh)"]
    boltz = ["NAME;CHRG;M;METHOD;SOLVENT;G_LOW;G_AV;dG_AV;STATUS;FAILED_CALC_COUNT"]
    for n, (q, m, et, du, dh, dg, e1, e2, s1, s2) in SPECIES.items():
        char, imag = ("TS", 1) if n == "c" else ("MIN", "")
        thermo.append(f"{n}.optfreq;{q};{m};wb97x-3c;{et};{du};{dh};{dg};True;False;{char};{imag}")
        sp1.append(f"{n}.sp;{q};{m};None;{e1};True")
        sp2.append(f"{n}.sp;{q};{m};pwpb95;{e2};True")
        solv.append(f"{n};{q};{m};cosmors;1,2-dichloroethane;{s1}")
        solv.append(f"{n};{q};{m};cosmors;thf;{s2}")
        if n.startswith("scanx") and n != "scanx.7":
            bfreq.append(f"{n}.bfreq;{q};{m};gfn2;{dg - 0.002}")
        if n in ("a", "b", "c"):
            boltz.append(f"{n}_censo;{q};{m};r2scan-3c;dichloroethane;-1.0;-0.999;"
                         f"{0.001 * 627.5094740631:.4f};OK;0")
    (root / "1_optfreq/outorca.csv").write_text("\n".join(thermo) + "\n")
    (root / "2_sp/r2scan0.csv").write_text("\n".join(sp1) + "\n")
    (root / "2_sp/pwpb95.csv").write_text("\n".join(sp2) + "\n")
    (root / "3_solv/cosmo.csv").write_text("\n".join(solv) + "\n")
    (root / "sph/bfreq.csv").write_text("\n".join(bfreq) + "\n")
    (root / "censo/censo.csv").write_text("\n".join(boltz) + "\n")

    # scan geometries: Ni-Br distance 1.9..2.5 A; dihedral Ni-Br-C-H 160..220 deg
    for i in range(1, 8):
        d = 1.8 + 0.1 * i
        phi = math.radians(150 + 10 * i)
        (root / f"xyz/scanx.{i}.xyz").write_text(
            f"4\nscan point {i}\n"
            f"Ni 0.0 0.0 0.0\nBr {d:.6f} 0.0 0.0\nC {d:.6f} 1.5 0.0\n"
            f"H {d - math.cos(phi):.6f} 1.5 {math.sin(phi):.6f}\n")
    # geometries for the atom-balance check: a (C2) + b (H2) -> c (C2H2)
    (root / "xyz/a.xyz").write_text("2\n\nC 0 0 0\nC 1.2 0 0\n")
    (root / "xyz/b.xyz").write_text("2\n\nH 0 0 0\nH 0.74 0 0\n")
    (root / "xyz/c.xyz").write_text("4\n\nC 0 0 0\nC 1.2 0 0\nH -1 0 0\nH 2.2 0 0\n")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own registry and archive directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(qc, "REGISTRY_FILE", home / ".qc_manager_config.json")
    monkeypatch.setattr(qc, "ARCHIVE_DIR", home / ".qc_data")
    qc._WARNINGS_SEEN.clear()
    return home


@pytest.fixture
def run(capsys):
    """Run 'qc <args>' in-process; returns (exit code, stdout, stderr)."""
    def _run(*args):
        try:
            rc = qc.main(list(args))
        except qc.QCError as e:
            print(f"error: {e}", file=sys.stderr)
            rc = 1
        except SystemExit as e:   # argparse errors
            rc = e.code
        out, err = capsys.readouterr()
        qc._WARNINGS_SEEN.clear()
        return rc, out, err
    return _run


@pytest.fixture
def project(tmp_path, monkeypatch, run):
    """A registered project with all five data sources, cwd inside it."""
    monkeypatch.chdir(tmp_path)
    assert run("init", "proj")[0] == 0
    root = tmp_path / "proj"
    write_fixture_files(root)
    monkeypatch.chdir(root)
    for args in (("thermo", "1_optfreq/outorca.csv"), ("sp", "2_sp/r2scan0.csv", "-f", "r2scan0"),
                 ("sp", "2_sp/pwpb95.csv"), ("solv", "3_solv/cosmo.csv"),
                 ("boltz", "censo/censo.csv"), ("bfreq", "sph/bfreq.csv")):
        rc, out, err = run("data", "add", *args)
        assert rc == 0, err
    # CENSO spells the solvent differently than COSMO-RS
    raw = qc.read_data_raw(qc.current_project())
    raw["solvent_aliases"] = {"dichloroethane": "1,2-dichloroethane"}
    qc.write_data_raw(qc.current_project(), raw)
    return root
