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

export async function connectWallet(
  provider: EthereumProvider = browserWallet(),
): Promise<string> {
  // Request explicit user consent and accept only a full EVM address. The API
  // later binds this account to the authenticated insurer credential.
  let accounts: unknown
  try {
    accounts = await provider.request({ method: 'eth_requestAccounts' })
  } catch (error) {
    throw providerError(error, 'Could not connect the insurer wallet')
  }
  const address = Array.isArray(accounts) ? accounts[0] : undefined
  if (typeof address !== 'string' || !ADDRESS_PATTERN.test(address)) {
    throw new Error('The wallet did not return a valid Ethereum account.')
  }
  return address
}

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
