/**
 * ATELES BRAND MARKS
 * ------------------
 * Two INDEPENDENT marks that also compose:
 *
 *   <AtelesSymbol />   the symbol alone — favicon, collapsed nav, tight spots
 *   <AtelesWordmark /> the word alone — anywhere the symbol is already present
 *   <AtelesLogo />     the header lockup of the two
 *
 * They are deliberately separate components rather than one inseparable SVG,
 * so the symbol can shrink to a favicon without dragging the word along and
 * the word can stand on its own.
 *
 * DIVISION OF LABOUR: the SYMBOL carries the meaning, the WORDMARK stays quiet.
 * Doubling the idea — swarm imagery in both — would be exactly the
 * over-stylization this avoids.
 *
 * RELATIONSHIP TO NEOTOMA: the Inspector brands with a typographic wordmark
 * only (`inspector/src/assets/neotoma_wordmark.svg`, Inter, weight 500, tight
 * tracking). Its `neotoma_mark.svg` is an unused placeholder — a letter in a
 * rounded box with a hardcoded gradient, referenced nowhere in the Inspector
 * source — so there was no symbol worth matching. These read as siblings
 * through the shared typography (Inter, the same tracking) rather than by
 * imitating that placeholder.
 */

/**
 * THE SYMBOL — a swarm: one core, six satellites.
 *
 * Ateles is the parent agent coordinating ~40 defined agents across four
 * tiers; the mark is that structure reduced to its minimum. The core dominates
 * so the hierarchy is unmistakable (one parent, not a peer mesh), and the six
 * satellites are UNIFORM and evenly spaced so the read is "many acting as one"
 * rather than a ranked list.
 *
 * Uniformity is also what makes it survive: an earlier version graded the
 * satellites by tier, which looked deliberate at 96px and turned to noise at
 * 20px because the smallest node dissolved. With equal nodes there is no
 * smallest one to lose, and the six stay separate and countable down to 16px.
 *
 * Geometry: core r=3.7, satellites r=1.6 on an R=8.6 ring at 60° steps from
 * top, on a 24px grid. Checked at 16/20/24/32/64/180px in both themes. Pure
 * fills, no strokes — nothing to thin out against the dark palette.
 */
export function AtelesSymbol({
  size = 20,
  className,
}: {
  size?: number;
  className?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="currentColor"
      className={className}
      role="img"
      aria-label="Ateles"
    >
      {/* The parent agent. */}
      <circle cx="12" cy="12" r="3.7" />
      {/* The swarm, evenly spaced around it. */}
      <circle cx="12" cy="3.4" r="1.6" />
      <circle cx="19.45" cy="7.7" r="1.6" />
      <circle cx="19.45" cy="16.3" r="1.6" />
      <circle cx="12" cy="20.6" r="1.6" />
      <circle cx="4.55" cy="16.3" r="1.6" />
      <circle cx="4.55" cy="7.7" r="1.6" />
    </svg>
  );
}

/**
 * THE WORDMARK — set, not drawn.
 *
 * Professional and clean, with no stylization: real text in the app's own type
 * (Inter, inherited from the body stack), no letterform turned into a picture
 * and no glyph swapped for an icon. Lowercase with slightly tightened tracking
 * matches the Neotoma wordmark's register, which is what makes the two apps
 * read as siblings.
 *
 * Live text rather than outlines, so it inherits `currentColor`, stays
 * selectable and searchable, and scales without a second asset.
 */
export function AtelesWordmark({ className }: { className?: string }) {
  return (
    <span className={`text-[14px] font-[620] tracking-[-0.01em] ${className ?? ""}`}>ateles</span>
  );
}

/** The header lockup: symbol plus wordmark, tighter than the nav's own rhythm. */
export function AtelesLogo({ className }: { className?: string }) {
  return (
    <span className={`flex items-center gap-[7px] ${className ?? ""}`}>
      <AtelesSymbol />
      <AtelesWordmark />
    </span>
  );
}
