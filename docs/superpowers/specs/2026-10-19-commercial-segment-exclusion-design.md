# Commercial Segment Preservation and Learning Exclusion Design

**Date:** 2026-10-19

**Status:** Approved design addendum awaiting written-spec review

**Parent specification:** `2026-10-19-docker-podcast-learning-platform-design.md`

## 1. Purpose

Podcast and YouTube transcripts may contain third-party advertisements, host-read
sponsor messages, affiliate promotions, promotions for the creator's own
products, cross-promotions, and show housekeeping. These segments are part of
the historical source record, but they must not silently become teachings,
general memory, search evidence, or Agent Factory skill material.

The system will preserve the complete canonical transcript and add a versioned,
auditable segment-classification layer. General learning uses only eligible
editorial segments. Commercial and ambiguous material remains inspectable and
correctable without requiring audio reacquisition or retranscription.

This document extends the parent specification. Where the parent specification
describes learning extraction directly from a canonical transcript, this
addendum inserts commercial classification and creation of an eligible learning
projection before extraction.

## 2. Goals

- Preserve the original normalized transcript, timestamps, hashes, and source
  lineage without alteration.
- Automatically exclude clear paid advertisements and creator promotions from
  general learning.
- Fail safely on ambiguity without blocking learning from the clearly editorial
  remainder of an item.
- Let the administrator inspect and correct every exclusion decision.
- Reuse corrections as rules and examples scoped to the same source.
- Prevent excluded text from entering summaries, teachings, claims, embeddings,
  Lamad learning state, Ebenezer retrieval, or Agent Factory skills.
- Permit explicitly requested promotional analysis without mixing promotional
  material into general memory.
- Reclassify the existing transcript backlog and classify all future items
  without retranscription.

## 3. Non-goals

- Deleting commercials from canonical transcripts or source media.
- Editing Amazon Transcribe or YouTube caption output in place.
- Treating model confidence as proof that a segment is commercial.
- Creating global rules from a correction made to one source.
- Blocking a complete learning bundle merely because some segments require
  review.
- Using commercial statements as evidence that advertised claims are true.
- Automatically learning advertising techniques from sponsor copy in general
  teaching workflows.

## 4. Classification Taxonomy

Every canonical transcript segment is covered by exactly one contiguous
classification range. A range uses one of these labels:

- `editorial`: substantive program material eligible for general learning.
- `paid_commercial`: third-party advertising, sponsor reads, and affiliate
  promotions.
- `self_promotion`: the creator's books, courses, newsletters, memberships,
  products, services, events, or paid offerings.
- `cross_promotion`: promotion of another show, channel, episode, or network
  property.
- `housekeeping`: non-substantive subscription reminders, platform calls to
  action, credits, and routine show administration.
- `uncertain`: material that cannot be classified safely at the current policy
  version.

`paid_commercial`, `self_promotion`, `cross_promotion`, `housekeeping`, and
`uncertain` are ineligible for general learning. Hooks, introductions, examples,
and closing material remain `editorial` when they make a substantive teaching
or argument; their location alone never makes them housekeeping.

Each classification range records:

- First and last stable source segment IDs.
- Start and end timestamps.
- Label and learning-eligibility decision.
- Confidence and bounded reason codes.
- Deterministic signals and source-rule matches.
- Classifier, prompt, schema, and policy versions.
- Whether the decision is automated, an exact administrator override, or a
  source-scoped learned match.
- Processing time and input transcript hash.

Free-form provider or model messages do not cross the persistence boundary.

## 5. Architecture and Data Flow

The complete flow is:

```text
canonical timestamped transcript
-> deterministic signal collection
-> restricted Claude block classification
-> validated segment-policy artifact
-> eligible learning-transcript projection
-> summary, teachings, claims, evidence, and program routes
-> immutable learning bundle
-> Lamad, Ebenezer, and Agent Factory delivery
```

The canonical transcript remains the source of truth. Classification never
rewrites its text, segment IDs, timestamps, or hashes. The eligibility projection
contains references to eligible canonical segment IDs and the text required by
the restricted extraction step. The projection is reproducible from the
canonical transcript and the versioned segment policy.

Commercial classification is a distinct durable pipeline stage between
transcript completion and learning extraction. It has its own processing state,
attempt count, artifact hashes, retry behavior, and administrator-visible
failure state.

## 6. Hybrid Classification

### 6.1 Deterministic signals

The first pass records signals rather than making broad text deletions. Signals
include:

- Explicit sponsor transitions and returns to program content.
- Purchase, discount-code, affiliate, trial, and promotional URL language.
- Repeated or near-identical blocks within episodes from the same source.
- Stable placement and timing patterns for recurring source-specific blocks.
- Exact administrator overrides and previously confirmed source patterns.
- Metadata that identifies known show or network promotional material.

A deterministic exclusion is permitted only for an exact override or a
versioned source-scoped rule with a positive match. General heuristics become
evidence for the classifier and do not independently delete content.

### 6.2 Restricted Claude classification

Restricted `claude -p` receives bounded blocks of transcript segments, nearby
context, deterministic signals, and source-scoped confirmed examples. Transcript
text is explicitly untrusted quoted data. The classifier has no shell,
filesystem, network, browser, MCP, slash-command, skill, or session tools.

Output must validate against a dedicated classification schema. It may only
reference segment IDs from its bounded input and must cover them exactly once
without gaps or overlap. It returns labels, confidence, bounded reason codes,
and boundary decisions; it cannot create learning content.

High-confidence, schema-valid decisions are applied automatically. Decisions
below the configured exclusion or editorial threshold become `uncertain`.
Thresholds are configuration owned by the versioned classification policy, not
ad hoc per-worker settings.

### 6.3 Boundary handling

Classification uses contiguous ranges, not isolated sentences. Nearby segments
may be supplied as read-only context so that sponsor transitions and returns to
editorial content are not cut mid-thought. Only IDs in the classification target
window may be labeled. Overlapping windows must agree on shared boundaries or
the disputed range becomes `uncertain`.

## 7. Corrections and Source-specific Learning

The administrator review queue lists uncertain ranges and permits review of any
automated range. Each entry displays transcript text, timestamps, label,
confidence, bounded reasons, matching rules, and media playback at the relevant
time when retained media is available.

A correction creates:

1. An exact immutable override for the reviewed segment range.
2. A source-scoped labeled example available to future classification.
3. A new segment-policy version and, when eligibility changed, a new learning
   bundle version.

Exact or near-identical recurring blocks may become deterministic fingerprints
for that source. Broader corrections influence only the restricted classifier's
source examples. The system does not synthesize unreviewed regular expressions
and never promotes a source rule to global scope automatically.

Rule changes trigger bounded reclassification only for affected items from that
source. Prior policies and bundles remain immutable and linked through explicit
supersession metadata.

## 8. Bundle Artifacts

Each content-item learning bundle contains:

- `transcript.json`: the complete immutable canonical transcript.
- `segment-policy.json`: all classification ranges, provenance, rule matches,
  decisions, overrides, review state, and version information.
- `learning-transcript.json`: the general-learning-eligible projection.
- `summary.md`, `teachings.json`, `claims.jsonl`, `evidence.jsonl`, and
  `program-routes.json`: derived only from eligible segment IDs.
- `manifest.json` and `bundle.json`: hashes and lineage for every artifact.

The manifest additionally records:

- Classification policy, schema, model, and prompt versions and hashes.
- Included, excluded, and uncertain segment and duration totals.
- Whether administrator review is needed.
- Source-rule and exact-override versions.
- Canonical transcript hash used to produce the policy.
- The superseded policy and bundle identities, when applicable.

No downstream artifact may cite a segment absent from
`learning-transcript.json`, except an explicitly promotional-analysis artifact
that is segregated and visibly labeled.

## 9. Learning and Delivery Policy

### 9.1 General learning

General summaries, lessons, exercises, frameworks, claims, evidence indexes,
embeddings, question answering, and skill candidates use only `editorial`
segments. `uncertain` is fail-closed and therefore ineligible until reviewed.

### 9.2 Lamad and Ebenezer

Lamad and Ebenezer receive the filtered learning projection, segment-policy
metadata, and provenance that identifies the complete private canonical
transcript. Their general learning and retrieval indexes must not index excluded
or uncertain text. The full transcript reference exists for authorized audit and
lineage; it is not a general retrieval document.

### 9.3 Agent Factory

Agent Factory receives eligible teachings, claims, evidence, and program routes.
Excluded text cannot become a released or candidate skill through the ordinary
pipeline.

An explicit promotional-analysis workflow for programs such as ASP-50 or
ASP-80 may request `self_promotion` or `cross_promotion` ranges. Those ranges are
delivered as a separately typed, visibly labeled research projection. They are
never merged into general teaching or memory and remain subject to Lamad
evaluation and Agent Factory promotion controls.

### 9.4 Publication revisions

An eligibility-changing correction produces a new immutable bundle and new
idempotency identities for all destinations. The new delivery supersedes the
prior revision. Replays of the same bundle remain idempotent, and all three
destinations converge on the same active revision.

## 10. Failure Handling

- Complete classifier failure stops bundle construction and delivery for that
  item while preserving the completed transcript.
- An individual ambiguous range becomes `uncertain`; it does not block delivery
  of eligible ranges from the same item.
- Invalid schema, unknown segment IDs, gaps, overlap, boundary disagreement, or
  transcript-hash mismatch rejects the classification artifact.
- Transient Claude, storage, or queue failures retry with bounded backoff.
- Repeated permanent failures surface in the administrator UI and stop
  automatic retries.
- A missing or corrupt policy or learning projection cannot fall back to the
  unfiltered transcript.
- Downstream rejection never changes the canonical transcript or classification
  decision.

## 11. Backfill and Rollout

All completed existing transcripts enter the classification stage before Phase
2 publishes their first learning bundle. Future transcripts enter it
automatically. The rollout reads existing canonical S3 artifacts and does not
download media or invoke Amazon Transcribe again.

Backfill is resumable, idempotent, source-bounded, and visible in the dashboard.
Items without a valid policy cannot be published. A policy-version upgrade may
enqueue reclassification, but the currently active bundle remains available
until its replacement validates and publishes successfully.

## 12. Administrator Experience and Observability

The dashboard shows, per item and source:

- Classification state and policy version.
- Included, excluded, and uncertain segment and duration totals.
- Ranges requiring review.
- Deterministic rule matches and administrator overrides.
- Active and superseded bundle revisions.
- Per-destination publication state.

Metrics and safe logs include stage duration, review-queue depth, label counts,
excluded-duration ratio, uncertain-duration ratio, source-rule matches,
classification failures, corrections, reclassification count, and publication
revision lag. An unexpected spike in excluded or uncertain content alerts the
administrator without logging transcript bodies.

## 13. Verification Strategy

Deterministic fixtures cover:

- Third-party advertisements and host-read sponsor messages.
- Dynamically inserted ads and repeated ad blocks.
- Affiliate links and discount codes.
- Creator books, courses, newsletters, products, services, and events.
- Cross-podcast and network promotion.
- Subscription reminders and routine housekeeping.
- Editorial introductions, calls to action used as teaching, and closing
  summaries that must not be false positives.
- Ads embedded between editorial sentences and ambiguous boundaries.
- Exact overrides, recurring source rules, and source-isolation behavior.
- Policy corrections, supersession, and idempotent downstream replay.

Contract and integration tests enforce these invariants:

1. Canonical transcript content, segment IDs, timestamps, and hash never change.
2. Classification covers every source segment exactly once.
3. Every general-learning citation references an eligible editorial segment.
4. Excluded and uncertain text is absent from summaries, teachings, claims,
   evidence indexes, embeddings, and ordinary downstream payloads.
5. Promotional-analysis payloads are separately typed and never accepted by a
   general-learning endpoint.
6. Source-specific rules have no effect on unrelated sources.
7. A correction creates a new policy and bundle version without overwriting the
   prior revision.
8. Lamad, Ebenezer, and Agent Factory converge on the same active revision after
   retries.
9. A classifier or policy failure cannot cause unfiltered transcript fallback.
10. Existing completed transcripts can be classified without provider or media
    network calls.

An opt-in evaluation set contains administrator-reviewed real examples from
each active source. Precision, recall, false-exclusion rate, uncertain-duration
rate, and boundary accuracy are reported by label and source before a new
classification policy is promoted. False exclusion of substantive editorial
content is a release-blocking regression.

## 14. Acceptance Criteria

1. The complete canonical transcript remains available and hash-identical after
   classification and correction.
2. Every segment has one auditable classification and eligibility decision.
3. Clear commercials and self-promotions are automatically excluded from
   general learning.
4. Ambiguous ranges are excluded, reviewable, and do not block learning from the
   approved remainder.
5. Administrator corrections affect the current item and become reusable only
   within the same source.
6. General learning artifacts and downstream payloads contain no excluded or
   uncertain segment text or references.
7. Explicit promotional analysis is separately requested, typed, labeled, and
   routed.
8. Corrections create immutable superseding bundles and idempotently update
   Lamad, Ebenezer, and Agent Factory.
9. Existing and future transcripts use the same classification contract without
   retranscription.
10. The dashboard exposes review work, policy lineage, exclusion totals, and
    delivery status without exposing transcript content in logs.
