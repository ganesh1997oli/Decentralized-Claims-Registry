// Public display metadata is shared by intake and receipt presentation. The
// backend remains the authority for insurer identities and credentials.
export const RESEARCH_INSURERS = [
  { id: 'northstar-mutual', label: 'Northstar Mutual' },
  { id: 'harbour-shield', label: 'Harbour Shield' },
  { id: 'cedar-insurance', label: 'Cedar Insurance' },
] as const

const IPFS_GATEWAY = (
  import.meta.env.VITE_IPFS_GATEWAY || 'https://gateway.pinata.cloud/ipfs'
).replace(/\/$/, '')

export function shorten(value: string, visible = 10): string {
  if (value.length <= visible * 2 + 3) return value
  return `${value.slice(0, visible)}...${value.slice(-visible)}`
}

export function ipfsUrl(pointer: string): string {
  return `${IPFS_GATEWAY}/${pointer.replace(/^ipfs:\/\//, '')}`
}

export function insurerLabel(insurerId: string): string {
  return (
    RESEARCH_INSURERS.find((insurer) => insurer.id === insurerId)?.label ??
    insurerId
      .split('-')
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' ')
  )
}

