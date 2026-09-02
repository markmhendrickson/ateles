import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * Variants reproduce the ORIGINAL badge colours exactly — the tinted
 * background / lighter text pairs from the old `.s-*`, `.tb-*` and `.a-*`
 * rules. They are meaning-bearing, not decoration: `live` marks a dispatched
 * or in-progress item, `ok` a completed one or a stored answer, `bad` blocked,
 * `warn` a suggestion or an unclassified tier.
 */
const badgeVariants = cva(
  "inline-flex flex-none items-center rounded-[5px] px-[7px] py-[2px] text-[11px] transition-colors",
  {
    variants: {
      variant: {
        // Neutral — the default status pill.
        muted: "bg-muted-foreground/[.18] text-muted-foreground",
        live: "bg-live/[.18] text-[#79aaff]",
        ok: "bg-ok/[.16] text-[#4ad991]",
        bad: "bg-bad/[.16] text-[#ff8078]",
        warn: "bg-warn/[.18] text-[#f5bd5f]",
        purple: "bg-[#a855f7]/[.18] text-[#c99cff]",
        /** Solid count badge — the collapsed sidebar's unanswered counter. */
        counter:
          "min-w-[20px] justify-center rounded-full bg-live px-[6px] text-white font-[650] tabular-nums",
        /** Monospace reference number, e.g. "#2". */
        ref: "rounded-[5px] border border-[hsl(var(--live)/0.30)] bg-[hsl(var(--live)/0.12)] px-[6px] py-px font-mono font-semibold text-live",
        refDone: "rounded-[5px] border border-border px-[6px] py-px font-mono font-semibold text-muted-foreground",
        /** Tool-allowlist style tag. */
        tag: "rounded-full border border-border bg-background px-[9px] py-[3px] font-mono text-[12px] text-muted-foreground",
      },
      /** Uppercase tracking, as the status pills originally had. */
      caps: {
        true: "uppercase tracking-[.04em]",
        false: "",
      },
    },
    defaultVariants: { variant: "muted", caps: false },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

/**
 * Forwards its ref so a Badge can be used as a Radix `asChild` trigger (e.g.
 * `TooltipTrigger asChild`), which passes a ref down to whatever it wraps. A
 * plain function component silently drops that ref and React warns at runtime.
 */
const Badge = React.forwardRef<HTMLSpanElement, BadgeProps>(
  ({ className, variant, caps, ...props }, ref) => (
    <span ref={ref} className={cn(badgeVariants({ variant, caps }), className)} {...props} />
  ),
);
Badge.displayName = "Badge";

export { Badge, badgeVariants };
