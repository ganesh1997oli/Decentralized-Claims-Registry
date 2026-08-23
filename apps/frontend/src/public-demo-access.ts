/**
 * Compile-time switch for a dissertation demonstration deployment.
 *
 * This is intentionally only a public/non-secret mode flag. FastAPI remains the
 * authority that decides whether an anonymous read is allowed, and all assessor
 * writes continue to require a valid assessor credential.
 */
export const PUBLIC_DEMO_READ_ONLY =
  String(import.meta.env.VITE_PUBLIC_DEMO_READ_ONLY ?? 'false')
    .trim()
    .toLowerCase() === 'true'
