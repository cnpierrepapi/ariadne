# Ariadne and the EU AI Act

The Act asks operators of high risk AI systems to prove things about their training
data that most teams cannot currently prove. Not to promise them, to document them,
keep the documentation current, and hand it to an authority on request.

That is a lineage problem wearing a legal hat. This page maps what the Act asks for
onto what Ariadne actually produces, and is explicit about the parts it does not
cover.

**Verify before relying on any of this.** Article numbers and dates below reflect
the Regulation as published, but the compliance timeline has been subject to
amendment proposals, and the obligations that bind you depend on your role,
provider or deployer, and on your system's classification. This is engineering
documentation, not legal advice, and no part of it substitutes for counsel.

## Why this matters now

High risk obligations under Annex III attach on **2 August 2026**. The Regulation
entered into force in August 2024, prohibited practices bound from February 2025,
and general purpose model obligations from August 2025.

Confirm the current date for your own classification before planning around it.
Amendment proposals to stagger or delay parts of the high risk regime were under
discussion, and the operative date is the one in force, not the one first
published.

## Whether this applies to you

Annex III lists the high risk categories. Ariadne is relevant wherever a model
decides something about a person using data assembled from a warehouse:

| Annex III area | The decision | What leaks in |
| --- | --- | --- |
| Creditworthiness and credit scoring | Loan approval, limit, price | Race, sex, age, marital status, and geography standing in for all four |
| Life and health insurance pricing | Premium, eligibility | Disability, age, health proxies such as prior spend |
| Employment, recruitment, worker management | Shortlisting, promotion, task allocation | Sex through occupation, age, disability, national origin through name or birthplace |
| Education and vocational training | Admission, assessment, proctoring | Socioeconomic proxies, national origin, disability |
| Essential public and private services | Benefits, housing, utilities | Postcode, household composition |
| Biometrics, law enforcement, migration, justice | Various | Out of scope for this reference stack |

The reference stack in this repository models the creditworthiness case, because
that is where the data is public and the rules are oldest. The machinery is not
specific to it. What changes between these rows is which attributes are sensitive
and which statute names them, and both are declared rather than compiled in.

## What the Act asks, and what Ariadne produces

### Article 10, data and data governance

The core obligation, and the one Ariadne exists to serve. Training, validation and
test data must be subject to governance practices appropriate to the purpose,
covering, among other things, examination for possible biases likely to affect
health, safety or fundamental rights, and measures to detect, prevent and mitigate
them.

| The obligation | What Ariadne produces |
| --- | --- |
| Examination for possible biases | `tools/reconstruct.py` measures whether a protected attribute can be rebuilt from the deployed model's features, held out, as a number rather than an assertion |
| Measures to detect and prevent | `tools/sentinel.py` fails a pipeline when a protected attribute reaches a deployed model, and exits non zero so it gates CI |
| Data provenance and collection process | `tools/trace.py` follows any feature back through every transformation to the source column, from connector emitted lineage, not hand written notes |
| Governance appropriate to the purpose | Sensitive attributes are declared on the dbt model with the statute naming them, and the checks read those declarations |

Two points worth understanding rather than skimming.

**Article 10(5) permits what the measurement needs.** Bias detection normally
requires the very special category data that the GDPR restricts. The Act
recognises the contradiction and permits processing special categories of personal
data where strictly necessary for bias detection and correction, subject to
safeguards including pseudonymisation and access controls. So holding race in the
warehouse in order to measure whether a model has reconstructed it is a
contemplated activity, not a workaround. Read the conditions carefully before
relying on this, as they are narrow.

**Article 10 does not ban correlation, and could not.** Income correlates with
race. So does education, occupation and neighbourhood. A model stripped of
everything correlated with a protected attribute predicts nothing at all. The
obligation is to examine, detect and mitigate, which presumes a human judgement
about necessity and alternatives. Ariadne supplies the examination. It does not,
and should not, return a verdict on legality.

### Article 9, risk management system

A continuous iterative process run across the entire lifecycle, requiring regular
systematic review and updating. Not a document produced once at launch.

`tools/exposure.py` records how much of each protected attribute the deployed model
carries, every time it is run, and fires when the figure moves further than
measured noise allows. The history is the review trail. Movement is attributed to
the columns the model gained between recordings, which is usually the answer.

This is the obligation most often met with a point in time audit, and a point in
time audit cannot satisfy a continuous requirement. A one line change to a
transformation can raise reconstructability of race by a wide margin while every
performance metric stays flat, which is demonstrated in [`examples/`](examples/).

### Article 11 and Annex IV, technical documentation

Annex IV requires, among much else, a description of the training methodology and
the data sets used, including provenance, scope and main characteristics, how the
data was obtained and selected, and labelling procedures.

Ariadne generates the provenance section rather than requiring someone to write
it. `tools/trace.py model <urn>` names the feature table, every feature, and for
each one the chain back to the source column. Because it reads the catalog rather
than a document, it cannot drift out of date the way a written annex does.

### Article 12, record keeping

Automatic recording of events over the system's lifetime. `state/exposure.json`
holds the measurement history with the model version, run identifier, feature list
and timestamp for each recording.

### Article 72, post market monitoring

Providers must actively collect and analyse data on performance throughout the
system's lifetime, against a documented plan. The delta watch is the part of such
a plan that covers the data side, which is the part performance monitoring
structurally cannot see.

### Article 26, deployer obligations

Deployers must use high risk systems in accordance with instructions, and where
they control input data, ensure it is relevant and sufficiently representative.
Anyone whose model is fed by a warehouse they own is exercising that control
whether or not they realise it. Column level lineage is how you establish which
inputs you actually control.

## What Ariadne does not do

Stated plainly, because a compliance tool that overstates its coverage is worse
than none.

- **It does not measure outcomes.** Disparate impact, adverse impact ratio and
  four fifths analysis are about who gets approved, not about what fed the model.
  Different question, different tooling.
- **It does not decide legality.** It reports what is present and what moved.
  Necessity, justification and less discriminatory alternatives are human
  judgements.
- **It does not cover the whole Act.** Human oversight (Article 14), accuracy and
  robustness (Article 15), transparency to deployers (Article 13), conformity
  assessment and registration are outside its scope entirely.
- **It cannot see what the catalog cannot see.** A feature computed inside
  application code, arriving at the model without passing through the warehouse,
  leaves no lineage to walk.
- **It needs the sensitive attribute to exist somewhere** in order to measure
  reconstructability. Where it does not, public reference data is the fallback,
  and that path is not built yet.

## Running the checks against your own stack

```bash
# does any protected attribute reach a deployed model
python tools/sentinel.py --fail-on-violation

# can the deployed model rebuild an attribute it does not contain
python tools/reconstruct.py --model <name> --repeats 3

# record it, and compare against the last recording
python tools/exposure.py record
python tools/exposure.py check --fail-on-violation
```

Both `--fail-on-violation` flags exit non zero, so either check drops into CI as a
gate on a transformation or training pipeline. Blocking a merge is a control in the
Article 10 sense. A dashboard nobody opens is not.
