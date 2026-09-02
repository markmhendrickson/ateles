import type { Config } from "tailwindcss";

/**
 * The theme maps onto NEOTOMA'S INSPECTOR palette (`inspector/src/index.css` in
 * the neotoma repo) so the Ateles app and the Inspector read as one product.
 * Both are shadcn/ui on the same CSS-variable contract, so the values live in
 * `styles.css` and this file only names them.
 *
 * The swarm-semantic colours (warn/bad/ok/live) stay first-class Tailwind
 * colours because they carry meaning here: `warn` is the undispatched flag,
 * the "declared but not executed" banner and the recommendation block, `ok` is
 * a stored answer, `live` is the dispatch/accent colour. Each is mapped onto
 * its nearest Inspector token rather than dropped.
 *
 * Type FAMILY and SIZE TOKENS come from the Inspector; the app's own density
 * scale (13px/1.4) is preserved in `styles.css` and deliberately not replaced.
 */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // The Inspector's named type scale, available for new work. Existing
      // bracket sizes from the density pass are left as they are.
      fontSize: {
        caption: ["0.6875rem", { lineHeight: "1.4" }],
        fine: ["0.75rem", { lineHeight: "1.4" }],
        ui: ["0.8125rem", { lineHeight: "1.5" }],
        small: ["0.8125rem", { lineHeight: "1.5" }],
      },
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        // Inspector surface tokens: recessed bands, loading placeholders.
        inset: "hsl(var(--inset))",
        skeleton: "hsl(var(--skeleton))",
        // Swarm-semantic colours, mapped onto their Inspector equivalents.
        warn: "hsl(var(--warn))",
        bad: "hsl(var(--bad))",
        ok: "hsl(var(--ok))",
        live: "hsl(var(--live))",
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      fontFamily: {
        sans: [
          "Inter",
          "system-ui",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "sans-serif",
        ],
        mono: ["JetBrains Mono", "Fira Code", "Roboto Mono", "ui-monospace", "Menlo", "monospace"],
      },
      keyframes: {
        "accordion-down": {
          from: { height: "0" },
          to: { height: "var(--radix-accordion-content-height)" },
        },
        "accordion-up": {
          from: { height: "var(--radix-accordion-content-height)" },
          to: { height: "0" },
        },
        // Preserved from the original stylesheet: a task landing mid-poll.
        "fresh-in": { from: { backgroundColor: "hsl(var(--live) / 0.22)" } },
        "pulse-ring": {
          "70%": { boxShadow: "0 0 0 8px hsl(var(--ok) / 0)" },
          "100%": { boxShadow: "0 0 0 0 hsl(var(--ok) / 0)" },
        },
      },
      animation: {
        "accordion-down": "accordion-down 0.2s ease-out",
        "accordion-up": "accordion-up 0.2s ease-out",
        "fresh-in": "fresh-in 0.8s ease",
        "pulse-ring": "pulse-ring 2s infinite",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
} satisfies Config;
