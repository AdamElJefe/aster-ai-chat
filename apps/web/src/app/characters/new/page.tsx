"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { AdultGate } from "@/components/Gate";
import { api, ApiError, json, type CharacterCard, type CharacterDetail, type CharacterView } from "@/lib/api";

const EMPTY: CharacterCard = {
  display_name: "",
  declared_age: 25,
  tagline: "",
  description: "",
  personality: "",
  speech_style: "",
  backstory: "",
  scenario: "",
  first_messages: [""],
  example_dialogue: [],
  content_tags: [],
};

const TEXT_FIELDS: { key: keyof CharacterCard; label: string; max: number; rows: number; hint?: string }[] = [
  { key: "tagline", label: "Tagline", max: 280, rows: 2, hint: "Shown publicly before age verification. Keep it non-explicit." },
  { key: "description", label: "Description", max: 2000, rows: 3 },
  { key: "personality", label: "Personality", max: 4000, rows: 3 },
  { key: "speech_style", label: "Speech style", max: 1000, rows: 2 },
  { key: "backstory", label: "Backstory", max: 6000, rows: 4 },
  { key: "scenario", label: "Scenario", max: 4000, rows: 3 },
];

function Editor() {
  const params = useSearchParams();
  const editId = params.get("edit");
  const router = useRouter();
  const [card, setCard] = useState<CharacterCard>(EMPTY);
  const [tags, setTags] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!editId) return;
    api<CharacterView>(`/characters/${editId}`).then((v) => {
      if (v.detail) {
        setCard(v.detail.card);
        setTags(v.detail.card.content_tags?.join(", ") ?? "");
      }
    });
  }, [editId]);

  const set = <K extends keyof CharacterCard>(k: K, v: CharacterCard[K]) => setCard((c) => ({ ...c, [k]: v }));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const body = {
      ...card,
      content_tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      first_messages: card.first_messages.filter((m) => m.trim()),
    };
    try {
      const saved = await api<CharacterDetail>(editId ? `/characters/${editId}` : "/characters", {
        method: editId ? "PATCH" : "POST",
        body: json(body),
      });
      router.push(`/characters/${saved.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't save");
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="mx-auto flex max-w-2xl flex-col gap-5">
      <div>
        <h1 className="text-2xl font-semibold">{editId ? "Edit character" : "Create a character"}</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Characters are private to you unless you submit them for review. Every character must be 18 or older; school-age
          framing isn&apos;t allowed. {editId && "Saving creates a new version; existing chats keep the version they started with."}
        </p>
      </div>
      <div className="grid gap-4 sm:grid-cols-[1fr_8rem]">
        <div>
          <label className="label" htmlFor="name">Name</label>
          <input id="name" className="field" required maxLength={80} value={card.display_name} onChange={(e) => set("display_name", e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="age">Age</label>
          <input
            id="age"
            className="field"
            type="number"
            required
            min={18}
            max={1000}
            value={card.declared_age}
            onChange={(e) => set("declared_age", Number(e.target.value))}
            aria-describedby="age-hint"
          />
          <p id="age-hint" className="hint">18 or older.</p>
        </div>
      </div>
      {TEXT_FIELDS.map((f) => (
        <div key={f.key}>
          <label className="label" htmlFor={f.key}>{f.label}</label>
          <textarea
            id={f.key}
            className="field"
            rows={f.rows}
            maxLength={f.max}
            value={(card[f.key] as string | undefined) ?? ""}
            onChange={(e) => set(f.key, e.target.value as never)}
            aria-describedby={f.hint ? `${f.key}-hint` : undefined}
          />
          {f.hint && <p id={`${f.key}-hint`} className="hint">{f.hint}</p>}
        </div>
      ))}
      <div>
        <label className="label" htmlFor="greeting">Opening message</label>
        <textarea
          id="greeting"
          className="field"
          rows={3}
          required
          maxLength={2000}
          value={card.first_messages[0] ?? ""}
          onChange={(e) => set("first_messages", [e.target.value])}
        />
      </div>
      <div>
        <label className="label" htmlFor="tags">Tags</label>
        <input id="tags" className="field" placeholder="fantasy, slow-burn" value={tags} onChange={(e) => setTags(e.target.value)} />
        <p className="hint">Comma-separated, up to 20.</p>
      </div>
      {error && <p role="alert" className="text-danger">{error}</p>}
      <div className="flex gap-3">
        <button className="btn-primary" disabled={busy}>{busy ? "Saving…" : "Save character"}</button>
        <button type="button" className="btn-ghost" onClick={() => router.back()}>Cancel</button>
      </div>
    </form>
  );
}

export default function Page() {
  return (
    <AdultGate>
      <Suspense>
        <Editor />
      </Suspense>
    </AdultGate>
  );
}
