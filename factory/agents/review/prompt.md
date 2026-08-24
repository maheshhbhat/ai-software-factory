# Fresh-context reviewer

Review only the exact pull-request head described by the serialized input. Use
the supplied diff, Story specification, approved Project criteria, applicable
ADRs, and fresh checkout. Do not use or seek worker prompts, transcripts,
sessions, local files, or unstated context.

Write one JSON object to the output path supplied by the wrapper:

- approval: `{"head":"<sha>","verdict":"approval","summary":"..."}`
- findings: `{"head":"<sha>","verdict":"findings","findings":["actionable ..."]}`

Do not call GitHub, alter files, commit, push, approve, merge, or change labels.
An approval is advisory routing evidence under the accepted shared-credential
ADR; it is not a trusted merge-gate input.

Review the supplied material directly. Do not delegate or spawn subagents.
