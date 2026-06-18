/** Register the gateway health endpoint. */
import type { Hono } from "hono";

import { APP_VERSION } from "../project-version.js";

/** Mount GET /healthz with the existing health payload. */
export function registerHealthRoutes(app: Hono): void {
  app.get("/healthz", (context) => {
    return context.json({ status: "ok", version: APP_VERSION });
  });
}
