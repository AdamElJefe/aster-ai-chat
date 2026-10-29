"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, ApiError, setCsrf, type Me } from "./api";

type SessionState = {
  me: Me | null;
  loading: boolean;
  refresh: () => Promise<void>;
};

async function loadMe(): Promise<Me | null> {
  try {
    const data = await api<Me>("/me");
    setCsrf(data.csrf_token);
    return data;
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 401)) console.error(e);
    setCsrf(null);
    return null;
  }
}

const SessionContext = createContext<SessionState>({ me: null, loading: true, refresh: async () => {} });

export function SessionProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(() => loadMe().then(setMe), []);

  useEffect(() => {
    loadMe().then((data) => {
      setMe(data);
      setLoading(false);
    });
  }, []);

  return <SessionContext.Provider value={{ me, loading, refresh }}>{children}</SessionContext.Provider>;
}

export const useSession = () => useContext(SessionContext);
