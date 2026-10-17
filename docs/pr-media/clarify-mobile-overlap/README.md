# Mobile clarification panel overlap

The mobile inner panel has 13px of bottom padding, but its expanded card extends
24px behind the composer. The response hint can therefore be covered even with
the phone keyboard closed. The narrow mobile override now sets `bottom:8px`,
matching the existing collapsed card gap. Desktop flyout positioning stays the
same. Question content and backend clarify handling are unchanged.

These are synthetic fixtures built from the actual clarification HTML and CSS.
The composer has a fixed 110px test height, and placeholder text is public dummy
content. They exercise rendered page geometry; they are not captures of a real
user conversation or proof of native keyboard behavior.

| Viewport | Before | After |
| --- | --- | --- |
| Desktop 1280×800 | [Before](before-1280x800.png) | [After](after-1280x800.png) |
| Narrow 720×800 | [Before](before-720x800.png) | [After](after-720x800.png) |
| Phone 390×844 | [Before](before-390x844.png) | [After](after-390x844.png) |
| Reduced 390×440 | [Before](before-390x440.png) | [After](after-390x440.png) |

The probe `scripts/check_clarify_mobile_overlap.cjs` fails on upstream base
`7a74ae1b56b2ade5a58d13bd027499021c2876c6`: mobile panel gap is -24px and the
hint fails hit testing because the composer covers it. The fixed mobile gap is
8px and the hint passes hit testing. Desktop/narrow gap remains -24px, where
the larger inner padding already keeps the hint uncovered. Long questions remain
scrollable and the collapsed header retains its 8px gap.
