import js from "@eslint/js";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";
import globals from "globals";

// Flat config (ESLint v9+). Plain JS/JSX only — no TypeScript parser/plugin,
// per Slice J (Section 23.1 of the Agile plan): zero .ts/.tsx in this repo.
export default [
  js.configs.recommended,
  {
    files: ["**/*.{js,jsx}"],
    plugins: {
      react,
      "react-hooks": reactHooks,
      "jsx-a11y": jsxA11y,
    },
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser, ...globals.es2021 },
    },
    settings: { react: { version: "detect" } },
    rules: {
      ...react.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.configs.recommended.rules,
      "react/prop-types": "off", // no PropTypes/TS in this codebase; runtime-checked via tests instead
      "react/react-in-jsx-scope": "off", // React 18 automatic JSX runtime
      "no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      // "Fetch on mount/dependency change" via useEffect(() => { void refresh(); }, [refresh])
      // is this codebase's established, repo-wide data-fetching idiom (pre-dates this
      // ESLint config; used in BacklogPanel/BoardPanel/EpicsPanel/PlanningPanel/TaxonomyPanel).
      // This rule targets React Compiler-era guidance to avoid it entirely, which would
      // require a full data-fetching architecture migration (react-query/suspense) that is
      // out of scope here.
      "react-hooks/set-state-in-effect": "off",
      // autoFocus on the first field of a form/dialog is an established, intentional UX
      // pattern already used throughout this codebase (LoginPage, ResetPage, ConfirmHost,
      // BacklogPanel) predating this config.
      "jsx-a11y/no-autofocus": "off",
    },
  },
  {
    // Vendored/generated output never gets linted.
    ignores: ["dist/**", "../app/static/**", "node_modules/**"],
  },
];
