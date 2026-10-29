"use client";

import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { AdultGate } from "@/components/Gate";
import {
  api,
  ApiError,
  json,
  newKey,
  type Balance,
  type Conversation,
  type GenerationAccepted,
  type Message,
  type MessagePage,
} from "@/lib/api";
import { followGeneration } from "@/lib/stream";

type Live = { messageId: string; text: string };

function Chat() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [conv, setConv] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [olderBefore, setOlderBefore] = useState<number | null>(null);
  const [live, setLive] = useState<Live | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [announce, setAnnounce] = useState("");
  const [balance, setBalance] = useState<Balance | null>(null);
  const stopRef = useRef<(() => void) | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const refreshBalance = useCallback(() => api<Balance>("/credits/balance").then(setBalance).catch(() => {}), []);

  const follow = useCallback(
    (eventsUrl: string, messageId: string) => {
      stopRef.current?.();
      setLive({ messageId, text: "" });
      stopRef.current = followGeneration(eventsUrl, {
        onStart: () => setLive({ messageId, text: "" }),
        onDelta: (t) => setLive((l) => (l ? { ...l, text: l.text + t } : l)),
        onComplete: (mid, content) => {
          // Dedupe by message id: the persisted message replaces the live bubble exactly once.
          setMessages((ms) => ms.map((m) => (m.id === mid ? { ...m, state: "completed", content } : m)));
          setLive(null);
          setAnnounce("Reply received.");
          void refreshBalance();
        },
        onFailed: (code) => {
          setMessages((ms) => ms.map((m) => (m.id === messageId ? { ...m, state: "failed" } : m)));
          setLive(null);
          setError(code === "USAGE_LIMIT_REACHED" ? "Daily limit reached." : "The reply failed. Your allowance was not charged.");
          void refreshBalance();
        },
      });
    },
    [refreshBalance],
  );

  useEffect(() => {
    (async () => {
      try {
        const [c, page] = await Promise.all([
          api<Conversation>(`/conversations/${id}`),
          api<MessagePage>(`/conversations/${id}/messages?limit=50`),
        ]);
        setConv(c);
        setMessages(page.items);
        setOlderBefore(page.next_before ?? null);
        const last = page.items.at(-1);
        if (last && last.role === "assistant" && (last.state === "pending" || last.state === "streaming") && last.request_id) {
          follow(`/api/v1/conversations/${id}/events/${last.request_id}`, last.id);
        }
      } catch (e) {
        setError(e instanceof ApiError && e.status === 404 ? "Chat not found." : "Couldn't load chat.");
      }
    })();
    void refreshBalance();
    return () => stopRef.current?.();
  }, [id, follow, refreshBalance]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length, live?.text]);

  async function loadOlder() {
    if (olderBefore === null) return;
    const page = await api<MessagePage>(`/conversations/${id}/messages?limit=50&before=${olderBefore}`);
    setMessages((ms) => [...page.items, ...ms]);
    setOlderBefore(page.next_before ?? null);
  }

  async function send(e: React.FormEvent) {
    e.preventDefault();
    const content = draft.trim();
    if (!content || live) return;
    setError(null);
    try {
      const acc = await api<GenerationAccepted>(`/conversations/${id}/messages`, {
        method: "POST",
        body: json({ content, client_message_id: crypto.randomUUID() }),
        idempotencyKey: newKey("msg"),
      });
      const now = new Date().toISOString();
      const ordinal = (messages.at(-1)?.ordinal ?? 0) + 1;
      setMessages((ms) => [
        ...ms,
        { id: acc.user_message_id!, turn_id: acc.turn_id, ordinal, role: "user", variant: 0, variant_count: 1, state: "completed", content, created_at: now },
        { id: acc.assistant_message_id, turn_id: acc.turn_id, ordinal, role: "assistant", variant: 0, variant_count: 1, state: "pending", content: null, created_at: now, request_id: acc.request_id },
      ]);
      setDraft("");
      follow(acc.events_url, acc.assistant_message_id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't send.");
    }
  }

  async function regenerate() {
    setError(null);
    try {
      const acc = await api<GenerationAccepted>(`/conversations/${id}/regenerate`, { method: "POST", idempotencyKey: newKey("regen") });
      setMessages((ms) => {
        const last = ms.at(-1)!;
        return [
          ...ms.slice(0, -1),
          { ...last, id: acc.assistant_message_id, variant: last.variant_count, variant_count: last.variant_count + 1, state: "pending", content: null, request_id: acc.request_id },
        ];
      });
      follow(acc.events_url, acc.assistant_message_id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't regenerate.");
    }
  }

  async function exportChat() {
    const data = await api<unknown>(`/conversations/${id}/export`);
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: `aster-chat-${id}.json` });
    a.click();
    URL.revokeObjectURL(url);
  }

  async function toggleArchive() {
    if (!conv) return;
    setConv(await api<Conversation>(`/conversations/${id}`, { method: "PATCH", body: json({ status: conv.status === "active" ? "archived" : "active" }) }));
  }

  async function remove() {
    if (!window.confirm("Delete this chat permanently? This can't be undone.")) return;
    await api(`/conversations/${id}`, { method: "DELETE" });
    router.push("/chats");
  }

  if (error && !conv) return <p role="alert">{error}</p>;
  if (!conv) return <p className="text-ink-muted">Loading…</p>;
  const last = messages.at(-1);
  const canRegenerate = !live && last?.role === "assistant" && last.ordinal > 0 && conv.status === "active";

  return (
    <div className="mx-auto flex h-[calc(100dvh-9rem)] max-w-3xl flex-col">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line pb-3">
        <div>
          <h1 className="font-semibold">{conv.character_name}</h1>
          <p className="text-xs text-ink-muted">AI-generated fictional character · version {conv.character_version}</p>
        </div>
        <div className="flex flex-wrap gap-2 text-sm">
          <button className="btn-ghost !px-3 !py-1.5" onClick={exportChat}>Export</button>
          <button className="btn-ghost !px-3 !py-1.5" onClick={toggleArchive}>{conv.status === "active" ? "Archive" : "Unarchive"}</button>
          <button className="btn-danger !px-3 !py-1.5" onClick={remove}>Delete</button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto py-4" role="log" aria-live="off" aria-label="Conversation">
        {olderBefore !== null && (
          <button className="btn-ghost mx-auto mb-4 block text-xs" onClick={loadOlder}>Load earlier messages</button>
        )}
        <ol className="flex flex-col gap-3">
          {messages.map((m) => {
            const isLive = live?.messageId === m.id;
            const text = isLive ? live.text : m.content;
            const mine = m.role === "user";
            return (
              <li key={m.id} className={`flex ${mine ? "justify-end" : "justify-start"}`}>
                <div
                  className={`max-w-[85%] whitespace-pre-wrap rounded-2xl px-4 py-2.5 text-[15px] leading-relaxed ${mine ? "bg-accent text-accent-ink" : "bg-surface-2"}`}
                  aria-busy={isLive || m.state === "pending"}
                >
                  <span className="sr-only">{mine ? "You: " : `${conv.character_name}: `}</span>
                  {m.state === "failed" ? (
                    <em className="text-danger">Reply failed.</em>
                  ) : text ? (
                    text
                  ) : (
                    <span className="text-ink-muted">…</span>
                  )}
                  {!mine && m.variant_count > 1 && (
                    <span className="mt-1 block text-[11px] text-ink-muted">Version {m.variant + 1} of {m.variant_count}</span>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
        <div ref={bottomRef} />
      </div>

      <p className="sr-only" role="status">{announce}</p>
      {error && <p role="alert" className="mb-2 text-sm text-danger">{error}</p>}

      {conv.status === "archived" ? (
        <p className="text-sm text-ink-muted">This chat is archived. Unarchive it to continue.</p>
      ) : (
        <form onSubmit={send} className="flex flex-col gap-2 border-t border-line pt-3">
          <label htmlFor="composer" className="sr-only">Message</label>
          <textarea
            id="composer"
            className="field min-h-[3rem] resize-none"
            rows={2}
            maxLength={4000}
            placeholder={`Message ${conv.character_name}`}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-ink-muted">
              {balance ? `${balance.remaining} of ${balance.limit} messages left today` : ""}
              {canRegenerate && " · Regenerating uses one message"}
            </span>
            <div className="flex gap-2">
              {canRegenerate && (
                <button type="button" className="btn-ghost" onClick={regenerate}>Regenerate</button>
              )}
              <button className="btn-primary" disabled={!!live || !draft.trim()}>Send</button>
            </div>
          </div>
        </form>
      )}
    </div>
  );
}

export default function Page() {
  return (
    <AdultGate>
      <Chat />
    </AdultGate>
  );
}
