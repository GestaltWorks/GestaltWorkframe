const apiBase = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");

export const apiUrl = (path: string): string => `${apiBase}${path}`;
