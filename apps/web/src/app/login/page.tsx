"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, ApiError, json } from "@/lib/api";
import { useSession } from "@/lib/session";

/** Development sign-in. Swap for an OIDC provider in production. */
export default function LoginPage() {
  const [handle, setHandle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { refresh } = useSession();
  const router = useRouter();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/auth/dev/login", { method: "POST", body: json({ handle: handle.trim().toLowerCase() }) });
      await refresh();
      router.push("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-sm">
      <h1 className="text-2xl font-semibold">Sign in (development)</h1>
      <p className="mt-2 text-sm text-ink-muted">
        Dev builds sign in by handle only, with no password. A real identity provider replaces this before any beta.
      </p>
      <form onSubmit={submit} className="card mt-6 flex flex-col gap-4 p-5">
        <div>
          <label className="label" htmlFor="handle">
            Handle
          </label>
          <input
            id="handle"
            className="field"
            autoComplete="username"
            required
            minLength={3}
            maxLength={32}
            pattern="[a-zA-Z0-9_]{3,32}"
            value={handle}
            onChange={(e) => setHandle(e.target.value)}
            aria-describedby="handle-hint"
          />
          <p id="handle-hint" className="hint">
            3–32 letters, numbers or underscores.
          </p>
        </div>
        {error && (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        )}
        <button className="btn-primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
