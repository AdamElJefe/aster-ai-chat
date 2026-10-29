"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Notice } from "@/components/Gate";
import { api, ApiError, devRegion, json, setDevRegion, type Balance } from "@/lib/api";
import { useSession } from "@/lib/session";

export default function AccountPage() {
  const { me, refresh } = useSession();
  const router = useRouter();
  const [balance, setBalance] = useState<Balance | null>(null);
  const [confirm, setConfirm] = useState("");
  const [region, setRegion] = useState(() => (typeof window === "undefined" ? "" : (devRegion() ?? "")));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (me) api<Balance>("/credits/balance").then(setBalance).catch(() => {});
  }, [me]);

  if (!me) return <Notice text="Sign in to manage your account." />;

  async function logout() {
    await api("/auth/logout", { method: "POST" });
    await refresh();
    router.push("/");
  }

  async function deleteAccount(e: React.FormEvent) {
    e.preventDefault();
    try {
      await api("/me", { method: "DELETE", body: json({ confirm_handle: confirm }) });
      await refresh();
      router.push("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Deletion failed");
    }
  }

  return (
    <div className="mx-auto flex max-w-xl flex-col gap-6">
      <h1 className="text-2xl font-semibold">Account</h1>
      <section className="card grid gap-2 p-5 text-sm">
        <p>Signed in as <strong>@{me.handle}</strong>{me.role === "admin" && " (admin)"}</p>
        <p>Adult access: <strong>{me.adult_access.replace("_", " ")}</strong> · Region: {me.region}</p>
        <p>Plan: {me.entitlements.plan_id}{balance && ` · ${balance.used} of ${balance.limit} messages used today (UTC)`}</p>
        <p className="text-ink-muted">{me.ai_disclosure}</p>
        <button className="btn-ghost mt-2 justify-self-start" onClick={logout}>Sign out</button>
      </section>

      <section className="card p-5">
        <h2 className="font-semibold">Dev: simulate region</h2>
        <p className="hint">Sends X-Dev-Region on API calls. Only honoured by dev builds.</p>
        <div className="mt-3 flex gap-2">
          <label htmlFor="region" className="sr-only">Region code</label>
          <input id="region" className="field max-w-[10rem]" placeholder="US-TEST" value={region} onChange={(e) => setRegion(e.target.value)} />
          <button className="btn-ghost" onClick={async () => { setDevRegion(region.trim().toUpperCase()); await refresh(); }}>Apply</button>
        </div>
      </section>

      <section className="card border-danger/40 p-5">
        <h2 className="font-semibold text-danger">Delete account</h2>
        <p className="mt-1 text-sm text-ink-muted">
          Permanently deletes your chats, characters and verification record. Usage records are kept without your
          identity for accounting.
        </p>
        <form onSubmit={deleteAccount} className="mt-3 flex flex-col gap-2 sm:flex-row">
          <label htmlFor="confirm" className="sr-only">Type your handle to confirm</label>
          <input id="confirm" className="field" placeholder={`Type ${me.handle} to confirm`} value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          <button className="btn-danger" disabled={confirm !== me.handle}>Delete</button>
        </form>
        {error && <p role="alert" className="mt-2 text-sm text-danger">{error}</p>}
      </section>
    </div>
  );
}
