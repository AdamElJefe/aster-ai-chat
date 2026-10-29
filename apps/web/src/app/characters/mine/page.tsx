"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CharacterTile } from "@/components/CharacterTile";
import { AdultGate } from "@/components/Gate";
import { api, type CharacterPage } from "@/lib/api";

function Mine() {
  const [page, setPage] = useState<CharacterPage | null>(null);
  useEffect(() => {
    api<CharacterPage>("/characters?scope=mine&limit=100").then(setPage);
  }, []);
  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">My characters</h1>
        <Link href="/characters/new" className="btn-primary">New character</Link>
      </div>
      {!page ? (
        <p className="text-ink-muted">Loading…</p>
      ) : page.items.length === 0 ? (
        <p className="text-ink-muted">You haven&apos;t created any characters yet. They stay private unless you submit one for review.</p>
      ) : (
        <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {page.items.map((c) => (
            <li key={c.id} className="flex"><CharacterTile c={c} /></li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function Page() {
  return (
    <AdultGate>
      <Mine />
    </AdultGate>
  );
}
