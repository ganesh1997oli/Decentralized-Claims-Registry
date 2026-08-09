import { describe, expect, it, vi } from 'vitest'
import type { EIP712TypedData } from './api.ts'
import {
  connectWallet,
  signForwardRequest,
  switchWalletChain,
  type EthereumProvider,
} from './wallet.ts'

const ADDRESS = '0x1111111111111111111111111111111111111111'
const SIGNATURE = `0x${'ab'.repeat(65)}`
const TYPED_DATA: EIP712TypedData = {
  types: {
    EIP712Domain: [{ name: 'name', type: 'string' }],
    ForwardRequest: [{ name: 'from', type: 'address' }],
  },
  primaryType: 'ForwardRequest',
  domain: {
    name: 'ClaimsRegistryForwarder',
    version: '1',
    chainId: 11155111,
    verifyingContract: '0x3333333333333333333333333333333333333333',
  },
  message: {
    from: ADDRESS,
    to: '0x2222222222222222222222222222222222222222',
    value: '0',
    gas: '250000',
    nonce: '7',
    deadline: '2000000000',
    data: '0x1234',
  },
}

function provider(results: unknown[]): EthereumProvider & { request: ReturnType<typeof vi.fn> } {
  return { request: vi.fn().mockImplementation(() => Promise.resolve(results.shift())) }
}

describe('browser wallet boundary', () => {
  it('connects, switches to Sepolia, and signs the exact typed data', async () => {
    const wallet = provider([[ADDRESS], null, SIGNATURE])

    await expect(connectWallet(wallet)).resolves.toBe(ADDRESS)
    await switchWalletChain(11155111, wallet)
    await expect(signForwardRequest(ADDRESS, TYPED_DATA, wallet)).resolves.toBe(
      SIGNATURE,
    )

    expect(wallet.request.mock.calls).toEqual([
      [{ method: 'eth_requestAccounts' }],
      [
        {
          method: 'wallet_switchEthereumChain',
          params: [{ chainId: '0xaa36a7' }],
        },
      ],
      [
        {
          method: 'eth_signTypedData_v4',
          params: [ADDRESS, JSON.stringify(TYPED_DATA)],
        },
      ],
    ])
  })

  it('turns a wallet rejection into a safe user-facing error', async () => {
    const wallet: EthereumProvider = {
      request: vi.fn().mockRejectedValue({ code: 4001 }),
    }

    await expect(connectWallet(wallet)).rejects.toThrow('wallet request was rejected')
  })

  it('rejects malformed accounts and signatures returned by an extension', async () => {
    await expect(connectWallet(provider([['not-an-address']]))).rejects.toThrow(
      'valid Ethereum account',
    )
    await expect(
      signForwardRequest(ADDRESS, TYPED_DATA, provider(['0xshort'])),
    ).rejects.toThrow('invalid EIP-712 signature')
  })
})
