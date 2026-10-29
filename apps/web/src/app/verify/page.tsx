"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Notice } from "@/components/Gate";
import { api, ApiError } from "@/lib/api";
import { useSession } from "@/lib/session";

type Start = { session_ref: string; provider: string; redirect_url: string };

export default function VerifyPage() {
  const { me } = useSession();
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!me) return <Notice text="Sign in first." />;
  if (me.adult_access === "verified") return <Notice text="You're verified. Enjoy!" />;

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const s = await api<Start>("/age/start", { method: "POST" });
      router.push(s.redirect_url);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't start verification");
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-lg">
      <h1 className="text-2xl font-semibold">Age verification</h1>
      <p className="mt-2 text-ink-muted">
        Chat is available to users 18 and older. You&apos;ll be sent to our verification provider. We keep only the result (adult
        or not, region, expiry), never documents or your date of birth.
      </p>
      {error && <p role="alert" className="mt-4 text-danger">{error}</p>}
      <button className="btn-primary mt-6" onClick={start} disabled={busy}>
        Continue to verification
      </button>
    </div>
  );
}
