/** Load the shared project release version for the Fedify gateway. */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const VERSION_FILE = resolve(dirname(fileURLToPath(import.meta.url)), "../../VERSION");

/** Read one non-empty version value from the canonical root VERSION file. */
export function readProjectVersion(path = VERSION_FILE): string {
  let version: string;
  try {
    version = readFileSync(path, "utf8").trim();
  } catch (error) {
    throw new Error(`Project version file is missing: ${path}`, { cause: error });
  }

  if (version.length === 0) {
    throw new Error(`Project version file is empty: ${path}`);
  }
  return version;
}

export const APP_VERSION = readProjectVersion();
