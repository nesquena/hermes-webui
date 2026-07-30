// ESLint flat config — runtime-error guard for the static JS bundle.
//
// Purpose: catch brick-class runtime errors that `node --check`, source-presence
// tests, and even executing the file all MISS, because the error only fires when a
// specific function actually runs in the browser. Canonical case: #3162 — a `const`
// binding reassigned inside `_ensureMessagesLoaded` threw a TypeError that bricked
// "load conversation messages" on every mobile message in v0.51.161-166.
//
// Scope discipline: ONLY rules that flag genuine "throws at runtime" bugs AND have
// ZERO hits on the current clean tree (so the gate is green today and only ever
// fails on a NEW regression). This is NOT a style linter.
//
// Deliberately EXCLUDED (verified to have pre-existing intentional hits 2026-05-30):
//   - no-dupe-keys (92 hits): intentional i18n locale-fallback override pattern
//   - no-func-assign (2 hits): switchPanel/switchSettingsSection override pattern
//   - no-redeclare (1 hit): redeclared loop var in panels.js
// If those are cleaned up later, they can be promoted into this guard.
//
// Run: npx eslint -c eslint.runtime-guard.config.mjs "static/**/*.js"
// (tests/test_static_js_runtime_lint.py runs this automatically when eslint is present.)

export default [
  // Bundled/minified third-party assets are ES modules and not ours to lint.
  { ignores: ["**/vendor/**", "**/*.min.js"] },
  {
    files: ["**/*.js"],
    languageOptions: { ecmaVersion: "latest", sourceType: "script" },
    rules: {
      // #3162: reassigning a `const` — runtime TypeError, only fires on execution.
      "no-const-assign": "error",
      // Assigning to an import binding — runtime TypeError.
      "no-import-assign": "error",
    },
  },
];
