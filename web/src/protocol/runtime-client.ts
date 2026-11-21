import type { GuiProtocolClient } from "@/protocol/client";

let activeClient: GuiProtocolClient | null = null;
let activeSubscriptionId: string | null = null;

export function getProtocolClient(): GuiProtocolClient | null {
  return activeClient;
}

export function setProtocolClient(client: GuiProtocolClient | null): void {
  activeClient = client;
}

export function getSubscriptionId(): string | null {
  return activeSubscriptionId;
}

export function setSubscriptionId(subscriptionId: string | null): void {
  activeSubscriptionId = subscriptionId;
}
