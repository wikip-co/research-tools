# Research Publishing Style Guide

Use this guide when an agent turns research sources into `content` markdown.

## Source Of Truth

- Write article content in the `content` repo only.
- Do not edit generated HTML or the `wikip.co/public` output repo.
- Prefer updating an existing article when the source naturally belongs there.
- Create a new article only when there is no strong existing match.

## Tone And Evidence

- Write in a neutral, concise, encyclopedia-like style.
- Do not overstate findings. Match the strength of the claim to the evidence type.
- Identify study type when it matters: review, systematic review, meta-analysis, randomized trial, observational study, animal study, in vitro study, case report, or mechanistic paper.
- Be explicit about limits when evidence is preliminary, indirect, non-human, or mechanistic.
- Avoid medical advice language. Describe findings and mechanisms rather than telling readers what to do.

## Structure

- Preserve existing YAML frontmatter.
- Preserve existing heading levels and local organization.
- Use short paragraphs or bullets consistent with the target article.
- Put new research under the most relevant existing heading.
- If no heading fits, add a clear `###` subsection near related material.
- Keep tags focused. Add tags only when they improve navigation and match existing tag style.

## Citations

- Every research-backed claim needs a footnote.
- Reuse an existing footnote when the source is already cited.
- Append new references at the bottom using the existing numbered footnote style.
- Prefer rich reference metadata when available:
  - `**Title:** [Title](URL)<br>`
  - `**Publication:** [Journal](URL)<br>`
  - `**Date:** ...<br>`
  - `**Study Type:** ...<br>`
  - `**Author(s):** ...<br>`
  - `**Institutions:** ...<br>`
  - `**Copy:** [archive](URL)`
- Archive PDFs or source snapshots when the workflow supports it.

## Content Integration

- Search and match before writing to avoid duplicate articles.
- Check whether the URL, DOI, or PMID is already cited.
- Summarize findings in your own words.
- Avoid dumping abstracts into the article.
- Keep additions proportional. A single paper usually deserves a concise update, not a full rewrite.
- Leave unrelated sections alone.

## Pull Requests

- Open a draft PR in `content` for review.
- The PR should include only relevant markdown changes and required metadata updates.
- Mention changed article paths and any unprocessed sources in the final agent message.
