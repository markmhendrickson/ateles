/**
 * APP SHELL
 * ---------
 * Branded header, nav between sections, and the persistent open-questions
 * sidebar that renders on every route.
 *
 * The task poll lives here rather than in the Tasks view because two surfaces
 * consume it: the task list and the sidebar. Questions are `task` entities, so
 * a single 10s fetch feeds both — a question raised while the operator is
 * reading an agent definition still appears without a reload.
 *
 * ALL DATA COMES FROM NEOTOMA, via the dev proxy's `/api/*` routes. There are
 * no fixtures and no local data files anywhere in this app; the only thing
 * persisted client-side is the sidebar's expanded/collapsed preference, which
 * is per-viewer UI state rather than domain data.
 *
 * READ-ONLY: every request this app makes is a GET — /api/tasks, /api/agents,
 * /api/sessions, /api/conversation. There is no write path here or behind the
 * proxy.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type Task, type TaskEntity, parseTask } from "./tasks";
import { Questions } from "./Questions";
import { QuestionDetail } from "./QuestionDetail";
import { questionRefs } from "./questionRefs";
import { type QuestionCoverage, questionCoverage } from "./questionCount";
import { TaskList } from "./TaskList";
import { AgentDirectory } from "./AgentDirectory";
import { Sessions } from "./Sessions";
import { Workflows } from "./Workflows";
import { Lifecycle } from "./Lifecycle";
import { Schemas } from "./Schemas";
import { type NavSection, isNavSection, useRoute } from "./route";
import { EntityPage } from "./EntityDetail";
import { EntitySheet, useEntitySheet } from "./EntitySheet";
import { QuestionDetailSkeleton } from "@/components/Skeletons";
import { showSkeleton } from "@/lib/loading";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { ArrowLeft } from "lucide-react";
import { AtelesLogo } from "./Brand";
import { GlobalSearch } from "./GlobalSearch";

const REFRESH_MS = 10_000;

/**
 * What the entity page's back button says, per section the operator came from.
 *
 * A map rather than a ternary chain: the chain needed a new arm for every tab
 * and silently fell through to "Session" for any section nobody remembered to
 * add, so a new tab's back button would quietly lie. Typed by `NavSection`, so
 * adding a tab without a label is a compile error instead.
 */
const BACK_LABELS: Record<NavSection, string> = {
  home: "Now",
  tasks: "Tasks",
  agents: "Agents",
  sessions: "Sessions",
  workflows: "Workflows",
  lifecycle: "Lifecycle",
  schemas: "Schemas",
};

/** The nav tabs, in order, with the label each one shows. */
const NAV_LABELS: { value: NavSection; label: string }[] = [
  { value: "home", label: "Now" },
  { value: "tasks", label: "Tasks" },
  { value: "agents", label: "Agents" },
  { value: "sessions", label: "Sessions" },
  { value: "workflows", label: "Workflows" },
  { value: "lifecycle", label: "Lifecycle" },
  { value: "schemas", label: "Schemas" },
];

export function App() {
  const [route, navigate] = useRoute();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [lastSync, setLastSync] = useState<Date | null>(null);
  /**
   * True until the first fetch has SETTLED — success or failure. Skeletons key
   * off this and never off "a request is in flight": with a 10s poll, the
   * latter would strobe the whole page every tick. Once the first response has
   * landed, subsequent polls re-render existing data in place.
   */
  const [firstLoadDone, setFirstLoadDone] = useState(false);
  /** Ids seen in a previous poll — anything new gets highlighted briefly. */
  const seen = useRef<Set<string> | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  /**
   * How many open questions EXIST, against how many the task page loaded.
   *
   * The rail filters this page's newest-200 tasks. That is fine while every
   * question is recent, and silently wrong the moment one is not — and the
   * question most likely to age out is the one longest unanswered, which is
   * exactly the one a queue must not lose. `/api/questions` supplies the
   * denominator so the rail can say when it is showing a subset.
   */
  const [questionCoverageState, setQuestionCoverage] = useState<QuestionCoverage>({
    kind: "unknown",
  });

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/tasks?limit=200");
      const body = await res.json();
      if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
      const parsed: Task[] = (body.entities as TaskEntity[]).map(parseTask);

      // Flag tasks that appeared since the last poll, so the operator can see
      // work landing live rather than having to diff the list by eye.
      if (seen.current) {
        const added = parsed.filter((t) => !seen.current!.has(t.id)).map((t) => t.id);
        if (added.length) {
          setFresh(new Set(added));
          setTimeout(() => setFresh(new Set()), REFRESH_MS);
        }
      }
      seen.current = new Set(parsed.map((t) => t.id));

      setTasks(parsed);
      setLastSync(new Date());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setFirstLoadDone(true);
    }
  }, []);

  useEffect(() => {
    void load();
    const id = setInterval(() => void load(), REFRESH_MS);
    return () => clearInterval(id);
  }, [load]);

  /**
   * The question denominator, on the same clock as the task poll.
   *
   * Deliberately a SEPARATE request rather than a field on `/api/tasks`: it is
   * a count over every question regardless of age, which is precisely what the
   * 200-row task page cannot answer about itself. A failure here leaves the
   * coverage `unknown`, and the rail then states the figure it can defend
   * rather than inventing one.
   */
  useEffect(() => {
    let alive = true;
    const loadCounts = async () => {
      try {
        const res = await fetch("/api/questions");
        const body = await res.json();
        if (!res.ok || body.error) throw new Error(body.error ?? `HTTP ${res.status}`);
        if (alive) setQuestionCoverage({ kind: "measured", total: body.total, done: body.done });
      } catch {
        if (alive) setQuestionCoverage({ kind: "unknown" });
      }
    };
    void loadCounts();
    const id = setInterval(() => void loadCounts(), REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  /**
   * Questions are `task` entities too, so they arrive on the same poll. Split
   * them out here: they belong to the sidebar, and counting them among the work
   * items would inflate every bucket in the task view.
   */
  const questions = useMemo(() => tasks.filter((t) => t.question), [tasks]);
  const work = useMemo(() => tasks.filter((t) => !t.question), [tasks]);

  const go = (section: NavSection) =>
    navigate({ section, agentId: null, questionId: null, sessionId: null, entityId: null });

  /**
   * THE SHEET — the default way an entity opens.
   *
   * A click anywhere in the app inspects the entity in an overlay, so the
   * operator keeps the page (and the scroll position) he was reading. The full
   * page at `#/entities/<id>` is the canonical address and is always one click
   * away from inside the sheet; both render the SAME `EntityDetail`.
   */
  const sheet = useEntitySheet();

  const openEntityPage = (entityId: string) => {
    sheet.close();
    navigate({ section: "entities", agentId: null, questionId: null, sessionId: null, entityId });
  };

  /**
   * Open one agent's detail page, from a task's `assigned_to`.
   *
   * The sheet is closed first for the same reason `openEntityPage` closes it:
   * this is a full-page navigation, and leaving an overlay open over the
   * destination strands the operator behind a panel about a different entity.
   */
  const openAgentPage = (agentId: string) => {
    sheet.close();
    navigate({ section: "agents", agentId, questionId: null, sessionId: null, entityId: null });
  };

  /**
   * Where "back" goes from a question or entity detail. The sidebar is present
   * on every route, so a question can be opened from anywhere; remember the
   * section the operator was on and return them to it rather than always
   * dumping them somewhere arbitrary.
   *
   * `null` until the operator has actually BEEN on a nav section. That matters:
   * seeding this with a section meant a cold load straight into
   * `#/questions/ent_…` inherited a nav highlight nobody had navigated to — the
   * screenshot that prompted this work showed **Sessions** lit up on a question
   * URL. Back then falls back to root, which is a real place, rather than
   * claiming the operator came from a tab they never opened.
   */
  const origin = useRef<NavSection | null>(null);
  // Only a nav section is somewhere "back" can return to; a question or an
  // entity detail is not itself a destination to come back to.
  if (isNavSection(route.section)) {
    origin.current = route.section;
  }
  /** Where a back button actually goes. Root is the honest default. */
  const backTo: NavSection = origin.current ?? "home";

  /**
   * WHICH NAV TAB IS LIT.
   *
   * A detail route is not a section, so it must not light an unrelated tab.
   * Only when the operator demonstrably navigated from a section does that
   * section stay highlighted while its detail is open; otherwise NO tab is
   * active, which is the truthful rendering of "you are not on a nav section".
   * Radix treats a value matching no trigger as no selection, so passing "" is
   * exactly that state.
   */
  const activeTab: string = isNavSection(route.section) ? route.section : (origin.current ?? "");

  const openQuestion = (id: string) =>
    navigate({
      section: "questions",
      agentId: null,
      questionId: id,
      sessionId: null,
      entityId: null,
    });

  const currentQuestion = useMemo(
    () => (route.questionId ? (questions.find((q) => q.id === route.questionId) ?? null) : null),
    [questions, route.questionId],
  );

  // Computed from the SAME question set the sidebar numbers, so the number the
  // operator reads on a card is the number he sees on the detail view.
  const refs = useMemo(() => questionRefs(questions), [questions]);

  // Same first-load-only rule as everywhere else: a question opened before the
  // first poll settles gets a skeleton, but the 10s poll never flashes one back
  // over a question the operator is reading.
  const questionPending = showSkeleton(!firstLoadDone, questions.length > 0);

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex min-h-screen flex-col">
        <nav className="sticky top-0 z-20 flex h-[38px] items-center gap-[14px] border-b bg-card px-4">
          <AtelesLogo />

          {/* Tabs drive the hash router; the header keeps its original look.
              `activeTab` is "" on a detail route the operator did not reach
              from a section, so no tab lights up rather than the wrong one. */}
          <Tabs value={activeTab} onValueChange={(v) => go(v as NavSection)}>
            <TabsList>
              {NAV_LABELS.map((t) => (
                <TabsTrigger key={t.value} value={t.value}>
                  {t.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>

          {/* Nav-mounted, so it is reachable from every route (Cmd/Ctrl-K
              anywhere). A result opens in the SAME sheet every other surface
              uses, so searching never costs the page being read. */}
          <GlobalSearch onOpenEntity={sheet.open} />

          <Tooltip>
            <TooltipTrigger asChild>
              <span
                className={cn(
                  "h-[9px] w-[9px] flex-none rounded-full",
                  error
                    ? "bg-bad"
                    : "animate-pulse-ring bg-ok shadow-[0_0_0_0_hsl(var(--ok)/0.6)]",
                )}
                aria-label={error ? "Connection error" : "Live"}
              />
            </TooltipTrigger>
            <TooltipContent>{error ?? "Live — polling every 10s"}</TooltipContent>
          </Tooltip>
        </nav>

        <div className="flex min-h-0 flex-1 items-start max-[860px]:flex-col">
          {/* Wider column and much less padding: 940px left a lot of the screen
              empty on a normal window, and 24px of top padding above a list is
              most of a row. */}
          <main className="mx-auto min-w-0 max-w-[1200px] flex-1 px-4 pb-8 pt-3">
            {error && (
              <div className="my-4 rounded-lg border border-[hsl(var(--bad)/0.35)] bg-[hsl(var(--bad)/0.12)] px-3 py-[10px] text-[13px]">
                <strong>Cannot reach Neotoma.</strong> {error}
              </div>
            )}

            {route.section === "entities" && route.entityId ? (
              /* The canonical full page for an entity. It fetches its own
                 entity, so a pasted `#/entities/ent_…` loads cold without
                 having come from a session page. */
              <>
                <Button
                  variant="outline"
                  size="sm"
                  className="mb-[14px]"
                  onClick={() => go(backTo)}
                >
                  <ArrowLeft className="h-[14px] w-[14px]" aria-hidden />
                  {BACK_LABELS[backTo]}
                </Button>
                {/* A reference followed from the full page opens in the sheet,
                    so the operator keeps this page behind it. */}
                <EntityPage id={route.entityId} onOpenEntity={sheet.open} />
              </>
            ) : route.section === "questions" ? (
              questionPending ? (
                <QuestionDetailSkeleton />
              ) : currentQuestion ? (
                <QuestionDetail
                  question={currentQuestion}
                  qref={refs.get(currentQuestion.id) ?? null}
                  onBack={() => go(backTo)}
                  backLabel={BACK_LABELS[backTo]}
                  onOpenEntity={sheet.open}
                />
              ) : (
                <div className="p-6 text-center text-muted-foreground">
                  <p>No open question with that id.</p>
                  <Button variant="chip" size="chip" onClick={() => go(backTo)}>
                    Back
                  </Button>
                </div>
              )
            ) : route.section === "tasks" ? (
              <TaskList
                work={work}
                firstLoadDone={firstLoadDone}
                lastSync={lastSync}
                fresh={fresh}
                // A task row opens the sheet, same as a session's related
                // entities — never a link out to Neotoma.
                onOpenEntity={sheet.open}
                onOpenAgent={openAgentPage}
              />
            ) : route.section === "workflows" ? (
              <Workflows />
            ) : route.section === "lifecycle" ? (
              <Lifecycle />
            ) : route.section === "schemas" ? (
              <Schemas />
            ) : route.section === "home" || route.section === "sessions" ? (
              /* ROOT and the INDEX are different views of the same data, so one
                 component serves both and `view` says which. Root is the live
                 session; `#/sessions` is the scannable index. */
              <Sessions
                view={route.section === "home" ? "current" : "index"}
                selected={route.sessionId}
                onSelect={(sessionId: string | null) =>
                  navigate({
                    section: "sessions",
                    agentId: null,
                    questionId: null,
                    sessionId,
                    entityId: null,
                  })
                }
                onOpenCurrent={() =>
                  navigate({
                    section: "home",
                    agentId: null,
                    questionId: null,
                    sessionId: null,
                    entityId: null,
                  })
                }
                // Entities referenced by a session open in the sheet, so
                // reading one never costs the session page.
                onOpenEntity={sheet.open}
                onOpenAgent={openAgentPage}
              />
            ) : (
              <AgentDirectory
                selected={route.agentId}
                onSelect={(agentId: string | null) =>
                  navigate({
                    section: "agents",
                    agentId,
                    questionId: null,
                    sessionId: null,
                    entityId: null,
                  })
                }
                // An assigned task opens in the sheet, so reading one never
                // costs the agent page the operator navigated to.
                onOpenEntity={sheet.open}
              />
            )}
          </main>

          {/* Read-only by construction — see Questions.tsx. It still refreshes on
              the shared 10s poll, so a newly-raised question appears unprompted. */}
          <Questions
            questions={questions}
            coverage={questionCoverage(questionCoverageState, questions)}
            firstLoadDone={firstLoadDone}
            onOpen={openQuestion}
            openId={route.questionId}
          />
        </div>

        {/* Mounted once at the root so it overlays whatever section is on
            screen. It renders the same `EntityDetail` the full page does. */}
        <EntitySheet
          id={sheet.current}
          depth={sheet.depth}
          onOpenChange={(open) => {
            if (!open) sheet.close();
          }}
          onPush={sheet.push}
          onBack={sheet.back}
          onFullPage={openEntityPage}
        />
      </div>
    </TooltipProvider>
  );
}
