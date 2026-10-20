/** Feed synthetic public ActivityPub documents to Fedify in isolated HTTP tests. */

import type { AuthenticatedDocumentLoaderFactory, DocumentLoaderFactory } from "@fedify/vocab-runtime";

/** Resolve only through a test's fetch stub, including JSON-LD contexts. */
export const fixtureDocumentLoaderFactory: DocumentLoaderFactory = () => async (url) => {
  // Fedify 2.2.19 checks DNS before calling fetch. These fixtures use reserved
  // hostnames, so the fake loader must sit at the document-loader boundary.
  const response = await fetch(url, { headers: { Accept: "application/activity+json" } });
  if (!response.ok) {
    throw new Error(`fixture document unavailable: ${url} (${response.status})`);
  }
  return {
    contextUrl: null,
    document: await response.json(),
    documentUrl: url,
  };
};

/** Use the same fake remote documents when Fedify signs local actor requests. */
export const fixtureAuthenticatedDocumentLoaderFactory: AuthenticatedDocumentLoaderFactory = () =>
  fixtureDocumentLoaderFactory();
