/**
 * Build config for the dashboard's compiled Tailwind stylesheet.
 * Mirrors the (retired) inline `tailwind.config` from base.html so the static
 * build is a drop-in replacement for the Play CDN. Regenerate with build.sh.
 */
module.exports = {
  darkMode: "class",
  content: [
    "src/agent_me/dashboard/templates/**/*.html",
    "src/agent_me/dashboard/static/*.js",
  ],
  theme: {
    extend: {
      colors: {
        nvgreen: { 100: "#cfff40", 200: "#a5de15", 300: "#76b900", 500: "#549a00", 700: "#3f8500", 900: "#265600" },
        nvgray: { 50: "#eeeeee", 100: "#cccccc", 200: "#a7a7a7", 300: "#898989", 500: "#4b4b4b", 700: "#313131", 800: "#222222", 900: "#1a1a1a", 950: "#0a0a0a" },
        ink: { 950: "#000000", 900: "#0a0a0a", 800: "#1a1a1a", 700: "#313131", 600: "#4b4b4b" },
        accent: { 400: "#76b900", 500: "#549a00", 600: "#3f8500" },
      },
      fontFamily: {
        sans: ["NVIDIA Sans", "Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "Helvetica", "Arial", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      // `rounded-card` was used in templates but never defined (a no-op under the
      // Play CDN). Define it so card panels round as originally intended.
      borderRadius: { card: "0.75rem" },
    },
  },
};
