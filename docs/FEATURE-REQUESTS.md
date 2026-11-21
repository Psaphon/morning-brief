# Feature Requests

Backlog ideas, not scheduled features.

## From plan brief-quality (2026-11-18)

- **Roundup splitting:** break newsletter-style roundups into separate items instead of leaving them out.
- **Story threads:** link an item to earlier coverage of the same entities ("previously: …"), so ongoing stories keep their context.
- **Model eval command:** run extraction over the synthetic fixtures with any local model and report the JSON-valid rate and validator drop rate, for the planned model audit.
- **Scorer spread:** scores bunch between 0.44 and 0.61. Fixing this is a contract change, to be planned jointly with atrade.
- **Feed review:** replace or narrow the arXiv cs.AI firehose.

## From plan filings-lane (2026-11-21)

- **Alias matching false positives:** alias-in-text matching produces false matches (for example, a carmaker's results article matched a search company). Consider word-boundary and context checks.
- **Contact footer fleet-wide:** every other repo with a public page should get the same contact footer as `fetch-contact`, built from runtime config.
