"""Trimming a question down to the words BM25 can use.

BM25 is a bag-of-words scorer with OR semantics: every term contributes, and a passage matching
many common terms outscores one matching the single rare term that actually identifies the
answer. A natural-language question is mostly common terms, so the discriminating word drowns.

Measured on this corpus, same document, same index:

    "Yermarus bid status unit 2"                      -> FGD Installation Status - NCR.xlsx  (correct)
    "What was the status of the bid for unit 2 of
     the Yermarus Thermal Power Station"              -> TPP.docx                            (wrong)

Twelve function words swamped one proper noun. This was one of four questions in the evaluation
set that retrieval failed outright, and all four were of the same shape.

**Only the lexical arm gets the trimmed query.** The semantic arm is given the question as
asked, because an embedding reads word order and function words as meaning — "penalties for
non-compliance" and "non-compliance penalties" are near-identical to BM25 and meaningfully
different to a sentence encoder. Trimming both would fix one arm by damaging the other.

The list below is English function words plus the handful of interrogative frames a question
starts with. It deliberately contains **no domain terms**: "emission", "compliance" and
"thermal" are common in this corpus and still discriminating, and a stopword list that learns
the corpus is one that stops working when the corpus grows.
"""

import re

# Function words, question frames and the verbs that carry no subject matter. Kept short on
# purpose -- every word here is one BM25 can no longer match on, so the bar is "this word
# appears in roughly every passage and identifies none of them".
_STOPWORDS = frozenset((
    # articles and demonstratives
    "a", "an", "the", "this", "that", "these", "those",
    # copulas and auxiliaries
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does", "did", "done",
    "have", "has", "had", "having",
    # question frames
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    # prepositions and conjunctions
    "of", "in", "on", "at", "to", "for", "from", "by", "with", "within", "into", "onto",
    "about", "as", "and", "or", "but", "nor", "so", "then", "than", "if", "else", "while",
    "during",
    # pronouns
    "it", "its", "they", "them", "their", "there", "here", "i", "me", "my", "we", "us", "our",
    "you", "your", "he", "she", "his", "her",
    # modals
    "can", "could", "shall", "should", "will", "would", "may", "might", "must",
    # quantifiers
    "any", "some", "all", "each", "both", "few", "more", "most", "other", "such", "not", "no",
    "only", "own", "same",
    # instructions to the system, not subject matter
    "please", "tell", "show", "give", "list", "explain", "describe", "summarise", "summarize",
))

# A question mark is not a term; neither is a stray bullet. Punctuation that *is* part of a term
# -- the dots in S.O. 3305 (E), the hyphen in 2024-25 -- is left alone by splitting on
# whitespace rather than on non-word characters.
# A trailing full stop is dropped only when the token does not end in an initialism: `Station.`
# loses it, `S.O.` keeps it, because the dots are part of how a notification number is written.
_EDGE_PUNCT = re.compile(r"^[^\w(]+|(?<!\.\w)[^\w).]+$|(?<![A-Z])\.$")

# Below this many surviving terms, the trim has taken too much to be trusted and the original
# question is used instead. A two-word question is already as sharp as it is going to get.
_MIN_TERMS = 2


def lexical_query(question: str) -> str:
    """The question with function words removed, or the question unchanged.

    Returns the original whenever trimming would leave too little to search on, so a short or
    unusual question is never made worse than it was.
    """
    if not question:
        return question

    kept = []
    for raw in question.split():
        token = _EDGE_PUNCT.sub("", raw)
        if not token:
            continue
        # A single capital letter is a label here, not an article: "Category A", "Annexure B",
        # "unit C". Stripping it as a stopword turned "Category A plants" into "Category
        # plants", which matches every category in the corpus instead of one.
        if len(token) == 1 and token.isupper():
            kept.append(token)
            continue
        if token.lower() in _STOPWORDS:
            continue
        kept.append(token)

    if len(kept) < _MIN_TERMS:
        return question
    return " ".join(kept)
