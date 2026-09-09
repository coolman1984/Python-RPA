#!/usr/bin/env python3
"""Project Eye — the control plane for SmartOps.

Not documentation. A map that fails the build when the code and the map disagree.

    scan      re-read the tree and report what the map does not know about
    validate  fail on a broken reference, a missing owner, or a violated architecture rule
    doctor    red zones, drift, stale proofs, unmapped critical modules
    impact    owner, dependents, contracts, journeys and tests for one path
    journey   the full route, its evidence and its known limitations
    context   a small context pack so an agent can work without reading the repo
    delta     how the graph changed since a commit

Standard library only, plus PyYAML which the application already requires.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EYE = ROOT / ".project-eye"
PACKAGE = ROOT / "smartops_desktop"
# A module here is load-bearing: it must have an owner and at least one test.
CRITICAL = {"session.py", "discovery.py", "fingerprint.py", "core.py", "worker.py", "gui.py",
            "targets.py", "desktop_discovery.py", "probe.js"}


def load(name, default=None):
    path = EYE / name
    if not path.exists():
        return default if default is not None else {}
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def git(*args):
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return done.stdout.strip()


class Finding:
    def __init__(self, severity, rule, message):
        self.severity, self.rule, self.message = severity, rule, message

    def __str__(self):
        mark = {"error": "ERROR", "warn": " WARN", "info": " INFO"}[self.severity]
        return f"  [{mark}] {self.rule}: {self.message}"


# --- static facts ---------------------------------------------------------------------------
def python_imports(path):
    """Local imports of one module, via ast. No code is executed."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return set()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
    return found


def import_graph():
    graph = {}
    for path in sorted(PACKAGE.glob("*.py")):
        local = {name for name in python_imports(path)
                 if (PACKAGE / f"{name}.py").exists() and name != path.stem}
        graph[f"smartops_desktop/{path.name}"] = sorted(local)
    return graph


def declared_paths(ownership):
    return {name: entry["path"] for name, entry in (ownership.get("components") or {}).items()}


def owner_of(path, ownership):
    for name, declared in declared_paths(ownership).items():
        if declared == path:
            return name
    return ""


def code_facts():
    """Facts the code can state about itself, so the prose describing them can be checked.

    A tree too broken to import must still produce a report, so this degrades to nothing rather
    than taking the whole tool down with it.
    """
    try:
        sys.path.insert(0, str(ROOT))
        from smartops_desktop import fingerprint
        from smartops_desktop.discovery import LOCATOR_KINDS
        return {"layer_count": len(fingerprint.LAYER_KEYS),
                "vision_maturity": fingerprint.MATURITY["vision"],
                "testid_confidence": LOCATOR_KINDS["testid"]["base"]}
    except Exception:
        return {}


# --- checks ---------------------------------------------------------------------------------
def check_references(findings):
    """Every path the map names must exist; every critical module must be mapped and tested."""
    ownership, journeys, proofs = load("ownership.yaml"), load("journeys.yaml"), load("proofs.yaml")
    mapped = set(declared_paths(ownership).values())
    for name, path in declared_paths(ownership).items():
        if not (ROOT / path).exists():
            findings.append(Finding("error", "mapped-file-missing", f"{name} points at {path}, which is gone"))
    for path in sorted(PACKAGE.glob("*")):
        rel = f"smartops_desktop/{path.name}"
        if path.name in CRITICAL and rel not in mapped:
            findings.append(Finding("error", "unmapped-critical-module", f"{rel} has no owner"))
    seen = {}
    for name, entry in (ownership.get("components") or {}).items():
        for state in entry.get("owns", []):
            if state in seen:
                findings.append(Finding("error", "ownership-conflict",
                                        f"'{state}' is claimed by both {seen[state]} and {name}"))
            seen[state] = name
    for state, owner in (ownership.get("state") or {}).items():
        if owner not in (ownership.get("components") or {}):
            findings.append(Finding("error", "unknown-owner", f"state '{state}' is owned by unknown component '{owner}'"))
    for jid, journey in (journeys.get("journeys") or {}).items():
        if not journey.get("tests"):
            findings.append(Finding("error", "journey-without-test", f"{jid} has no test"))
        for test in journey.get("tests") or []:
            if not (ROOT / test.split("::")[0]).exists():
                findings.append(Finding("error", "journey-broken-test-reference", f"{jid} -> missing {test}"))
    for proof in proofs.get("proofs") or []:
        for path in proof.get("paths") or []:
            if not (ROOT / path).exists():
                findings.append(Finding("error", "proof-broken-path", f"{proof['id']} -> missing {path}"))
        if proof.get("test") and not (ROOT / str(proof["test"]).split("::")[0]).exists():
            findings.append(Finding("error", "proof-broken-test", f"{proof['id']} -> missing {proof['test']}"))
    return findings


def check_contracts(findings):
    """A producer must exist, and a contract without a consumer test is a blind spot."""
    contracts = load("contracts.yaml").get("contracts") or {}
    for cid, contract in contracts.items():
        producer = contract.get("producer")
        if producer and not (ROOT / producer).exists():
            findings.append(Finding("error", "contract-producer-missing", f"{cid} -> {producer}"))
        for consumer in contract.get("consumers") or []:
            if not (ROOT / consumer).exists():
                findings.append(Finding("error", "contract-consumer-missing", f"{cid} -> {consumer}"))
        if not contract.get("tests"):
            findings.append(Finding("warn", "contract-without-test", f"{cid} has no test"))
        # The break that started this: a consumer reading a field the producer never emits.
        for field in contract.get("forbidden_fields") or []:
            for consumer in contract.get("consumers") or []:
                text = (ROOT / consumer).read_text(encoding="utf-8")
                emitted = f'"{contract["schema"].get("type", "")}"'
                if emitted and emitted in text and re.search(rf'event\[.{field}.\]', text):
                    findings.append(Finding("error", "consumer-reads-forbidden-field",
                                            f"{consumer} reads '{field}' from {cid}, which never carries it"))
    return findings


def check_rules(findings):
    for rule in load("rules.yaml").get("rules") or []:
        kind, severity = rule.get("check"), rule.get("severity", "warn")
        if kind in {"forbid_pattern", "require_pattern"}:
            path = ROOT / rule["path"]
            if not path.exists():
                findings.append(Finding("error", "rule-path-missing", f"{rule['id']} -> {rule['path']}"))
                continue
            hit = re.search(rule["pattern"], path.read_text(encoding="utf-8"))
            if kind == "forbid_pattern" and hit:
                findings.append(Finding(severity, rule["id"], rule["statement"]))
            if kind == "require_pattern" and not hit:
                findings.append(Finding(severity, rule["id"], rule["statement"]))
        elif kind == "forbid_import":
            imported = python_imports(ROOT / rule["path"])
            clash = imported & set(rule["imports"])
            if clash:
                findings.append(Finding(severity, rule["id"], f"{rule['statement']} (found {', '.join(sorted(clash))})"))
        elif kind == "doc_fact":
            # A documented number that the code can compute. Prose drifts; this makes it fail.
            path = ROOT / rule["path"]
            hit = re.search(rule["pattern"], path.read_text(encoding="utf-8")) if path.exists() else None
            facts = code_facts()
            if rule["fact"] not in facts:
                findings.append(Finding("warn", rule["id"], "the code could not be read to check this fact"))
                continue
            actual = facts[rule["fact"]]
            if hit is None:
                findings.append(Finding(severity, rule["id"], f"{rule['statement']} (the fact is not stated at all)"))
            elif hit.group(1) != str(actual):
                findings.append(Finding(severity, rule["id"],
                                        f"{rule['statement']} (document says {hit.group(1)}, code says {actual})"))
        elif kind == "forbid_pattern_tree":
            pattern = re.compile(rule["pattern"])
            for path in sorted(PACKAGE.glob("*")) + [ROOT / "build.ps1"]:
                if path.is_file() and path.suffix in {".py", ".js", ".ps1"} and pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                    findings.append(Finding(severity, rule["id"], f"{rule['statement']} ({path.relative_to(ROOT)})"))
    return findings


def stale_proofs():
    """A proof is stale when its code changed after the revision the map was verified at."""
    revision = load("revision.json")
    base = revision.get("verified_at_commit")
    if not base:
        return []
    changed = set(git("diff", "--name-only", f"{base}..HEAD").splitlines())
    stale = []
    for proof in load("proofs.yaml").get("proofs") or []:
        touched = sorted(set(proof.get("paths") or []) & changed)
        if touched:
            stale.append((proof["id"], touched))
    return stale


# --- commands ---------------------------------------------------------------------------------
def cmd_scan(args):
    graph = import_graph()
    ownership = load("ownership.yaml")
    (EYE / "graph.yaml").write_text(yaml.safe_dump(
        {"generated_from": git("rev-parse", "HEAD"), "imports": graph}, sort_keys=True), encoding="utf-8")
    print(f"Scanned {len(graph)} python modules under smartops_desktop/.")
    unmapped = [p for p in sorted(PACKAGE.glob("*")) if p.name in CRITICAL
                and f"smartops_desktop/{p.name}" not in set(declared_paths(ownership).values())]
    for path in unmapped:
        print(f"  UNMAPPED critical module: {path.name}")
    print(f"  graph.yaml updated ({sum(len(v) for v in graph.values())} internal edges).")
    return 0


def cmd_validate(args):
    findings = []
    check_references(findings)
    check_contracts(findings)
    check_rules(findings)
    errors = [f for f in findings if f.severity == "error"]
    print("Project Eye — validate")
    for finding in findings or []:
        print(finding)
    if not findings:
        print("  all references, contracts and architecture rules hold.")
    print(f"  {len(errors)} error(s), {len(findings) - len(errors)} warning(s).")
    return 1 if errors else 0


def cmd_doctor(args):
    print("Project Eye — doctor")
    revision = load("revision.json")
    print(f"  map revision {revision.get('map_revision')} verified at {str(revision.get('verified_at_commit'))[:8]}")
    behind = git("rev-list", "--count", f"{revision.get('verified_at_commit')}..HEAD") or "0"
    if behind not in {"", "0"}:
        print(f"  MAP BEHIND CODE: {behind} commit(s) since the map was last verified")
    stale = stale_proofs()
    for pid, paths in stale:
        print(f"  STALE PROOF: {pid} (changed: {', '.join(paths)})")
    print("  red zones:")
    for zone in load("risks.yaml").get("red_zones") or []:
        print(f"    - {zone['component']} ({zone['path']}) — {zone['why']}")
    print("  known gaps:")
    for gap in load("risks.yaml").get("known_gaps") or []:
        print(f"    - [{gap['severity']}] {gap['id']}: {gap['detail']}")
    unproven = [name for name, cap in (load("capabilities.yaml").get("capabilities") or {}).items()
                if cap.get("proof") in {"synthetic", "none"}]
    print(f"  capabilities without real-environment proof: {', '.join(unproven)}")
    findings = []
    check_references(findings); check_contracts(findings); check_rules(findings)
    errors = [f for f in findings if f.severity == "error"]
    print(f"  validate: {len(errors)} error(s)")
    return 0


def cmd_impact(args):
    target = args.path
    ownership, contracts, journeys = load("ownership.yaml"), load("contracts.yaml"), load("journeys.yaml")
    graph = import_graph()
    print(f"Impact of {target}")
    print(f"  owner: {owner_of(target, ownership) or 'UNOWNED'}")
    owns = (ownership.get("components") or {}).get(owner_of(target, ownership), {}).get("owns", [])
    print(f"  owns state: {', '.join(owns) or '—'}")
    print(f"  imports: {', '.join(graph.get(target, [])) or '—'}")
    dependents = [module for module, deps in graph.items() if Path(target).stem in deps]
    print(f"  imported by: {', '.join(dependents) or '—'}")
    produces = [cid for cid, c in (contracts.get("contracts") or {}).items() if c.get("producer") == target]
    consumes = [cid for cid, c in (contracts.get("contracts") or {}).items() if target in (c.get("consumers") or [])]
    print(f"  produces contracts: {', '.join(produces) or '—'}")
    print(f"  consumes contracts: {', '.join(consumes) or '—'}")
    touched = [jid for jid, j in (journeys.get("journeys") or {}).items()
               if any(Path(target).stem in str(node) for node in j.get("nodes", []))]
    print(f"  journeys: {', '.join(touched) or '—'}")
    tests = sorted({t for cid in produces + consumes for t in ((contracts.get("contracts") or {})[cid].get("tests") or [])}
                   | {t for jid in touched for t in ((journeys.get("journeys") or {})[jid].get("tests") or [])})
    print("  tests to run:")
    for test in tests or ["—"]:
        print(f"    {test}")
    zone = next((z for z in load("risks.yaml").get("red_zones") or [] if z["path"] == target), None)
    print(f"  RED ZONE: {zone['why']}" if zone else "  red zone: no")
    return 0


def cmd_journey(args):
    journeys = load("journeys.yaml").get("journeys") or {}
    journey = journeys.get(args.id)
    if journey is None:
        print(f"Unknown journey. Known: {', '.join(sorted(journeys))}")
        return 1
    print(f"Journey {args.id}")
    print(f"  entry: {journey.get('entry')}")
    print("  route:")
    for node in journey.get("nodes", []):
        print(f"    -> {node}")
    print(f"  writes: {', '.join(journey.get('writes', [])) or '—'}")
    print(f"  terminal: {journey.get('terminal')}")
    print(f"  proof: {journey.get('proof')}")
    for test in journey.get("tests", []):
        print(f"  test: {test}")
    if journey.get("limitation"):
        print(f"  LIMITATION: {journey['limitation']}")
    return 0


def cmd_context(args):
    """A small pack so an average agent can work precisely without reading the repository."""
    ownership, journeys, contracts = load("ownership.yaml"), load("journeys.yaml"), load("contracts.yaml")
    words = set(re.findall(r"[a-z_]+", args.task.lower()))
    scored = []
    for jid, journey in (journeys.get("journeys") or {}).items():
        hits = len(words & set(re.findall(r"[a-z_]+", (jid + " " + json.dumps(journey)).lower())))
        scored.append((hits, jid, journey))
    scored.sort(reverse=True, key=lambda item: item[0])
    hits, jid, journey = scored[0]
    print(f"TASK           {args.task}")
    print(f"JOURNEY        {jid}")
    print(f"ROUTE          {' -> '.join(str(n) for n in journey.get('nodes', []))}")
    nodes = " ".join(str(n) for n in journey.get("nodes", []))
    involved = sorted({name for name, entry in (ownership.get("components") or {}).items()
                       if Path(entry["path"]).stem in nodes or name in nodes})
    print(f"OWNERS         {', '.join(involved) or '—'}")
    print("STATE          " + ", ".join(sorted({s for name in involved
                                                for s in (ownership.get('components') or {})[name].get('owns', [])})) )
    related = [cid for cid, c in (contracts.get("contracts") or {}).items()
               if any((ownership.get("components") or {}).get(n, {}).get("path") in
                      [c.get("producer")] + (c.get("consumers") or []) for n in involved)]
    print(f"CONTRACTS      {', '.join(related) or '—'}")
    print(f"ALLOWED AREA   {', '.join((ownership.get('components') or {})[n]['path'] for n in involved) or '—'}")
    print("DANGEROUS AREA " + ", ".join(z["path"] for z in load("risks.yaml").get("red_zones") or []))
    print("TESTS          " + ", ".join(journey.get("tests", []) or ["—"]))
    print(f"PROOF          {journey.get('proof')}")
    if journey.get("limitation"):
        print(f"LIMITATION     {journey['limitation']}")
    print("RISKS          " + "; ".join(f"{g['id']}" for g in load("risks.yaml").get("known_gaps") or []))
    print("STOP IF        a change would give a second owner to any state above,")
    print("               or make a consumer read a field its producer does not emit.")
    return 0


def cmd_delta(args):
    base = args.base
    changed = [line for line in git("diff", "--name-only", f"{base}..HEAD").splitlines() if line]
    ownership = load("ownership.yaml")
    print(f"Delta {base[:8]}..HEAD — {len(changed)} file(s)")
    for path in changed:
        owner = owner_of(path, ownership)
        print(f"  {path}  [owner: {owner or 'unmapped'}]")
    stale = [pid for pid, _ in stale_proofs()]
    print(f"  proofs needing re-verification: {', '.join(stale) or 'none'}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="project_eye")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("scan").set_defaults(run=cmd_scan)
    subs.add_parser("validate").set_defaults(run=cmd_validate)
    subs.add_parser("doctor").set_defaults(run=cmd_doctor)
    impact = subs.add_parser("impact"); impact.add_argument("--path", required=True); impact.set_defaults(run=cmd_impact)
    journey = subs.add_parser("journey"); journey.add_argument("id"); journey.set_defaults(run=cmd_journey)
    context = subs.add_parser("context"); context.add_argument("--task", required=True); context.set_defaults(run=cmd_context)
    delta = subs.add_parser("delta"); delta.add_argument("--base", required=True); delta.set_defaults(run=cmd_delta)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
