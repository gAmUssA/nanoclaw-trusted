---
alwaysApply: true
---

# Humanize Outbound Prose

## When It Applies

- Any email draft written for the operator: a new email, a reply, a forwarding note
- Any other prose the operator will send under their name to someone else: a LinkedIn or Slack message, a comment, a cover note, an abstract
- Not chat replies to the operator, status posts, or summaries of inbound mail

## Required Behavior

- Write the draft, then run `Skill(skill: "tessl__blog-writer")` on it as a humanization pass before posting it to the chat
- The skill takes a non-blog artifact through its scoped-edit route: keep the draft's format, language, recipient and purpose
- Post only the humanized draft, never the pre-pass draft
- Names, dates, numbers, links, quotes and commitments stay exactly as given; the pass changes wording, never content
- No personal identity is installed in containers, so voice calibration reports `not applicable`; the catalog and structural passes still run

## When the Skill Is Missing

- `tessl__blog-writer` not installed: post the draft with one line saying the humanizer pass did not run
- Never skip the pass silently
