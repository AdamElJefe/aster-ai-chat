"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { useSession } from "@/lib/session";

const REASONS: Record<string, string> = {
  unverified: "Verify that you're an adult to chat and create characters.",
  expired: "Your age verification has expired. Please verify again.",
  policy_changed: "Our access policy changed. Please verify again to continue.",
  underage: "This service is only available to users 18 and older.",
  region_blocked: "The service isn't available in your region.",
};

/** UI-only reflection of the server gate. The API enforces access independently. */
export function AdultGate({ children }: { children: ReactNode }) {
  const { me, loading } = useSession();
  if (loading) return <p className="text-ink-muted">Loading…</p>;
  if (!me)
    return (
      <Notice text="Sign in to continue.">
        <Link href="/login" className="btn-primary">
          Sign in
        </Link>
      </Notice>
    );
  if (me.adult_access !== "verified") {
    const canVerify = ["unverified", "expired", "policy_changed"].includes(me.adult_access);
    return (
      <Notice text={REASONS[me.adult_access] ?? "Adult verification required."}>
        {canVerify && (
          <Link href="/verify" className="btn-primary">
            Verify age
          </Link>
        )}
      </Notice>
    );
  }
  return <>{children}</>;
}

export function Notice({ text, children }: { text: string; children?: ReactNode }) {
  return (
    <div role="status" className="card flex flex-col items-start gap-3 p-5">
      <p>{text}</p>
      {children}
    </div>
  );
}
