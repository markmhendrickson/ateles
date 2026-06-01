import { BASE_URL } from './client'

export function getSourceContentUrl(sourceId: string): string {
  return `${BASE_URL}/sources/${sourceId}/content`
}

/**
 * Returns the content URL for a source.
 * Server-side resolution handles JSON-to-PDF sibling redirect,
 * so the client no longer needs to probe for alternative sources.
 */
export async function resolveSourceContentUrl(
  sourceId: string,
  _sourceHint?: string | null,
): Promise<string> {
  return getSourceContentUrl(sourceId)
}
