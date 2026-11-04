"""Shared handling of a table's merged cells, for every reader that gets a grid of blocks.

A slide's table (python-pptx) and a screenshot of one (a ruled grid found in pixels) arrive the
same way once their cells are known: some cells are one block spread over several positions. Both
need the same two things done to that, so it is done once, here.
"""

# (row, column, height, width) of the merged block a cell belongs to; None for a cell of its own.
Span = tuple[int, int, int, int]


def flatten_header(rows: list[list[str]], spans: list[list[Span | None]]) -> list[list[str]]:
    """Turns a header drawn over two or three rows into one row of full column names.

    "SO2 (mg/NM3)" over "< 500 MW" and ">= 500 MW" is the two columns "SO2 (mg/NM3) < 500 MW" and
    "SO2 (mg/NM3) >= 500 MW". Left as two rows, the column holding 200 has no name, and a row
    group split by the chunker repeats only the top half of it.

    Leading rows that are one block across the table's whole width are titles, not headers, and
    are left alone for the chunker to treat as a caption. The header is then as many rows as the
    tallest block starting on its first row, or two where a block spans only some of the columns
    (a group name over its sub-columns).
    """
    n_rows = len(rows)
    n_cols = len(rows[0]) if rows else 0
    start = 0
    while start < n_rows and n_cols > 1 and (b := spans[start][0]) and b[3] == n_cols:
        start = b[0] + b[2]

    depth = 1
    for c in range(n_cols):
        block = spans[start][c] if start < n_rows else None
        if block and block[0] == start:
            depth = max(depth, block[2])
            if 1 < block[3] < n_cols:
                depth = max(depth, 2)

    if not (1 < depth <= 3 and start + depth < n_rows):
        return rows

    header = []
    for c in range(n_cols):
        parts: list[str] = []
        for r in range(start, start + depth):
            if rows[r][c] and (not parts or parts[-1] != rows[r][c]):
                parts.append(rows[r][c])
        header.append(" ".join(parts))
    return [*rows[:start], header, *rows[start + depth:]]
