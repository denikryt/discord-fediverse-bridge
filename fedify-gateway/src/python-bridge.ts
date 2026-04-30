import type { BridgeEvent } from "./types.js";

export async function deliverEventToPythonBridge(
  eventsUrl: string,
  sharedSecret: string,
  event: BridgeEvent,
): Promise<void> {
  const body = JSON.stringify(event);
  console.log("[Bridge] Sending event to Python bridge:", {
    url: eventsUrl,
    deliveryId: event.delivery_id,
    eventType: event.event_type,
    bodySize: body.length,
    communityActorId: event.community_actor_id,
    objectApId: event.object.ap_id,
  });
  console.log("[Bridge] Event payload:", body);

  const response = await fetch(eventsUrl, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${sharedSecret}`,
      "Content-Type": "application/json",
      "X-Bridge-Delivery-Id": event.delivery_id,
    },
    body,
  });

  console.log("[Bridge] Response status:", response.status, response.statusText);
  if (!response.ok) {
    const responseBody = await response.text();
    console.log("[Bridge] Response body:", responseBody);
    throw new Error(
      `Python bridge rejected delivery ${event.delivery_id}: ${response.status} ${response.statusText} ${responseBody}`,
    );
  }
  console.log("[Bridge] Event delivered successfully");
}
