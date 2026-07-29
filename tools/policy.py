"""Load the declared rules, so nothing about a jurisdiction is compiled in.

Ariadne's traversal, its invariants and its reconstruction measurement are all
indifferent to which industry they are pointed at. What differs between a lending
model and a hiring model is which attributes are sensitive and which statute names
them, and both of those are facts to be declared rather than code to be written.

Two kinds of fact live in policy/, deliberately apart.

`attributes.yml` says which column of this warehouse holds which human attribute,
and how its values are coded. That is true no matter which law you are reading.

Each regime file says which attributes it restricts, on what basis, with a
citation. That is true no matter how your warehouse spells its columns. Regimes
name attributes and never name columns, which is what lets a pack move between
warehouses.

`basis` is what stops the output being a wall of undifferentiated alarms:

    prohibited   no exception exists, so a finding is a finding
    conditional  permitted under a stated condition, so a finding is a question
    examine      not prohibited, but the regime expects documented examination

The same attribute can sit at different levels in different regimes, which is the
clearest evidence that this belongs in configuration. Age is conditional under
ECOA, which permits it in a statistically sound scoring system, and prohibited
under the ADEA, which has no such carve out.

    python tools/policy.py list
    python tools/policy.py show ecoa
"""

from __future__ import annotations

import argparse
import os
import sys

import yaml

POLICY_DIR = os.environ.get(
    "ARIADNE_POLICY_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "policy"),
)
DEFAULT_REGIME = os.environ.get("ARIADNE_POLICY", "ecoa")

PROHIBITED = "prohibited"
CONDITIONAL = "conditional"
EXAMINE = "examine"
ORDER = {PROHIBITED: 0, CONDITIONAL: 1, EXAMINE: 2}


def _read(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"no such policy file: {path}")
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def available() -> list[str]:
    """Every regime declared, by the name you pass on the command line."""
    if not os.path.isdir(POLICY_DIR):
        return []
    return sorted(
        os.path.splitext(f)[0] for f in os.listdir(POLICY_DIR)
        if f.endswith((".yml", ".yaml")) and not f.startswith("attributes")
    )


def attributes() -> dict[str, dict]:
    """The warehouse's own description of itself, keyed by attribute name."""
    return _read(os.path.join(POLICY_DIR, "attributes.yml")).get("attributes", {})


class Policy:
    """One regime, resolved against this warehouse's attribute declarations."""

    def __init__(self, regime: str = DEFAULT_REGIME):
        for suffix in (".yml", ".yaml"):
            path = os.path.join(POLICY_DIR, regime + suffix)
            if os.path.exists(path):
                break
        else:
            raise SystemExit(
                f"no regime named {regime!r}. available: {', '.join(available())}")

        self.regime = regime
        self.spec = _read(path)
        self.attributes = attributes()

        self.name: str = self.spec.get("name", regime)
        self.long_name: str = self.spec.get("long_name", self.name)
        self.jurisdiction: str = self.spec.get("jurisdiction", "")
        self.decision_context: str = self.spec.get("decision_context", "")
        self.citation: str = self.spec.get("citation", "")
        self.protected_tags: tuple[str, ...] = tuple(
            self.spec.get("protected_tags") or ["protected_attribute"])

        # Duties are obligations about the DECISION rather than about which
        # columns may reach the model: tell the person, give the principal
        # factors, offer human review. They do not reduce to restricted
        # attributes and restricted attributes do not reduce to them, so they
        # are carried separately and read straight through to the compliance
        # document. A regime with none declared has none, not an empty one.
        self.duties: list[dict] = list(self.spec.get("duties") or [])

        self.restricted: dict[str, dict] = {}
        for entry in self.spec.get("restricted") or []:
            name = entry.get("attribute")
            if not name:
                continue
            if name not in self.attributes:
                # a regime naming an attribute this warehouse does not declare is
                # not an error. it means the data simply is not held here.
                continue
            merged = dict(self.attributes[name])
            merged.update({k: v for k, v in entry.items() if k != "attribute"})
            merged["attribute"] = name
            merged.setdefault("basis", PROHIBITED)
            self.restricted[name] = merged

    def tags_in_scope(self) -> tuple[str, ...]:
        return self.protected_tags

    def is_in_scope(self, applied_tags) -> bool:
        """Does a column carrying these tags fall in scope of this regime."""
        return bool(set(applied_tags) & set(self.protected_tags))

    def by_column(self) -> dict[str, dict]:
        """Restricted attributes keyed by the column that holds them."""
        return {spec["column"]: spec for spec in self.restricted.values()
                if spec.get("column")}

    def basis_of_column(self, column: str) -> str | None:
        spec = self.by_column().get(column)
        return spec.get("basis") if spec else None

    def testable(self) -> list[dict]:
        """Attributes the reconstruction measurement can compare two groups on."""
        return [
            spec for spec in self.restricted.values()
            if spec.get("kind") == "categorical" and spec.get("categories")
        ]

    def untestable(self) -> list[dict]:
        """Restricted attributes the measurement cannot handle, and why.

        Reported rather than dropped. An attribute silently missing from a report
        is indistinguishable from an attribute that came back clean.
        """
        out: list[dict] = []
        for spec in self.restricted.values():
            if spec.get("kind") != "categorical":
                out.append({**spec, "why": f"{spec.get('kind', 'unknown')}, and the "
                                           f"measurement compares two groups"})
            elif not spec.get("categories"):
                out.append({**spec, "why": "no categories declared, so there is no "
                                           "reference group to compare against"})
        return out

    def __repr__(self) -> str:
        return f"Policy({self.regime!r}, {len(self.restricted)} attributes)"


def load(regime: str | None = None) -> Policy:
    return Policy(regime or DEFAULT_REGIME)


def _show(policy: Policy) -> None:
    print(f"{policy.name}  ({policy.regime})")
    print(f"  {policy.long_name}")
    if policy.jurisdiction:
        print(f"  jurisdiction: {policy.jurisdiction}")
    if policy.citation:
        print(f"  citation: {policy.citation}")
    print(f"  tags in scope: {', '.join(policy.protected_tags)}")
    if policy.decision_context:
        print(f"  applies to: {' '.join(policy.decision_context.split())}")

    print(f"\n  restricted attributes, {len(policy.restricted)}:")
    ranked = sorted(policy.restricted.values(),
                    key=lambda s: (ORDER.get(s["basis"], 9), s["attribute"]))
    for spec in ranked:
        testable = "" if spec in policy.testable() else "   (tags only)"
        print(f"    {spec['basis']:12} {spec['attribute']:16} "
              f"{spec.get('column', ''):22}{testable}")
        if spec.get("citation"):
            print(f"    {'':12} {spec['citation']}")

    if policy.duties:
        print(f"\n  duties about the decision, {len(policy.duties)}:")
        for duty in policy.duties:
            print(f"    {duty.get('name', '')}")
            print(f"      {' '.join((duty.get('citation') or '').split())}")
            print(f"      requires {len(duty.get('requires') or [])}, "
                  f"we supply {len(duty.get('ariadne_supplies') or [])}, "
                  f"we do not supply {len(duty.get('not_supplied') or [])}")

    skipped = policy.untestable()
    if skipped:
        print(f"\n  not measurable by reconstruction, {len(skipped)}:")
        for spec in skipped:
            print(f"    {spec['attribute']:16} {' '.join(spec['why'].split())}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="regimes declared in policy/")
    p_show = sub.add_parser("show", help="one regime, resolved against the warehouse")
    p_show.add_argument("regime", nargs="?", default=DEFAULT_REGIME)
    args = ap.parse_args()

    if args.cmd == "list":
        declared = attributes()
        print(f"{len(declared)} attributes declared for this warehouse: "
              f"{', '.join(sorted(declared))}\n")
        # widths come from the packs themselves. a fixed width was fine for three
        # regimes and broke the moment one arrived with a longer name than the
        # pad, which is the sort of thing that silently uglifies captured output.
        loaded = [(regime, Policy(regime)) for regime in available()]
        wid = max(len(regime) for regime, _ in loaded)
        wname = max(len(policy.name) for _, policy in loaded)
        for regime, policy in loaded:
            print(f"  {regime:{wid}}  {policy.name:{wname}}  "
                  f"{len(policy.restricted)} attributes, "
                  f"tags {', '.join(policy.protected_tags)}")
        return 0

    _show(Policy(args.regime))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
