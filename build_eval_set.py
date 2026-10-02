"""Draft an evaluation set from the corpus, for colleagues to correct.

There is no way to tell whether a retrieval change helped. BM25, OCR and reranking all shipped
on a demonstration -- "look, this passage moved from rank 23 to rank 1, and it reads better" --
which is an argument, not a measurement. An evaluation set turns the next change into a number.

**What this produces is a draft, not an answer key.** The questions are generated from real
passages, and the passage they came from is recorded as the expected source. That is the part
worth keeping: it is objective, it needs no judgement, and it supports the measurement that
matters most -- did retrieval put the right document in front of the model. The expected
*answer* is left blank on purpose, because inventing one from the same passage the question came
from would be marking our own homework.

So the workflow is: generate, hand to colleagues, let them fix the wording, delete the questions
nobody would ask, and add the ones they actually ask that this missed. The last of those is the
most valuable and is the one thing a generator cannot produce.

    python build_eval_set.py --count 100 --out eval_set.json
    python build_eval_set.py --count 100 --out eval_set.json --with-model   # richer phrasing
"""

import argparse
import json
import os
import random
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.getcwd())

from sqlalchemy import text as sql

from src.db.engine import SessionLocal

# Passages that make poor questions: a page of numbers, a signature block, a contents list.
_MIN_CHARS = 320
_MAX_CHARS = 2400


def _prose_ratio(text: str) -> float:
    if not text:
        return 0.0
    counted = sum(
        len(tok) for tok in text.split()
        if len(tok) >= 3 and not any(c.isdigit() for c in tok)
    )
    return counted / len(text)


def candidates(db, per_document: int) -> list[dict]:
    """Passages worth asking about, spread across documents rather than concentrated.

    Spread matters: Part_7 has 283 passages and the Ministry memorandum has 2, so sampling
    uniformly would produce an evaluation set that is mostly one document and would say nothing
    about whether the rest of the corpus is reachable.
    """
    rows = db.execute(sql("""
        SELECT c.chunk_id, c.document_id, c.text, c.page_number, c.section_heading,
               c.is_table, d.filename, d.extraction_method
        FROM document_chunks c
        JOIN documents d ON d.document_id = c.document_id
        WHERE d.status = 'LIVE' AND d.deleted_at IS NULL
          AND length(c.text) BETWEEN :lo AND :hi
          AND c.is_table = false
        ORDER BY c.document_id, c.chunk_index
    """), {"lo": _MIN_CHARS, "hi": _MAX_CHARS}).mappings().all()

    by_doc = defaultdict(list)
    for r in rows:
        if _prose_ratio(r["text"]) < 0.45:
            continue
        by_doc[r["document_id"]].append(dict(r))

    picked = []
    rng = random.Random(20261002)
    for chunks in by_doc.values():
        rng.shuffle(chunks)
        picked.extend(chunks[:per_document])
    rng.shuffle(picked)
    return picked


# ----------------------------------------------------------------------------------
# Without a model: questions derived from what the passage states.
# ----------------------------------------------------------------------------------

_SENTENCE = re.compile(r"(?<=[.;:])\s+")

_PATTERNS = (
    # (regex over the passage, question template, what it is testing)
    (re.compile(r"\b(?:by|before|upto|up to)\s+(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s+\d{4}|\w+\s+\d{4})", re.IGNORECASE),
     "What is the deadline of {0} for?", "a date"),
    (re.compile(r"\b(Category\s+[ABC])\b"),
     "What applies to {0} thermal power plants?", "a category"),
    (re.compile(r"\b((?:Rs\.?|₹)\s?[\d.]+(?:\s?(?:per unit|paise|crore|lakh))?)", re.IGNORECASE),
     "What is {0} charged for?", "an amount"),
    (re.compile(r"\b(Section\s+\d+[A-Z]?)\b"),
     "What does {0} provide for?", "a section reference"),
    (re.compile(r"\b((?:S\.?O\.?|G\.?S\.?R\.?)\s?\d+\s?\(E\))", re.IGNORECASE),
     "What does notification {0} cover?", "a notification number"),
    (re.compile(r"\b(\d{1,3}(?:,\d{2,3})*\s?MW)\b"),
     "What does the figure {0} refer to?", "a capacity figure"),
    (re.compile(r"\b(FGD|CEMS|SO2|NOx|ESP|SNCR|SCR)\b"),
     "What does this corpus say about {0}?", "an abbreviation"),
)


def derive(chunk: dict) -> list[dict]:
    text = chunk["text"]
    out = []
    for pattern, template, kind in _PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        out.append({"question": template.format(m.group(1).strip()), "tests": kind})
    heading = (chunk["section_heading"] or "").strip()
    if heading and 8 < len(heading) < 90 and not heading[0].isdigit():
        out.append({"question": f"What does the corpus say about {heading}?",
                    "tests": "a section heading"})
    return out


# ----------------------------------------------------------------------------------
# With a model: ask Sarvam to write the question a colleague would actually ask.
# ----------------------------------------------------------------------------------

_PROMPT = """You write evaluation questions for a policy knowledge base used by an Indian air \
quality organisation.

Read the passage and write ONE question that it answers. Rules:
- The question must be answerable from this passage alone.
- Write it as a colleague would ask it, not as a quiz. No "according to the passage".
- Be specific enough that a search engine could find this passage from the question.
- Do not include the answer.
- Reply with the question and nothing else."""


def with_model(chunk: dict) -> str | None:
    import httpx

    from src.core.config import settings

    key = settings.SARVAM_API_KEY
    if not key:
        return None
    try:
        r = httpx.post(
            "https://api.sarvam.ai/v1/chat/completions",
            json={
                "model": settings.SARVAM_MODEL,
                "messages": [
                    {"role": "system", "content": _PROMPT},
                    {"role": "user", "content": chunk["text"][:2400]},
                ],
                "temperature": 0.4, "max_tokens": 120,
            },
            headers={"Authorization": f"Bearer {key}", "api-subscription-key": key,
                     "Content-Type": "application/json"},
            timeout=90.0,
        )
        if r.status_code >= 400:
            return None
        content = (r.json()["choices"][0]["message"].get("content") or "").strip()
        return content.strip('"').split("\n")[0] or None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--per-document", type=int, default=6,
                    help="Cap per document, so one large file cannot dominate the set.")
    ap.add_argument("--out", default="eval_set.json")
    ap.add_argument("--with-model", action="store_true",
                    help="Use Sarvam to phrase each question. Slower, and costs about "
                         "Rs 0.02 per question, but reads like something a person would ask.")
    args = ap.parse_args()

    with SessionLocal() as db:
        pool = candidates(db, args.per_document)

    print(f"{len(pool)} passages eligible across "
          f"{len({c['document_id'] for c in pool})} documents")

    items, seen = [], set()
    for chunk in pool:
        if len(items) >= args.count:
            break

        questions = []
        if args.with_model:
            q = with_model(chunk)
            if q:
                questions.append({"question": q, "tests": "model-phrased"})
        if not questions:
            questions = derive(chunk)

        for q in questions:
            key = q["question"].lower().strip()
            if key in seen or len(items) >= args.count:
                continue
            seen.add(key)
            items.append({
                "id": len(items) + 1,
                "question": q["question"],
                "tests": q["tests"],
                # The objective half of the answer key: retrieval is correct when it puts this
                # document in front of the model. Filled in automatically and needs no review.
                "expect_document": chunk["filename"].rsplit("/", 1)[-1],
                "expect_page": chunk["page_number"],
                "expect_section": chunk["section_heading"],
                "source_chunk_id": str(chunk["chunk_id"]),
                "read_by": chunk["extraction_method"] or "NATIVE",
                # For a human to fill in. Left blank deliberately — generating it from the same
                # passage the question came from would be marking our own homework.
                "expected_answer": "",
                "reviewed_by": "",
                "notes": "",
            })

    payload = {
        "version": 1,
        "generated": "draft - needs review by A-PAG colleagues",
        "how_to_use": (
            "Fix the wording, delete questions nobody would ask, and add the ones you do ask "
            "that are missing. `expect_document` is the objective half and needs no review: "
            "retrieval is correct when it returns that document. Leave `expected_answer` blank "
            "unless you want to grade answer quality too."
        ),
        "count": len(items),
        "questions": items,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    by_doc = defaultdict(int)
    for i in items:
        by_doc[i["expect_document"]] += 1
    print(f"\nwrote {len(items)} questions to {args.out}, across {len(by_doc)} documents")
    for name, n in sorted(by_doc.items(), key=lambda kv: -kv[1])[:8]:
        print(f"   {n:>3}  {name[:66]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
