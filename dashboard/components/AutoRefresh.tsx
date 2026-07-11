"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

// Re-fetches the server component tree on an interval so the dashboard stays
// live without a full page reload.
export default function AutoRefresh({ seconds = 15 }: { seconds?: number }) {
  const router = useRouter();
  useEffect(() => {
    const id = setInterval(() => router.refresh(), seconds * 1000);
    return () => clearInterval(id);
  }, [router, seconds]);
  return null;
}
