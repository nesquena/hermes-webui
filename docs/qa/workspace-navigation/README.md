# Workspace navigation and workbench evidence

Synthetic fixture data only. These are actual Chromium screenshots of the
implementation, not mockups. No production configuration or project data was
changed. Herm's attention-based conversation organization and quiet workbench
were the reference; quick navigation is a SynthPulse addition.

- `before-library.png`: previous commit f267fcbb, before in-main search and
  direct chat. The new browser case failed there because the search was absent.
- `library-*.png`: one main bot search surface, stable filters, direct chat,
  desktop/narrow/mobile and light/dark appearance. Desktop sidebar returns in
  bot details, without changing the user's sidebar-collapse preference.
- `navigation-*.png`: keyboard dialog and the single Projects result for spaces.
  Neither Spaces nor Kanban is a standalone destination. Existing merged folder
  APIs and migration state remain unchanged.
- `projects-folders.png`: existing My work folders reached through Projects.
  The test sets/restores the migration marker only in disposable fixture state.
- `conversations-*.png`: grouped loaded conversations, bot identities, filter
  counts and clearer primary actions. Status comes from existing session state.

Validation: 36 focused Python tests passed. The integrated 23-case browser run
passed 22; the new contrast check initially sampled during a theme transition.
After resolving computed CSS colors and finishing theme animations, all 8 bot
and navigation cases passed, including the previously failing scenario. The
updated general bot-navigation contract also passed separately. Earlier narrow
bot-detail action placement was fixed and passes in both final runs that cover
it. Counts overlap and are not additive. Each browser run kept source unchanged.

Measured text contrast for library introduction, count, primary/secondary
actions and search placeholder is at least 4.81:1 in light and 5.48:1 in dark.
Computed colors, alpha-composited backgrounds and ancestor opacity are included;
custom skins and the whole application's accessibility are not certified.

`verification.json` contains only named outcomes, run identifiers and contrast
measurements. It excludes credentials, raw traces, logs and fixture auth state.
Older scope-specific evidence remains in the sibling QA directories.
