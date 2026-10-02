"""Score retrieval against the evaluation set, so a change can be shown to have helped.

Measures the one thing that needs no human judgement: **did retrieval put the right document in
front of the model.** Every question in the set records the passage it was written from, so
"correct" means that document appeared, and where.

Three numbers, and the difference between them is the point:

    hit@1    the expected document was the top result
    hit@5    it was in the top five
    MRR      1/rank of the first correct hit, averaged -- rewards being first, not merely present

Deliberately not scoring answer text. That needs a judgement about wording, which means either a
human or a second model marking the first one's homework, and neither belongs in a number you
quote to compare two commits. What this does measure is the half that retrieval controls, which
is the half that changes when BM25, reranking or chunking changes.

    python run_eval.py                       # current configuration
    python run_eval.py --no-rerank           # what reranking is worth, on this corpus
    python run_eval.py --mode semantic       # what the lexical arm is worth
    python run_eval.py --compare             # all three, side by side
"""

import argparse
import json
import os
import statistics
import sys
import time
import uuid

sys.path.insert(0, os.getcwd())

from src.db.engine import SessionLocal
from src.modules.document_pipeline.embedding.provider import (
    FastEmbedProvider,
)
from src.modules.retrieval.service import RetrievalService, SearchMode


def rank_of(expected: str, hits) -> int | None:
    """1-based position of the first passage from the expected document, or None."""
    for i, h in enumerate(hits, 1):
        if h.filename.rsplit("/", 1)[-1] == expected:
            return i
    return None


def evaluate(service, questions, *, mode, rerank, limit, label):
    ranks, latencies, misses = [], [], []

    with SessionLocal() as db:
        # A throwaway admin identity: the question is whether retrieval finds the document, and
        # a permission filter that hid half the corpus would measure something else.
        viewer = uuid.uuid4()
        for q in questions:
            t = time.time()
            hits, _ = service.search(
                db, q["question"], user_id=viewer, is_admin=True,
                limit=limit, mode=mode, rerank=rerank,
            )
            latencies.append(time.time() - t)
            r = rank_of(q["expect_document"], hits)
            ranks.append(r)
            if r is None:
                misses.append(q)

    found = [r for r in ranks if r is not None]
    n = len(ranks)
    return {
        "label": label,
        "n": n,
        "hit@1": sum(1 for r in found if r == 1) / n if n else 0.0,
        "hit@5": sum(1 for r in found if r <= 5) / n if n else 0.0,
        "hit@k": len(found) / n if n else 0.0,
        "mrr": sum(1 / r for r in found) / n if n else 0.0,
        "median_s": statistics.median(latencies) if latencies else 0.0,
        "misses": misses,
    }


def show(res, limit):
    print(f"\n{res['label']}")
    print(f"   hit@1   {res['hit@1']:6.1%}   the expected document was the top result")
    print(f"   hit@5   {res['hit@5']:6.1%}   it was in the top five")
    print(f"   hit@{limit:<3} {res['hit@k']:6.1%}   it appeared at all")
    print(f"   MRR     {res['mrr']:6.3f}   1/rank of the first correct hit")
    print(f"   median  {res['median_s']:6.2f}s  per question")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--set", default="eval_set.json")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--mode", default="hybrid", choices=[m.value for m in SearchMode])
    ap.add_argument("--no-rerank", action="store_true")
    ap.add_argument("--compare", action="store_true",
                    help="Score hybrid+rerank, hybrid alone and semantic alone, side by side.")
    ap.add_argument("--show-misses", type=int, default=5)
    args = ap.parse_args()

    with open(args.set, encoding="utf-8") as fh:
        data = json.load(fh)
    questions = [q for q in data["questions"] if q.get("question") and q.get("expect_document")]
    print(f"{len(questions)} questions from {args.set}")

    service = RetrievalService(provider=FastEmbedProvider())

    if args.compare:
        runs = [
            (SearchMode.HYBRID, True, "hybrid + rerank  (shipped)"),
            (SearchMode.HYBRID, False, "hybrid, no rerank"),
            (SearchMode.SEMANTIC, False, "semantic only"),
            (SearchMode.LEXICAL, False, "lexical only (BM25)"),
        ]
    else:
        runs = [(SearchMode(args.mode), not args.no_rerank,
                 f"{args.mode}{'' if args.no_rerank else ' + rerank'}")]

    results = []
    for mode, rerank, label in runs:
        res = evaluate(service, questions, mode=mode, rerank=rerank,
                       limit=args.limit, label=label)
        results.append(res)
        show(res, args.limit)

    if len(results) > 1:
        print(f"\n{'configuration':28s} {'hit@1':>8s} {'hit@5':>8s} {'MRR':>8s} {'median':>9s}")
        print("-" * 66)
        for r in results:
            print(f"{r['label']:28s} {r['hit@1']:8.1%} {r['hit@5']:8.1%} "
                  f"{r['mrr']:8.3f} {r['median_s']:8.2f}s")

    # The misses are the useful output: each one is either a bad question or a real retrieval
    # failure, and reading a handful tells you which.
    worst = results[0]["misses"][: args.show_misses]
    if worst:
        print(f"\nnot found at all ({len(results[0]['misses'])} of {len(questions)}):")
        for q in worst:
            print(f"   {q['question'][:72]}")
            print(f"      expected {q['expect_document'][:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
