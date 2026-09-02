/**
 * ENTITY SHEET — inspection without losing your place.
 *
 * The operator asked for this specifically: reading a task referenced from the
 * session page should not cost him the session page. The sheet slides over the
 * current view, and dismissing it returns him to exactly where he was, still
 * scrolled to the same row.
 *
 * IT RENDERS THE SAME `EntityDetail` AS THE FULL PAGE. This file contributes
 * the frame — overlay, header, the "Open full page" affordance — and no
 * entity-specific markup at all. Two presentations of every entity type is
 * precisely where a divergent second renderer would grow, so there is only one.
 *
 * SHEET vs FULL PAGE
 * ------------------
 * Every entity supports both. The sheet is the default for a click, because
 * inspection is the common case and context is the thing worth protecting. The
 * full page is the CANONICAL ADDRESS: it has a URL, so it survives a reload,
 * can be pasted between sessions, and can be bookmarked — none of which a sheet
 * can do. "Open full page" is therefore always offered, never conditioned on
 * how long the body happens to be.
 *
 * A related row inside the sheet swaps the sheet's subject in place rather than
 * navigating, so following a chain of references still costs nothing.
 */
import { useState } from "react";
import { EntityDetail, useEntity } from "./EntityDetail";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Maximize2 } from "lucide-react";

/**
 * Sheet navigation state: the entity currently shown, plus the ones behind it.
 *
 * A stack rather than a single id, so following a reference inside the sheet
 * can be backed out of without closing the panel entirely.
 */
export function useEntitySheet() {
  const [stack, setStack] = useState<string[]>([]);

  return {
    /** The entity on screen, or null when the sheet is closed. */
    current: stack.length ? stack[stack.length - 1] : null,
    open: (id: string) => setStack([id]),
    push: (id: string) => setStack((s) => [...s, id]),
    back: () => setStack((s) => s.slice(0, -1)),
    close: () => setStack([]),
    depth: stack.length,
  };
}

export function EntitySheet({
  id,
  depth,
  onOpenChange,
  onPush,
  onBack,
  onFullPage,
}: {
  id: string | null;
  depth: number;
  onOpenChange: (open: boolean) => void;
  onPush: (id: string) => void;
  onBack: () => void;
  onFullPage: (id: string) => void;
}) {
  const { payload, error, firstLoadDone } = useEntity(id);

  return (
    <Sheet open={Boolean(id)} onOpenChange={onOpenChange}>
      <SheetContent>
        {/* Radix requires a Title for the dialog's accessible name. The detail
            body renders its own heading, so this one is visually hidden rather
            than duplicated on screen. */}
        <SheetTitle className="sr-only">Entity detail</SheetTitle>

        <div className="flex flex-none items-center gap-2 border-b px-5 py-3 pr-12">
          {depth > 1 && (
            <Button variant="outline" size="sm" onClick={onBack}>
              Back
            </Button>
          )}
          {/* Always offered — the full page is the canonical address for every
              entity, not a fallback for long ones. */}
          {id && (
            <Button variant="outline" size="sm" onClick={() => onFullPage(id)}>
              <Maximize2 className="h-[13px] w-[13px]" aria-hidden />
              Open full page
            </Button>
          )}
        </div>

        {/* `flexBounded` because this root is sized by flex, not by a max-height.
            Without it the viewport had no max-height to inherit, grew to the full
            content height, and the sheet stopped scrolling entirely. */}
        <ScrollArea className="min-h-0 flex-1" flexBounded>
          <div className="px-5 pb-8 pt-4">
            <EntityDetail
              payload={payload}
              error={error}
              firstLoadDone={firstLoadDone}
              // A reference followed from inside the sheet stays in the sheet.
              onOpenEntity={onPush}
            />
          </div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  );
}
