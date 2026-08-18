// EIP-1193 wallet boundary for the sponsored-claim flow. This module requests
// account access, a readable sign-in proof, chain selection, and one EIP-712
// authorization. It never asks the wallet to send a transaction because the
// isolated relayer pays the gas.
import type { EIP712TypedData } from './api.ts'

type EthereumRequest = {
  method: string
  params?: unknown[] | Record<string, unknown>
}

export type EthereumProvider = {
  request(request: EthereumRequest): Promise<unknown>
}

declare global {
  interface Window {
    ethereum?: EthereumProvider
  }
}

const ADDRESS_PATTERN = /^0x[0-9a-fA-F]{40}$/
const SIGNATURE_PATTERN = /^0x[0-9a-fA-F]{130}$/

function providerError(error: unknown, fallback: string): Error {
  // Wallet providers expose implementation-specific objects. Convert the only
  // standardized user-rejection code and safe text into ordinary UI errors
  // without allowing an arbitrary object to reach React rendering.
  if (
    typeof error === 'object' &&
    error !== null &&
    'code' in error &&
    (error as { code: unknown }).code === 4001
  ) {
    return new Error('The wallet request was rejected.')
  }
  if (
    typeof error === 'object' &&
    error !== null &&
    'message' in error &&
    typeof (error as { message: unknown }).message === 'string'
  ) {
    return new Error(`${fallback}: ${(error as { message: string }).message}`)
  }
  return new Error(fallback)
}

/**
 * Returns the browser-injected EIP-1193 provider without requesting access.
 *
 * Discovery is kept separate from `connectWallet` so rendering the form has no
 * wallet side effect and tests can supply a provider without modifying Window.
 */
export function browserWallet(): EthereumProvider {
  // Discover the EIP-1193 provider lazily so rendering the form does not prompt
  // for wallet access and tests can inject a deterministic provider.
  if (!window.ethereum) {
    throw new Error(
      'No EVM wallet was found. Install or enable a wallet that supports EIP-712.',
    )
  }
  return window.ethereum
}

/**
 * Requests the account the claimant or their authorized representative uses.
 *
 * FastAPI later recovers the same address from a one-time challenge signature,
 * then independently resolves its relationship to the submitted policy.
 */
export async function connectWallet(
  provider: EthereumProvider = browserWallet(),
): Promise<string> {
  // Request explicit user consent and accept only a full EVM address. The API
  // later recovers this account from the one-time claimant challenge.
  let accounts: unknown
  try {
    accounts = await provider.request({ method: 'eth_requestAccounts' })
  } catch (error) {
    throw providerError(error, 'Could not connect the claimant wallet')
  }
  const address = Array.isArray(accounts) ? accounts[0] : undefined
  if (typeof address !== 'string' || !ADDRESS_PATTERN.test(address)) {
    throw new Error('The wallet did not return a valid Ethereum account.')
  }
  return address
}

/** Signs the readable one-time message used to establish a claimant session. */
export async function signClaimantChallenge(
  address: string,
  message: string,
  provider: EthereumProvider = browserWallet(),
): Promise<string> {
  if (!ADDRESS_PATTERN.test(address) || !message) {
    throw new Error('Cannot verify an invalid claimant wallet challenge.')
  }
  // `personal_sign` expects UTF-8 bytes as hex. Encoding locally preserves the
  // exact human-readable message that FastAPI stored and will recover later.
  const encodedMessage = `0x${Array.from(new TextEncoder().encode(message), (byte) =>
    byte.toString(16).padStart(2, '0'),
  ).join('')}`
  let signature: unknown
  try {
    signature = await provider.request({
      method: 'personal_sign',
      params: [encodedMessage, address],
    })
  } catch (error) {
    throw providerError(error, 'Could not verify claimant wallet ownership')
  }
  if (typeof signature !== 'string' || !SIGNATURE_PATTERN.test(signature)) {
    throw new Error('The wallet returned an invalid sign-in signature.')
  }
  return signature
}

/**
 * Aligns the wallet with the chain selected by the backend deployment manifest.
 *
 * EIP-712 includes `chainId` in its domain, so signing while the wallet shows a
 * different network is both confusing to the user and unusable by the deployed
 * forwarder. The application does not add unknown chains automatically.
 */
export async function switchWalletChain(
  chainId: number,
  provider: EthereumProvider = browserWallet(),
): Promise<void> {
  // The backend's reviewed deployment is authoritative; signing on the wallet's
  // previously selected chain would create an invalid EIP-712 domain.
  if (!Number.isSafeInteger(chainId) || chainId <= 0) {
    throw new Error('The claims API returned an invalid chain ID.')
  }
  try {
    await provider.request({
      method: 'wallet_switchEthereumChain',
      params: [{ chainId: `0x${chainId.toString(16)}` }],
    })
  } catch (error) {
    throw providerError(error, `Could not switch the wallet to chain ${chainId}`)
  }
}

/**
 * Requests an EIP-712 authorization for one prepared forwarder request.
 *
 * The return value is a signature, not a transaction. The private key remains
 * in the wallet, and a valid signature can execute only the target, calldata,
 * nonce, deadline, value, and gas allowance displayed in `typedData`.
 */
export async function signForwardRequest(
  address: string,
  typedData: EIP712TypedData,
  provider: EthereumProvider = browserWallet(),
): Promise<string> {
  // eth_signTypedData_v4 shows structured call fields instead of an opaque hash.
  // No transaction or private key leaves the wallet at this stage: the result is
  // an authorization that the restricted relayer may execute and pay for.
  if (!ADDRESS_PATTERN.test(address)) {
    throw new Error('Cannot sign with an invalid Ethereum account.')
  }
  let signature: unknown
  try {
    signature = await provider.request({
      method: 'eth_signTypedData_v4',
      params: [address, JSON.stringify(typedData)],
    })
  } catch (error) {
    throw providerError(error, 'Could not authorize the sponsored transaction')
  }
  if (typeof signature !== 'string' || !SIGNATURE_PATTERN.test(signature)) {
    throw new Error('The wallet returned an invalid EIP-712 signature.')
  }
  return signature
}
