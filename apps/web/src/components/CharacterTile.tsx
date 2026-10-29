import Link from "next/link";
import type { CharacterPreview } from "@/lib/api";

function initials(name: string) {
  return name
    .split(/\s+/)
    .slice(0, 2)
    .map((p) => p[0] ?? "")
    .join("")
    .toUpperCase();
}

/** Deterministic hue per character until character portraits provide real portraits. */
function hue(id: string) {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return h;
}

export function CharacterTile({ c }: { c: CharacterPreview }) {
  return (
    <Link href={`/characters/${c.id}`} className="card group flex flex-col gap-3 p-4 transition-colors hover:border-accent/60">
      <div
        aria-hidden
        className="grid aspect-[4/3] place-items-center rounded-lg text-3xl font-semibold text-white/90"
        style={{ background: `linear-gradient(135deg, hsl(${hue(c.id)} 45% 32%), hsl(${(hue(c.id) + 50) % 360} 50% 22%))` }}
      >
        {initials(c.display_name)}
      </div>
      <div>
        <h3 className="font-semibold group-hover:text-accent">{c.display_name}</h3>
        <p className="mt-1 line-clamp-2 text-sm text-ink-muted">{c.tagline}</p>
      </div>
      <ul className="mt-auto flex flex-wrap gap-1.5" aria-label="Tags">
        {c.tags.slice(0, 4).map((t) => (
          <li key={t} className="rounded-full bg-surface-2 px-2 py-0.5 text-xs text-ink-muted">
            {t}
          </li>
        ))}
      </ul>
    </Link>
  );
}
