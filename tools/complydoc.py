"""Produce the compliance record for one model, one regime, one assessment.

WHAT THIS IS NOT. It is not a rendering of a policy pack. A pack says what a
statute restricts, which is the same on every day of the year and tells a
regulator nothing about whether this operator did anything. This document is
about ONE RUN: what the model was on that date, what changed since the previous
assessment, what was measured, what moved, what did not move, what was concluded
and where the record of it now lives.

The distinction matters because it is the difference between a policy and
evidence of compliance with a policy, and only the second is filed with anybody.

WHY THE SILENT MEASUREMENTS ARE IN IT. Section 5 lists every attribute that was
measured, including all the ones that found nothing. A report showing only what
fired is indistinguishable from a report where the rest was never run, and a
regulator asking whether an operator looked cannot tell those apart. The silence
is the evidence that the examination happened.

WHY DUTIES HAVE THEIR OWN SECTION. `restricted` in a pack answers "may this
attribute reach this model". A duty answers "what does the operator owe the person
the decision was made about": tell them, give them the principal factors, let them
reach a human. Quebec Law 25 s. 12.1, the ECOA adverse action notice and Article
86 of the EU AI Act are all of that kind, and none of them is expressible as a
list of forbidden columns. Section 9 states, per duty, what this record supplies
and what it does NOT, because a compliance document that quietly implies full
coverage of a duty it only partly evidences is worse than one that says so.

Every statute named comes from the pack. Nothing here knows any law.

    python tools/complydoc.py --model workforce-classifier --policy canada
    python tools/complydoc.py --model income-classifier --policy ecoa --out doc.pdf
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)

import history
from policy import load as load_policy

INK = colors.HexColor("#101418")
MUTED = colors.HexColor("#5c6675")
RULE = colors.HexColor("#c9d1dc")
ALARM = colors.HexColor("#b3261e")
BAND = colors.HexColor("#eef1f6")

# Kept in one place so the cover, the running footer and the file name cannot
# disagree about which document this is.
TITLE = "Model Data Provenance and Attribute Exposure Assessment"


def _styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle("h1", parent=base["Title"], fontName="Helvetica-Bold",
                             fontSize=17, leading=21, textColor=INK, alignment=0,
                             spaceAfter=2),
        "sub": ParagraphStyle("sub", parent=base["Normal"], fontName="Helvetica",
                              fontSize=10, leading=14, textColor=MUTED),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold",
                             fontSize=11, leading=14, textColor=INK,
                             spaceBefore=16, spaceAfter=5),
        "h3": ParagraphStyle("h3", parent=base["Heading3"], fontName="Helvetica-Bold",
                             fontSize=9.5, leading=13, textColor=INK,
                             spaceBefore=10, spaceAfter=3),
        "body": ParagraphStyle("body", parent=base["Normal"], fontName="Helvetica",
                               fontSize=9, leading=13, textColor=INK,
                               alignment=TA_JUSTIFY, spaceAfter=5),
        "note": ParagraphStyle("note", parent=base["Normal"], fontName="Helvetica-Oblique",
                               fontSize=8.5, leading=12, textColor=MUTED, spaceAfter=4),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontName="Helvetica",
                               fontSize=8, leading=10.5, textColor=INK),
        "cellb": ParagraphStyle("cellb", parent=base["Normal"], fontName="Helvetica-Bold",
                                fontSize=8, leading=10.5, textColor=INK),
        "mono": ParagraphStyle("mono", parent=base["Normal"], fontName="Courier",
                               fontSize=7.5, leading=10, textColor=INK),
    }


def _flat(text) -> str:
    return " ".join(str(text or "").split())


def _table(rows, widths, st, header=True, zebra=True):
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), BAND),
                  ("LINEBELOW", (0, 0), (-1, 0), 0.6, MUTED)]
    if zebra and header:
        for i in range(2, len(rows), 2):
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f7f9fc")))
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    t.setStyle(TableStyle(style))
    return t


def _bullets(items, st, prefix="•"):
    return [Paragraph(f"{prefix}&nbsp;&nbsp;{_flat(i)}", st["body"]) for i in items]


# --------------------------------------------------------------------------
# gathering. Everything that can fail is allowed to, and says so in the document
# rather than leaving a section quietly absent.
# --------------------------------------------------------------------------


def catalog_incidents(table: str | None, regime_name: str) -> tuple[list[dict], str | None]:
    """Incidents this regime has on the feature table, read from the catalog.

    Read back rather than assumed. If the write did not land, or the catalog is
    unreachable, that has to appear in the document as an unfiled finding rather
    than as a silent omission.
    """
    if not table:
        return [], "no feature table recorded for this model"
    try:
        import graph
        import trace

        urn = None
        for name in (table, f"warehouse.{table}"):
            found = trace.entities(name)
            if found:
                for platform in ("dbt", "postgres", "mlflow"):
                    for candidate in found:
                        if trace.platform(candidate) == platform:
                            urn = candidate
                            break
                    if urn:
                        break
                urn = urn or found[0]
                break
        if not urn:
            return [], f"no catalog entity for {table}"
        query = ("query($urn: String!) { dataset(urn: $urn) { incidents(start:0,count:200)"
                 " { incidents { urn title priority status { state } } } } }")
        block = graph.gql(query, {"urn": urn})["dataset"]["incidents"]["incidents"] or []
        mine = [
            i for i in block
            if (i.get("status") or {}).get("state") == "ACTIVE"
            and (i.get("title") or "").endswith(f"under {regime_name}")
        ]
        return mine, None
    except Exception as exc:  # noqa: BLE001 - the reason is printed, not swallowed
        return [], f"catalog not reachable: {type(exc).__name__}"


def provenance(table: str | None, column: str) -> tuple[list[tuple[int, str]], str | None]:
    """Where a column entered, by walking column level lineage.

    Ordered furthest hop first, so the chain reads the way the data travelled
    rather than the way the walk ran. The name is tried with and without the
    catalog prefix because the graph and the model registry disagree about
    whether a table name carries one, and without both spellings this returns
    nothing and reads as an absence of lineage.
    """
    if not table or not column:
        return [], "no column to trace"
    try:
        import trace

        for name in (f"warehouse.{table}", table):
            found = trace.ancestry(name, column)
            if not found:
                continue
            ordered = sorted(found.items(), key=lambda kv: (-kv[1], kv[0][0]))
            chain = [(hops, f"{tbl}.{col}") for (tbl, col), hops in ordered]
            chain.append((0, f"{table}.{column}"))
            return chain, None
        return [], "no column lineage returned for either spelling of the table name"
    except Exception as exc:  # noqa: BLE001
        return [], f"lineage not reachable: {type(exc).__name__}"


# --------------------------------------------------------------------------
# the document
# --------------------------------------------------------------------------


def build(model: str, regime: str | None, out_path: str, operator: str) -> str:
    pack = load_policy(regime)
    st = _styles()
    recordings = history.for_model(history.load(), model)
    if len(recordings) < 2:
        raise SystemExit(f"need two recordings of {model} to assess a change")

    previous, current = recordings[-2], recordings[-1]
    findings, context = history.compare(previous, current)
    fired = {(f["attribute"], f["group"]) for f in findings}
    table = current.get("feature_table")
    gained = context.get("features_gained") or []

    assessed_on = (current.get("recorded_at") or "")[:10]
    doc_id = hashlib.sha256(
        f"{model}|{current.get('version')}|{pack.regime}|{current.get('recorded_at')}"
        .encode()).hexdigest()[:12].upper()

    incidents, incident_problem = catalog_incidents(table, pack.name)
    story: list = []

    # ---------- 1 identification ----------
    story += [
        Paragraph(TITLE, st["h1"]),
        Paragraph(f"{_flat(pack.long_name)}", st["sub"]),
        Spacer(1, 8),
        HRFlowable(width="100%", thickness=1, color=MUTED, spaceAfter=10),
    ]
    ident = [
        ["Document reference", doc_id],
        ["Operator", operator],
        ["Regime assessed against", f"{_flat(pack.long_name)}"],
        ["Jurisdiction", _flat(pack.jurisdiction) or "not stated in the pack"],
        ["Statutory citation", _flat(pack.citation) or "not stated in the pack"],
        ["System assessed", f"{model}, version {current.get('version')}"],
        ["Assessment date", assessed_on],
        ["Compared against", f"version {previous.get('version')}, "
                             f"{(previous.get('recorded_at') or '')[:10]}"],
        ["Generated", dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")],
    ]
    story.append(_table(
        [[Paragraph(a, st["cellb"]), Paragraph(b, st["cell"])] for a, b in ident],
        [52 * mm, 116 * mm], st, header=False, zebra=False))

    # ---------- 2 the system ----------
    story.append(Paragraph("1. System assessed", st["h2"]))
    story.append(Paragraph(
        f"The features listed are those the deployed model actually consumed, read "
        f"from the model registry rather than from documentation. Applicable decision "
        f"context under this regime: {_flat(pack.decision_context) or 'not stated.'}",
        st["body"]))
    sysrows = [
        ["Registered model", model],
        ["Version in Production", str(current.get("version"))],
        ["Feature table", table or "not recorded"],
        ["Training run", current.get("run_id") or "not recorded"],
        ["Feature count", str(len(current.get("features") or []))],
        ["Reported accuracy", f"{current.get('model_accuracy'):.4f}"
                              if current.get("model_accuracy") is not None else "not recorded"],
    ]
    story.append(_table(
        [[Paragraph(a, st["cellb"]), Paragraph(b, st["cell"])] for a, b in sysrows],
        [52 * mm, 116 * mm], st, header=False, zebra=False))
    story.append(Paragraph("Features consumed", st["h3"]))
    story.append(Paragraph(", ".join(current.get("features") or []), st["mono"]))

    # ---------- 3 what changed ----------
    story.append(Paragraph("2. Change since the previous assessment", st["h2"]))
    lost = context.get("features_lost") or []
    was, now = context.get("accuracy_was"), context.get("accuracy_now")
    if gained or lost:
        if gained:
            story.append(Paragraph(
                f"<b>Features added:</b> {', '.join(gained)}", st["body"]))
        if lost:
            story.append(Paragraph(
                f"<b>Features removed:</b> {', '.join(lost)}", st["body"]))
    else:
        story.append(Paragraph(
            "No change to the feature set between the two versions assessed.", st["body"]))
    if was is not None and now is not None:
        direction = "an increase" if now > was else "a decrease"
        story.append(Paragraph(
            f"Reported model accuracy moved from {was:.4f} to {now:.4f}, {direction} of "
            f"{abs(now - was):.4f}. This is recorded because it establishes what "
            f"performance monitoring would have shown at the time, and therefore whether "
            f"any conclusion in this document could have been reached from performance "
            f"monitoring alone.", st["body"]))

    # ---------- 4 method ----------
    story.append(Paragraph("3. Method", st["h2"]))
    story += _bullets([
        "For each attribute the regime restricts and this warehouse can express, a "
        "classifier is trained to recover that attribute from the feature set the "
        "deployed model consumes. The attribute under test is excluded from its own "
        "inputs, so the score is not the model reading the answer back to itself.",
        "Each measurement is repeated across three random seeds and reported with its "
        "spread, so that a change can be distinguished from a refit.",
        f"A change counts as real when it exceeds the larger of {history.FLOOR} and "
        f"{history.SIGMAS} times the pooled spread of the two measurements. The "
        "threshold is therefore derived from observed variance and is not selected.",
        "Attributes the measurement cannot handle are reported in section 5 with the "
        "reason, and are never dropped silently.",
        "Column provenance is established by walking column level lineage in the "
        "metadata catalog. It is a property of the recorded graph, not an inference.",
    ], st)

    # ---------- 5 all measurements ----------
    story.append(Paragraph("4. Measurements performed", st["h2"]))
    story.append(Paragraph(
        "Every measurement carried out in this assessment appears below, including "
        "those that found nothing. A record listing only the measurements that "
        "produced a finding cannot be distinguished from one where the remainder were "
        "never carried out.", st["body"]))

    before = {(m["column"], m["group"]): m for m in previous["measurements"]}
    rows = [[Paragraph(h, st["cellb"]) for h in
             ("Attribute", "Group compared", "Previous", "This run", "Change", "Result")]]
    silent = 0
    for m in current["measurements"]:
        old = before.get((m["column"], m["group"]))
        if not old:
            continue
        delta = m["auc"] - old["auc"]
        hit = (m["attribute"], m["group"]) in fired
        silent += 0 if hit else 1
        rows.append([
            Paragraph(m["attribute"], st["cell"]),
            Paragraph(f"{m['group']} vs {m['against']}", st["cell"]),
            Paragraph(f"{old['auc']:.4f}", st["cell"]),
            Paragraph(f"{m['auc']:.4f}", st["cell"]),
            Paragraph(f"{delta:+.4f}", st["cell"]),
            Paragraph("<b>EXCEEDED</b>" if hit else "within noise",
                      st["cellb"] if hit else st["cell"]),
        ])
    story.append(_table(rows, [24 * mm, 52 * mm, 20 * mm, 20 * mm, 20 * mm, 32 * mm], st))
    story.append(Paragraph(
        f"{len(rows) - 1} measurements carried out. {len(findings)} exceeded the "
        f"threshold. {silent} did not.", st["note"]))

    # ---------- 6 not measurable ----------
    untestable = pack.untestable()
    if untestable:
        story.append(Paragraph("5. Restricted attributes not measurable", st["h2"]))
        story.append(Paragraph(
            "These attributes are restricted under this regime and were not measured. "
            "They are stated because an omitted attribute is otherwise "
            "indistinguishable from one that came back clean.", st["body"]))
        rows = [[Paragraph(h, st["cellb"]) for h in ("Attribute", "Column", "Why not measured")]]
        for spec in untestable:
            rows.append([
                Paragraph(spec["attribute"], st["cell"]),
                Paragraph(spec.get("column", ""), st["cell"]),
                Paragraph(_flat(spec["why"]), st["cell"]),
            ])
        story.append(_table(rows, [30 * mm, 42 * mm, 96 * mm], st))
        story.append(Paragraph(
            "Governance over these attributes rests on catalog tags alone for this "
            "assessment.", st["note"]))

    # ---------- 7 findings ----------
    story.append(Paragraph("6. Findings", st["h2"]))
    if not findings:
        story.append(Paragraph(
            "No measurement exceeded its threshold in this assessment.", st["body"]))
    for f in findings:
        spec = pack.restricted.get(f["attribute"], {})
        chain, chain_problem = provenance(table, gained[0]) if gained else ([], "no column added")
        block = [
            Paragraph(f"{f['attribute']} ({f['group']} against {f['against']})", st["h3"]),
            Paragraph(
                f"Recoverability of this attribute from the model's feature set moved from "
                f"{f['was']:.4f} to {f['now']:.4f}, a change of {f['delta']:+.4f}. That is "
                f"{f['multiples_of_noise']:.0f} times the measured noise of "
                f"{f['noise_stdev']:.4f} and exceeds the threshold of {f['threshold']:.4f}. "
                f"Observed between version {f['from_version']} and version "
                f"{f['to_version']}.", st["body"]),
        ]
        if spec:
            block.append(Paragraph(
                f"<b>Status under this regime:</b> {spec.get('basis', 'unknown')}. "
                f"<b>Citation:</b> {_flat(spec.get('citation')) or 'none recorded'}."
                + (f" {_flat(spec.get('note'))}" if spec.get("note") else ""), st["body"]))
        else:
            block.append(Paragraph(
                "This attribute is measured but is not restricted under this regime. "
                "It is reported for completeness.", st["body"]))
        if gained:
            block.append(Paragraph(
                f"<b>Associated change:</b> the model gained "
                f"{', '.join(gained)} between the two assessments.", st["body"]))
        if chain:
            block.append(Paragraph("Provenance of the added column", st["h3"]))
            block.append(Paragraph(
                "Established by walking column level lineage in the catalog, furthest "
                "hop first. Where the column is named differently at a hop, that is the "
                "name it carries there.", st["note"]))
            prov = [[Paragraph(h, st["cellb"]) for h in ("Hops back", "Column at that point")]]
            for hops, where in chain:
                prov.append([
                    Paragraph(str(hops) if hops else "the model reads it here", st["cell"]),
                    Paragraph(where, st["mono"]),
                ])
            block.append(_table(prov, [26 * mm, 142 * mm], st))
        elif chain_problem:
            block.append(Paragraph(
                f"Provenance could not be included in this document: {chain_problem}.",
                st["note"]))
        story.append(KeepTogether(block))

    # ---------- 8 filed record ----------
    story.append(Paragraph("7. Record filed in the metadata catalog", st["h2"]))
    if incidents:
        story.append(Paragraph(
            "The following incidents are open in the catalog against the feature table "
            "under this regime, read back from the catalog rather than from any local "
            "record. They are the durable form of this assessment.", st["body"]))
        rows = [[Paragraph(h, st["cellb"]) for h in ("Severity", "Finding", "Catalog reference")]]
        for i in sorted(incidents, key=lambda x: x["title"]):
            rows.append([
                Paragraph(i.get("priority") or "", st["cell"]),
                Paragraph(i["title"].rsplit(" under ", 1)[0], st["cell"]),
                Paragraph(i["urn"], st["mono"]),
            ])
        story.append(_table(rows, [20 * mm, 76 * mm, 72 * mm], st))
    else:
        story.append(Paragraph(
            f"No open incidents were read back for this regime. {incident_problem or ''} "
            f"Findings in section 6 are therefore recorded in this document only.",
            st["body"]))

    # ---------- 9 duties ----------
    story.append(PageBreak())
    story.append(Paragraph("8. Duties concerning the decision", st["h2"]))
    story.append(Paragraph(
        "The sections above concern which attributes may reach the model. The duties "
        "below concern what is owed to the person a decision is made about, which is a "
        "separate obligation that does not reduce to a list of restricted columns. For "
        "each, this record states what it supplies and what it does not, so that no "
        "part of a duty is treated as discharged by this document when it is not.",
        st["body"]))
    if not pack.duties:
        story.append(Paragraph(
            "No duties of this kind are declared for this regime.", st["body"]))
    for duty in pack.duties:
        block = [Paragraph(_flat(duty.get("name")), st["h3"]),
                 Paragraph(f"<b>Citation:</b> {_flat(duty.get('citation'))}", st["body"])]
        if duty.get("scope_note"):
            block.append(Paragraph(
                f"<b>Scope:</b> {_flat(duty['scope_note'])}", st["body"]))
        if duty.get("applies_when"):
            block.append(Paragraph(
                f"<b>Applies when:</b> {_flat(duty['applies_when'])}", st["body"]))
        if duty.get("requires"):
            block.append(Paragraph("The duty requires", st["h3"]))
            block += _bullets(duty["requires"], st)
        if duty.get("ariadne_supplies"):
            block.append(Paragraph("Supplied by this assessment", st["h3"]))
            block += _bullets(duty["ariadne_supplies"], st)
        if duty.get("not_supplied"):
            block.append(Paragraph("NOT supplied by this assessment", st["h3"]))
            # not a dingbat. The base fonts have no glyph for one and reportlab
            # drops it silently, which left these four items with no marker at all.
            block += _bullets(duty["not_supplied"], st, prefix="&#215;")
        story.append(KeepTogether(block))

    # ---------- 10 limits ----------
    story.append(Paragraph("9. Scope and limitations", st["h2"]))
    limits = [
        "This assessment concerns the data reaching a model and what that data permits "
        "the model to infer. It does not determine whether any decision was unlawful. "
        "Correlation with a protected attribute is not itself prohibited; the questions "
        "of harm, business necessity and less discriminatory alternatives are not "
        "answered here.",
        "Attributes not declared for this warehouse cannot be assessed. Where the regime "
        "restricts an attribute that the warehouse does not hold in a form this "
        "assessment can read, its absence from this document reflects the data, not a "
        "conclusion of compliance.",
        "The measurement establishes what is recoverable from the feature set. It does "
        "not establish that the model in fact relies on that information when producing "
        "an output.",
        "Findings rest on the metadata catalog being an accurate record of the "
        "pipeline. Lineage that was never ingested cannot be walked.",
        "The absolute level of recoverability is difficult to interpret in isolation and "
        "no threshold is asserted for it. What is reported is the change between two "
        "dated assessments of the same model.",
    ]
    story += _bullets(limits, st)

    # ---------- 11 attestation ----------
    story.append(Paragraph("10. Basis of preparation", st["h2"]))
    story.append(Paragraph(
        f"Prepared automatically from the recorded measurement history and the metadata "
        f"catalog on {dt.datetime.now(dt.timezone.utc).strftime('%d %B %Y')}. The "
        f"statutory positions stated in this document are taken from the declared policy "
        f"pack <b>{pack.regime}</b> and were not determined by the tool. The pack should "
        f"be verified against the current consolidation of the instrument before this "
        f"document is relied upon.", st["body"]))
    story.append(Spacer(1, 14))
    story.append(_table([
        [Paragraph("Reviewed by", st["cellb"]), Paragraph("", st["cell"]),
         Paragraph("Date", st["cellb"]), Paragraph("", st["cell"])],
        [Paragraph("Accountable owner", st["cellb"]), Paragraph("", st["cell"]),
         Paragraph("Date", st["cellb"]), Paragraph("", st["cell"])],
    ], [34 * mm, 60 * mm, 16 * mm, 58 * mm], st, header=False, zebra=False))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(20 * mm, 12 * mm, f"{doc_id}   {model} v{current.get('version')}"
                                            f"   {pack.name}   {assessed_on}")
        canvas.drawRightString(190 * mm, 12 * mm, f"Page {canvas.getPageNumber()}")
        canvas.setStrokeColor(RULE)
        canvas.line(20 * mm, 15 * mm, 190 * mm, 15 * mm)
        canvas.restoreState()

    SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=22 * mm,
        title=f"{TITLE}, {model}", author="Ariadne",
        subject=_flat(pack.long_name),
    ).build(story, onFirstPage=footer, onLaterPages=footer)

    return doc_id


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--policy", help="regime, see tools/policy.py list")
    ap.add_argument("--out", help="output pdf path")
    ap.add_argument("--operator", default="Operator not stated",
                    help="the organisation running the model")
    args = ap.parse_args()

    out = args.out or f"{args.model}-{args.policy or 'ecoa'}.pdf"
    doc_id = build(args.model, args.policy, out, args.operator)
    size = os.path.getsize(out)
    print(f"{out}  {size:,} bytes  reference {doc_id}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
