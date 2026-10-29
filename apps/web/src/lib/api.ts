import type { components } from "./api-types";

type S = components["schemas"];
export type Me = S["MeResponse"];
export type CharacterPreview = S["CharacterPreview"];
export type CharacterPage = S["CharacterPage"];
export type CharacterView = S["CharacterView"];
export type CharacterDetail = S["CharacterDetail"];
export type CharacterCard = S["CharacterCard"];
export type Conversation = S["ConversationOut"];
export type ConversationPage = S["ConversationPage"];
export type Message = S["MessageOut"];
export type MessagePage = S["MessagePage"];
export type GenerationAccepted = S["GenerationAccepted"];
export type Balance = S["BalanceResponse"];

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
  ) {
    super(message);
  }
}

let csrfToken: string | null = null;
export function setCsrf(token: string | null) {
  csrfToken = token;
}

const DEV_REGION_KEY = "aster.devRegion";
export function devRegion(): string | null {
  try {
    return localStorage.getItem(DEV_REGION_KEY);
  } catch {
    return null;
  }
}
export function setDevRegion(region: string) {
  try {
    localStorage.setItem(DEV_REGION_KEY, region);
  } catch {
    /* storage unavailable: header just won't be sent */
  }
}

export async function api<T>(path: string, init: RequestInit & { idempotencyKey?: string } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const method = (init.method ?? "GET").toUpperCase();
  if (init.body !== undefined && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (method !== "GET" && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  if (init.idempotencyKey) headers.set("Idempotency-Key", init.idempotencyKey);
  const region = devRegion();
  if (region) headers.set("X-Dev-Region", region);
  const res = await fetch(`/api/v1${path}`, { ...init, headers, credentials: "same-origin", cache: "no-store" });
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const err = body?.error ?? {};
    throw new ApiError(res.status, err.code ?? "HTTP_ERROR", err.message ?? res.statusText, err.request_id);
  }
  return body as T;
}

export const json = (value: unknown) => JSON.stringify(value);

export function newKey(prefix: string) {
  return `${prefix}_${crypto.randomUUID().replace(/-/g, "")}`;
}
