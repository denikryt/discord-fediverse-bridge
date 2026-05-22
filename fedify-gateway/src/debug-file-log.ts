import { createWriteStream, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { GatewayConfig } from "./config.js";

let debugLogStream: ReturnType<typeof createWriteStream> | null = null;
let debugLogPath: string | null = null;
let fetchLoggingInstalled = false;
let originalFetch: typeof fetch | null = null;

export function initializeDebugFileLog(config: GatewayConfig): string | null {
  if (config.logLevel !== "debug") {
    return null;
  }
  if (debugLogStream != null) {
    return debugLogPath;
  }

  const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
  const logsDir = resolve(projectRoot, "logs");
  mkdirSync(logsDir, { recursive: true });

  const stamp = new Date().toISOString().replaceAll(":", "-").replaceAll(".", "-");
  debugLogPath = resolve(logsDir, `gateway-debug-${stamp}-pid${process.pid}.jsonl`);
  debugLogStream = createWriteStream(debugLogPath, { flags: "wx" });
  appendDebugFileLog("gateway.debug_log.started", {
    logPath: debugLogPath,
    pid: process.pid,
    fedifyOrigin: config.fedifyOrigin,
  });
  return debugLogPath;
}

export function appendDebugFileLog(event: string, payload: Record<string, unknown>): void {
  if (debugLogStream == null) {
    return;
  }

  debugLogStream.write(`${JSON.stringify({ ts: new Date().toISOString(), event, ...payload })}\n`);
}

export function installDebugFetchLogging(config: GatewayConfig): void {
  if (config.logLevel !== "debug" || fetchLoggingInstalled) {
    return;
  }
  initializeDebugFileLog(config);
  originalFetch = globalThis.fetch.bind(globalThis);
  fetchLoggingInstalled = true;

  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const startedAt = Date.now();
    const requestSummary = summarizeFetchRequest(input, init);
    appendDebugFileLog("fetch.request", requestSummary);

    try {
      const response = await originalFetch!(input, init);
      const responseBody = await safeReadResponseBody(response);
      appendDebugFileLog("fetch.response", {
        ...requestSummary,
        status: response.status,
        statusText: response.statusText,
        headers: headersToRecord(response.headers),
        body: responseBody,
        durationMs: Date.now() - startedAt,
      });
      return response;
    } catch (error) {
      appendDebugFileLog("fetch.error", {
        ...requestSummary,
        error: error instanceof Error ? error.message : String(error),
        durationMs: Date.now() - startedAt,
      });
      throw error;
    }
  }) as typeof fetch;
}

function summarizeFetchRequest(input: RequestInfo | URL, init?: RequestInit): Record<string, unknown> {
  const request = input instanceof Request ? input : null;
  const url = request?.url ?? input.toString();
  const method = init?.method ?? request?.method ?? "GET";
  const headers = headersToRecord(init?.headers ?? request?.headers ?? undefined);
  const body = summarizeBody(init?.body);

  return {
    url,
    method,
    headers,
    body,
  };
}

function headersToRecord(headers: HeadersInit | Headers | undefined): Record<string, string> {
  if (headers == null) {
    return {};
  }
  if (headers instanceof Headers) {
    return Object.fromEntries(headers.entries());
  }
  if (Array.isArray(headers)) {
    return Object.fromEntries(headers.map(([key, value]) => [key, value]));
  }
  return Object.fromEntries(
    Object.entries(headers).map(([key, value]) => [key, String(value)]),
  );
}

function summarizeBody(body: BodyInit | null | undefined): string | null {
  if (body == null) {
    return null;
  }
  if (typeof body === "string") {
    return body;
  }
  if (body instanceof URLSearchParams) {
    return body.toString();
  }
  if (body instanceof ArrayBuffer) {
    return Buffer.from(body).toString("utf8");
  }
  if (ArrayBuffer.isView(body)) {
    return Buffer.from(body.buffer, body.byteOffset, body.byteLength).toString("utf8");
  }
  return `[unlogged ${body.constructor.name} body]`;
}

async function safeReadResponseBody(response: Response): Promise<string> {
  try {
    return await response.clone().text();
  } catch (error) {
    return `[unreadable response body: ${error instanceof Error ? error.message : String(error)}]`;
  }
}
