"use strict";

// Run before styles are painted so a saved theme also applies on reload.
(() => {
  const root = document.documentElement;
  const key = "sapper-theme";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  const valid = value => value === "light" || value === "dark";
  let preference = null;
  try { preference = localStorage.getItem(key); } catch (_) { /* Storage may be disabled. */ }
  if (!valid(preference)) preference = null;

  function apply() {
    const theme = preference || (system.matches ? "dark" : "light");
    root.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#152431" : "#f3f6f8";
    document.querySelectorAll("[data-theme-choice]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.themeChoice === theme));
    });
    window.dispatchEvent(new CustomEvent("sapper:themechange", {detail: {theme}}));
  }

  apply();
  system.addEventListener("change", () => { if (!preference) apply(); });
  window.addEventListener("storage", event => {
    if (event.key === key || event.key === null) {
      preference = valid(event.newValue) ? event.newValue : null;
      apply();
    }
  });
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-theme-choice]").forEach(button => {
      button.addEventListener("click", () => {
        preference = button.dataset.themeChoice;
        try { localStorage.setItem(key, preference); } catch (_) { /* Keep the in-page choice. */ }
        apply();
      });
    });
    apply();
  });
})();
