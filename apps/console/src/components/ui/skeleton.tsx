import { cn } from "@/lib/utils";

function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("animate-pulse rounded-md bg-secondary", className)}
      // Loading placeholders are decorative; screen readers get the live status
      // text instead of a grid of empty boxes.
      aria-hidden
      {...props}
    />
  );
}

export { Skeleton };
