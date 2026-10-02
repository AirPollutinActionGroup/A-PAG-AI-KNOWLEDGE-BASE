"""Making a follow-up question searchable.

Each question reaches retrieval on its own. That is right for "what are the FGD timelines" and
wrong for "what about Category B?", which carries almost none of the words that would find the
passage it is about -- the subject lives in the question before it.

The fix is **query expansion, not conversation state**: when a question looks like a follow-up,
the previous question's words are prepended *for the search only*. The model still receives the
question as asked, and the expansion never reaches the answer or a citation.

Deliberately narrow. Expanding every question would drag the previous subject into genuinely new
ones -- ask about FGD timelines, then about stubble burning, and the second search would be half
about FGD. So expansion happens only when the question is too thin to stand alone, by two tests
that are both about the question's own shape rather than about what it means:

- it opens with a referring word ("what about...", "and the deadlines?"), or
- it is very short and carries no noun the corpus would recognise.

Both are heuristics and both are visible in the response, because a search that quietly searched
for something other than what was typed is worse than one that failed.
"""

import re

# Openers that point back at something already said. A question starting this way is continuing
# the previous one, not beginning a new one.
_REFERRING_OPENERS = (
    "what about", "and what", "and the", "what of", "how about", "and how",
    "is it", "are they", "are these", "does it", "do they", "did it", "was it",
    "why is", "why are", "why did", "when is", "when did", "who is", "who are",
    "and", "but", "also", "then", "so",
)

# Pronouns and determiners with no referent inside the question itself.
_DANGLING = re.compile(
    r"\b(it|its|they|them|their|this|that|these|those|the same|above|there)\b",
    re.IGNORECASE,
)

# Long enough to stand on its own, measured in words rather than characters so a question full
# of long terms is not mistaken for a thin one.
_SELF_CONTAINED_WORDS = 8


def looks_like_followup(question: str) -> bool:
    """True when a question probably cannot be searched on its own.

    Conservative on purpose: a false positive drags the previous subject into a genuinely new
    question, which is worse than a false negative, where the search is merely as good as it
    was before this existed.
    """
    q = (question or "").strip().lower()
    if not q:
        return False

    words = q.split()
    if len(words) <= 3:
        return True

    if any(q.startswith(opener + " ") for opener in _REFERRING_OPENERS):
        return True

    # A short question leaning on a pronoun: "does it apply to Category C?"
    return len(words) < _SELF_CONTAINED_WORDS and bool(_DANGLING.search(q))


def expand(question: str, previous: str | None) -> tuple[str, bool]:
    """Returns the query to search with, and whether it was expanded.

    The flag is returned rather than inferred by comparing strings, because the caller shows it
    to the reader: a search that silently looked for something other than what was typed is
    worse than one that found nothing.
    """
    previous = (previous or "").strip()
    question = (question or "").strip()
    if not previous or not question or not looks_like_followup(question):
        return question, False

    # The previous question's words, not its answer. An answer is long, full of boilerplate, and
    # would swamp the terms that actually matter in a short follow-up.
    return f"{previous} {question}", True
