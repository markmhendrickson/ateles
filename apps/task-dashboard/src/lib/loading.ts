/**
 * FIRST LOAD vs REFRESH
 * ---------------------
 * The one rule that keeps skeletons off the poll. This app refetches every 10
 * seconds; showing a skeleton whenever a request is in flight would strobe the
 * whole page on every tick. A skeleton belongs on screen only while the first
 * fetch has not yet produced anything to show — after that, polls re-render
 * existing data in place and the operator keeps reading uninterrupted.
 *
 * Lives in its own module rather than beside the placeholder components so
 * React Fast Refresh keeps working for that file (a mixed component/non-
 * component export invalidates it).
 */
export function showSkeleton(loading: boolean, hasData: boolean): boolean {
  return loading && !hasData;
}
