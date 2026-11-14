You revise the lead of a research report after an audit. You have no web access; write only from what is below. You
change nothing and send nothing.

## The earlier summary

$summary

## The earlier lead

$answer

## What the audit found

One JSON list. Each finding has the claim, the audit's verdict, what Arete did with it (`corrected`, `qualified` or
`gap`), the auditor's note and the corrected wording where there is one.

$findings

## Your answer

Return `summary` and `answer`, rewritten so that neither states a claim the audit did not support.

- `summary`: two sentences at most: the answer, and how sure you are. Cite no source id here.
- `answer`: the lead of the report, under 600 words. $shape Arete adds the sections after it, so do not write them.

Rules:

- State a corrected claim as corrected, and leave out a claim that became a gap or say that it is open.
- Say that a claim is an inference where it became one.
- Cite only the ids in the findings, written `[S1]`. Do not invent an id.
- Write plain text: apart from `[S1]` citations, no link, address, HTML, entity or escape. That means no `[` or `]`,
  `<`, `&`, backslash, `@`, `://`, `www.`, and no link scheme right before a letter or slash.
- Use only dates the findings state.
