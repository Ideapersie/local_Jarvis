---
name: interview-prep
description: Use when preparing for a specific upcoming interview. Turns one application plus the user's known weaknesses into a concrete prep plan, and records what to work on as prep notes.
---

# Preparing for one interview

You are given an application id and its details. Produce a plan the user can act
on this week, not a generic interview guide.

## Read first, in this order

1. `mcp__jarvis__get_prep_notes` for this application — what is already known and
   unresolved. **Do not repeat work that is already noted.**
2. `brain/weaknesses.md` — the gaps that keep recurring. This is the whole point:
   a plan that ignores them is a plan for a different candidate.
3. `brain/profile.md` — what roles are being targeted, so advice fits.
4. `mcp__jarvis__get_skills` — where confidence is furthest below target.
5. `brain/interview-log.md` — if this company appears, what happened last time.

## Then search

Search the web for this firm's actual process for this role: round structure,
question style, anything candidates consistently report. Prefer recent sources.

**If you cannot find anything specific to the firm, say so plainly and plan from
the role type instead.** A confident description of an interview process you
invented is worse than admitting the process is unknown — the user will prepare
for the wrong thing and only find out in the room.

## Write the plan

Keep it under 300 words. Structure:

- **Format** — what the round is likely to involve, with a source. Mark clearly
  when this is inferred from the role type rather than found.
- **Days available** — you are told how many. Allocate concretely: what to do
  today, what to do the day before. Do not produce a seven-day plan for a
  two-day gap.
- **Weakest link** — the one thing most likely to lose this interview, drawn
  from `weaknesses.md` and the skills gaps. One item, not a list. If the honest
  answer is that there is not enough history to say, say that.
- **Questions to ask them** — two, specific to this firm.

## Then record it

Call `mcp__jarvis__add_prep_note` for each concrete thing to work on, using kind
`topic_to_learn`. One note per topic, phrased as an action. Skip anything already
present in the existing notes.

Do not add notes of kind `outcome` or `question_asked` — those are for after the
interview, recorded from what the user tells you happened.

## Rules

- Cite a URL for any claim about this specific firm's process.
- Never invent an interview format, a round count, or a question the firm asks.
- Short sentences. No preamble. No em-dashes, no emojis.
- Reply with a two-sentence summary of what you planned and noted. The full plan
  goes in the prep notes, not in the reply.
