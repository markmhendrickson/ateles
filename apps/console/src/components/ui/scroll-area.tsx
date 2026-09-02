import * as React from "react";
import * as ScrollAreaPrimitive from "@radix-ui/react-scroll-area";
import { cn } from "@/lib/utils";

const ScrollArea = React.forwardRef<
  React.ElementRef<typeof ScrollAreaPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof ScrollAreaPrimitive.Root> & {
    viewportClassName?: string;
    /**
     * Set when the root is sized by FLEX (`min-h-0 flex-1`) rather than by an
     * explicit `max-h-*`. See the viewport comment below — without it the
     * viewport has no max-height to inherit and silently stops scrolling.
     */
    flexBounded?: boolean;
  }
>(({ className, children, viewportClassName, flexBounded, ...props }, ref) => (
  <ScrollAreaPrimitive.Root ref={ref} className={cn("relative overflow-hidden", className)} {...props}>
    {/*
      TWO WAYS a caller bounds this, and the viewport must honour BOTH.

      1. An explicit `max-h-*` on the root (the prompt bodies, the JSON blocks,
         the questions rail). `h-full` cannot resolve against a parent that only
         sets max-height, so the viewport would sit at full content height and
         CLIP the overflow. Inheriting the root's max-height fixes that case.

      2. A FLEX-BOUNDED root — `min-h-0 flex-1` inside a column, which is how the
         entity sheet sizes its body. Here there is no max-height to inherit, so
         `inherit` resolved to `none` and the viewport grew to the full height of
         the content (4558px inside a 900px sheet): `scrollHeight === clientHeight`,
         so nothing scrolled and everything below the fold was unreachable. The
         `absolute inset-0` pair constrains the viewport to the box flex already
         computed, restoring scrolling without disturbing case 1 — which keeps
         `static` positioning and its inherited max-height.
    */}
    <ScrollAreaPrimitive.Viewport
      className={cn(
        "w-full rounded-[inherit] [&>div]:!block",
        "data-[bound=flex]:absolute data-[bound=flex]:inset-0 data-[bound=flex]:h-auto",
        viewportClassName,
      )}
      data-bound={flexBounded ? "flex" : undefined}
      style={flexBounded ? undefined : { maxHeight: "inherit" }}
    >
      {children}
    </ScrollAreaPrimitive.Viewport>
    <ScrollBar />
    <ScrollAreaPrimitive.Corner />
  </ScrollAreaPrimitive.Root>
));
ScrollArea.displayName = ScrollAreaPrimitive.Root.displayName;

const ScrollBar = React.forwardRef<
  React.ElementRef<typeof ScrollAreaPrimitive.ScrollAreaScrollbar>,
  React.ComponentPropsWithoutRef<typeof ScrollAreaPrimitive.ScrollAreaScrollbar>
>(({ className, orientation = "vertical", ...props }, ref) => (
  <ScrollAreaPrimitive.ScrollAreaScrollbar
    ref={ref}
    orientation={orientation}
    className={cn(
      "flex touch-none select-none transition-colors",
      orientation === "vertical" && "h-full w-2.5 border-l border-l-transparent p-[1px]",
      orientation === "horizontal" && "h-2.5 flex-col border-t border-t-transparent p-[1px]",
      className,
    )}
    {...props}
  >
    <ScrollAreaPrimitive.ScrollAreaThumb className="relative flex-1 rounded-full bg-border" />
  </ScrollAreaPrimitive.ScrollAreaScrollbar>
));
ScrollBar.displayName = ScrollAreaPrimitive.ScrollAreaScrollbar.displayName;

export { ScrollArea, ScrollBar };
