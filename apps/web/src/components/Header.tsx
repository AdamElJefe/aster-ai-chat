"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useSession } from "@/lib/session";

const NAV = [
  { href: "/", label: "Discover" },
  { href: "/chats", label: "Chats" },
  { href: "/characters/mine", label: "My characters" },
  { href: "/account", label: "Account" },
];

export function Header() {
  const { me } = useSession();
  const path = usePathname();
  return (
    <header className="sticky top-0 z-40 border-b border-line bg-bg/90 backdrop-blur">
      <div className="mx-auto flex max-w-5xl items-center gap-4 px-4 py-3">
        <Link href="/" className="text-lg font-semibold tracking-tight">
          Aster<span className="ml-1 rounded bg-surface-2 px-1.5 py-0.5 align-middle text-[10px] text-ink-muted">DEV</span>
        </Link>
        <nav aria-label="Main" className="flex flex-1 gap-1 overflow-x-auto text-sm">
          {NAV.map((n) => {
            const active = n.href === "/" ? path === "/" : path.startsWith(n.href);
            return (
              <Link
                key={n.href}
                href={n.href}
                aria-current={active ? "page" : undefined}
                className={`whitespace-nowrap rounded-md px-2.5 py-1.5 ${active ? "bg-surface-2 text-ink" : "text-ink-muted hover:text-ink"}`}
              >
                {n.label}
              </Link>
            );
          })}
        </nav>
        {me ? (
          <span className="hidden text-sm text-ink-muted sm:inline">@{me.handle}</span>
        ) : (
          <Link href="/login" className="btn-primary">
            Sign in
          </Link>
        )}
      </div>
    </header>
  );
}
