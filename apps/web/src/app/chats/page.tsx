"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { AdultGate } from "@/components/Gate";
import { api, type ConversationPage } from "@/lib/api";

function Chats() {
  const [status, setStatus] = useState<"active" | "archived">("active");
  const [loaded, setLoaded] = useState<{ status: string; page: ConversationPage } | null>(null);
  const page = loaded?.status === status ? loaded.page : null;

  useEffect(() => {
    api<ConversationPage>(`/conversations?status=${status}&limit=50`).then((p) => setLoaded({ status, page: p }));
  }, [status]);

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Chats</h1>
        <div role="tablist" aria-label="Chat status" className="flex gap-1 rounded-lg bg-surface p-1">
          {(["active", "archived"] as const).map((s) => (
            <button
              key={s}
              role="tab"
              aria-selected={status === s}
              onClick={() => setStatus(s)}
              className={`rounded-md px-3 py-1 text-sm capitalize ${status === s ? "bg-surface-2" : "text-ink-muted"}`}
            >
              {s}
            </button>
          ))}
        </div>
      </div>
      {!page ? (
        <p className="text-ink-muted">Loading…</p>
      ) : page.items.length === 0 ? (
        <p className="text-ink-muted">
          No {status} chats. <Link className="text-accent underline" href="/">Find a character</Link>.
        </p>
      ) : (
        <ul className="card divide-y divide-line">
          {page.items.map((c) => (
            <li key={c.id}>
              <Link href={`/chats/${c.id}`} className="flex items-center justify-between gap-3 px-4 py-3 hover:bg-surface-2">
                <span>
                  <span className="block font-medium">{c.title}</span>
                  <span className="text-sm text-ink-muted">with {c.character_name}</span>
                </span>
                <time className="shrink-0 text-xs text-ink-muted" dateTime={c.last_message_at}>
                  {new Date(c.last_message_at).toLocaleString()}
                </time>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function Page() {
  return (
    <AdultGate>
      <Chats />
    </AdultGate>
  );
}
