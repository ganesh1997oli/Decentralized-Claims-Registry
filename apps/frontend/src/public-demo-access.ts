/**
 * Compile-time switch for a dissertation demonstration deployment.
 *
 * These are intentionally public/non-secret presentation flags. FastAPI remains
 * the authority for every anonymous read or write; changing a browser bundle
 * alone never grants access. Both switches default to false.
 */
export const PUBLIC_DEMO_READ_ONLY =
  String(import.meta.env.VITE_PUBLIC_DEMO_READ_ONLY ?? 'false')
    .trim()
    .toLowerCase() === 'true'

/**
 * Supervised dissertation prototype switch for the assessor surface only.
 *
 * When the matching server flag is enabled, visitors may append fictional
 * off-chain outcomes without a key. Production must leave this false and use
 * individual assessor credentials so each revision remains attributable.
 */
export const PUBLIC_PROTOTYPE_ASSESSOR =
  String(import.meta.env.VITE_PUBLIC_PROTOTYPE_ASSESSOR ?? 'false')
    .trim()
    .toLowerCase() === 'true'
