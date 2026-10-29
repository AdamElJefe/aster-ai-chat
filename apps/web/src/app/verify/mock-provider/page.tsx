"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { api, ApiError, json } from "@/lib/api";
import { useSession } from "@/lib/session";

/**
 * Stand-in for the external age-verification vendor's hosted page (dev/test only).
 * Simulates the vendor returning a signed result, which our server then verifies.
 */
function MockProvider() {
  const params = useSearchParams();
  const ref = params.get("session_ref");
  const router = useRouter();
  const { refresh } = useSession();
  const [status, setStatus] = useState<string | null>(null);

  async function decide(outcome: "adult" | "underage") {
    setStatus("Verifying…");
    try {
      const { token } = await api<{ token: string }>("/dev/mock-age-provider/decide", {
        method: "POST",
        body: json({ session_ref: ref, outcome }),
      });
      const res = await api<{ adult_access: boolean }>("/age/callback", { method: "POST", body: json({ token }) });
      await refresh();
      if (res.adult_access) router.push("/");
      else setStatus("Verification did not confirm adult access.");
    } catch (e) {
      setStatus(e instanceof ApiError ? e.message : "Verification failed");
    }
  }

  if (!ref) return <p>Missing session reference.</p>;
  return (
    <div className="mx-auto max-w-md">
      <div className="card border-warn/50 p-5">
        <p className="text-xs font-semibold uppercase tracking-wide text-warn">Simulated provider, dev only</p>
        <h1 className="mt-2 text-xl font-semibold">Mock age check</h1>
        <p className="mt-2 text-sm text-ink-muted">
          A real provider would run its check here. Choose an outcome to simulate the signed result.
        </p>
        <div className="mt-5 flex flex-wrap gap-3">
          <button className="btn-primary" onClick={() => decide("adult")}>
            Simulate: verified adult
          </button>
          <button className="btn-ghost" onClick={() => decide("underage")}>
            Simulate: under 18
          </button>
        </div>
        {status && <p role="status" className="mt-4 text-sm">{status}</p>}
      </div>
    </div>
  );
}

export default function Page() {
  return (
    <Suspense>
      <MockProvider />
    </Suspense>
  );
}
