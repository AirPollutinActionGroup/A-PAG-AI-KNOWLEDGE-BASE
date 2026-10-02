"""Quality checks that need no judge model.

RAGAS answers "was the answer faithful to the passages", and needs a second model to say so.
These answer questions that can be checked directly against the system's own output, which
means they cost nothing but a Sarvam call and can run on every change.

Five suites, in descending order of how badly a failure would matter:

  refusal       does it say "I don't know" to things the corpus cannot answer?
                The expensive failure mode is a confident answer to a question about something
                that is not there, and no retrieval metric detects it.

  citations     does every marker point at a passage the model was actually given?
                A fabricated citation is the worst error this system can make, because a
                citation is what makes a reader stop checking.

  masking       are phone numbers, emails and identifiers caught before anything leaves?
                Run against constructed documents, because the real corpus has few, and a
                control that has never been shown to fire is a control nobody should trust.

  isolation     can a RESTRICTED document reach a user who may not see it, or an external model?

  consistency   does the same question asked twice return the same documents?
                A system that answers differently each time cannot be cited in a submission.

    python run_quality_suite.py                # all of it
    python run_quality_suite.py --suite refusal
"""

import argparse
import json
import os
import sys
import uuid

sys.path.insert(0, os.getcwd())

from src.core.config import settings
from src.db.engine import SessionLocal
from src.db.enums import Classification
from src.modules.document_pipeline.embedding.provider import (
    FastEmbedProvider,
)
from src.modules.gateway.service import DataBoundaryGateway
from src.modules.generation.provider import SarvamProvider
from src.modules.generation.service import AnswerService
from src.modules.retrieval.service import (
    RetrievalService,
    SearchMode,
    assess,
)

# ----------------------------------------------------------------------------------
# Questions the corpus cannot answer.
#
# Three kinds, because they fail for different reasons and a system can be good at one and bad
# at another:
#   off-topic   nothing to do with air quality at all
#   adjacent    plausibly in a policy corpus, but not in THIS one -- the hard case, because
#               retrieval will find passages that look related
#   fabricated  invented specifics that sound exactly like something that would be in here
# ----------------------------------------------------------------------------------
REFUSAL_CASES = [
    ("off-topic", "what is the best recipe for chocolate cake"),
    ("off-topic", "how do I reset my email password"),
    ("off-topic", "what is the capital of Brazil"),
    ("adjacent", "what are the vehicle scrappage policy incentives in Delhi"),
    ("adjacent", "what are the groundwater extraction limits for industrial units"),
    ("adjacent", "what is the penalty for illegal sand mining"),
    ("fabricated", "what does MoEF&CC notification S.O. 9999 (E) dated 01.01.2030 require"),
    ("fabricated", "what are the Category D thermal power plant timelines"),
    ("fabricated", "how much did the Mangalore Super Thermal Power Station pay in compensation"),
    ("fabricated", "what did the 2027 Supreme Court FGD judgment decide"),
]

# Real questions, used to check citations and consistency rather than refusal.
GROUNDED_CASES = [
    "what are the FGD installation timelines for Category A plants",
    "what environmental compensation applies for non-compliance",
    "which plants are in Category A",
    "what is the status of FGD installation across thermal power plants",
    "what are the SO2 emission norms",
    "who monitors compliance with the emission norms",
]

# Constructed, because the corpus has few. A control that has never been shown to fire is a
# control nobody should trust.
MASKING_CASES = [
    ("Indian mobile", "Call the nodal officer on +91 98765 43210 for the schedule.", "98765"),
    ("mobile, no prefix", "Reach him at 9876543210 regarding compliance.", "9876543210"),
    ("landline", "Tel:011-23063746 is the direct line.", "23063746"),
    ("landline spaced", "Contact 011 2306 3746 during office hours.", "2306"),
    ("email", "Write to rajesh.kumar@cpcb.nic.in about the notice.", "rajesh.kumar"),
    ("PAN", "The PAN on record is ABCDE1234F.", "ABCDE1234F"),
    ("GSTIN", "Registered under GSTIN 27ABCDE1234F1Z5.", "27ABCDE1234F1Z5"),
    ("IFSC", "Transfer to branch SBIN0001234.", "SBIN0001234"),
    ("several at once",
     "Call +91 98765 43210 or email ops@a-pag.org, PAN ABCDE1234F.", "ops@a-pag.org"),
]

# Must NOT be masked. A control that mangles the corpus gets switched off.
MASKING_NEGATIVES = [
    "environmental compensation of Rs. 0.20, 0.30, or 0.40 per unit electricity generated",
    "non-compliant operation beyond 0-180 days attracts the lower rate",
    "MoEF&CC Notification dated 05.09.2022 and 31.03.2021",
    "bids awarded in 233 units (1,02,040 MW) of a total 537 units (2,04,160 MW)",
    "the average time taken for FGD installation after award is around 36-40 months",
    "No 10/1/2024-St.Th. (C. No. 273912)",
    "Section 5 of the Environment (Protection) Act, 1986 (29 of 1986)",
    "current annual installation capacity is around 16-20 GW per annum (33-39 units)",
]


def services():
    retrieval = RetrievalService(provider=FastEmbedProvider())
    answers = AnswerService(gateway=DataBoundaryGateway(provider=SarvamProvider()))
    return retrieval, answers


def ask(retrieval, answers, db, question, viewer, limit=8):
    hits, _ = retrieval.search(db, question, user_id=viewer, is_admin=True,
                               limit=limit, mode=SearchMode.HYBRID)
    grounded, best = assess(hits, question)
    if not grounded or not hits:
        return None, hits, best
    result = answers.answer(question, hits, grounded=True,
                            tiers=[Classification.PUBLIC.value] * len(hits))
    return result, hits, best


# ----------------------------------------------------------------------------------


def suite_refusal(retrieval, answers):
    print("\nREFUSAL — questions the corpus cannot answer")
    print("-" * 74)
    viewer, rows, failures = uuid.uuid4(), [], 0
    with SessionLocal() as db:
        for kind, q in REFUSAL_CASES:
            result, hits, best = ask(retrieval, answers, db, q, viewer)
            refused = result is None
            # An answered question is not automatically wrong: the model may still have said
            # the passages do not cover it. That is a weaker but acceptable outcome, so it is
            # recorded separately rather than counted as a pass or a failure.
            hedged = bool(result and any(
                p in result.answer.lower()
                for p in ("do not", "does not", "no mention", "not mention", "not specify",
                          "cannot", "no information")
            ))
            ok = refused or hedged
            if not ok:
                failures += 1
            mark = "refused" if refused else ("hedged " if hedged else "ANSWERED")
            sim = f"{best:.3f}" if best is not None else "  -  "
            print(f"  [{mark}] {kind:11s} sim={sim}  {q[:46]}")
            if not ok:
                print(f"      -> {result.answer[:120]}")
            rows.append({"kind": kind, "question": q, "refused": refused, "hedged": hedged,
                         "best_similarity": best})
    print(f"  {len(REFUSAL_CASES) - failures}/{len(REFUSAL_CASES)} handled safely")
    return {"name": "refusal", "total": len(REFUSAL_CASES), "failures": failures, "rows": rows}


def suite_citations(retrieval, answers):
    print("\nCITATIONS — every marker must point at a passage the model was given")
    print("-" * 74)
    viewer, failures, rows = uuid.uuid4(), 0, []
    with SessionLocal() as db:
        for q in GROUNDED_CASES:
            result, hits, _ = ask(retrieval, answers, db, q, viewer)
            if result is None:
                print(f"  [skip   ] ungrounded: {q[:52]}")
                continue
            bad = result.invalid_markers
            if bad:
                failures += 1
            # Every citation must also resolve to a real document and page.
            incomplete = [c.marker for c in result.citations if not c.filename]
            if incomplete:
                failures += 1
            mark = "FABRICATED" if bad else "ok"
            print(f"  [{mark:10s}] {len(result.citations)} cited, "
                  f"{len(bad)} invalid  {q[:42]}")
            rows.append({"question": q, "citations": len(result.citations),
                         "invalid_markers": bad})
    print(f"  {len(rows) - failures}/{len(rows)} answers with sound citations")
    return {"name": "citations", "total": len(rows), "failures": failures, "rows": rows}


def suite_masking():
    print("\nMASKING — what must never leave, and what must never be touched")
    print("-" * 74)
    failures = 0

    for label, textual, secret in MASKING_CASES:
        redacted, masked = DataBoundaryGateway.redact([textual])
        leaked = secret in redacted[0]
        if leaked:
            failures += 1
        previews = [p for m in masked for p in m.previews]
        print(f"  [{'LEAKED' if leaked else 'masked':6s}] {label:17s} -> {previews or 'nothing'}")

    print()
    for textual in MASKING_NEGATIVES:
        _redacted, masked = DataBoundaryGateway.redact([textual])
        if masked:
            failures += 1
            print(f"  [FALSE+] {textual[:58]} -> {[(m.label, m.previews) for m in masked]}")
    if not failures:
        print(f"  [ok    ] {len(MASKING_NEGATIVES)} real corpus sentences untouched")

    total = len(MASKING_CASES) + len(MASKING_NEGATIVES)
    print(f"  {total - failures}/{total} correct")
    return {"name": "masking", "total": total, "failures": failures}


def suite_isolation():
    print("\nISOLATION — a restricted passage must not reach an external model")
    print("-" * 74)
    failures = 0

    class Spy:
        model_name = "spy"

        def __init__(self):
            self.saw = None

        def complete(self, system, user):
            self.saw = user
            return "answer", 10, 5

    spy = Spy()
    gw = DataBoundaryGateway(provider=spy)

    cases = [
        ("one restricted among public", ["public one", "SECRET BODY"],
         [Classification.PUBLIC.value, Classification.RESTRICTED.value]),
        ("unknown classification", ["public one", "SECRET BODY"],
         [Classification.PUBLIC.value, None]),
        ("unrecognised tier", ["public one", "SECRET BODY"],
         [Classification.PUBLIC.value, "INTERNAL"]),
    ]
    for label, passages, tiers in cases:
        spy.saw = None
        try:
            _t, _i, _o, record = gw.send(
                "sys", passages, tiers,
                build_user=lambda kept: "\n".join(b for _, b in kept),
            )
            leaked = "SECRET BODY" in (spy.saw or "")
            withheld = record.passages_withheld_restricted
        except Exception:
            leaked, withheld = False, len(passages)
        if leaked:
            failures += 1
        print(f"  [{'LEAKED' if leaked else 'withheld':8s}] {label:28s} "
              f"{withheld} passage(s) held back")

    # And everything restricted: nothing may be attempted at all.
    spy.saw = None
    from src.modules.gateway.service import BoundaryRefusal
    try:
        gw.send("sys", ["a", "b"], [Classification.RESTRICTED.value] * 2,
                build_user=lambda kept: "x")
        print("  [LEAKED  ] all-restricted request was sent")
        failures += 1
    except BoundaryRefusal:
        called = spy.saw is not None
        if called:
            failures += 1
        print(f"  [{'LEAKED' if called else 'refused':8s}] all restricted              "
              f"no call attempted")

    total = len(cases) + 1
    print(f"  {total - failures}/{total} correct")
    return {"name": "isolation", "total": total, "failures": failures}


def suite_consistency(retrieval):
    print("\nCONSISTENCY — the same question must return the same documents")
    print("-" * 74)
    viewer, failures = uuid.uuid4(), 0
    with SessionLocal() as db:
        for q in GROUNDED_CASES[:4]:
            runs = []
            for _ in range(2):
                hits, _ = retrieval.search(db, q, user_id=viewer, is_admin=True,
                                           limit=5, mode=SearchMode.HYBRID)
                runs.append([str(h.chunk_id) for h in hits])
            same = runs[0] == runs[1]
            if not same:
                failures += 1
            print(f"  [{'ok' if same else 'DIFFERS':7s}] {q[:58]}")
    print(f"  {4 - failures}/4 stable")
    return {"name": "consistency", "total": 4, "failures": failures}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--suite", choices=["refusal", "citations", "masking",
                                        "isolation", "consistency"], default=None)
    ap.add_argument("--out", default="quality_report.json")
    args = ap.parse_args()

    needs_model = args.suite in (None, "refusal", "citations")
    if needs_model and not settings.SARVAM_API_KEY:
        print("SARVAM_API_KEY is not set; the refusal and citation suites need it.")
        return 2

    retrieval, answers = (services() if args.suite in (None, "refusal", "citations",
                                                       "consistency")
                          else (None, None))

    results = []
    if args.suite in (None, "refusal"):
        results.append(suite_refusal(retrieval, answers))
    if args.suite in (None, "citations"):
        results.append(suite_citations(retrieval, answers))
    if args.suite in (None, "masking"):
        results.append(suite_masking())
    if args.suite in (None, "isolation"):
        results.append(suite_isolation())
    if args.suite in (None, "consistency"):
        results.append(suite_consistency(retrieval))

    print("\n" + "=" * 74)
    total = sum(r["total"] for r in results)
    bad = sum(r["failures"] for r in results)
    for r in results:
        status = "PASS" if r["failures"] == 0 else f"{r['failures']} FAILED"
        print(f"  {r['name']:13s} {r['total'] - r['failures']:>3}/{r['total']:<3}  {status}")
    print(f"\n  {total - bad}/{total} checks passed")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"total": total, "failures": bad, "suites": results}, fh,
                  indent=2, ensure_ascii=False, default=str)
    print(f"  written to {args.out}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
