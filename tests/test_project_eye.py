"""Project Eye must be able to FAIL.

A rule that cannot fail is advice, not control. Every check here is proven by feeding the tool a
deliberately broken map or tree and asserting that it refuses.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "project_eye.py"


def run(*args, cwd=ROOT):
    return subprocess.run([sys.executable, str(cwd / "tools" / "project_eye.py"), *args],
                          cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def sandbox(tmp_path):
    """A throwaway copy of the repository, so a broken map never touches the real one."""
    copy = tmp_path / "repo"
    copy.mkdir()
    for item in (".project-eye", "smartops_desktop", "tools", "tests", "build.ps1",
                 "requirements.txt", "main.py", "README.md", "PROJECT-MAP.md", "PROJECT_EYE.md"):
        source = ROOT / item
        if source.is_dir():
            shutil.copytree(source, copy / item, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, copy / item)
    return copy


def edit(sandbox, name, mutate):
    path = sandbox / ".project-eye" / name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# --- the tool works on the real repository --------------------------------------------------
def test_the_real_map_validates_clean():
    done = run("validate")
    assert done.returncode == 0, done.stdout
    assert "0 error(s)" in done.stdout


def test_scan_finds_every_python_module_and_writes_a_graph():
    done = run("scan")
    assert done.returncode == 0 and "Scanned" in done.stdout
    assert (ROOT / ".project-eye" / "graph.yaml").exists()


@pytest.mark.parametrize("journey", ["RECORD-ACTION", "ENRICH-ACTION", "SAVE-RECORDING", "RADAR-ELEMENT"])
def test_every_critical_journey_is_described_with_its_evidence(journey):
    done = run("journey", journey)
    assert done.returncode == 0
    assert "route:" in done.stdout and "proof:" in done.stdout and "test:" in done.stdout


def test_impact_answers_the_questions_a_change_needs_before_it_is_made():
    done = run("impact", "--path", "smartops_desktop/session.py")
    assert "owner: recording_session" in done.stdout
    assert "produces contracts:" in done.stdout and "worker_event.step_added" in done.stdout
    assert "RED ZONE" in done.stdout
    assert "tests/test_journeys.py" in done.stdout


def test_the_context_pack_routes_a_vague_task_to_the_right_journey():
    done = run("context", "--task", "Fix recording save")
    assert "JOURNEY        SAVE-RECORDING" in done.stdout
    assert "DraftJournal.timeline" in done.stdout
    assert "STOP IF" in done.stdout


# --- drift detection: each of these must be caught -------------------------------------------
def test_a_mapped_file_that_disappears_is_caught(sandbox):
    (sandbox / "smartops_desktop" / "targets.py").unlink()
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1
    assert "mapped-file-missing" in done.stdout or "unmapped-critical-module" in done.stdout


def test_a_critical_module_with_no_owner_is_caught(sandbox):
    edit(sandbox, "ownership.yaml", lambda data: data["components"].pop("target_identity"))
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "unmapped-critical-module" in done.stdout


def test_two_owners_for_one_piece_of_state_is_caught(sandbox):
    """The exact class of fault behind the recorded-vs-enriched break: two authorities."""
    edit(sandbox, "ownership.yaml",
         lambda data: data["components"]["gui"]["owns"].append("recorded_timeline"))
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "ownership-conflict" in done.stdout
    assert "recorded_timeline" in done.stdout


def test_an_unknown_owner_for_a_piece_of_state_is_caught(sandbox):
    edit(sandbox, "ownership.yaml", lambda data: data["state"].update({"recorded_timeline": "nobody"}))
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "unknown-owner" in done.stdout


def test_a_journey_pointing_at_a_deleted_test_is_caught(sandbox):
    edit(sandbox, "journeys.yaml",
         lambda data: data["journeys"]["SAVE-RECORDING"].update({"tests": ["tests/test_gone.py::nope"]}))
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "journey-broken-test-reference" in done.stdout


def test_a_journey_without_any_test_is_caught(sandbox):
    edit(sandbox, "journeys.yaml", lambda data: data["journeys"]["SAVE-RECORDING"].update({"tests": []}))
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "journey-without-test" in done.stdout


def test_a_contract_whose_producer_vanished_is_caught(sandbox):
    edit(sandbox, "contracts.yaml",
         lambda data: data["contracts"]["recorded_step"].update({"producer": "smartops_desktop/gone.py"}))
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "contract-producer-missing" in done.stdout


def test_a_proof_pointing_at_a_deleted_path_is_caught(sandbox):
    edit(sandbox, "proofs.yaml",
         lambda data: data["proofs"][0].update({"paths": ["smartops_desktop/never_existed.py"]}))
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "proof-broken-path" in done.stdout


# --- architecture rules must be able to fail -------------------------------------------------
def test_the_gui_reclaiming_the_timeline_authority_is_caught(sandbox):
    """If someone makes the GUI save its own mirror again, this must fail the build."""
    gui = sandbox / "smartops_desktop" / "gui.py"
    gui.write_text(gui.read_text(encoding="utf-8").replace("DraftJournal.timeline", "self.mirror_timeline"),
                   encoding="utf-8")
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "gui-not-timeline-authority" in done.stdout


def test_the_gui_taking_back_a_capacity_limit_is_caught(sandbox):
    gui = sandbox / "smartops_desktop" / "gui.py"
    gui.write_text(gui.read_text(encoding="utf-8").replace(
        "        if kind == CAPACITY:", "        if len(self.captured) >= 1000: return\n        if kind == CAPACITY:"),
        encoding="utf-8")
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "gui-no-capacity-limit" in done.stdout


def test_the_probe_defaulting_back_to_radar_is_caught(sandbox):
    """The dangerous one: a new frame in radar mode swallows the user's real click."""
    probe = sandbox / "smartops_desktop" / "probe.js"
    probe.write_text(probe.read_text(encoding="utf-8").replace(
        "window.__smartopsProbeMode || 'off'", "window.__smartopsProbeMode || 'radar'"), encoding="utf-8")
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "probe-default-off" in done.stdout


def test_the_probe_taking_back_confidence_policy_is_caught(sandbox):
    probe = sandbox / "smartops_desktop" / "probe.js"
    probe.write_text(probe.read_text(encoding="utf-8") + "\n// confidence = 0.9\n", encoding="utf-8")
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "probe-no-confidence" in done.stdout


def test_the_fingerprint_contract_gaining_a_dependency_is_caught(sandbox):
    module = sandbox / "smartops_desktop" / "fingerprint.py"
    text = module.read_text(encoding="utf-8")
    module.write_text(text.replace("from datetime import", "import sqlite3\nfrom datetime import"), encoding="utf-8")
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "fingerprint-pure" in done.stdout


def test_the_worker_opening_the_gui_database_is_caught(sandbox):
    worker = sandbox / "smartops_desktop" / "worker.py"
    worker.write_text(worker.read_text(encoding="utf-8") + "\ndef bad():\n    return Store('x')\n", encoding="utf-8")
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "worker-not-db-owner" in done.stdout


def test_a_machine_specific_path_reappearing_is_caught(sandbox):
    core = sandbox / "smartops_desktop" / "core.py"
    core.write_text(core.read_text(encoding="utf-8").replace(
        '"chrome_launcher": "",', '"chrome_launcher": r"D:\\WORK\\helper.py",'), encoding="utf-8")
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "no-machine-specific-path" in done.stdout


# --- stale proof detection --------------------------------------------------------------------
def test_a_proof_goes_stale_when_the_code_behind_it_moves(sandbox):
    """Evidence is only as fresh as the code it was gathered from.

    Pinned to the parent of the last commit that touched session.py, so the condition is real
    rather than whatever HEAD~1 happened to contain.
    """
    import json
    last = subprocess.run(["git", "log", "-1", "--format=%H", "--", "smartops_desktop/session.py"],
                          cwd=ROOT, capture_output=True, text=True).stdout.strip()
    before = subprocess.run(["git", "rev-parse", f"{last}~1"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    assert before, "the repository must have history for this check to mean anything"
    revision = sandbox / ".project-eye" / "revision.json"
    data = json.loads(revision.read_text(encoding="utf-8"))
    data["verified_at_commit"] = before
    revision.write_text(json.dumps(data, indent=2), encoding="utf-8")
    shutil.copytree(ROOT / ".git", sandbox / ".git", ignore=shutil.ignore_patterns("objects/pack/tmp*"))
    done = run("doctor", cwd=sandbox)
    assert "STALE PROOF" in done.stdout, done.stdout
    assert "smartops_desktop/session.py" in done.stdout


def test_the_tools_own_generated_output_is_not_reported_as_drift(sandbox):
    """graph.yaml and revision.json are written by the tool. A change there is not the map
    falling behind the code, and reporting it as such trains people to ignore the report."""
    import json
    revision = sandbox / ".project-eye" / "revision.json"
    data = json.loads(revision.read_text(encoding="utf-8"))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    data["verified_at_commit"] = head
    revision.write_text(json.dumps(data, indent=2), encoding="utf-8")
    shutil.copytree(ROOT / ".git", sandbox / ".git", ignore=shutil.ignore_patterns("objects/pack/tmp*"))
    done = run("doctor", cwd=sandbox)
    assert "MAP BEHIND CODE" not in done.stdout, done.stdout


def test_a_documented_number_that_drifts_from_the_code_is_caught(sandbox):
    """PROJECT-MAP.md had three stale facts before this check existed: Vision's maturity, the
    journal model and the test-id score. Prose about code is now checked against the code."""
    doc = sandbox / "PROJECT-MAP.md"
    doc.write_text(doc.read_text(encoding="utf-8").replace(
        "| `layers.vision` | NOT_IMPLEMENTED |", "| `layers.vision` | VERIFIED |"), encoding="utf-8")
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "doc-vision-maturity" in done.stdout
    assert "document says VERIFIED, code says NOT_IMPLEMENTED" in done.stdout


def test_a_drifting_confidence_number_in_the_documentation_is_caught(sandbox):
    doc = sandbox / "PROJECT-MAP.md"
    doc.write_text(doc.read_text(encoding="utf-8").replace("data-cy`) | 0.96 |", "data-cy`) | 0.42 |"),
                   encoding="utf-8")
    done = run("validate", cwd=sandbox)
    assert done.returncode == 1 and "doc-testid-confidence" in done.stdout
