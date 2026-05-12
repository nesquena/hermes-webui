# Systems Thinking for Building Websites — Course for Kei

Created: 2026-05-04
Source book: `/Users/kei/Desktop/AI-Books/Learning Systems Thinking Essential Non-Linear Skills and Practices for Software Professionals (Diana Montalion) (z-library.sk, 1lib.sk, z-lib.sk).pdf`
Verified source facts: local PDF opens; title `Learning Systems Thinking`; author `Diana Montalion`; 282 pages; 12 chapters; TOC extracted from PDF.

Copyright boundary: this course teaches the ideas with paraphrase, examples, exercises, and Kei/Yuto applications. Do not reproduce long book passages.

## Goal

Make systems thinking easy enough to use while learning/building websites first, then reusable for software architecture, AI agents, product systems, and Yuto's future operating system.

## Main metaphor

A website is not just pages + code.

A website is a system of:

- user goals
- user mental models
- content
- navigation
- visual hierarchy
- components
- performance
- accessibility
- business goals
- developer workflow
- analytics/feedback
- maintenance cost
- AI/code-generation quality

If one part improves but the relationships get worse, the website gets worse.

Example: adding animation may improve first impression, but worsen load time, accessibility, comprehension, and maintenance.

## Course shape

Duration: 8 modules + 1 capstone
Cadence: 1 module per session or per week
Practice object: one small website/landing page/project
Output: a reusable `Website System Design Canvas`

Each module uses this loop:

1. Learn one systems-thinking concept
2. Apply it to a website
3. Build or inspect one small artifact
4. Check for second-order effects
5. Record a reusable rule/pattern

## Module 0 — Orientation: Stop seeing websites as screens

### Simple idea

A website is behavior over time, not only a static layout.

### Key concepts

- system
- boundary
- parts
- relationships
- purpose
- behavior over time

### Website application

Do not ask only: “Does this page look good?”

Ask:

- Who comes here?
- What are they trying to do?
- What do they already believe?
- What must they notice first?
- What feedback tells us if it worked?
- What will break when content grows?

### Exercise

Pick one website idea and fill:

- Purpose:
- Primary user:
- User desired outcome:
- Business/project desired outcome:
- Main conversion/action:
- Constraints:
- What must not happen:

### Output

`Website System Brief v0`

## Module 1 — Linear vs nonlinear thinking

### Simple idea

Linear thinking says: “Add X → get Y.”
Systems thinking says: “Add X → changes relationships → behavior may shift in surprising ways.”

### Website example

Linear:
- Add more sections to explain more.

Systems:
- More sections may increase clarity for some users but increase cognitive load, scroll fatigue, inconsistent rhythm, worse performance, and weaker conversion.

### Practice questions

Before adding anything, ask:

1. What behavior do we want?
2. What relationship changes if we add this?
3. What could get worse?
4. How will we know?

### Exercise

Take a landing page idea. List 5 possible improvements. For each, write one possible unintended consequence.

### Output

`Change Impact Table`

## Module 2 — Iceberg model for websites

### Simple idea

Visible problems are events. Real leverage is lower: patterns, structure, mental models.

### Iceberg layers

1. Event: what happened?
2. Pattern: does it repeat?
3. Structure: what setup makes it likely?
4. Mental model: what belief created that structure?

### Website example

Event:
- Users do not click CTA.

Pattern:
- Users scroll around but do not decide.

Structure:
- Value proposition is vague; CTA appears before trust; sections compete visually.

Mental model:
- Builder believes “more features = more convincing.”

### Exercise

For your website, choose one likely problem:

- users bounce
- users do not understand offer
- users do not click CTA
- page feels messy
- page loads slow
- generated code becomes hard to maintain

Map it through the iceberg.

### Output

`Website Iceberg Diagnosis`

## Module 3 — Conceptual integrity

### Simple idea

A good website feels like one coherent idea, not a pile of sections.

### Website application

Conceptual integrity means these align:

- promise
- audience
- tone
- visual style
- section rhythm
- component system
- CTA
- performance budget
- accessibility
- implementation pattern

### Anti-pattern

A landing page with:

- premium hero
- random playful icons
- enterprise copy
- weak CTA
- heavy animation
- inconsistent spacing

This is not just “design taste.” It is a system with conflicting ideas.

### Exercise

Write the website's core idea in one sentence:

“This website helps [user] achieve [outcome] by making [belief/action] feel [emotion/quality].”

Then check every section against that sentence.

### Output

`Conceptual Integrity Statement`

## Module 4 — Feedback loops

### Simple idea

Systems are shaped by feedback. If feedback is delayed, missing, or wrong, behavior drifts.

### Website feedback loops

Good loops:

- user testing → copy revision
- Lighthouse/performance check → asset budget
- accessibility scan → component fixes
- analytics → section pruning
- design review → visual coherence
- code review → maintainability

Bad loops:

- AI generates code → looks okay → no review → hidden bloat grows
- add feature → no measurement → keep adding features
- optimize conversion → trust worsens → brand weakens

### Exercise

Design 3 feedback loops for your website:

1. User comprehension loop
2. Performance loop
3. Maintainability loop

For each:

- signal:
- frequency:
- threshold:
- action if bad:

### Output

`Website Feedback Loop Plan`

## Module 5 — Delays and second-order effects

### Simple idea

Some effects appear late. Good systems thinking asks what happens next, not only now.

### Website examples

Immediate win / later cost:

- heavy animation looks impressive → slower page, harder maintenance
- AI-generated component ships fast → style duplication grows
- vague broad copy attracts more people → wrong users convert poorly
- adding CMS flexibility → content entropy grows

### Exercise

For each major design/technical decision, write:

- immediate benefit
- delayed cost
- second-order effect
- guardrail

### Output

`Second-Order Effects Checklist`

## Module 6 — Pattern thinking

### Simple idea

Repeated problems usually come from repeated structures.

### Website pattern catalog

Pattern: Copy says what product is, not why user should care.
- Symptom: users understand features but do not feel urgency
- Structure: builder-centered writing
- Leverage: write from user job-to-be-done

Pattern: Page looks premium but feels hard to scan.
- Symptom: visual wow but low comprehension
- Structure: weak hierarchy/spacing/content rhythm
- Leverage: one main idea per section

Pattern: Fast build, slow future change.
- Symptom: every small edit breaks layout
- Structure: duplicated components, no token system
- Leverage: component contract and design tokens

### Exercise

Create 5 patterns from your website work:

- symptom
- recurring structure
- mental model
- better design rule

### Output

`Website Pattern Library`

## Module 7 — Modeling together

### Simple idea

A model is a thinking tool, not decoration.

### Website models to draw

1. User journey map
2. Page section flow
3. Component dependency map
4. Content model
5. Performance budget model
6. Feedback loop model

### Simple ASCII model

User intent
  ↓
Hero promise
  ↓
Proof / trust
  ↓
Details / objection handling
  ↓
CTA
  ↓
Feedback signal

### Exercise

Draw your website as a flow from user intent to action. Mark where confusion, friction, or trust loss can happen.

### Output

`Website System Map`

## Module 8 — Redefining success

### Simple idea

Bad metrics make bad systems. Success must include health of the larger system.

### Website success metrics

Not enough:
- page published
- looks nice
- high Lighthouse score
- many animations

Better:
- user understands offer in 5 seconds
- CTA is clear
- page loads fast enough on target devices
- accessibility baseline passes
- component system is maintainable
- copy matches actual product
- analytics can answer real questions
- future changes are cheap

### Exercise

Define success with 4 dimensions:

1. User success
2. Business/project success
3. Technical success
4. Maintenance/system health

### Output

`Website Success Definition`

## Capstone — Build one website as a system

### Deliverables

1. Website System Brief
2. Conceptual Integrity Statement
3. Website System Map
4. Feedback Loop Plan
5. Second-Order Effects Checklist
6. Pattern Library
7. Success Definition
8. Final website/prototype
9. Review report

### Suggested workflow

Step 1: define purpose and boundaries
Step 2: map user journey
Step 3: write one-sentence promise
Step 4: design sections from intent to action
Step 5: build minimal page
Step 6: run performance/accessibility/readability checks
Step 7: revise based on feedback
Step 8: record reusable patterns

## Website System Design Canvas

Use this for every future website.

### 1. Purpose

- What system outcome should this website create?
- What should users do/understand/feel?

### 2. Boundary

In scope:
- 

Out of scope:
- 

### 3. Actors

- Primary user:
- Secondary user:
- Builder/maintainer:
- Business/project owner:

### 4. User journey

- Entry point:
- Existing belief/problem:
- First thing they must understand:
- Main friction:
- Desired action:

### 5. Conceptual integrity

Core sentence:

“This website helps [user] achieve [outcome] by making [belief/action] feel [emotion/quality].”

Design rules:
- 
- 
- 

### 6. Sections

For each section:

- purpose:
- user question answered:
- needed proof:
- CTA or transition:
- risk if unclear:

### 7. Feedback loops

- comprehension signal:
- conversion signal:
- performance signal:
- accessibility signal:
- maintainability signal:

### 8. Constraints

- performance budget:
- accessibility baseline:
- device/browser targets:
- content constraints:
- maintenance constraints:

### 9. Second-order effects

Decision:
Immediate benefit:
Delayed cost:
Guardrail:

### 10. Success definition

User success:
Business/project success:
Technical success:
Maintenance success:

## Yuto operating system for this course

### How Yuto should teach

- Teach one concept at a time.
- Use Kei's website/project as the live case.
- Convert theory into a canvas/checklist immediately.
- Avoid long abstract lecture unless Kei asks.
- End each lesson with one exercise and one artifact.

### How Yuto should review work

Use this order:

1. User intent clarity
2. Conceptual integrity
3. Section flow
4. Visual hierarchy
5. Performance
6. Accessibility
7. Code maintainability
8. Feedback/measurement
9. Second-order risks
10. Next smallest improvement

### When to create reusable knowledge

Create/patch knowledge or skills when:

- a website pattern repeats
- a checklist improves review quality
- a failure mode appears more than once
- Kei explicitly says “เก็บไว้ใช้ต่อ”

## First lesson to run

Lesson 0: Stop seeing websites as screens.

Prompt for Kei:

“เลือกเว็บหนึ่งเว็บที่จะใช้เป็นสนามทดลอง แล้วตอบ 6 ช่องนี้: purpose, primary user, user desired outcome, project desired outcome, main action, what must not happen.”

Then Yuto turns it into `Website System Brief v0`.
