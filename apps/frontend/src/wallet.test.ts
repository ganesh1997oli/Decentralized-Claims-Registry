import { describe, expect, it, vi } from 'vitest'
import type { CoverageDecisionProposal, EIP712TypedData } from './api.ts'
import {
  connectWallet,
  sendCoverageDecisionTransaction,
  signClaimantChallenge,
  signForwardRequest,
  switchWalletChain,
  type EthereumProvider,
} from './wallet.ts'

const ADDRESS = '0x1111111111111111111111111111111111111111'
const SIGNATURE = `0x${'ab'.repeat(65)}`
const TRANSACTION_HASH = `0x${'cd'.repeat(32)}`
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

const DECISION_PROPOSAL: CoverageDecisionProposal = {
  decision_id: '11111111-1111-4111-8111-111111111111',
  claim_id: 7,
  decision_status: 'Approved',
  decision_hash: `0x${'12'.repeat(32)}`,
  decision_maker_address: ADDRESS,
  proposed_by: 'northstar-governance-1',
  human_outcome_id: '22222222-2222-4222-8222-222222222222',
  human_outcome_revision: 2,
  created_at: '2026-08-18T19:30:00Z',
  confirmed_transaction_hash: null,
  confirmed_at: null,
  chain_id: 11155111,
  contract_address: '0x2222222222222222222222222222222222222222',
  transaction_data: '0x1234',
}

function provider(results: unknown[]): EthereumProvider & { request: ReturnType<typeof vi.fn> } {
  return { request: vi.fn().mockImplementation(() => Promise.resolve(results.shift())) }
}

describe('browser wallet boundary', () => {
  it('encodes and signs the exact readable claimant challenge', async () => {
    const wallet = provider([SIGNATURE])

    await expect(
      signClaimantChallenge(ADDRESS, 'Verify claim access', wallet),
    ).resolves.toBe(SIGNATURE)
    expect(wallet.request).toHaveBeenCalledWith({
      method: 'personal_sign',
      params: ['0x56657269667920636c61696d20616363657373', ADDRESS],
    })
  })

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

  it('switches chain and sends only the audited governance calldata', async () => {
    const wallet = provider([null, TRANSACTION_HASH])

    await expect(
      sendCoverageDecisionTransaction(DECISION_PROPOSAL, wallet),
    ).resolves.toBe(TRANSACTION_HASH)
    expect(wallet.request.mock.calls).toEqual([
      [
        {
          method: 'wallet_switchEthereumChain',
          params: [{ chainId: '0xaa36a7' }],
        },
      ],
      [
        {
          method: 'eth_sendTransaction',
          params: [
            {
              from: ADDRESS,
              to: DECISION_PROPOSAL.contract_address,
              data: DECISION_PROPOSAL.transaction_data,
              value: '0x0',
            },
          ],
        },
      ],
    ])
  })

  it('rejects malformed governance calldata before opening the wallet', async () => {
    const wallet = provider([])

    await expect(
      sendCoverageDecisionTransaction(
        { ...DECISION_PROPOSAL, transaction_data: 'not-calldata' },
        wallet,
      ),
    ).rejects.toThrow('invalid transaction fields')
    expect(wallet.request).not.toHaveBeenCalled()
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
