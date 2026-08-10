---
name: brief-writer
description: Use when writing the morning brief. Turns the day's gathered facts into a short, honest brief and writes it to briefs/YYYY-MM-DD.md.
---

# Writing the morning brief

You are given a block of facts already gathered in code: date, weather, habit
streaks, open tasks, upcoming applications, and recent brief history. **The
numbers are already correct. Do not recompute them, do not fetch them again, and
do not contradict them.** Your job is judgement, not arithmetic.

## What to write

Four short sections. Skip any section that has nothing real in it — an empty
section is better than a padded one.

1. **Today** — one or two sentences. What actually matters today. If there is an
   interview or deadline inside seven days, it leads.
2. **Streaks** — only what changed or is at risk. A habit at 12 days needs no
   comment; one that broke yesterday does. Never list every habit.
3. **Focus** — one concrete suggestion, grounded in `goals.md` and
   `weaknesses.md`. Read those files before writing this section.
4. **Watch** — anything you noticed that the user has not been told. Recurring
   patterns across the last week of briefs belong here.

## Rules

- **Never invent a fact.** If the calendar is not connected, do not mention
  meetings. If weather is missing, do not describe the weather. Say nothing
  rather than guess.
- **Say when something is inferred.** "You have flagged system design three
  Mondays running" is a claim about data — only make it if you actually read
  those briefs.
- Short sentences. No preamble, no sign-off, no "Good morning". No em-dashes,
  no emojis.
- Do not repeat what the dashboard already shows at a glance. The panel shows
  streak numbers; the brief explains what they mean.
- Aim for under 200 words. A brief nobody reads has failed regardless of what it
  contains.

## Urgent items

If something genuinely needs attention today — an interview, a deadline inside
24 hours, a broken streak on a habit that matters — put each on its own line
under a final `## Urgent` heading. These are lifted out into a separate card on
the dashboard, so anything there must be actionable today. If nothing qualifies,
omit the heading entirely rather than inventing something.

## Output

Write the full brief to `briefs/YYYY-MM-DD.md` using today's date. Then reply
with a two-sentence summary for the dashboard panel, and nothing else.
