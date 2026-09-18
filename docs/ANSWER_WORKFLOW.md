# Answering with bounded, traceable evidence

The MCP client remains the answer model. No new LLM credentials or background
answer service are needed. The service prepares evidence and checks citations;
the client reviews whether each scientific statement follows from that evidence.

## One question, three steps

1. Call `prepare_answer(query, n=10, budget_codepoints=8000)`. Optional parent,
   attachment and main/SI filters have the same meaning as `search_papers`.
   `second_query` can supply retrieval terms, while `query` remains the question.
2. Read the returned `instructions` and `evidence`. Generate this structure:

   ```json
   {
     "claims": [
       {"text": "A statement supported under the stated conditions.",
        "citations": [{"evidence_id": "E1"}]}
     ],
     "missing_information": []
   }
   ```

   Keep the subject, reaction, conditions, value/unit and comparison together.
   Separate different studies and acidic/alkaline or activity/durability tests.
   Do not add unrelated facts. When evidence is incomplete, return the supported
   part and name only the information needed to finish answering the question.
   Missing information is a limitation, not a factual claim requiring a fake cite.
3. Call `check_answer(packet, answer)` with the original packet. If citation
   checks fail, correct IDs/quotes or remove unsupported claims before presenting
   the response. A citation may optionally include `quote` for a narrower exact
   excerpt. Omit it to attach the full cited source fragment automatically.
   After checking, review semantic support yourself, then show the returned
   `answer` and use `claim_bindings` to expose source pages and quotations.

Evidence IDs belong to one packet: `E1` in two packets can refer to different
papers. Keep each final packet unchanged and check only claims supported by that
packet. Do not carry a citation ID forward when preparing a replacement packet.

When first-pass evidence is insufficient, discover the relevant paper with
`search_notes` and `get_note`, then repeat preparation with the parent key
returned by those tools. For a comparison, discover each named study separately
and allocate the total source budget across its final packets (for example,
4000 codepoints per paper for a two-paper, 8000-codepoint answer). Check each
packet's claims independently. A focused follow-up preserves the original
question; it must not guess a source key or import a value from memory.

The equivalent HTTP endpoints are `POST /prepare_answer` with the search
arguments, and `POST /check_answer` with `{"packet": ..., "answer": ...}`.
Malformed requests return errors; failed citation checks return
`citation_checks_passed: false`, an error list and `answer: null`.

## Selection and budget

Preparation expands already retrieved canonical hits using Module 1. Original
ranked hits are packed first; surrounding context uses only the remaining budget.
When the original hits fit, their source coordinates are all retained. This
prevents expanded windows from displacing already retrieved facts.
Selected source intervals are subtracted from later windows with the same
generation/file/attachment identity. Disjoint fragments stay separate: gaps are
never silently joined into a sentence. Main text and SI remain separately cited.

The total source-text budget defaults to 8000 Unicode code points and can be
lowered to 256. It includes derived page joins and excludes instructions, JSON
metadata and model tokens. `used_codepoints` is the actual selected source text.
No source coordinate occurs twice in a prepared packet. Whole windows may be
cut or dropped to meet this budget. `boundary_notice` makes this explicit.
Original search results and rankings are unchanged. When the requested budget
is smaller than the original hits, later hits can be cut or omitted. No packet
can guarantee completeness of retrieval or of scientific conditions.

Every evidence entry carries its exact canonical page spans, hashes, original
hit ID and source identity. It can span pages but cannot span attachments.
Source text, including units, line breaks and figure captions, stays verbatim.

## What is checked

`check_answer` revalidates packet content against the server's pinned canonical
generation, compares the full source mapping, rejects unknown citation IDs and
checks any supplied quote verbatim. The packet ID is a reproducibility checksum,
not authentication or proof that a server issued the packet. No extra secret or
session registry is required. A server restart onto another generation requires
preparing a new packet.

`citation_checks_passed` means source and citation integrity only.
`semantic_support` is always `requires_review`: an accurate quote can accompany
an inaccurate claim. A no-claim answer must explain missing evidence, but the
checker cannot establish whether that refusal is justified. The host must check
that conditions belong to the claimed measurement and that a supported partial
answer has not been unnecessarily refused.

This workflow requires a canonical PDF index. A legacy index produces an
explicit error rather than unverified answer evidence. `search_papers` remains
available for raw retrieval, investigating gaps and legacy compatibility.

## Validation scope

The initial acceptance replay uses five previously inspected questions and fifty
frozen hits. With the 8000-codepoint budget, all original source coordinates are
retained, no coordinate is repeated, and each packet respects the total limit.
This is an engineering acceptance set, not a held-out benchmark. Matched local
model answers are reviewed separately: correct citations do not prevent wrong
condition attribution, unnecessary refusal or irrelevant additions. The host's
semantic review remains part of the workflow.
