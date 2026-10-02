"""Score *answer* quality with RAGAS, using OpenAI as the judge.

`run_eval.py` measures whether retrieval found the right document. It cannot see whether the
answer that followed was faithful to it -- an answer can cite the correct page and still say
something the page does not support, and that is the failure this system exists to prevent.
RAGAS measures that, with a second model reading the answer against the passages it came from.

Three metrics, chosen because they need no answer key:

    faithfulness        is every claim in the answer supported by the retrieved passages?
                        this is hallucination detection, and the reason to run this at all
    response_relevancy  does the answer address the question that was asked?
    context_precision   were the retrieved passages relevant, or padding?

`context_recall` is deliberately absent: it needs a ground-truth answer, and `eval_set.json`
leaves `expected_answer` blank on purpose. Fill those in during review and it can be added.

**This sends passages to OpenAI, which is a second external destination.** The architecture is
explicit that nothing reaches an external model except through the Data Boundary Gateway, and an
evaluation harness is not an exception -- it is exactly the "second path out" that would make the
gateway decorative. So the same rules apply here, enforced rather than assumed:

  - only PUBLIC documents are evaluated; a RESTRICTED passage is never part of a run
  - every passage is redacted through the gateway's own recogniser set before it is sent
  - what was masked is reported at the end, so a run that leaked nothing can be shown to have
    leaked nothing

The judge is OpenAI rather than Sarvam on purpose. Using the model under test to grade its own
answers measures its self-consistency, not its truthfulness.

    export OPENAI_API_KEY=sk-...
    python run_ragas.py --count 25
    python run_ragas.py --count 25 --judge gpt-5-mini
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


def public_only(db, results):
    """Drops any passage from a document that is not explicitly PUBLIC.

    The same rule the gateway applies, applied again here rather than trusted, because this
    harness talks to a different provider and a filter that exists in one place is a filter
    that will eventually be missing from the other.
    """
    from src.db.models import Document as DocumentORM

    if not results:
        return []
    ids = {r.document_id for r in results}
    rows = db.query(DocumentORM.document_id, DocumentORM.classification).filter(
        DocumentORM.document_id.in_(ids)
    ).all()
    tier = {row[0]: row[1] for row in rows}
    return [r for r in results if tier.get(r.document_id) == Classification.PUBLIC.value]


def build_samples(questions, limit, passages):
    """Runs each question through the real pipeline and collects what RAGAS needs."""
    retrieval = RetrievalService(provider=FastEmbedProvider())
    answers = AnswerService(gateway=DataBoundaryGateway(provider=SarvamProvider()))

    samples, skipped, masked_total = [], [], {}
    viewer = uuid.uuid4()

    with SessionLocal() as db:
        for i, q in enumerate(questions[:limit], 1):
            question = q["question"]
            hits, _ = retrieval.search(
                db, question, user_id=viewer, is_admin=True,
                limit=passages, mode=SearchMode.HYBRID,
            )
            hits = public_only(db, hits)
            grounded, _ = assess(hits, question)
            if not grounded or not hits:
                # An ungrounded question never reaches a model, so there is no answer to judge.
                # Counted rather than dropped silently: a run that quietly evaluated 12 of 25
                # questions would report a score for a different set than the one requested.
                skipped.append(question)
                continue

            result = answers.answer(question, hits, grounded=True,
                                    tiers=[Classification.PUBLIC.value] * len(hits))
            if not result.answer:
                skipped.append(question)
                continue

            # Redacted before anything is handed to the judge. The gateway already redacted what
            # went to Sarvam; this is the same step for the second destination.
            used = hits[: settings.GENERATION_MAX_PASSAGES]
            contexts, masked = DataBoundaryGateway.redact([h.text for h in used])
            for m in masked:
                masked_total[m.label] = masked_total.get(m.label, 0) + m.count

            samples.append({
                "user_input": question,
                "response": result.answer,
                "retrieved_contexts": contexts,
            })
            print(f"  {i:>3}/{min(limit, len(questions))}  {question[:62]}")

    return samples, skipped, masked_total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--set", default="eval_set.json")
    ap.add_argument("--count", type=int, default=25,
                    help="Questions to judge. Each costs a Sarvam answer plus several judge "
                         "calls, so start small.")
    ap.add_argument("--passages", type=int, default=8)
    ap.add_argument("--judge", default=os.environ.get("RAGAS_JUDGE_MODEL", "gpt-5-mini"))
    ap.add_argument("--out", default="ragas_report.json")
    args = ap.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set. The judge is OpenAI rather than Sarvam on purpose: "
              "using the model under test to grade its own answers measures self-consistency, "
              "not truthfulness.")
        return 2
    if not settings.SARVAM_API_KEY:
        print("SARVAM_API_KEY is not set, so there are no answers to judge.")
        return 2

    with open(args.set, encoding="utf-8") as fh:
        questions = [q for q in json.load(fh)["questions"] if q.get("question")]

    print(f"building answers for {min(args.count, len(questions))} question(s)\n")
    samples, skipped, masked = build_samples(questions, args.count, args.passages)

    if not samples:
        print("\nnothing to judge.")
        return 1

    print(f"\n{len(samples)} answered, {len(skipped)} ungrounded (never reached a model)")
    if masked:
        print(f"redacted before the judge saw them: {masked}")
    else:
        print("no pattern matched in these passages; nothing needed redacting")

    from langchain_openai import ChatOpenAI
    from ragas import EvaluationDataset, evaluate
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (
        Faithfulness,
        LLMContextPrecisionWithoutReference,
        ResponseRelevancy,
    )

    judge = LangchainLLMWrapper(ChatOpenAI(model=args.judge))
    print(f"\njudging with {args.judge}\n")

    result = evaluate(
        dataset=EvaluationDataset.from_list(samples),
        metrics=[
            Faithfulness(),
            ResponseRelevancy(),
            LLMContextPrecisionWithoutReference(),
        ],
        llm=judge,
    )

    print("\n" + "=" * 70)
    print(result)

    scores = {k: v for k, v in dict(result).items()} if hasattr(result, "keys") else {}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({
            "judge": args.judge,
            "answered": len(samples),
            "ungrounded": len(skipped),
            "redacted": masked,
            "scores": scores,
        }, fh, indent=2, ensure_ascii=False, default=str)
    print(f"\nwritten to {args.out}")

    print("\nfaithfulness is the one to watch: it asks whether every claim in the answer is")
    print("supported by the passages it was given. A low score there is hallucination, which")
    print("no amount of retrieval accuracy would have caught.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
