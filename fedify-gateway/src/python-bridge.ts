import type { InternalBridgeEvent } from "./types.js";

export async function deliverEventToPythonBridge(
  eventsUrl: string,
  sharedSecret: string,
  event: InternalBridgeEvent,
): Promise<void> {
  // The Python bridge is the single downstream consumer, so this helper owns
  // the authenticated POST contract and its diagnostic logging.
  const isDebug = process.env.FEDIFY_LOG_LEVEL === "debug";
  const body = JSON.stringify(event);
  console.log("[Bridge] Sending event:", {
    url: eventsUrl,
    deliveryId: event.delivery_id,
    eventType: event.event_type,
    objectId: describeEventObject(event),
  });
  if (isDebug) {
    console.log("[Bridge][debug] Event payload:", body);
    console.log("[Bridge][debug] Event body size:", body.length);
  }

  let response: Response;
  try {
    response = await fetch(eventsUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${sharedSecret}`,
        "Content-Type": "application/json",
        "X-Bridge-Delivery-Id": event.delivery_id,
      },
      body,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(
      `Python bridge fetch failed for ${event.delivery_id} to ${eventsUrl}: ${message}`,
    );
  }

  console.log("[Bridge] Response status:", {
    deliveryId: event.delivery_id,
    status: response.status,
    statusText: response.statusText,
  });
  let parsedResponse: { status?: string; detail?: string } | null = null;
  try {
    parsedResponse = (await response.clone().json()) as {
      status?: string;
      detail?: string;
    };
  } catch {
    parsedResponse = null;
  }

  if (!response.ok) {
    const responseBody = await response.text();
    if (isDebug) {
      console.log("[Bridge][debug] Response body:", responseBody);
    }
    throw new Error(
      `Python bridge rejected delivery ${event.delivery_id}: ${response.status} ${response.statusText} ${responseBody}`,
    );
  }
  console.log("[Bridge] Event delivered:", {
    deliveryId: event.delivery_id,
    objectId: describeEventObject(event),
    resultStatus: parsedResponse?.status ?? "ok",
    resultDetail: parsedResponse?.detail ?? null,
  });
}

function describeEventObject(event: InternalBridgeEvent): string {
  // Content events and follow lifecycle events do not share the same object
  // shape, so logging narrows them to one stable identifier string.
  if (event.event_type === "follow.accepted") {
    return event.object.follow_activity_id;
  }
  return event.object.ap_id;
}
