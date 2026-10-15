"use strict";
// Light or dark: the visitor's choice (kept in this browser only), otherwise the device's.
// Loaded before the stylesheet applies, so the page never flashes the other theme. The
// effective theme is always on <html data-theme>, which the stylesheet and map read.
(() => {
  const KEY = "torontrack-theme";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  const saved = () => {
    try {
      const value = localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : null;
    } catch {
      return null;
    }
  };
  const apply = (theme) => {
    document.documentElement.dataset.theme = theme;
    for (const meta of document.querySelectorAll('meta[name="theme-color"]')) {
      meta.content = theme === "dark" ? "#000000" : "#ffffff";
    }
  };
  apply(saved() || (system.matches ? "dark" : "light"));
  system.addEventListener("change", () => {
    if (!saved()) {
      apply(system.matches ? "dark" : "light");
      window.dispatchEvent(new Event("themechange"));
    }
  });
  window.toggleTheme = () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // Private windows may refuse storage; the switch still applies to this page.
    }
    apply(next);
    window.dispatchEvent(new Event("themechange"));
  };
})();
