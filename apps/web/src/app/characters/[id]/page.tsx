"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Notice } from "@/components/Gate";
import { api, ApiError, json, type CharacterView, type Conversation } from "@/lib/api";
import { useSession } from "@/lib/session";

export default function CharacterPage() {
  const { id } = useParams<{ id: string }>();
  const { me } = useSession();
  const router = useRouter();
  const [view, setView] = useState<CharacterView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<CharacterView>(`/characters/${id}`)
      .then(setView)
      .catch((e) => setError(e instanceof ApiError && e.status === 404 ? "Character not found." : "Couldn't load character."));
  }, [id, me?.adult_access]);

  async function startChat() {
    setBusy(true);
    try {
      const conv = await api<Conversation>("/conversations", { method: "POST", body: json({ character_id: id }) });
      router.push(`/chats/${conv.id}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't start a chat.");
      setBusy(false);
    }
  }

  async function requestPublish() {
    try {
      await api(`/characters/${id}/publish`, { method: "POST" });
      setView(await api<CharacterView>(`/characters/${id}`));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't submit for review.");
    }
  }

  if (error) return <Notice text={error} />;
  if (!view) return <p className="text-ink-muted">Loading…</p>;
  const { preview, detail } = view;

  return (
    <article className="mx-auto flex max-w-2xl flex-col gap-5">
      <header>
        <p className="text-xs uppercase tracking-wide text-ink-muted">
          {preview.is_curated ? "Curated · " : ""}AI-generated fictional character
        </p>
        <h1 className="mt-1 text-3xl font-semibold">{preview.display_name}</h1>
        <p className="mt-2 text-ink-muted">{preview.tagline}</p>
      </header>

      {!detail ? (
        <Notice text={me ? "Verify your age to see the full character and start chatting." : "Sign in and verify your age to chat."}>
          <Link href={me ? "/verify" : "/login"} className="btn-primary">
            {me ? "Verify age" : "Sign in"}
          </Link>
        </Notice>
      ) : (
        <>
          <div className="flex flex-wrap gap-3">
            <button className="btn-primary" onClick={startChat} disabled={busy}>
              {busy ? "Starting…" : "Start a chat"}
            </button>
            {detail.is_owner && (
              <>
                <Link className="btn-ghost" href={`/characters/new?edit=${detail.id}`}>
                  Edit
                </Link>
                {(detail.moderation_state === "draft" || detail.moderation_state === "rejected") && !detail.is_curated && (
                  <button className="btn-ghost" onClick={requestPublish}>
                    Submit for public review
                  </button>
                )}
              </>
            )}
          </div>
          {detail.is_owner && (
            <p className="text-sm text-ink-muted">
              Visibility: <strong>{detail.visibility}</strong> · Review: <strong>{detail.moderation_state.replace("_", " ")}</strong> · Version {detail.version}
            </p>
          )}
          <dl className="card grid gap-4 p-5 text-sm">
            <Field label="Age" value={`${detail.card.declared_age} (adult)`} />
            <Field label="About" value={detail.card.description} />
            <Field label="Personality" value={detail.card.personality} />
            <Field label="Scenario" value={detail.card.scenario} />
            <Field label="Opening line" value={detail.card.first_messages[0] ?? ""} />
          </dl>
        </>
      )}
    </article>
  );
}

function Field({ label, value }: { label: string; value?: string }) {
  if (!value) return null;
  return (
    <div>
      <dt className="text-ink-muted">{label}</dt>
      <dd className="mt-0.5 whitespace-pre-wrap">{value}</dd>
    </div>
  );
}
