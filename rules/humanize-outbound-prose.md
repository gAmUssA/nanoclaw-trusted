---
alwaysApply: false
applyTo: "** — when drafting an email or other prose the operator will send under their name"
---

# Humanize Outbound Prose

## When It Applies

- Any email draft written for the operator: a new email, a reply, a forwarding note
- Any other prose the operator will send under their name to someone else: a LinkedIn or Slack message, a comment, a cover note, an abstract
- Not chat replies to the operator, status posts, or summaries of inbound mail

## Required Behavior

- Write the draft, then run `Skill(skill: "tessl__blog-writer")` on it as a humanization pass before posting it to the chat
- Ask the skill to humanize the existing draft, keeping its format, language, recipient and purpose
- Post only the humanized draft, never the pre-pass draft
- Names, dates, numbers, links, quotes and commitments stay exactly as given; the pass changes wording, never content

## Missing Skill

- Narrow exception for a container without `tessl__blog-writer`.
- Preconditions (all required):
  1. The `Skill()` call fails because the skill is not installed
  2. The posted draft carries one line saying the humanizer pass did not run
- Every other draft runs the pass before it is posted
