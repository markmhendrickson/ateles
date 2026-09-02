import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * `chip` and `nav` reproduce the original filter-chip and nav-link treatments,
 * including their selected states — these are the app's most recognizable
 * controls, so they keep their exact geometry and colours.
 */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground rounded-md hover:bg-primary/90",
        outline:
          "rounded-[7px] border border-border bg-card text-muted-foreground hover:text-foreground hover:border-live",
        ghost: "rounded-[7px] hover:bg-accent hover:text-accent-foreground",
        link: "text-live underline-offset-4 hover:underline",
        /** Rounded filter chip. */
        chip: "gap-[7px] rounded-full border border-border bg-card text-muted-foreground hover:text-foreground",
        /** Header nav link. */
        nav: "rounded-[7px] border border-transparent bg-transparent text-muted-foreground hover:text-foreground",
      },
      size: {
        default: "h-8 px-3 py-1.5 text-[13px]",
        sm: "h-[26px] px-[9px] text-[12px]",
        chip: "px-[9px] py-[3px] text-[12px]",
        nav: "px-[10px] py-[4px] text-[12.5px]",
      },
      /** Selected state for chip/nav, matching the original `.on` rules. */
      active: {
        true: "",
        false: "",
      },
    },
    compoundVariants: [
      {
        variant: "chip",
        active: true,
        className: "border-live bg-live/[.12] text-foreground",
      },
      {
        variant: "nav",
        active: true,
        className: "border-border bg-live/[.13] text-foreground",
      },
    ],
    defaultVariants: { variant: "default", size: "default", active: false },
  },
);

export interface ButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "color">,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, active, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        ref={ref}
        className={cn(buttonVariants({ variant, size, active }), className)}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

export { Button, buttonVariants };
